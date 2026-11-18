"""The current Leader runs standard recoveries with the operator guards.

Each one reuses the operator service and every guard it has, adds the Leader and bound-Supervisor checks, and
sends one notice to the bound Supervisor. The action audit stays the existing one; nothing here adds a second
event for the same action. An operation id makes a repeated request a replay: a conflicting request with the
same id is refused, and a notice that failed after the action committed is retried by the same request without
repeating the action. Policy, limits, merge and approval are not reachable from this module.
"""
import hashlib
import json
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, delete, exists, insert, literal, select, true, update
from sqlalchemy.orm import aliased
from sqlalchemy.exc import IntegrityError

from app.models.database import (
    AgentTeamPreset, AgentTeamSlot, FactoryAuditEvent, GithubMaintenanceEvent, GithubAttemptScopeRevision, GithubOwnerObservationPause, GithubWorkItem,
    GithubWorkspace, MailAgentSession, MailExternalActor, MailMessage, MailReceipt, TeamGithubScope,
)
from app.models.api_model import naive_utc_confirmation
from app.services import factory_audit_service as audit
from app.services.agent_mail_service import agent_mail_service
from app.services.github_approval_service import (
    ActiveCancellationNoticeError, GithubApprovalError, github_approval_service,
)
from app.services.github_coordination_service import hold_code
from app.services.github_end_stopped_item_service import end_stopped_item
from app.services.github_terminal_revision_service import (
    TERMINAL_ITEMS, finalize_merged_item, finalize_merged_preview,
)
from app.services.github_workspace_service import (
    GithubWorkspaceConfigError, GithubWorkspaceCredentialRevokeError, github_workspace_service,
)

KEY = "leader-recovery"


class LeaderRecoveryError(Exception):
    def __init__(self, code: str, status_code: int = 409):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class _Ctx:
    """Plain values captured before any rollback can expire the loaded rows."""
    item_id: int
    issue_number: int
    scope_id: int
    member_id: int
    session_id: int
    preset_id: int = 0
    supervisor_id: int = 0
    claim_key: str = ""      # the shared ledger row this execution rests on, and the request it was claimed for
    claim_digest: str = ""


def _digest(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, default=str, sort_keys=True).encode()).hexdigest()


def _ctx(item, session) -> _Ctx:
    return _Ctx(item.id, item.issue_number, item.scope_id, session.member_id, session.id)


def _actor(ctx: _Ctx) -> dict:
    return audit.derive_actor(actor_kind="member", member_id=ctx.member_id, session_id=ctx.session_id)


async def _fresh_leader(db, item_id):
    """The current Leader member, read from the database now.

    The shared participant lookup goes through ``db.get`` and so returns the preset this session already
    loaded; a replacement Leader that another connection committed would not show. Every row here is a fresh
    query. Owner separation is kept: the Leader must differ from the owner and sit in another slot.
    """
    item_row = (await db.execute(select(GithubWorkItem.scope_id, GithubWorkItem.owner_slot_id).where(
        GithubWorkItem.id == item_id))).one_or_none()
    preset_id = await db.scalar(select(TeamGithubScope.preset_id).where(
        TeamGithubScope.id == item_row.scope_id)) if item_row is not None else None
    leader_slot_id = await db.scalar(select(AgentTeamPreset.leader_slot_id).where(
        AgentTeamPreset.id == preset_id)) if preset_id is not None else None
    slot_id = await db.scalar(select(AgentTeamSlot.id).where(
        AgentTeamSlot.id == leader_slot_id, AgentTeamSlot.preset_id == preset_id,
        AgentTeamSlot.enabled.is_(True))) if leader_slot_id is not None else None
    owner = await agent_mail_service._slot_member(db, item_row.owner_slot_id) if item_row is not None else None
    leader = await agent_mail_service._slot_member(db, slot_id)
    if owner is None:
        raise LeaderRecoveryError("owner_not_registered")
    if leader is None:
        raise LeaderRecoveryError("leader_not_registered")
    if owner.id == leader.id or owner.team_slot_id == leader.team_slot_id:
        raise LeaderRecoveryError("owner_cannot_approve_own_work")
    return leader


