"""#1239: waits on a decision, as append-only start and end events keyed by the request id.

Both events are written in the transaction of the request's status change. Who answered is recorded only when known.
The ledger binds each event to the work item's lifetime, so an id reused by a later item is another wait. A request
row is deleted only with its work item; such a wait has no end and reads as unknown, not open.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import FactoryAuditEvent, FactoryContextKey, GithubApprovalRequest, MailMessage, MailTeamMember
from app.services import factory_audit_service as audit

STARTED = "decision_wait_started"
ENDED = "decision_wait_ended"
# #1240: a Mail request to a member, keyed by its message id, and an input wait on a pane, keyed by its episode.
MAIL_STARTED, MAIL_ENDED = "mail_wait_started", "mail_wait_ended"
INPUT_STARTED, INPUT_ENDED = "input_wait_started", "input_wait_ended"
# #1299: an effort-limit episode, keyed by the Mail message id of the limit notice to the Leader.
EFFORT_STARTED, EFFORT_ENDED = "effort_wait_started", "effort_wait_ended"
# #1300: a Supervisor decision beyond the effort ceiling, keyed by the Mail message id of the ceiling notice.
CEILING_STARTED, CEILING_ENDED = "ceiling_wait_started", "ceiling_wait_ended"
# #1338: the owner waits for the operator at a recovery checkpoint hold, keyed by the continuation request id and the
# held stage, or at a recorded observation pause, keyed by the pause id.
HOLD_STARTED, HOLD_ENDED = "hold_wait_started", "hold_wait_ended"
PAUSE_STARTED, PAUSE_ENDED = "pause_wait_started", "pause_wait_ended"
PROVIDER_STARTED, PROVIDER_ENDED = "provider_wait_started", "provider_wait_ended"
# The reason complete_current_revision gives a revision it supersedes in an item's terminal transition.
ITEM_TERMINAL_REASON = "The work item reached a terminal state."
# The Mail root of a structured request carries this delivery key; its decision wait already counts it.
STRUCTURED_REQUEST_KEY = "github-approval:"


def wait_operation(kind: str, request_id: int) -> str:
    """The operation id of a wait event; #1240, #1299 and #1300 deduplicate on the request id it names."""
    return f"{kind}:request:{request_id}"


async def record_wait_started(
    db: AsyncSession, request: GithubApprovalRequest, *, source: str, request_kind: str | None = None,
) -> None:
    """Who asked, who must answer, and when it was asked. A scope amendment names its own kind (#1299)."""
    request_kind = request_kind or request.request_kind
    await db.flush()
    operation_id = wait_operation(STARTED, request.id)
    await audit.record_event(
        db, event_kind=STARTED, source=source, occurred_at=datetime.utcnow(),
        fact_source="github_approval_requests", fact_time=request.created_at,
        actor=audit.derive_actor(actor_kind="member", member_id=request.owner_member_id),
        item_id=request.work_item_id, request_id=request.id,
        after_values={"wait_request_id": request.id, "request_kind": request_kind,
                      "owner_member_id": request.owner_member_id,
                      "awaited_member_id": request.leader_member_id, "awaited_role": "leader"},
        action_outcome="applied", sanitized_reason=f"{request_kind} waits on the leader",
        operation_id=operation_id, correlation_id=operation_id)


async def record_wait_ended(
    db: AsyncSession, request_ids: Iterable[int], *, outcome: str, source: str,
    answered_by_member_id: int | None = None, ended_at: datetime | None = None,
) -> None:
    """End each started wait among ``request_ids``. A wait never ends twice; one never started gets no end."""
    for request_id in sorted(set(request_ids)):
        # The current request's own item lifetime: an id reused after its item was deleted is another wait.
        item_id = await db.scalar(select(GithubApprovalRequest.work_item_id).where(GithubApprovalRequest.id == request_id))
        item_key = await audit.current_context_key(db, "item", item_id) if item_id is not None else None
        started = await _event(db, STARTED, request_id, item_key=item_key)
        if started is None or await _event(db, ENDED, request_id, item_key=item_key) is not None:
            continue
        operation_id = wait_operation(ENDED, request_id)
        await audit.record_event(
            db, event_kind=ENDED, source=source, occurred_at=datetime.utcnow(),
            fact_source="github_approval_requests", fact_time=ended_at or datetime.utcnow(),
            actor=audit.derive_actor(actor_kind="member", member_id=answered_by_member_id)
            if answered_by_member_id is not None else audit.derive_actor(actor_kind="system"),
            item_id=started.item_id, request_id=request_id, item_context_key=started.item_context_key,
            scope_context_key=started.scope_context_key, team_context_key=started.team_context_key,
            after_values={"wait_request_id": request_id, "wait_outcome": outcome,
                          "answered_by_member_id": answered_by_member_id},
            action_outcome="applied", sanitized_reason=f"decision wait ended: {outcome}",
            operation_id=operation_id, correlation_id=operation_id)


