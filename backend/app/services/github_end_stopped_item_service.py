"""End an inspected stopped attempt with a recorded non-delivery disposition."""
from datetime import datetime
import re
from typing import Literal

import httpx
from mcp_shim.work_remaining_protocol import public_generation
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import exists, select, update

from app.models.database import (
    GithubApprovalRequest, GithubAttemptScopeRevision, GithubMaintenanceEvent,
    GithubOwnerObservationPause, GithubWorkItem, GithubWorkspace,
)
from app.services import decision_wait_service as decision_waits
from app.services import factory_audit_service as audit
from app.services.github_approval_service import GithubApprovalError, github_approval_service
from app.services.github_client import GithubClientResponseError, github_client
from app.services.github_coordination_service import hold_code
from app.services.github_terminal_revision_service import TERMINAL_REVISIONS
from app.services.github_workspace_service import (
    GithubWorkspaceConfigError, GithubWorkspaceCredentialRevokeError, github_workspace_service,
)
from app.services.maintenance_operations import digest
from app.services.work_item_states import ATTENTION_STATUSES


class EndStoppedItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    operation_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    expected_dispatch_generation: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    expected_scope_revision: int = Field(ge=0)
    outcome: Literal["deferred", "superseded"]
    reason: str = Field(min_length=1, max_length=240)
    target_milestone: str | None = Field(default=None, min_length=1, max_length=120)
    superseding_pr_number: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def check_destination(self):
        if self.outcome == "deferred":
            valid = self.target_milestone is not None and self.superseding_pr_number is None
        else:
            valid = self.superseding_pr_number is not None and self.target_milestone is None
        if not valid:
            raise ValueError("supply exactly the destination for the selected outcome")
        return self


async def _delivery_evidence(scope, item, number):
    from app.services.github_verification_service import github_verification_service

    if number == item.pr_number:
        raise ValueError("superseding_pr_must_differ")
    try:
        token = await github_approval_service.github_read_token(scope)
        pull = await github_client.get_pull(scope.repo_owner, scope.repo_name, number, token=token)
        base_ref = await github_verification_service.normalize_base_ref(
            scope, github_client, token=token, base_ref=item.dispatch_base_ref)
    except (GithubApprovalError, GithubClientResponseError, httpx.HTTPError):
        raise ValueError("superseding_pr_unreadable") from None

    base = pull.get("base")
    repo = base.get("repo") if isinstance(base, dict) else None
    sha = pull.get("merge_commit_sha")
    if (type(pull.get("number")) is not int or pull["number"] != number or pull.get("state") != "closed"
            or pull.get("merged") is not True or not pull.get("merged_at") or not isinstance(repo, dict)
            or str(repo.get("full_name", "")).casefold() != f"{scope.repo_owner}/{scope.repo_name}".casefold()
            or not base_ref or base.get("ref") != base_ref
            or not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None):
        raise ValueError("superseding_pr_not_merged_into_target")
    return {"superseding_pr_number": number, "merge_commit_sha": sha}


async def _assert_not_paused(db, item_id):
    if hold_code():
        raise ValueError("operator_hold")
    if await db.scalar(select(GithubOwnerObservationPause.id).where(
            GithubOwnerObservationPause.work_item_id == item_id,
            GithubOwnerObservationPause.status.in_(("paused", "waiting")))) is not None:
        raise ValueError("observation_pause_active")
    if hold_code():
        raise ValueError("operator_hold")


async def end_pending_requests(db, item_id: int, *, now: datetime) -> None:
    """Supersede the item's pending requests; #1239: their decision waits end with them."""
    pending = (await db.scalars(select(GithubApprovalRequest.id).where(
        GithubApprovalRequest.work_item_id == item_id, GithubApprovalRequest.status == "pending"))).all()
    await db.execute(update(GithubApprovalRequest).where(
        GithubApprovalRequest.work_item_id == item_id, GithubApprovalRequest.status == "pending")
        .values(status="superseded", superseded_at=now))
    await decision_waits.record_wait_ended(
        db, pending, outcome="superseded", ended_at=now, source="github_end_stopped_item_service.end_stopped_item")


