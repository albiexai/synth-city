"""Read-only routing for a stopped attempt's next action."""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from app.services.github_coordination_service import external_hold_code
from app.services.github_dispatch_service import github_dispatch_service
from app.services.github_recovery_gate import GithubRecoveryOnlyAttempt, configured_recovery_only_attempt
from app.services.work_item_states import needs_attention

_OPERATOR_STOPS = frozenset({"abandoned_by_operator", "owner_observation_pause", "owner_observation_unavailable"})
_OPERATOR_CHECKPOINTS = frozenset({"decision_hold", "ack_hold"})


class StoppedAttemptStep(Enum):
    PAUSED = ("operator", "paused")
    INSPECT = ("operator", "inspect")
    DECIDE = ("leader", "decide")
    ACKNOWLEDGE = ("owner", "acknowledge")
    RETRY = ("leader", "retry")
    RECONCILE_PLAN = ("leader", "reconcile_plan")

    @property
    def actor(self) -> str:
        return self.value[0]


def stopped_attempt_policy(hold: str | None) -> tuple[str | None, GithubRecoveryOnlyAttempt | None]:
    if hold != "recovery_only":
        return hold, None
    external = external_hold_code()
    if external:
        return external, None
    return hold, configured_recovery_only_attempt()


def _bound_revision(item, revision) -> bool:
    return (revision is not None and revision.work_item_id == item.id
            and revision.dispatch_nonce == item.dispatch_nonce)


def operator_checkpoint_required(item, revision) -> bool:
    return (item.dispatch_status == "escalated" and _bound_revision(item, revision)
            and revision.status in {"proposed", "approved"}
            and revision.recovery_checkpoint_stage in _OPERATOR_CHECKPOINTS)


def _continuation_step(item, pending, revision, now: datetime) -> StoppedAttemptStep | None:
    if (not _bound_revision(item, revision) or revision.origin_kind != "continuation"
            or revision.originating_escalation_reason != item.escalation_reason
            or revision.owner_slot_id != item.owner_slot_id
            or (revision.expires_at is not None and revision.expires_at <= now)):
        return None
    stage = revision.recovery_checkpoint_stage
    if (stage in {None, "decision_open"} and revision.status == "proposed"
            and pending is not None and pending.request_kind == "continuation"
            and pending.dispatch_nonce == item.dispatch_nonce and pending.scope_revision_id == revision.id):
        return StoppedAttemptStep.DECIDE
    if (stage in {None, "ack_open"} and revision.status == "approved" and pending is None
            and revision.delivery_message_id is not None and revision.delivered_at is not None):
        return StoppedAttemptStep.ACKNOWLEDGE
    return None


def stopped_attempt_step(
    item, pending, revision, hold: str | None, recovery_attempt: GithubRecoveryOnlyAttempt | None,
    *, now: datetime,
) -> StoppedAttemptStep | None:
    """Resolve who acts next while preserving the independent write-route checks."""
    if not needs_attention(item.dispatch_status):
        return None
    if hold and (hold != "recovery_only" or recovery_attempt is None or not recovery_attempt.matches_item(item)):
        return StoppedAttemptStep.PAUSED
    if (item.dispatch_status == "failed" or item.escalation_reason in _OPERATOR_STOPS
            or operator_checkpoint_required(item, revision)):
        return StoppedAttemptStep.PAUSED if hold else StoppedAttemptStep.INSPECT
    continuation = _continuation_step(item, pending, revision, now)
    if hold == "recovery_only":
        if continuation is not None and revision.recovery_checkpoint_stage in {"decision_open", "ack_open"}:
            return continuation
        return StoppedAttemptStep.PAUSED
    if continuation is not None:
        return continuation
    if pending is not None and pending.dispatch_nonce != item.dispatch_nonce:
        pending = None
    if (item.escalation_reason == "plan_blocked" and pending is None and item.active_scope_revision == 0
            and not _bound_revision(item, revision)):
        return StoppedAttemptStep.RECONCILE_PLAN
    eligible = github_dispatch_service.retry_eligibility(item, pending_approval=pending is not None)
    return StoppedAttemptStep.RETRY if eligible.allowed else StoppedAttemptStep.INSPECT