async def _event(db: AsyncSession, kind: str, request_id: int, *, item_key: str | None):
    return (await db.scalars(select(FactoryAuditEvent).where(
        FactoryAuditEvent.event_kind == kind, FactoryAuditEvent.operation_id == wait_operation(kind, request_id),
        FactoryAuditEvent.item_context_key == item_key).limit(1))).first()


async def _once(db: AsyncSession, operation_id: str, *, item_key: str | None = None) -> bool:
    """Whether the operation is recorded; ``item_key`` binds it to one item lifetime, as the replay key does."""
    query = select(FactoryAuditEvent.id).where(FactoryAuditEvent.operation_id == operation_id)
    if item_key is not None:
        query = query.where(FactoryAuditEvent.item_context_key == item_key)
    return (await db.scalar(query.limit(1))) is not None


async def record_mail_wait_started(db: AsyncSession, message: MailMessage, *, source: str) -> None:
    """A pending Mail request waits on its recipient, unless it is the Mail side of a structured request."""
    if message.request_status != "pending" or (message.delivery_key or "").startswith(STRUCTURED_REQUEST_KEY):
        return
    await db.flush()
    operation_id = f"{MAIL_STARTED}:message:{message.id}"
    team = await db.scalar(select(MailTeamMember.team_preset_id).where(MailTeamMember.id == message.recipient_member_id))
    await audit.record_event(
        db, event_kind=MAIL_STARTED, source=source, occurred_at=datetime.utcnow(), fact_source="mail_messages",
        fact_time=message.created_at, team_preset_id=team,
        actor=audit.derive_actor(actor_kind="member", member_id=message.sender_member_id)
        if message.sender_member_id is not None else audit.derive_actor(actor_kind="system"),
        after_values={"wait_message_id": message.id, "request_kind": message.kind,
                      "owner_member_id": message.sender_member_id, "owner_actor_id": message.sender_actor_id,
                      "awaited_member_id": message.recipient_member_id},
        action_outcome="applied", sanitized_reason=f"{message.kind} waits on its recipient",
        operation_id=operation_id, correlation_id=operation_id)


async def record_mail_wait_ended(
    db: AsyncSession, message_id: int, *, outcome: str, source: str, answered_by_member_id: int | None = None,
) -> None:
    """End a started Mail wait once. Who answered stays unattributed unless known."""
    started = (await db.scalars(select(FactoryAuditEvent).where(
        FactoryAuditEvent.operation_id == f"{MAIL_STARTED}:message:{message_id}").limit(1))).first()
    operation_id = f"{MAIL_ENDED}:message:{message_id}"
    if started is None or await _once(db, operation_id):
        return
    await audit.record_event(
        db, event_kind=MAIL_ENDED, source=source, occurred_at=datetime.utcnow(), fact_source="mail_messages",
        fact_time=datetime.utcnow(), team_preset_id=started.team_preset_id, team_context_key=started.team_context_key,
        actor=audit.derive_actor(actor_kind="member", member_id=answered_by_member_id)
        if answered_by_member_id is not None else audit.derive_actor(actor_kind="system"),
        after_values={"wait_message_id": message_id, "wait_outcome": outcome,
                      "answered_by_member_id": answered_by_member_id},
        action_outcome="applied", sanitized_reason=f"mail wait ended: {outcome}",
        operation_id=operation_id, correlation_id=operation_id)


def _episode(kind: str, binding_id: int, started_at: datetime) -> str:
    return f"{kind}:binding:{binding_id}:{audit._naive(started_at).isoformat()}"