async def _authorize(db, item, session, *, need_supervisor=True):
    """The same current-Leader proof the settle tools use, plus the stop conditions and the bound Supervisor."""
    if hold_code():
        raise LeaderRecoveryError("operator_hold")
    scope = await db.get(TeamGithubScope, item.scope_id)
    preset = await db.get(AgentTeamPreset, scope.preset_id) if scope is not None else None
    if scope is None or preset is None or not scope.enabled or not preset.autonomy_enabled:
        raise LeaderRecoveryError("recovery_context_unavailable")
    leader = await _fresh_leader(db, item.id)
    if leader.id != session.member_id:
        raise LeaderRecoveryError("leader_only", 403)
    supervisor = None
    if preset.supervisor_actor_id is not None:
        supervisor = await db.get(MailExternalActor, preset.supervisor_actor_id)
    if need_supervisor and supervisor is None:
        raise LeaderRecoveryError("supervisor_not_bound")
    await _refuse_observation_pause(db, item.id)
    if hold_code():  # HOLD may have arrived during the reads above
        raise LeaderRecoveryError("operator_hold")
    return scope, preset, leader, supervisor


async def _refuse_observation_pause(db, item_id):
    """A waiting or paused observation is recovered by the operator alone, from the unchanged context."""
    if await db.scalar(select(GithubOwnerObservationPause.id).where(
            GithubOwnerObservationPause.work_item_id == item_id,
            GithubOwnerObservationPause.status.in_(("waiting", "paused")))) is not None:
        raise LeaderRecoveryError("observation_pause_active")


async def _assert_authority(db, ctx, *, terminal=False):
    """Everything the entry check proved, read again inside the action's own transaction.

    The delegated services call this right before their commit (and a release before any external effect), so
    a HOLD, a Leader or session change, a disabled scope or autonomy, an unbound Supervisor, a new observation
    pause or a nonterminal item that appeared during an awaited probe stops the write. A refusal here rolls the
    action back, so nothing is written and no success notice follows.
    """
    if hold_code():
        raise LeaderRecoveryError("operator_hold")
    row = (await db.execute(select(GithubWorkItem.scope_id, GithubWorkItem.dispatch_status).where(
        GithubWorkItem.id == ctx.item_id))).one_or_none()
    scope = (await db.execute(select(TeamGithubScope.enabled, TeamGithubScope.preset_id).where(
        TeamGithubScope.id == row.scope_id))).one_or_none() if row is not None else None
    preset = (await db.execute(select(AgentTeamPreset.autonomy_enabled, AgentTeamPreset.supervisor_actor_id).where(
        AgentTeamPreset.id == scope.preset_id))).one_or_none() if scope is not None else None
    if row is None or scope is None or preset is None or not scope.enabled or not preset.autonomy_enabled:
        raise LeaderRecoveryError("recovery_context_unavailable")
    if preset.supervisor_actor_id != ctx.supervisor_id or await db.scalar(
            select(MailExternalActor.id).where(MailExternalActor.id == ctx.supervisor_id)) is None:
        raise LeaderRecoveryError("supervisor_not_bound")
    if await db.scalar(select(MailAgentSession.id).where(
            MailAgentSession.id == ctx.session_id, MailAgentSession.member_id == ctx.member_id,
            MailAgentSession.closed_at.is_(None))) is None:
        raise LeaderRecoveryError("leader_only", 403)
    leader = await _fresh_leader(db, ctx.item_id)
    if leader.id != ctx.member_id:
        raise LeaderRecoveryError("leader_only", 403)
    await _refuse_observation_pause(db, ctx.item_id)
    if terminal and row.dispatch_status not in TERMINAL_ITEMS:
        raise LeaderRecoveryError("item_not_terminal")
    if ctx.claim_key and await db.scalar(select(GithubMaintenanceEvent.id).where(
            GithubMaintenanceEvent.operation_id == ctx.claim_key,
            GithubMaintenanceEvent.request_sha256 == ctx.claim_digest)) is None:
        # Every admitted execution, claimant or replay, commits only while the ledger row it rests on still holds
        # this request. A cleanup that freed the id meanwhile stops it here, inside its own transaction.
        raise LeaderRecoveryError("recovery_context_changed")
    if hold_code():  # the last await is behind us: a HOLD that arrived during these reads must still stop the write
        raise LeaderRecoveryError("operator_hold")


