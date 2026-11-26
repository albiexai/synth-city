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
    # The fixture's extension clock (T0) predates the real Mail time of the notice, so the end is clamped to it.
    assert end.fact_time == max(T0, start.fact_time) and end.actor_member_id == fx.leader


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
    seen = (await _leader_notice(fx))[0].created_at + timedelta(minutes=3)  # observed after the notice: kept
    await _tick(fx, seen)
    await _tick(fx, seen + timedelta(minutes=1))  # once
    _start, end = await _effort_events(fx)
    assert end.after_values["wait_outcome"] == "item_ended" and end.after_values["answered_by_member_id"] is None
    assert end.fact_time == seen


async def test_an_escalated_item_keeps_its_wait_open(fx):
    await _at_limit(fx)
    await _set(fx, dispatch_status="escalated")
    await _tick(fx, T0 + timedelta(minutes=3))
    assert [event.event_kind for event in await _effort_events(fx)] == [waits.EFFORT_STARTED]


async def _interrupted_at_limit(fx, monkeypatch):
    """V1 on 37eeada9: the Leader's notice is durable but the wait start was never written."""
    from app.services import github_effort_limit_service as effort

    async def interrupted(*_args, **_kwargs):
        raise RuntimeError("interrupted after the notice")

    with monkeypatch.context() as patched:
        patched.setattr(effort.decision_waits, "record_effort_wait_started", interrupted)
        with pytest.raises(RuntimeError):
            await _at_limit(fx)
    assert len(await _leader_notice(fx)) == 1 and await _effort_events(fx) == []


async def test_an_extension_after_an_interrupted_start_records_the_whole_episode(fx, monkeypatch):
    await _interrupted_at_limit(fx, monkeypatch)
    await _extend(fx, 3)  # also the concurrent case: the extension sees the notice before the start
    (notice,) = await _leader_notice(fx)
    start, end = await _effort_events(fx)
    assert (start.after_values["wait_message_id"], start.fact_time) == (notice.id, notice.created_at)
    assert (end.after_values["wait_outcome"], end.after_values["answered_by_member_id"], end.fact_time) == (
        "extended", fx.leader, max(T0, start.fact_time))
    async with fx.maker() as session:  # the interrupted writer finishing late adds nothing
        from app.services import github_effort_limit_service as effort
        await effort.effort_limit_service._record_effort_waits(session, fx.item_id, now=T0 + timedelta(minutes=9))
        await session.commit()
        assert await waits.unended_decision_waits(session, now=T0 + timedelta(hours=1)) == []
    assert len(await _effort_events(fx)) == 2


async def test_an_item_end_after_an_interrupted_start_records_the_whole_episode(fx, monkeypatch):
    await _interrupted_at_limit(fx, monkeypatch)
    await _set(fx, dispatch_status="merged")
    await _tick(fx, T0 + timedelta(minutes=3))
    start, end = await _effort_events(fx)
    assert start.event_kind == waits.EFFORT_STARTED and end.after_values["wait_outcome"] == "item_ended"


@pytest.mark.parametrize("found_by", ["a later extension", "reconciliation"])
async def test_a_notice_the_extension_did_not_answer_ends_unattributed(fx, monkeypatch, found_by):
    from app.services import github_effort_limit_service as effort
    await _interrupted_at_limit(fx, monkeypatch)

    async def unseen(*_args, **_kwargs):  # the first extension did not see the notice: it was committed later
        return None

    with monkeypatch.context() as patched:
        patched.setattr(effort.effort_limit_service, "_record_effort_waits", unseen)
        await _extend(fx, 3)
    if found_by == "a later extension":
        await _extend(fx, 3, expected=3)
    else:  # the next limit's notice finds it; it ends at the extension's time and the new wait stays open
        await _set(fx, effort_elapsed_seconds=18 * 60 - 1, effort_last_tick_at=T0 + timedelta(minutes=5))
        await _tick(fx, T0 + timedelta(minutes=5, seconds=2))
    events = await _effort_events(fx)
    end = next(e for e in events if e.event_kind == waits.EFFORT_ENDED)
    start = next(e for e in events if e.event_kind == waits.EFFORT_STARTED
                 and e.after_values["wait_message_id"] == end.after_values["wait_message_id"])
    assert (end.after_values["wait_outcome"], end.after_values["answered_by_member_id"]) == ("extended", None)
    # V1 (25370): the extension came before this notice, so the episode is zero-length, never reversed.
    assert (end.actor_member_id, end.fact_time) == (None, start.fact_time)
    if found_by == "reconciliation":
        later = (await _leader_notice(fx))[-1]
        async with fx.maker() as session:
            open_waits = await waits.unended_decision_waits(session, now=T0 + timedelta(hours=1))
        assert [(w.wait_kind, w.request_id) for w in open_waits] == [("effort", later.id)]