async def record_input_wait_started(
    db: AsyncSession, *, preset_id: int, slot_id: int, binding_id: int, started_at: datetime, reason: str,
    owner_member_id: int | None, awaited_member_id: int | None, awaited_actor_id: int | None, source: str,
) -> None:
    """A member waits for input on its pane; the Supervisor, else the Leader, is who the notice asks."""
    operation_id = _episode(INPUT_STARTED, binding_id, started_at)
    await audit.record_event(
        db, event_kind=INPUT_STARTED, source=source, occurred_at=datetime.utcnow(), fact_source="agent_pane_bindings",
        fact_time=audit._naive(started_at), team_preset_id=preset_id, team_slot_id=slot_id,
        actor=audit.derive_actor(actor_kind="scheduler", scheduler="agent_activity_service"),
        after_values={"wait_binding_id": binding_id, "request_kind": reason, "owner_member_id": owner_member_id,
                      "awaited_member_id": awaited_member_id, "awaited_actor_id": awaited_actor_id},
        action_outcome="applied", sanitized_reason=f"input wait: {reason}", operation_id=operation_id,
        correlation_id=operation_id)


async def record_input_wait_ended(
    db: AsyncSession, *, binding_id: int, started_at: datetime | None, outcome: str, ended_at: datetime, source: str,
) -> None:
    """End a started input-wait episode once; nobody is known to have answered it."""
    if started_at is None:
        return
    started = (await db.scalars(select(FactoryAuditEvent).where(
        FactoryAuditEvent.operation_id == _episode(INPUT_STARTED, binding_id, started_at)).limit(1))).first()
    operation_id = _episode(INPUT_ENDED, binding_id, started_at)
    if started is None or await _once(db, operation_id):
        return
    await audit.record_event(
        db, event_kind=INPUT_ENDED, source=source, occurred_at=datetime.utcnow(), fact_source="agent_pane_bindings",
        # The observation time, not the insertion time.
        fact_time=audit._naive(ended_at), team_preset_id=started.team_preset_id, team_slot_id=started.team_slot_id,
        team_context_key=started.team_context_key,
        actor=audit.derive_actor(actor_kind="scheduler", scheduler="agent_activity_service"),
        after_values={"wait_binding_id": binding_id, "wait_outcome": outcome, "answered_by_member_id": None},
        action_outcome="applied", sanitized_reason=f"input wait ended: {outcome}", operation_id=operation_id,
        correlation_id=operation_id)


async def record_effort_wait_started(
    db: AsyncSession, *, item_id: int, notice_id: int, noticed_at: datetime, owner_member_id: int | None,
    leader_member_id: int, source: str,
) -> None:
    """#1299: the effort-limit notice to the Leader starts a wait on the Leader's extension decision."""
    operation_id = f"{EFFORT_STARTED}:message:{notice_id}"
    # Rebuilt on every reconciliation. Bound to the item lifetime: SQLite can reuse a deleted Mail row's id.
    item_key = await audit.current_context_key(db, "item", item_id)
    if item_key is not None and await _once(db, operation_id, item_key=item_key):
        return
    await audit.record_event(
        db, event_kind=EFFORT_STARTED, source=source, occurred_at=datetime.utcnow(), fact_source="mail_messages",
        fact_time=audit._naive(noticed_at), item_id=item_id,
        actor=audit.derive_actor(actor_kind="scheduler", scheduler="github_effort_limit_service"),
        after_values={"wait_message_id": notice_id, "request_kind": "effort_extension",
                      "owner_member_id": owner_member_id, "awaited_member_id": leader_member_id,
                      "awaited_role": "leader"},
        action_outcome="applied", sanitized_reason="effort extension waits on the leader",
        operation_id=operation_id, correlation_id=operation_id)


async def record_effort_wait_ended(
    db: AsyncSession, *, item_id: int, outcome: str, ended_at: datetime, source: str,
    answered_by_member_id: int | None = None, notice_ids: list[int] | None = None,
) -> None:
    """End the item's open effort waits in its current lifetime, or only those of ``notice_ids``."""
    await _end_item_waits(
        db, EFFORT_STARTED, EFFORT_ENDED, item_id=item_id, outcome=outcome, ended_at=ended_at, source=source,
        notice_ids=notice_ids, values={"answered_by_member_id": answered_by_member_id},
        actor=audit.derive_actor(actor_kind="member", member_id=answered_by_member_id)
        if answered_by_member_id is not None else audit.derive_actor(actor_kind="system"))