async def _reload(db, *rows):
    """A lost claim rolls the session back, which expires every loaded row; read what the flow still needs again."""
    for row in rows:
        await db.refresh(row)


async def _existing_claim(db, item_id, kind, operation_id, digest):
    event = await db.scalar(select(GithubMaintenanceEvent).where(
        GithubMaintenanceEvent.operation_id == f"{KEY}:{kind}:{operation_id}"))
    if event is None:
        return False
    if event.work_item_id != item_id or event.request_sha256 != digest:
        raise LeaderRecoveryError("operation_conflict")
    return True


RELEASE_CLEAN = "claimed:r2"   # a release claim written by protocol version 2; a plain 'claimed' release row is a #997 legacy row


async def _claim(db, item_id, kind, operation_id, digest):
    """One ledger row per operation id; a concurrent duplicate meets the unique id and becomes a replay."""
    db.add(GithubMaintenanceEvent(operation_id=f"{KEY}:{kind}:{operation_id}", work_item_id=item_id,
                                  outcome=RELEASE_CLEAN if kind == "release" else "claimed", request_sha256=digest))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        if not await _existing_claim(db, item_id, kind, operation_id, digest):
            raise LeaderRecoveryError("recovery_context_changed") from None
        return True
    return False


async def _no_effect(db, kind, ctx, operation_id, *, revision_number=None, workspace_id=None, leased_at=None):
    """The condition 'the action this id claimed has not committed', as SQL the deleting statement evaluates itself."""
    if kind == "release":
        return ~exists().where(
            FactoryAuditEvent.event_kind == "workspace_release", FactoryAuditEvent.action_outcome == "applied",
            FactoryAuditEvent.correlation_id == f"workspace_release:workspace:{workspace_id}:leased_at:{leased_at.isoformat()}")
    if kind == "finalize":
        done = aliased(GithubMaintenanceEvent)
        return ~exists().where(done.operation_id == f"finalize_merged:{operation_id}")
    revision_id = await db.scalar(select(GithubAttemptScopeRevision.id).where(
        GithubAttemptScopeRevision.work_item_id == ctx.item_id,
        GithubAttemptScopeRevision.revision == revision_number))
    if revision_id is None:
        return true()
    return ~exists().where(
        FactoryAuditEvent.event_kind == "recovery_cancellation", FactoryAuditEvent.action_outcome == "applied",
        FactoryAuditEvent.operation_id == f"active_cancellation:revision:{revision_id}")


async def _unclaim(db, kind, ctx, operation_id, **proof):
    """Free an id whose action did nothing, in ONE statement that also proves no effect committed.

    A separate proof read followed by an unconditional delete lets a replay commit in between and lose its
    ledger row. Here the database evaluates the no-effect condition and deletes under the same write lock, so a
    row whose action committed is never removed. True means this call freed the id.
    """
    await db.rollback()
    no_effect = await _no_effect(db, kind, ctx, operation_id, **proof)
    result = await db.execute(delete(GithubMaintenanceEvent).where(
        GithubMaintenanceEvent.operation_id == f"{KEY}:{kind}:{operation_id}",
        GithubMaintenanceEvent.outcome == (RELEASE_CLEAN if kind == "release" else "claimed"), no_effect))
    await db.commit()
    return result.rowcount == 1


