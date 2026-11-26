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

from app.models.database import FactoryAuditEvent, FactoryContextKey, GithubApprovalRequest, MailMessage, MailTeamMember
from app.services import factory_audit_service as audit

STARTED = "decision_wait_started"
ENDED = "decision_wait_ended"
# #1240: a Mail request to a member, keyed by its message id, and an input wait on a pane, keyed by its episode.
MAIL_STARTED, MAIL_ENDED = "mail_wait_started", "mail_wait_ended"
INPUT_STARTED, INPUT_ENDED = "input_wait_started", "input_wait_ended"
# The Mail root of a structured request carries this delivery key; its decision wait already counts it.
STRUCTURED_REQUEST_KEY = "github-approval:"


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


async def _once(db: AsyncSession, operation_id: str) -> bool:
    return (await db.scalar(select(FactoryAuditEvent.id).where(FactoryAuditEvent.operation_id == operation_id)
                            .limit(1))) is not None


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
    db: AsyncSession, *, binding_id: int, started_at: datetime | None, outcome: str, source: str,
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
        fact_time=datetime.utcnow(), team_preset_id=started.team_preset_id, team_slot_id=started.team_slot_id,
        team_context_key=started.team_context_key,
        actor=audit.derive_actor(actor_kind="scheduler", scheduler="agent_activity_service"),
        after_values={"wait_binding_id": binding_id, "wait_outcome": outcome, "answered_by_member_id": None},
        action_outcome="applied", sanitized_reason=f"input wait ended: {outcome}", operation_id=operation_id,
        correlation_id=operation_id)


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
    wait_kind: str = "decision"  # "decision", "mail" (keyed by message id) or "input" (keyed by pane binding id)
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
    for kind, (start_kind, end_kind) in {"mail": (MAIL_STARTED, MAIL_ENDED), "input": (INPUT_STARTED, INPUT_ENDED)}.items():
        query = select(FactoryAuditEvent).where(FactoryAuditEvent.event_kind == start_kind)
        if team_context_key is not None:
            query = query.where(FactoryAuditEvent.team_context_key == team_context_key)
        ended_ids = set((await db.scalars(select(FactoryAuditEvent.operation_id).where(
            FactoryAuditEvent.event_kind == end_kind))).all())
        for start in (await db.scalars(query.order_by(FactoryAuditEvent.id))).all():
            if start.operation_id.replace(start_kind, end_kind, 1) in ended_ids:
                continue
            values, began = start.after_values or {}, start.fact_time or start.occurred_at
            waits.append(DecisionWait(
                request_id=values.get("wait_message_id") or values.get("wait_binding_id"),
                request_kind=values.get("request_kind"), owner_member_id=values.get("owner_member_id"),
                awaited_member_id=values.get("awaited_member_id"), started_at=began,
                age_seconds=(now - began).total_seconds(), state="open", wait_kind=kind,
                awaited_actor_id=values.get("awaited_actor_id")))
    return waits
