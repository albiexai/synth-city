"""#1299 (part 2 of #1239): decision waits for effort extensions and scope amendments."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, text

from app.models.database import FactoryAuditEvent, GithubWorkItem, MailMessage
from app.models.schemas import MailMessageCreate
from app.services import decision_wait_service as waits
from app.services.agent_mail_service import agent_mail_service
from app.services.github_approval_service import github_approval_service
from tests.agent_teams.test_effort_limit import T0, _extend, _limited, _set, _tick, fx  # noqa: F401
from tests.agent_teams.test_scope_amendments import amendment, db, healthy_attempt  # noqa: F401

pytestmark = pytest.mark.asyncio
EFFORT = (waits.EFFORT_STARTED, waits.EFFORT_ENDED)


async def _events(session, kinds, request_id=None):
    query = select(FactoryAuditEvent).where(FactoryAuditEvent.event_kind.in_(kinds))
    if request_id is not None:  # the attempt's own approved plan has its part 1 wait too
        query = query.where(FactoryAuditEvent.request_id == request_id)
    return (await session.scalars(query.order_by(FactoryAuditEvent.id))).all()


async def _effort_events(fx):
    async with fx.maker() as session:
        return await _events(session, EFFORT)


async def _at_limit(fx):
    await _limited(fx, limit=15, effort_elapsed_seconds=899, effort_last_tick_at=T0)
    await _tick(fx, T0 + timedelta(seconds=2))


async def _leader_notice(fx):
    async with fx.maker() as session:
        rows = (await session.scalars(select(MailMessage).where(MailMessage.recipient_member_id == fx.leader))).all()
        return [row for row in rows if (row.payload or {}).get("kind") == "github_effort_limit_reached"]


# Effort extensions
async def test_the_limit_notice_starts_one_wait_on_the_leader_keyed_by_its_message_id(fx):
    await _at_limit(fx)
    await _tick(fx, T0 + timedelta(seconds=62))  # still over the limit at the same total
    (notice,) = await _leader_notice(fx)
    (start,) = await _effort_events(fx)
    assert start.event_kind == waits.EFFORT_STARTED and start.operation_id == f"effort_wait_started:message:{notice.id}"
    assert start.item_id == fx.item_id and start.fact_time == notice.created_at
    assert start.after_values == {"wait_message_id": notice.id, "request_kind": "effort_extension",
                                  "owner_member_id": fx.owner, "awaited_member_id": fx.leader,
                                  "awaited_role": "leader"}


async def test_an_extension_ends_the_wait_answered_by_the_leader(fx):
    await _at_limit(fx)
    await _extend(fx, 3)
    start, end = await _effort_events(fx)
    assert end.event_kind == waits.EFFORT_ENDED and end.item_context_key == start.item_context_key
    assert end.after_values == {"wait_message_id": start.after_values["wait_message_id"], "wait_outcome": "extended",
                                "answered_by_member_id": fx.leader}
    assert end.fact_time == T0 and end.actor_member_id == fx.leader


async def test_a_later_limit_after_an_extension_is_a_new_wait(fx):
    await _at_limit(fx)
    await _extend(fx, 3)
    await _set(fx, effort_elapsed_seconds=18 * 60 - 1, effort_last_tick_at=T0 + timedelta(minutes=5))
    await _tick(fx, T0 + timedelta(minutes=5, seconds=2))
    kinds = [event.event_kind for event in await _effort_events(fx)]
    assert kinds == [waits.EFFORT_STARTED, waits.EFFORT_ENDED, waits.EFFORT_STARTED]


@pytest.mark.parametrize("status", ["merged", "cancelled"])
async def test_an_ended_item_ends_its_wait_unattributed(fx, status):
    await _at_limit(fx)
    await _set(fx, dispatch_status=status)
    await _tick(fx, T0 + timedelta(minutes=3))
    await _tick(fx, T0 + timedelta(minutes=4))  # once
    _start, end = await _effort_events(fx)
    assert end.after_values["wait_outcome"] == "item_ended" and end.after_values["answered_by_member_id"] is None
    assert end.fact_time == T0 + timedelta(minutes=3)


async def test_an_escalated_item_keeps_its_wait_open(fx):
    await _at_limit(fx)
    await _set(fx, dispatch_status="escalated")
    await _tick(fx, T0 + timedelta(minutes=3))
    assert [event.event_kind for event in await _effort_events(fx)] == [waits.EFFORT_STARTED]


async def test_an_unanswered_effort_wait_stays_open_with_its_age(fx):
    await _at_limit(fx)
    (notice,) = await _leader_notice(fx)
    async with fx.maker() as session:
        (wait,) = await waits.unended_decision_waits(session, now=notice.created_at + timedelta(minutes=40))
    assert (wait.wait_kind, wait.request_id, wait.state, wait.age_seconds) == ("effort", notice.id, "open", 2400.0)
    assert wait.awaited_member_id == fx.leader


async def test_a_deleted_items_effort_wait_is_unknown(fx):
    await _at_limit(fx)
    async with fx.maker() as session:
        await session.execute(text("DELETE FROM github_work_items WHERE id = :id"), {"id": fx.item_id})
        await session.commit()
        (wait,) = await waits.unended_decision_waits(session, now=T0 + timedelta(hours=1))
    assert (wait.wait_kind, wait.state, wait.reason) == ("effort", "unknown", "work item deleted")


async def test_ending_a_reused_items_wait_leaves_the_deleted_items_wait_unknown(fx):
    from app.services import factory_audit_service as audit
    await _at_limit(fx)
    async with fx.maker() as session:
        await session.execute(text("DELETE FROM github_approval_requests"))
        await session.execute(text("DELETE FROM github_work_items WHERE id = :id"), {"id": fx.item_id})
        await session.commit()
        session.add(GithubWorkItem(id=fx.item_id, scope_id=fx.scope_id, issue_number=93, issue_title="again",
                                   issue_url="u", github_updated_at=T0, dispatch_status="dispatched"))
        await session.flush()
        await audit.context_key_for(session, "item", fx.item_id)  # the new item's own lifetime
        await waits.record_effort_wait_ended(session, item_id=fx.item_id, outcome="extended", ended_at=T0,
                                             answered_by_member_id=fx.leader, source="test")
        await session.commit()
        assert await _events(session, (waits.EFFORT_ENDED,)) == []
        (wait,) = await waits.unended_decision_waits(session, now=T0 + timedelta(hours=1))
    assert (wait.wait_kind, wait.state) == ("effort", "unknown")


async def test_the_effort_wait_and_the_owners_mail_request_are_both_recorded(fx):
    """Supervisor, 2026-11-26 (02:10Z): nothing is suppressed; #1245 counts the overlapping time once."""
    await _at_limit(fx)
    async with fx.maker() as session:
        sent = await agent_mail_service.send_message(session, MailMessageCreate(
            kind="context_request", sender_member_id=fx.owner, recipient_member_id=fx.leader,
            body_markdown="May I have more time?", payload={"work_item_id": fx.item_id, "dispatch_nonce": fx.nonce}),
            authenticated_sender_member_id=fx.owner, auto_nudge=False)
    await _extend(fx, 3)
    async with fx.maker() as session:
        effort = await _events(session, EFFORT)
        mail = await _events(session, (waits.MAIL_STARTED,))
        waiting = await waits.unended_decision_waits(session, now=T0 + timedelta(hours=1))
    assert [e.after_values["wait_message_id"] for e in mail] == [sent.id]
    assert effort[0].after_values["wait_message_id"] != sent.id
    assert {e.after_values["awaited_member_id"] for e in (effort[0], *mail)} == {fx.leader}
    assert effort[0].fact_time is not None and mail[0].fact_time is not None
    assert [(w.wait_kind, w.request_id) for w in waiting] == [("mail", sent.id)]  # still unanswered by Mail