async def _effect_recorded(db, kind, ctx, operation_id, *, revision_number=None, workspace_id=None, leased_at=None):
    """Whether the action this operation id claimed has committed, proved by its own recorded fact."""
    if kind == "release":
        return await _release_applied(db, workspace_id, leased_at)
    if kind == "finalize":
        return await db.scalar(select(GithubMaintenanceEvent.id).where(
            GithubMaintenanceEvent.operation_id == f"finalize_merged:{operation_id}")) is not None
    revision_id = await db.scalar(select(GithubAttemptScopeRevision.id).where(
        GithubAttemptScopeRevision.work_item_id == ctx.item_id,
        GithubAttemptScopeRevision.revision == revision_number))
    return revision_id is not None and await db.scalar(select(FactoryAuditEvent.id).where(
        FactoryAuditEvent.event_kind == "recovery_cancellation", FactoryAuditEvent.action_outcome == "applied",
        FactoryAuditEvent.operation_id == f"active_cancellation:revision:{revision_id}")) is not None


async def _abandon(db, kind, ctx, operation_id, replay, **proof):
    """A refused or lost action frees its operation id only if the action did not commit.

    The claim record is shared with every replay. A claimant that lost the race to its own replay must not
    erase the record the winner's success rests on; it answers with the replay outcome instead. True means the
    effect is recorded and the claim stays.
    """
    await db.rollback()
    if kind != "release" and not replay and await _unclaim(db, kind, ctx, operation_id, **proof):
        return False  # the action did not commit and the id is free again
    # A release claim is kept: its credential revoker may have run, so its binding is never dropped (split B
    # designs the full protocol). A replay never deletes. Otherwise a winner's committed effect keeps the row.
    return await _effect_recorded(db, kind, ctx, operation_id, **proof)


async def _insert_notice(db, *, preset_id, leader_member_id, supervisor_actor_id, key, subject, body, payload) -> bool:
    """Insert the Supervisor message unless its key exists, in one statement. True only for the inserting caller."""
    payload = dict(payload, supervisor_actor_id=supervisor_actor_id)
    statement = insert(MailMessage).from_select(
        ["kind", "sender_member_id", "audience_type", "audience_id", "subject", "body_markdown", "payload",
         "delivery_key", "created_at"],
        select(literal("message"), literal(leader_member_id), literal("team_preset"), literal(str(preset_id)),
               literal(subject), literal(body), literal(payload, type_=JSON), literal(key),
               literal(datetime.utcnow())).where(
                   ~exists().where(MailMessage.delivery_key == key))).returning(MailMessage.id)
    message_id = (await db.execute(statement)).scalar_one_or_none()
    if message_id is None:
        await db.rollback()
        return False
    db.add(MailReceipt(message_id=message_id, member_id=leader_member_id, read_at=datetime.utcnow()))
    await db.commit()
    return True


async def _notify(db, ctx: _Ctx, *, kind, operation_id, digest, summary, reason):
    key = f"{KEY}:{kind}:{ctx.item_id}:{operation_id}:{digest[:16]}"
    body = (f"The Leader ran '{kind}' for work item {ctx.item_id} (issue #{ctx.issue_number}): {summary} "
            f"Reason: {audit.sanitize_reason(reason)}. This notice grants no authority.")
    try:
        sent = await _insert_notice(db, preset_id=ctx.preset_id, leader_member_id=ctx.member_id,
            supervisor_actor_id=ctx.supervisor_id, key=key, subject=f"Leader recovery: {kind}", body=body,
            payload={"leader_recovery": {"kind": kind, "work_item_id": ctx.item_id, "operation_id": operation_id}})
    except Exception:
        await db.rollback()
        # The action stays committed. The failure is recorded and visible; the same request retries only the notice.
        await audit.record_observation(db, event_kind="leader_recovery_notification", source=f"{__name__}._notify",
            occurred_at=datetime.utcnow(), actor=_actor(ctx), scope_id=ctx.scope_id, item_id=ctx.item_id,
            correlation_id=key, operation_id=f"{key}:uncertain", action_outcome="uncertain",
            sanitized_reason="supervisor notice delivery unsettled after the action committed")
        raise LeaderRecoveryError("recovery_notice_failed", 502) from None
    return "sent" if sent else "already_sent"


