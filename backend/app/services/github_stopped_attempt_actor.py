"""Read-only routing for a stopped attempt's next action."""
from __future__ import annotations

from typing import Literal

from app.services.github_dispatch_service import github_dispatch_service
from app.services.work_item_states import needs_attention

_OPERATOR_STOPS = frozenset({"abandoned_by_operator", "owner_observation_pause", "owner_observation_unavailable"})
_OPERATOR_CHECKPOINTS = frozenset({"decision_hold", "ack_hold"})


def _bound_revision(item, revision) -> bool:
    return (revision is not None and revision.work_item_id == item.id
            and revision.dispatch_nonce == item.dispatch_nonce)


def operator_checkpoint_required(item, revision) -> bool:
    return (item.dispatch_status == "escalated" and _bound_revision(item, revision)
            and revision.status in {"proposed", "approved"}
            and revision.recovery_checkpoint_stage in _OPERATOR_CHECKPOINTS)


def stopped_attempt_actor(item, pending, checkpoint, hold: str | None) -> Literal["leader", "operator"] | None:
    """Resolve who acts next while preserving the independent write-route checks."""
    if not needs_attention(item.dispatch_status):
        return None
    if (hold or item.dispatch_status == "failed" or item.escalation_reason in _OPERATOR_STOPS
            or operator_checkpoint_required(item, checkpoint)):
        return "operator"
    if (pending is not None and pending.request_kind == "continuation"
            and pending.dispatch_nonce == item.dispatch_nonce and _bound_revision(item, checkpoint)
            and pending.scope_revision_id == checkpoint.id and checkpoint.status == "proposed"
            and checkpoint.origin_kind == "continuation"):
        return "leader"
    eligible = github_dispatch_service.retry_eligibility(item, pending_approval=pending is not None)
    return "leader" if eligible.allowed else "operator"