# Scope amendments
@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_a_scope_amendment_is_one_wait_ended_by_the_leaders_decision(db, healthy_attempt, decision):
    a = healthy_attempt
    _revision, approval, _ = await github_approval_service.create_scope_amendment_request(
        db, a.item, a.scope, **amendment(a))
    await github_approval_service.ensure_continuation_request_message(db, a.item, approval, _revision)
    for _ in range(2):
        await github_approval_service.decide_continuation(db, a.item, authenticated_leader_member_id=a.leader.id,
            decision=decision, reason="Reviewed amendment", request_id=approval.id)
    start, end = await _events(db, (waits.STARTED, waits.ENDED), approval.id)
    await db.refresh(approval)
    assert (start.request_id, start.after_values["request_kind"]) == (approval.id, "scope_amendment")
    assert start.after_values["awaited_member_id"] == a.leader.id and start.fact_time == approval.created_at
    assert end.after_values == {"wait_request_id": approval.id, "wait_outcome": decision,
                                "answered_by_member_id": a.leader.id}
    assert end.fact_time == approval.decided_at
    assert await _events(db, (waits.MAIL_STARTED,)) == []  # its Mail root is the structured request's


async def test_an_expired_scope_amendment_ends_its_wait_unattributed(db, healthy_attempt):
    from datetime import datetime
    a = healthy_attempt
    revision, approval, _ = await github_approval_service.create_scope_amendment_request(
        db, a.item, a.scope, **amendment(a))
    revision.expires_at = datetime.utcnow() - timedelta(seconds=1)
    await db.commit()
    assert await github_approval_service.expire_continuation_if_needed(db, approval, revision) == "expired_pending"
    _start, end = await _events(db, (waits.STARTED, waits.ENDED), approval.id)
    assert end.after_values["wait_outcome"] == "expired" and end.after_values["answered_by_member_id"] is None


async def test_a_rolled_back_amendment_leaves_no_wait(db, healthy_attempt, monkeypatch):
    a = healthy_attempt

    async def refuse(*_args, **_kwargs):
        raise RuntimeError("context changed")

    monkeypatch.setattr(github_approval_service, "claim_healthy_scope_context", refuse)
    with pytest.raises(RuntimeError):
        await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **amendment(a))
    await db.rollback()
    assert [e.after_values["request_kind"] for e in await _events(db, (waits.STARTED,))] == ["initial_plan"] * len(
        await _events(db, (waits.STARTED,)))