def _need_reason(reason):
    reason = (reason or "").strip()
    if not reason:
        raise LeaderRecoveryError("recovery_reason_required", 400)
    return reason


async def cancel_revision(db, item, session, *, revision_number, dispatch_nonce, operation_id, reason):
    reason = _need_reason(reason)
    ctx = _ctx(item, session)
    scope, preset, _leader, supervisor = await _authorize(db, item, session)
    ctx = replace(ctx, preset_id=preset.id, supervisor_id=supervisor.id)
    digest = _digest("cancel", ctx.item_id, revision_number, dispatch_nonce, reason)
    ctx = replace(ctx, claim_key=f"{KEY}:cancel:{operation_id}", claim_digest=digest)
    replay = await _existing_claim(db, ctx.item_id, "cancel", operation_id, digest)
    if not replay:
        replay = await _claim(db, ctx.item_id, "cancel", operation_id, digest)
    await _reload(db, item)
    owner_notice_error = None
    try:
        _item, _revision, changed = await github_approval_service.cancel_active_continuation(
            db, item, revision_number=revision_number, dispatch_nonce=dispatch_nonce, reason=reason,
            actor=_actor(ctx), before_commit=lambda: _assert_authority(db, ctx))
    except ActiveCancellationNoticeError as exc:
        owner_notice_error, changed = exc, True  # the cancellation is committed; only its owner notice failed
    except (GithubApprovalError, LeaderRecoveryError) as exc:
        if not await _abandon(db, "cancel", ctx, operation_id, replay, revision_number=revision_number):
            if isinstance(exc, LeaderRecoveryError):
                raise
            raise LeaderRecoveryError(exc.detail, exc.status_code) from exc
        changed = False  # the cancellation committed under this operation id (a replay won): answer as the replay
    notice = await _notify(db, ctx, kind="cancel", operation_id=operation_id,
        digest=digest, summary=f"revision {revision_number} was cancelled.", reason=reason)
    if owner_notice_error is not None:
        raise LeaderRecoveryError(owner_notice_error.detail, owner_notice_error.status_code)
    return {"status": "cancelled" if changed else "already_cancelled", "revision": revision_number, "notice": notice}


async def finalize_preview(db, item, session, *, read_pull):
    scope, _preset, _leader, _supervisor = await _authorize(db, item, session, need_supervisor=False)
    try:
        return await finalize_merged_preview(db, item, scope, read_pull)
    except ValueError as exc:
        raise LeaderRecoveryError(str(exc)) from None


async def finalize_merged(db, item, session, *, read_pull, operation_id, expected_context_sha256, reason):
    reason = _need_reason(reason)
    ctx = _ctx(item, session)
    scope, preset, _leader, supervisor = await _authorize(db, item, session)
    ctx = replace(ctx, preset_id=preset.id, supervisor_id=supervisor.id)
    digest = _digest("finalize", ctx.item_id, expected_context_sha256, reason)
    ctx = replace(ctx, claim_key=f"{KEY}:finalize:{operation_id}", claim_digest=digest)
    replay = await _existing_claim(db, ctx.item_id, "finalize", operation_id, digest)
    if not replay:
        replay = await _claim(db, ctx.item_id, "finalize", operation_id, digest)
    await _reload(db, item, scope)
    try:
        result = await finalize_merged_item(db, item, scope, read_pull, expected_context_sha256=expected_context_sha256,
            operation_id=operation_id, reason=reason, decided_by="the Leader", actor=_actor(ctx),
            before_commit=lambda: _assert_authority(db, ctx))
    except (ValueError, LeaderRecoveryError) as exc:
        if not await _abandon(db, "finalize", ctx, operation_id, replay):
            if isinstance(exc, LeaderRecoveryError):
                raise
            raise LeaderRecoveryError(str(exc)) from None
        result = {"status": "already_finalized"}  # the finalize committed under this operation id: answer as the replay
    notice = await _notify(db, ctx, kind="finalize", operation_id=operation_id,
        digest=digest, summary="the merged pull request was recorded.", reason=reason)
    return {"status": result["status"], "work_item_id": ctx.item_id, "notice": notice}