async def record_ceiling_wait_started(
    db: AsyncSession, notice: MailMessage, *, item_id: int, leader_member_id: int | None, source: str,
    launch_id: int | None = None,
) -> None:
    """#1300: the ceiling notice starts a wait on the Supervisor, in the notice's own transaction.

    The snapshot names the notice's attempt by its non-secret launch identity, never by its nonce.
    """
    await db.flush()
    payload = notice.payload or {}
    operation_id = f"{CEILING_STARTED}:message:{notice.id}"
    await audit.record_event(
        db, event_kind=CEILING_STARTED, source=source, occurred_at=datetime.now(UTC), fact_source="mail_messages",
        fact_time=audit._naive(notice.created_at), item_id=item_id,
        context_snapshot={"launch_attempt": audit.launch_attempt_key(item_id, launch_id)},
        actor=audit.derive_actor(actor_kind="member", member_id=leader_member_id)
        if leader_member_id is not None else audit.derive_actor(actor_kind="system"),
        after_values={"wait_message_id": notice.id, "request_kind": "effort_ceiling",
                      "owner_member_id": leader_member_id, "awaited_member_id": None, "awaited_role": "supervisor",
                      "awaited_actor_id": payload.get("supervisor_actor_id")},
        action_outcome="applied", sanitized_reason="effort ceiling waits on the supervisor",
        operation_id=operation_id, correlation_id=operation_id)


async def open_ceiling_waits(db: AsyncSession, item_id: int) -> list[FactoryAuditEvent]:
    """The item's ceiling wait starts in its current lifetime that have no end."""
    item_key = await audit.current_context_key(db, "item", item_id)
    if item_key is None:
        return []
    starts = (await db.scalars(select(FactoryAuditEvent).where(
        FactoryAuditEvent.event_kind == CEILING_STARTED, FactoryAuditEvent.item_context_key == item_key)
        .order_by(FactoryAuditEvent.id))).all()
    return [start for start in starts if not await _once(
        db, start.operation_id.replace(CEILING_STARTED, CEILING_ENDED, 1), item_key=item_key)]


async def record_ceiling_wait_ended(
    db: AsyncSession, *, item_id: int, outcome: str, ended_at: datetime, source: str, actor: dict | None = None,
    notice_ids: list[int] | None = None, evidence: dict | None = None, before_values: dict | None = None,
    correlation_id: str | None = None, detail: str | None = None, context_snapshot: dict | None = None,
) -> None:
    """End the item's open ceiling waits, or those of ``notice_ids``, on the evidence that resolved them.

    ``actor`` is whoever acted; the answerer stays unknown: an operator token or a Leader never proves the
    Supervisor's (or the Director's) answer. ``correlation_id`` links the end to the action's own event. Numbers
    the value allowlist has no key for go in ``detail``, after the reason.
    """
    await _end_item_waits(
        db, CEILING_STARTED, CEILING_ENDED, item_id=item_id, outcome=outcome, ended_at=ended_at, source=source,
        notice_ids=notice_ids, values={"answered_by_member_id": None, **(evidence or {})},
        actor=actor or audit.derive_actor(actor_kind="system"), before_values=before_values,
        correlation_id=correlation_id, detail=detail, context_snapshot=context_snapshot)


async def _end_item_waits(
    db: AsyncSession, start_kind: str, end_kind: str, *, item_id: int, outcome: str, ended_at: datetime,
    source: str, actor: dict, values: dict, notice_ids: list[int] | None, before_values: dict | None = None,
    correlation_id: str | None = None, detail: str | None = None, context_snapshot: dict | None = None,
) -> None:
    item_key = await audit.current_context_key(db, "item", item_id)
    if item_key is None:
        return
    starts = (await db.scalars(select(FactoryAuditEvent).where(
        FactoryAuditEvent.event_kind == start_kind, FactoryAuditEvent.item_context_key == item_key,
        True if notice_ids is None else FactoryAuditEvent.operation_id.in_(
            [f"{start_kind}:message:{n}" for n in notice_ids])))).all()
    label = end_kind.removesuffix("_ended").replace("_", " ")
    for started in starts:
        operation_id = started.operation_id.replace(start_kind, end_kind, 1)
        if await _once(db, operation_id, item_key=item_key):
            continue
        # An extension found after a notice it had already satisfied gives a zero-length episode, never a reversed
        # one (V1, #1334): the end is never before the start.
        ended = max(audit._naive(ended_at), started.fact_time or audit._naive(ended_at))
        await audit.record_event(
            db, event_kind=end_kind, source=source, occurred_at=datetime.utcnow(), fact_source="mail_messages",
            fact_time=ended, item_id=started.item_id, item_context_key=item_key,
            scope_context_key=started.scope_context_key, team_context_key=started.team_context_key, actor=actor,
            after_values={"wait_message_id": started.after_values.get("wait_message_id"), "wait_outcome": outcome,
                          **values}, before_values=before_values, context_snapshot=context_snapshot,
            action_outcome="applied", sanitized_reason=f"{label} ended: {outcome}" + (f"; {detail}" if detail else ""),
            operation_id=operation_id, correlation_id=correlation_id or operation_id)