async def end_stopped_item(db, item, scope, request, *, actor, before_commit=None):
    """Record the disposition and clear the same inspected acquisition in one transaction."""
    item_id, scope_id = item.id, item.scope_id
    operation_id = f"end_stopped_item:{request.operation_id}"
    request_sha = digest([item_id, scope_id, item.created_at.isoformat(), request.model_dump(mode="json")])

    async def replayed():
        previous = await db.scalar(select(GithubMaintenanceEvent).where(
            GithubMaintenanceEvent.operation_id == operation_id))
        if previous is None:
            return False
        if previous.work_item_id != item_id or previous.request_sha256 != request_sha:
            raise ValueError("operation_conflict")
        return True

    response = {"work_item_id": item_id, "outcome": request.outcome}
    await _assert_not_paused(db, item_id)
    if await replayed():
        return {"status": "already_ended", **response}
    generation = public_generation(item_id, item.dispatch_nonce) if item.dispatch_nonce else None
    if generation != request.expected_dispatch_generation or item.active_scope_revision != request.expected_scope_revision:
        raise ValueError("end_stopped_item_context_changed")
    if item.dispatch_status not in ATTENTION_STATUSES:
        raise ValueError("item_not_stopped")

    identity_fields = (
        "scope_id", "dispatch_nonce", "dispatch_status", "escalation_reason", "launch_id", "owner_slot_id",
        "handoff_state", "handoff_target_slot_id", "approval_round_count", "active_scope_revision",
        "pr_number", "dispatch_base_ref", "retry_requested_at",
    )
    original_status = item.dispatch_status
    conditions = [GithubWorkItem.id == item_id, *(
        getattr(GithubWorkItem, field) == getattr(item, field) for field in identity_fields)]
    workspace = await github_workspace_service.get_leased_workspace(db, item_id)
    evidence = ({"target_milestone": request.target_milestone} if request.outcome == "deferred"
                else await _delivery_evidence(scope, item, request.superseding_pr_number))
    reason = audit.sanitize_reason(request.reason)
    destination = (f"Deferred to {audit.sanitize_text(request.target_milestone)}"
                   if request.outcome == "deferred" else f"Superseded by PR #{request.superseding_pr_number}")
    if workspace is not None and await github_workspace_service.delegated_release_blocker(scope, workspace, item):
        raise ValueError("workspace_not_clean")

    applied = False

    async def settle():
        nonlocal applied
        await _assert_not_paused(db, item_id)
        if before_commit is not None:
            await before_commit()
        if applied:
            return
        now = datetime.utcnow()
        claim = await db.execute(update(GithubWorkItem).where(*conditions,
            ~exists().where(GithubWorkspace.leased_item_id == item_id))
            .values(dispatch_status="cancelled", escalation_reason=None, pending_reason=None,
                    handoff_state=None, handoff_target_slot_id=None,
                    status_note=f"{destination}. {reason}", updated_at=now)
            .execution_options(synchronize_session="fetch"))
        if claim.rowcount != 1:
            raise ValueError("end_stopped_item_context_changed")
        # The release calls this under the acquisition lock. A previous owner write may have
        # finished while it was waiting, so confirm again before revoking access or editing config.
        if workspace is not None and await github_workspace_service.delegated_release_blocker(scope, workspace, item):
            raise ValueError("workspace_not_clean")

        await end_pending_requests(db, item_id, now=now)
        await db.execute(update(GithubAttemptScopeRevision).where(
            GithubAttemptScopeRevision.work_item_id == item_id,
            GithubAttemptScopeRevision.status.not_in(TERMINAL_REVISIONS))
            .values(status="cancelled", cancelled_at=now, cancellation_reason=destination))
        db.add(GithubMaintenanceEvent(operation_id=operation_id, work_item_id=item_id,
                                      outcome=request.outcome, request_sha256=request_sha))
        await audit.record_event(db, event_kind="stopped_work_item_ended",
            source="github_end_stopped_item_service.end_stopped_item", occurred_at=now,
            actor=actor, scope_id=scope_id, item_id=item_id, action_outcome="applied",
            operation_id=operation_id, sanitized_reason=reason,
            before_values={"dispatch_status": original_status},
            after_values={"dispatch_status": "cancelled", "outcome": request.outcome, **evidence})
        await _assert_not_paused(db, item_id)
        applied = True

    if workspace is None:
        await settle()
        await db.commit()
    else:
        try:
            released = await github_workspace_service.force_release_acquisition(
                db, workspace_id=workspace.id, scope_id=scope_id, item_id=item_id,
                expected_leased_at=workspace.leased_at, lease_token=workspace.lease_token,
                actor_kind=actor["actor_kind"], actor_member_id=actor.get("actor_member_id"),
                actor_session_id=actor.get("actor_session_id"), before_commit=settle)
        except (GithubWorkspaceConfigError, GithubWorkspaceCredentialRevokeError) as exc:
            raise ValueError(exc.block_code) from None
        if not released:
            if await replayed():
                return {"status": "already_ended", **response}
            raise ValueError("end_stopped_item_context_changed")
    return {"status": "ended", "workspace_released": workspace is not None, **response}