async def _release_applied(db, workspace_id, expected_leased_at):
    correlation = f"workspace_release:workspace:{workspace_id}:leased_at:{expected_leased_at.isoformat()}"
    return await db.scalar(select(FactoryAuditEvent.id).where(
        FactoryAuditEvent.event_kind == "workspace_release", FactoryAuditEvent.correlation_id == correlation,
        FactoryAuditEvent.action_outcome == "applied")) is not None


def _held(workspace, item_id, expected_leased_at):
    return (workspace.leased_item_id == item_id and workspace.leased_at == expected_leased_at
            and workspace.lease_token is not None)


class _ReleaseAlreadyDone(Exception):
    """The ledger row of this request already shows its committed release."""


async def _mark_executing(db, ctx, attempt, *, created_here):
    """Commit this attempt's marker on the claim row BEFORE any external effect can start.

    The row's outcome text is the state: 'claimed' is clean (no external effect started), 'executing:<attempt>'
    means that attempt may have started its credential revoker, and a trailing ':u' means the attempt adopted an
    earlier executing or legacy row, so it INHERITS that uncertainty. The marker is a compare-and-set on the exact
    state read, so two callers can never both own it. Returns True when the uncertainty is inherited.
    """
    outcome = await db.scalar(select(GithubMaintenanceEvent.outcome).where(
        GithubMaintenanceEvent.operation_id == ctx.claim_key, GithubMaintenanceEvent.request_sha256 == ctx.claim_digest))
    # Only a version 2 clean row this very call created is clean. A plain 'claimed' row (legacy), a clean row a
    # concurrent claimant has not marked yet, and any executing row are adopted as inherited uncertainty.
    if outcome is not None and outcome.startswith("done:"):
        raise _ReleaseAlreadyDone()   # this very request's release committed (its done row is written with it)
    clean = outcome == RELEASE_CLEAN and created_here
    if outcome is None or not (outcome in (RELEASE_CLEAN, "claimed") or outcome.startswith("executing:")):
        raise LeaderRecoveryError("recovery_context_changed")
    marker = f"executing:{attempt}" if clean else f"executing:{attempt}:u"
    result = await db.execute(update(GithubMaintenanceEvent).where(
        GithubMaintenanceEvent.operation_id == ctx.claim_key, GithubMaintenanceEvent.request_sha256 == ctx.claim_digest,
        GithubMaintenanceEvent.outcome == outcome).values(outcome=marker))
    await db.commit()
    if result.rowcount != 1:
        raise LeaderRecoveryError("recovery_context_changed")
    return not clean


async def _notice_allowed(db, ctx, *, own_attempt=None):
    """May this call speak for a committed release? The proof of the release is lease-bound, so the ledger row of
    THIS operation id and THIS request must also show it.

    The attempt that ran the release needs its own done row. A replay needs a same-request done row, or the
    historical exception of plan 384 clause 4: a plain legacy 'claimed' row (the #997 interim wrote only those;
    version 2 writes 'claimed:r2'). An executing row, a clean version 2 row, a missing row or another request
    refuses.
    """
    await db.rollback()
    outcome = await db.scalar(select(GithubMaintenanceEvent.outcome).where(
        GithubMaintenanceEvent.operation_id == ctx.claim_key,
        GithubMaintenanceEvent.request_sha256 == ctx.claim_digest))
    if outcome is None:
        return False
    if own_attempt is not None:
        return outcome == f"done:{own_attempt}"
    return outcome.startswith("done:") or outcome == "claimed"