async def record_hold_wait_started(db: AsyncSession, *, request_id: int, item_id: int, stage: str, source: str) -> None:
    """#1338: a recovery checkpoint hold starts a wait on the operator, in the hold's own transaction."""
    owner = await db.scalar(select(GithubApprovalRequest.owner_member_id).where(GithubApprovalRequest.id == request_id))
    await _start_operator_wait(
        db, HOLD_STARTED, f"request:{request_id}:{stage}", item_id=item_id, request_id=request_id,
        fact_source="github_attempt_scope_revisions", fact_time=datetime.now(UTC),
        values={"wait_request_id": request_id, "request_kind": "checkpoint_hold",
                "recovery_checkpoint_stage": stage, "owner_member_id": owner, "awaited_role": "operator"},
        reason=f"{stage} waits on the operator", source=source)


async def record_hold_wait_ended(
    db: AsyncSession, *, request_id: int, item_id: int, stage: str, outcome: str, ended_at: datetime, source: str,
    actor: dict | None = None, evidence: dict | None = None,
) -> None:
    """End a hold wait. ``actor`` is exactly the one the release supplied; none is unknown, never assumed."""
    await _end_operator_wait(db, HOLD_STARTED, HOLD_ENDED, f"request:{request_id}:{stage}", item_id=item_id,
                             outcome=outcome, ended_at=ended_at, source=source, actor=actor, evidence=evidence)


async def record_pause_wait_started(
    db: AsyncSession, record, *, item_id: int, owner_member_id: int | None, source: str,
) -> None:
    """#1338: a recorded observation pause, once paused, starts a wait on the operator in the pause's commit.

    ``owner_member_id`` is the owner the pause service verified, never whoever resumes the pause.
    """
    await db.flush()
    await _start_operator_wait(
        db, PAUSE_STARTED, f"pause:{record.id}", item_id=item_id, request_id=None,
        fact_source="github_owner_observation_pauses", fact_time=record.paused_at,
        values={"wait_pause_id": record.id, "request_kind": "observation_pause",
                "owner_member_id": owner_member_id, "awaited_role": "operator"},
        reason="observation pause waits on the operator", source=source)


async def record_pause_wait_ended(
    db: AsyncSession, *, pause_id: int, item_id: int, outcome: str, ended_at: datetime, source: str,
    actor: dict | None = None, evidence: dict | None = None,
) -> None:
    """End a pause wait. ``actor`` is exactly the one the resume supplied; none is unknown, never assumed."""
    await _end_operator_wait(db, PAUSE_STARTED, PAUSE_ENDED, f"pause:{pause_id}", item_id=item_id,
                             outcome=outcome, ended_at=ended_at, source=source, actor=actor, evidence=evidence)


