"""#1239: waits on a decision, as append-only start and end events keyed by the request id.

Both events are written in the transaction of the request's status change. Who answered is recorded only when known.
The ledger binds each event to the work item's lifetime, so an id reused by a later item is another wait. A request
row is deleted only with its work item; such a wait has no end and reads as unknown, not open.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import FactoryAuditEvent, FactoryContextKey, GithubApprovalRequest
from app.services import factory_audit_service as audit

STARTED = "decision_wait_started"
ENDED = "decision_wait_ended"


def wait_operation(kind: str, request_id: int) -> str:
    """The operation id of a wait event; #1240, #1299 and #1300 deduplicate on the request id it names."""
    return f"{kind}:request:{request_id}"


async def record_wait_started(db: AsyncSession, request: GithubApprovalRequest, *, source: str) -> None:
    """Who asked, who must answer, and when it was asked."""
    await db.flush()
    operation_id = wait_operation(STARTED, request.id)
    await audit.record_event(
        db, event_kind=STARTED, source=source, occurred_at=datetime.utcnow(),
        fact_source="github_approval_requests", fact_time=request.created_at,
        actor=audit.derive_actor(actor_kind="member", member_id=request.owner_member_id),
        item_id=request.work_item_id, request_id=request.id,
        after_values={"wait_request_id": request.id, "request_kind": request.request_kind,
                      "owner_member_id": request.owner_member_id,
                      "awaited_member_id": request.leader_member_id, "awaited_role": "leader"},
        action_outcome="applied", sanitized_reason=f"{request.request_kind} waits on the leader",
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
    return waits