async def _settle_release_claim(db, ctx, operation_id, attempt, *, inherited, started, workspace_id, leased_at):
    """After a failed release attempt: free the id only if THIS attempt provably never started its revoker.

    An inherited marker is never reverted, and an attempt whose fence passed may have started the revoker, so
    its marker stays. A clean attempt reverts its own marker by exact attempt id and then frees the id with the
    atomic no-effect delete.
    """
    await db.rollback()
    if started or inherited:
        return
    reverted = await db.execute(update(GithubMaintenanceEvent).where(
        GithubMaintenanceEvent.operation_id == ctx.claim_key, GithubMaintenanceEvent.request_sha256 == ctx.claim_digest,
        GithubMaintenanceEvent.outcome == f"executing:{attempt}").values(outcome=RELEASE_CLEAN))
    await db.commit()
    if reverted.rowcount == 1:
        await _unclaim(db, "release", ctx, operation_id, workspace_id=workspace_id, leased_at=leased_at)


async def release_workspace(db, item, session, *, workspace_id, expected_leased_at, operation_id, reason):
    reason = _need_reason(reason)
    ctx = _ctx(item, session)
    scope, preset, _leader, supervisor = await _authorize(db, item, session)
    ctx = replace(ctx, preset_id=preset.id, supervisor_id=supervisor.id)
    scope_id, item_status = scope.id, item.dispatch_status
    workspace = await db.get(GithubWorkspace, workspace_id, populate_existing=True)
    if workspace is None or workspace.scope_id != scope_id:
        raise LeaderRecoveryError("workspace_not_found", 404)
    digest = _digest("release", ctx.item_id, workspace_id, expected_leased_at.isoformat(), reason)
    ctx = replace(ctx, claim_key=f"{KEY}:release:{operation_id}", claim_digest=digest)
    replay = await _existing_claim(db, ctx.item_id, "release", operation_id, digest)

    async def already_released():
        # The effect proof belongs to the lease, not to this request. A replay that woke after its id was freed
        # and reused by a corrected request must not speak for that release: it answers only while the ledger
        # row for THIS operation id still holds THIS request's digest.
        if not await _notice_allowed(db, ctx):
            raise LeaderRecoveryError("recovery_context_changed")
        notice = await _notify(db, ctx, kind="release", operation_id=operation_id, digest=digest,
            summary=f"workspace {workspace_id} was released.", reason=reason)
        return {"status": "already_released", "workspace_id": workspace_id, "notice": notice}

    if not _held(workspace, ctx.item_id, expected_leased_at):
        if replay and workspace.leased_item_id != ctx.item_id and await _release_applied(db, workspace_id, expected_leased_at):
            return await already_released()
        raise LeaderRecoveryError("workspace_lease_changed")
    if item_status not in TERMINAL_ITEMS:
        raise LeaderRecoveryError("item_not_terminal")
    # Stricter than the operator route: a real inspection of every workspace kind; dirty, unpublished or unreadable refuses.
    if await github_workspace_service.delegated_release_blocker(scope, workspace, item):
        raise LeaderRecoveryError("workspace_not_clean")
    if not replay:
        replay = await _claim(db, ctx.item_id, "release", operation_id, digest)
    await _reload(db, item, scope, workspace)
    if not _held(workspace, ctx.item_id, expected_leased_at):
        if workspace.leased_item_id != ctx.item_id and await _release_applied(db, workspace_id, expected_leased_at):
            return await already_released()  # a replay released it; the shared claim record stays
        raise LeaderRecoveryError("workspace_lease_changed")  # the claim is kept: see _abandon
    lease_token = workspace.lease_token
    attempt = uuid.uuid4().hex[:12]
    try:
        inherited = await _mark_executing(db, ctx, attempt, created_here=not replay)   # durable before any external effect
    except _ReleaseAlreadyDone:
        return await already_released()
    await _reload(db, item, scope, workspace)
    fence = {"passed": 0}

    async def guarded():
        await _assert_authority(db, ctx, terminal=True)
        # The done row is written inside the writer transaction that clears the lease and records the release
        # audit: the service's rollback reverts it on any failure and its commit makes it durable with the release.
        # A repeated fence matches no executing row any more, so it must find this attempt's own done row.
        written = await db.execute(update(GithubMaintenanceEvent).where(
            GithubMaintenanceEvent.operation_id == ctx.claim_key, GithubMaintenanceEvent.request_sha256 == ctx.claim_digest,
            GithubMaintenanceEvent.outcome.startswith("executing:")).values(outcome=f"done:{attempt}"))
        if written.rowcount != 1 and await db.scalar(select(GithubMaintenanceEvent.outcome).where(
                GithubMaintenanceEvent.operation_id == ctx.claim_key,
                GithubMaintenanceEvent.request_sha256 == ctx.claim_digest)) != f"done:{attempt}":
            raise LeaderRecoveryError("recovery_context_changed")
        fence["passed"] += 1   # once the first fence passed, the revoker may start

    def settle():
        return _settle_release_claim(db, ctx, operation_id, attempt, inherited=inherited,
            started=fence["passed"] > 0, workspace_id=workspace_id, leased_at=expected_leased_at)
    try:
        released = await github_workspace_service.force_release_acquisition(db, actor_kind="member",
            actor_member_id=ctx.member_id, actor_session_id=ctx.session_id, workspace_id=workspace_id,
            scope_id=scope_id, item_id=ctx.item_id, expected_leased_at=expected_leased_at,
            lease_token=lease_token, before_commit=guarded)
    except GithubWorkspaceCredentialRevokeError as exc:
        await settle()
        raise LeaderRecoveryError(exc.block_code, 503) from exc
    except GithubWorkspaceConfigError as exc:
        await settle()
        raise LeaderRecoveryError(exc.block_code) from exc
    except LeaderRecoveryError:
        await settle()
        if await _abandon(db, "release", ctx, operation_id, replay, workspace_id=workspace_id, leased_at=expected_leased_at):
            return await already_released()
        raise
    if not released:
        await settle()
        if await _abandon(db, "release", ctx, operation_id, replay, workspace_id=workspace_id, leased_at=expected_leased_at):
            return await already_released()  # a replay released it; the shared claim record stays
        raise LeaderRecoveryError("workspace_lease_changed")
    if not await _notice_allowed(db, ctx, own_attempt=attempt):   # this attempt's own done row, written with the release
        raise LeaderRecoveryError("recovery_context_changed")
    notice = await _notify(db, ctx, kind="release", operation_id=operation_id,
        digest=digest, summary=f"workspace {workspace_id} was released.", reason=reason)
    return {"status": "released", "workspace_id": workspace_id, "notice": notice}