async def reconcile_provider_wait(
    db: AsyncSession, item, *, lease_at: datetime | None, blocked_at: datetime | None,
    recovered_at: datetime | None, now: datetime,
) -> bool:
    """Reconcile one provider interval under the effort ledger's item writer lock."""
    item_key = await audit.current_context_key(db, "item", item.id)
    latest = None if item_key is None else await db.scalar(select(FactoryAuditEvent).where(
        FactoryAuditEvent.item_context_key == item_key,
        FactoryAuditEvent.event_kind.in_((PROVIDER_STARTED, PROVIDER_ENDED)),
    ).order_by(FactoryAuditEvent.id.desc()).limit(1))
    launch = audit.launch_attempt_key(item.id, item.launch_id)
    source = "github_effort_limit_service.tick_item"
    actor = audit.derive_actor(actor_kind="scheduler", scheduler="github_effort_limit_service")
    if latest is not None and latest.event_kind == PROVIDER_STARTED:
        same_owner = (lease_at is not None and latest.fact_time >= lease_at
                      and (latest.context_snapshot or {}).get("launch_attempt") == launch
                      and (latest.after_values or {}).get("owner_slot_id") == item.owner_slot_id)
        if same_owner and (recovered_at is None or recovered_at <= latest.fact_time):
            return True
        outcome = "provider_recovered" if same_owner else "owner_attempt_ended"
        await audit.record_event(
            db, event_kind=PROVIDER_ENDED, source=source, occurred_at=now,
            fact_source="owner_native_activity", fact_time=recovered_at if same_owner else now,
            item_id=item.id, actor=actor, action_outcome=outcome,
            context_snapshot={"launch_attempt": (latest.context_snapshot or {}).get("launch_attempt")},
            operation_id=latest.operation_id.replace(PROVIDER_STARTED, PROVIDER_ENDED, 1),
        )
    if blocked_at is None:
        return False
    await audit.record_event(
        db, event_kind=PROVIDER_STARTED, source=source, occurred_at=now,
        fact_source="owner_native_activity", fact_time=blocked_at, item_id=item.id, actor=actor,
        context_snapshot={"launch_attempt": launch}, action_outcome="provider_usage_limit",
        after_values={"provider": "codex-cli", "owner_slot_id": item.owner_slot_id},
        operation_id=f"{PROVIDER_STARTED}:{uuid4()}",
    )
    return True


async def reconcile_operator_waits(db: AsyncSession, *, scope_id: int, now: datetime) -> bool:
    """End the scope's open hold and pause waits that nothing released, at their first resolving transition.

    ``hold_cleared`` when the hold's revision ended on its own; ``item_terminal`` when the item ended first. Both
    unattributed. The end takes the transition's own time, never the poll's (V1 review 6070971861): the revision's
    end or its request's supersession, the item's end-stopped event, or else the item's last write, an approximate
    bound no later than the poll.
    Whether any end was written.
    """
    from sqlalchemy import func

    from app.models.database import GithubAttemptScopeRevision, GithubWorkItem
    from app.services.github_terminal_revision_service import (
        TERMINAL_ITEMS,
        TERMINAL_REVISIONS,
    )

    starts = (await db.scalars(select(FactoryAuditEvent).where(
        FactoryAuditEvent.event_kind.in_((HOLD_STARTED, PAUSE_STARTED)),
        FactoryAuditEvent.item_id.in_(select(GithubWorkItem.id).where(GithubWorkItem.scope_id == scope_id)))
        .order_by(FactoryAuditEvent.id))).all()
    wrote = False
    for start in starts:
        item_key = await audit.current_context_key(db, "item", start.item_id)
        start_kind = start.event_kind
        end_kind = HOLD_ENDED if start_kind == HOLD_STARTED else PAUSE_ENDED
        suffix = start.operation_id.removeprefix(f"{start_kind}:")
        if start.item_context_key != item_key or await _once(db, f"{end_kind}:{suffix}", item_key=item_key):
            continue
        status, written = (await db.execute(select(GithubWorkItem.dispatch_status, GithubWorkItem.updated_at)
                                            .where(GithubWorkItem.id == start.item_id))).one()
        item_ended = status in TERMINAL_ITEMS
        # End-stopped records its event and cancels the item's revisions with one time.
        stopped = await db.scalar(select(func.min(FactoryAuditEvent.occurred_at)).where(
            FactoryAuditEvent.event_kind == "stopped_work_item_ended", FactoryAuditEvent.item_id == start.item_id,
            FactoryAuditEvent.item_context_key == item_key))
        values = start.after_values or {}
        revision = None
        if start_kind == HOLD_STARTED:
            revision = (await db.execute(select(
                GithubAttemptScopeRevision.status, GithubAttemptScopeRevision.recovery_checkpoint_stage,
                GithubAttemptScopeRevision.cancelled_at, GithubAttemptScopeRevision.completed_at,
                GithubAttemptScopeRevision.expires_at, GithubAttemptScopeRevision.cancellation_reason,
            ).where(GithubAttemptScopeRevision.approval_request_id == values.get("wait_request_id")))).first()
        still_held = revision is not None and revision[0] not in TERMINAL_REVISIONS \
            and revision[1] == values.get("recovery_checkpoint_stage")
        if start_kind == PAUSE_STARTED or still_held:
            if not item_ended:
                continue
            outcome, at, evidence = "item_terminal", stopped or written, {"dispatch_status": status}
        else:
            rev_status, stage, cancelled_at, completed_at, expires_at, reason = revision or (None,) * 6
            # Approval cancellation and stale supersession record the request's superseded_at and no revision end
            # time (V1 review 6071348579).
            superseded_at = await db.scalar(select(GithubApprovalRequest.superseded_at).where(
                GithubApprovalRequest.id == values.get("wait_request_id")))
            ended_at = (cancelled_at or completed_at or (expires_at if rev_status == "expired" else None)
                        or superseded_at)
            # The item's own terminal transition ended this revision: complete_current_revision completes or
            # supersedes it with its fixed reason, or end-stopped cancelled it at the event's time.
            by_item = item_ended and (rev_status == "completed" or reason == ITEM_TERMINAL_REASON
                                      or (stopped is not None and ended_at == stopped))
            if by_item:
                outcome, at, evidence = "item_terminal", ended_at or stopped or written, {"dispatch_status": status}
            else:
                outcome, at, evidence = "hold_cleared", ended_at, {"recovery_checkpoint_stage": stage}
        await _end_operator_wait(db, start_kind, end_kind, suffix, item_id=start.item_id, outcome=outcome,
                                 ended_at=min(audit._naive(at), audit._naive(now)) if at else now,
                                 source="decision_wait_service.reconcile_operator_waits", evidence=evidence)
        wrote = True
    return wrote