async def test_another_attempts_notice_is_not_ended_by_this_attempts_extension(fx):
    from app.services.github_effort_limit_service import effort_limit_service
    await _at_limit(fx)
    await _set(fx, dispatch_nonce="the-next-attempt", effort_extension_minutes=3)
    async with fx.maker() as session:
        await effort_limit_service._record_effort_waits(session, fx.item_id, now=T0 + timedelta(minutes=1))
        await session.commit()
    assert [e.event_kind for e in await _effort_events(fx)] == [waits.EFFORT_STARTED]


async def test_a_redispatch_before_recovery_keeps_the_episode_to_the_items_end(fx, monkeypatch):
    """V1 review 6068009562 (1): the earlier attempt's notice is recovered within the item lifetime."""
    await _interrupted_at_limit(fx, monkeypatch)
    await _set(fx, dispatch_nonce="the-next-attempt", effort_extension_minutes=3)
    await _set(fx, dispatch_status="merged")
    await _tick(fx, T0 + timedelta(minutes=3))
    start, end = await _effort_events(fx)
    assert start.after_values["wait_message_id"] == (await _leader_notice(fx))[0].id
    assert (end.after_values["wait_outcome"], end.after_values["answered_by_member_id"]) == ("item_ended", None)


@pytest.mark.parametrize("owner_notice", [True, False])
async def test_a_recovered_start_names_the_owner_notified_at_the_time(fx, monkeypatch, owner_notice):
    """V1 review 6068009562 (2): never the current assignment; unknown without the owner's notice."""
    from app.services.github_effort_limit_service import effort_limit_service
    await _interrupted_at_limit(fx, monkeypatch)
    async with fx.maker() as session:
        if not owner_notice:
            await session.execute(text("UPDATE mail_messages SET delivery_key = NULL WHERE delivery_key LIKE :key"),
                                  {"key": "github-effort:%:owner"})
        await session.execute(text("UPDATE github_work_items SET owner_slot_id = NULL WHERE id = :id"),
                              {"id": fx.item_id})
        await session.commit()
        await effort_limit_service._record_effort_waits(session, fx.item_id, now=T0 + timedelta(minutes=1))
        await session.commit()
    (start,) = await _effort_events(fx)
    assert start.after_values["owner_member_id"] == (fx.owner if owner_notice else None)


async def test_a_reused_mail_id_still_starts_and_reads_another_items_wait(fx):
    """V1 review 6068009562 (3): the once-guards and the open-wait reader are bound to the item lifetime."""
    async with fx.maker() as session:
        other = GithubWorkItem(scope_id=fx.scope_id, issue_number=94, issue_title="other", issue_url="u",
                               github_updated_at=T0, dispatch_status="dispatched")
        session.add(other)
        await session.flush()
        from app.services import factory_audit_service as audit
        await audit.context_key_for(session, "item", other.id)  # a working item already has its lifetime
        for item_id in (fx.item_id, other.id):  # SQLite gave the second notice the first one's freed id
            await waits.record_effort_wait_started(
                session, item_id=item_id, notice_id=7, noticed_at=T0, owner_member_id=None,
                leader_member_id=fx.leader, source="test")
        await waits.record_effort_wait_ended(session, item_id=fx.item_id, outcome="extended", ended_at=T0,
                                             source="test")
        await waits.record_effort_wait_ended(session, item_id=fx.item_id, outcome="extended", ended_at=T0,
                                             source="test")  # a replay in the same lifetime adds nothing
        await session.commit()
        assert [e.item_id for e in await _events(session, (waits.EFFORT_STARTED, waits.EFFORT_ENDED))] == [
            fx.item_id, other.id, fx.item_id]
        (wait,) = await waits.unended_decision_waits(session, now=T0 + timedelta(hours=1))
        assert (wait.wait_kind, wait.request_id, wait.state) == ("effort", 7, "open")
        await waits.record_effort_wait_ended(session, item_id=other.id, outcome="item_ended", ended_at=T0,
                                             source="test")
        await session.commit()
        assert len(await _events(session, (waits.EFFORT_ENDED,))) == 2
        assert await waits.unended_decision_waits(session, now=T0 + timedelta(hours=1)) == []


async def test_rebuilding_after_an_owner_change_keeps_the_recorded_start(fx):
    from app.services.github_effort_limit_service import effort_limit_service
    await _at_limit(fx)
    await _set(fx, owner_slot_id=None)
    async with fx.maker() as session:
        await effort_limit_service._record_effort_waits(session, fx.item_id, now=T0 + timedelta(minutes=1))
        await session.commit()
    (start,) = await _effort_events(fx)
    assert start.after_values["owner_member_id"] == fx.owner


async def test_the_next_tick_at_the_limit_writes_an_interrupted_start(fx, monkeypatch):
    await _interrupted_at_limit(fx, monkeypatch)
    await _tick(fx, T0 + timedelta(seconds=62))
    (start,) = await _effort_events(fx)
    assert start.after_values["wait_message_id"] == (await _leader_notice(fx))[0].id


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
        from app.services.github_effort_limit_service import effort_limit_service
        await effort_limit_service._record_effort_waits(session, fx.item_id, now=T0)  # the old notice is not its own
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