async def end_stopped(db, item, session, request):
    ctx = _ctx(item, session)
    scope, preset, _leader, supervisor = await _authorize(db, item, session)
    ctx = replace(ctx, preset_id=preset.id, supervisor_id=supervisor.id)
    try:
        result = await end_stopped_item(db, item, scope, request, actor=_actor(ctx),
                                        before_commit=lambda: _assert_authority(db, ctx))
    except ValueError as exc:
        raise LeaderRecoveryError(str(exc)) from None
    notice = await _notify(db, ctx, kind="end_stopped", operation_id=request.operation_id,
        digest=_digest(request.model_dump(mode="json")), summary=f"the stopped item was {request.outcome}.",
        reason=request.reason)
    return {**result, "notice": notice}


_OPERATION = {"pattern": r"^[A-Za-z0-9_.-]{1,100}$"}


class CancelRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    revision_number: int = Field(gt=0)
    dispatch_nonce: str = Field(min_length=1)
    operation_id: str = Field(**_OPERATION)
    reason: str = Field(min_length=1, max_length=240)


class FinalizeMergedLeaderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    operation_id: str = Field(**_OPERATION)
    expected_context_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=1, max_length=240)


class ReleaseWorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: int = Field(gt=0)
    expected_leased_at: datetime
    operation_id: str = Field(**_OPERATION)
    reason: str = Field(min_length=1, max_length=240)

    def leased_at(self) -> datetime:
        return naive_utc_confirmation(self.expected_leased_at)