async def _start_operator_wait(
    db: AsyncSession, start_kind: str, suffix: str, *, item_id: int, request_id: int | None, fact_source: str,
    fact_time, values: dict, reason: str, source: str,
) -> None:
    operation_id = f"{start_kind}:{suffix}"
    item_key = await audit.current_context_key(db, "item", item_id)
    if item_key is not None and await _once(db, operation_id, item_key=item_key):
        return
    await audit.record_event(
        db, event_kind=start_kind, source=source, occurred_at=datetime.now(UTC), fact_source=fact_source,
        fact_time=audit._naive(fact_time), item_id=item_id, request_id=request_id,
        actor=audit.derive_actor(actor_kind="system"), after_values=values, action_outcome="applied",
        sanitized_reason=reason, operation_id=operation_id, correlation_id=operation_id)


async def _end_operator_wait(
    db: AsyncSession, start_kind: str, end_kind: str, suffix: str, *, item_id: int, outcome: str,
    ended_at: datetime, source: str, actor: dict | None = None, evidence: dict | None = None,
) -> None:
    """One end per start in its item lifetime, never before it; a missing actor stays unknown (system)."""
    item_key = await audit.current_context_key(db, "item", item_id)
    if item_key is None:
        return
    started = (await db.scalars(select(FactoryAuditEvent).where(
        FactoryAuditEvent.operation_id == f"{start_kind}:{suffix}",
        FactoryAuditEvent.item_context_key == item_key).limit(1))).first()
    operation_id = f"{end_kind}:{suffix}"
    if started is None or await _once(db, operation_id, item_key=item_key):
        return
    key = "wait_request_id" if start_kind == HOLD_STARTED else "wait_pause_id"
    ended = max(audit._naive(ended_at), started.fact_time or audit._naive(ended_at))
    await audit.record_event(
        db, event_kind=end_kind, source=source, occurred_at=datetime.now(UTC), fact_source=started.fact_source,
        fact_time=ended, item_id=started.item_id, request_id=started.request_id, item_context_key=item_key,
        scope_context_key=started.scope_context_key, team_context_key=started.team_context_key,
        actor=actor or audit.derive_actor(actor_kind="system"),
        after_values={key: (started.after_values or {}).get(key), "wait_outcome": outcome,
                      "answered_by_member_id": None, **(evidence or {})},
        action_outcome="applied",
        sanitized_reason=f"{end_kind.removesuffix('_ended').replace('_', ' ')} ended: {outcome}",
        operation_id=operation_id, correlation_id=operation_id)


@dataclass(frozen=True)
class DecisionWait:
    request_id: int
    request_kind: str | None
    owner_member_id: int | None
    awaited_member_id: int | None
    started_at: datetime
    age_seconds: float | None
    state: str  # "open", or "unknown" when the work item was deleted before the request ended
    reason: str | None = None
    # "decision", "mail", "effort" or "ceiling" (keyed by message id), "input" (keyed by pane binding id), "hold"
    # (keyed by request id) or "pause" (keyed by pause id)
    wait_kind: str = "decision"
    awaited_actor_id: int | None = None


async def unended_decision_waits(
    db: AsyncSession, *, now: datetime, team_context_key: str | None = None,
) -> list[DecisionWait]:
    """Every started wait without an end: open with its age, or unknown when its work item was deleted."""
    query = select(FactoryAuditEvent).where(FactoryAuditEvent.event_kind == STARTED)
    if team_context_key is not None:
        query = query.where(FactoryAuditEvent.team_context_key == team_context_key)
    starts = (await db.scalars(query.order_by(FactoryAuditEvent.id))).all()
    ended = set((await db.execute(select(FactoryAuditEvent.operation_id, FactoryAuditEvent.item_context_key).where(
        FactoryAuditEvent.event_kind == ENDED))).all())
    retired = set((await db.scalars(select(FactoryContextKey.context_key).where(
        FactoryContextKey.key_kind == "item", FactoryContextKey.retired_at.is_not(None)))).all())
    waits = []
    for start in starts:
        values = start.after_values or {}
        request_id = values.get("wait_request_id")
        if not isinstance(request_id, int) or (wait_operation(ENDED, request_id), start.item_context_key) in ended:
            continue
        began, deleted = start.fact_time or start.occurred_at, start.item_context_key in retired
        waits.append(DecisionWait(
            request_id=request_id, request_kind=values.get("request_kind"),
            owner_member_id=values.get("owner_member_id"), awaited_member_id=values.get("awaited_member_id"),
            started_at=began, age_seconds=None if deleted else (now - began).total_seconds(),
            state="unknown" if deleted else "open", reason="work item deleted" if deleted else None))
    for kind, (start_kind, end_kind) in {"mail": (MAIL_STARTED, MAIL_ENDED), "input": (INPUT_STARTED, INPUT_ENDED),
                                         "effort": (EFFORT_STARTED, EFFORT_ENDED),
                                         "ceiling": (CEILING_STARTED, CEILING_ENDED),
                                         "hold": (HOLD_STARTED, HOLD_ENDED),
                                         "pause": (PAUSE_STARTED, PAUSE_ENDED)}.items():
        query = select(FactoryAuditEvent).where(FactoryAuditEvent.event_kind == start_kind)
        if team_context_key is not None:
            query = query.where(FactoryAuditEvent.team_context_key == team_context_key)
        ended_ids = set((await db.execute(select(FactoryAuditEvent.operation_id, FactoryAuditEvent.item_context_key)
                                          .where(FactoryAuditEvent.event_kind == end_kind))).all())
        for start in (await db.scalars(query.order_by(FactoryAuditEvent.id))).all():
            # An end matches its start's item lifetime too (effort waits; Mail and input waits carry none).
            if (start.operation_id.replace(start_kind, end_kind, 1), start.item_context_key) in ended_ids:
                continue
            values, began = start.after_values or {}, start.fact_time or start.occurred_at
            # An item-bound wait (effort, ceiling, hold, pause) whose work item was deleted is unknown, as for
            # decision waits.
            deleted = start.item_context_key is not None and start.item_context_key in retired
            waits.append(DecisionWait(
                request_id=(values.get("wait_message_id") or values.get("wait_binding_id")
                            or values.get("wait_request_id") or values.get("wait_pause_id")),
                request_kind=values.get("request_kind"), owner_member_id=values.get("owner_member_id"),
                awaited_member_id=values.get("awaited_member_id"), started_at=began,
                age_seconds=None if deleted else (now - began).total_seconds(),
                state="unknown" if deleted else "open", reason="work item deleted" if deleted else None,
                wait_kind=kind, awaited_actor_id=values.get("awaited_actor_id")))
    return waits
