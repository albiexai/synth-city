"""#1240: Mail requests and input waits are waits on someone, next to the decision waits of #1239."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.database import FactoryAuditEvent, MailExternalActor, MailMessage
from app.models.schemas import ExternalAgentMailContextRequest, MailMessageCreate
from app.services import decision_wait_service as waits
from app.services.agent_mail_service import agent_mail_service
from app.services.external_agent_mail_service import external_agent_mail_service
from app.services.github_approval_service import github_approval_service
from tests.agent_mail.test_api import _dispatch_approval_fixture
from tests.agent_teams.test_native_input_wait import record_wait, wait_team

pytestmark = pytest.mark.asyncio
KINDS = (waits.MAIL_STARTED, waits.MAIL_ENDED, waits.INPUT_STARTED, waits.INPUT_ENDED)


async def _events(db, *kinds):
    return (await db.scalars(select(FactoryAuditEvent).where(FactoryAuditEvent.event_kind.in_(kinds or KINDS))
                             .order_by(FactoryAuditEvent.id))).all()


async def _ask(db, members, kind="context_request"):
    message = await agent_mail_service.send_message(db, MailMessageCreate(
        kind=kind, sender_member_id=members[1].id, recipient_member_id=members[0].id, body_markdown="Which base?"),
        authenticated_sender_member_id=members[1].id, auto_nudge=False)
    return await db.get(MailMessage, message.id)


async def _open(db, kind):
    return [w for w in await waits.unended_decision_waits(db, now=datetime.utcnow()) if w.wait_kind == kind]


# Mail requests
@pytest.mark.parametrize("kind", ["context_request", "handoff"])
async def test_an_unanswered_mail_request_is_one_open_wait_on_its_recipient(db, kind):
    _item, members, _tokens = await _dispatch_approval_fixture(db)
    root = await _ask(db, members, kind)
    (start,) = await _events(db)
    assert start.event_kind == waits.MAIL_STARTED and start.operation_id == f"mail_wait_started:message:{root.id}"
    assert start.after_values == {"wait_message_id": root.id, "request_kind": kind, "owner_member_id": members[1].id,
                                  "owner_actor_id": None, "awaited_member_id": members[0].id}
    assert start.fact_time == root.created_at
    (wait,) = await waits.unended_decision_waits(db, now=root.created_at + timedelta(minutes=90))
    assert (wait.wait_kind, wait.request_id, wait.state, wait.age_seconds) == ("mail", root.id, "open", 5400.0)
    assert (wait.owner_member_id, wait.awaited_member_id, wait.awaited_actor_id) == (members[1].id, members[0].id, None)


async def test_an_answer_ends_the_mail_wait_answered_by_the_recipient(db):
    _item, members, _tokens = await _dispatch_approval_fixture(db)
    root = await _ask(db, members)
    await agent_mail_service.send_message(db, MailMessageCreate(
        kind="answer", sender_member_id=members[0].id, thread_root_id=root.id, body_markdown="master"),
        authenticated_sender_member_id=members[0].id, auto_nudge=False)
    await agent_mail_service.ack_message(db, root.id, members[1].id)  # answered, then acknowledged: one end
    start, end = await _events(db)
    assert end.event_kind == waits.MAIL_ENDED and end.actor_member_id == members[0].id
    assert end.after_values == {"wait_message_id": root.id, "wait_outcome": "answered",
                                "answered_by_member_id": members[0].id}
    assert await _open(db, "mail") == []


async def test_a_mail_wait_ends_once_whatever_ends_it_later(db):
    _item, members, _tokens = await _dispatch_approval_fixture(db)
    root = await _ask(db, members)
    await waits.record_mail_wait_ended(db, root.id, outcome="answered", answered_by_member_id=members[0].id, source="t")
    await waits.record_mail_wait_ended(db, root.id, outcome="acknowledged", source="t")
    await db.commit()
    assert [e.after_values["wait_outcome"] for e in await _events(db, waits.MAIL_ENDED)] == ["answered"]


async def test_a_handoff_ack_ends_its_wait(db):
    _item, members, _tokens = await _dispatch_approval_fixture(db)
    root = await _ask(db, members, "handoff")
    await agent_mail_service.ack_message(db, root.id, members[0].id)
    (_start, end) = await _events(db)
    assert end.after_values == {"wait_message_id": root.id, "wait_outcome": "acknowledged",
                                "answered_by_member_id": members[0].id}


async def test_an_external_actors_request_is_asked_by_that_actor_and_its_acknowledgement_is_unattributed(db):
    _item, members, _tokens = await _dispatch_approval_fixture(db)
    actor = MailExternalActor(actor_key="retro-supervisor", display_name="Supervisor", kind="external_tool",
                              token_hash="synthetic-hash")
    db.add(actor)
    await db.commit()
    sent = await external_agent_mail_service.send_context_request(db, actor, ExternalAgentMailContextRequest(
        recipient_member_id=members[0].id, body_markdown="Status?"))
    (start,) = await _events(db)
    assert (start.after_values["owner_member_id"], start.after_values["owner_actor_id"]) == (None, actor.id)
    await external_agent_mail_service.acknowledge_actor_request(db, actor, sent.message.id)
    (_start, end) = await _events(db)
    assert end.after_values == {"wait_message_id": sent.message.id, "wait_outcome": "acknowledged",
                                "answered_by_member_id": None}


async def test_a_structured_requests_mail_root_is_not_a_second_wait(db):
    item, members, _tokens = await _dispatch_approval_fixture(db)
    request, _created = await github_approval_service.create_initial_request(
        db, item, authenticated_owner_member_id=members[1].id, summary="bounded plan")
    await agent_mail_service.send_message(db, MailMessageCreate(
        kind="context_request", sender_member_id=members[1].id, recipient_member_id=members[0].id,
        body_markdown="plan", payload={"approval_request_id": request.id}),
        authenticated_sender_member_id=members[1].id, auto_nudge=False,
        delivery_key=f"github-approval:{request.id}:request")
    assert await _events(db) == []
    (wait,) = await waits.unended_decision_waits(db, now=datetime.utcnow())
    assert (wait.wait_kind, wait.request_id) == ("decision", request.id)


async def test_a_plain_message_is_not_a_wait(db):
    _item, members, _tokens = await _dispatch_approval_fixture(db)
    await _ask(db, members, "message")
    assert await _events(db) == []


async def test_a_rolled_back_mail_request_leaves_no_wait(db):
    _item, members, _tokens = await _dispatch_approval_fixture(db)
    await agent_mail_service._send_message(db, MailMessageCreate(
        kind="context_request", sender_member_id=members[1].id, recipient_member_id=members[0].id,
        body_markdown="q"), authenticated_sender_member_id=members[1].id, commit=False)
    assert len(await _events(db)) == 1  # written in the message's own transaction
    await db.rollback()
    assert await _events(db) == []


# Input waits
@pytest.mark.parametrize("supervised", [True, False])
async def test_a_question_on_screen_is_one_wait_on_the_supervisor_or_the_leader(db, supervised):
    team, actor, slot, binding, session, leader = await wait_team(db, supervised=supervised)
    now = datetime.now(timezone.utc)
    await record_wait(db, team, slot, binding, "question", now=now)
    await record_wait(db, team, slot, binding, "trust", now=now + timedelta(seconds=30))  # the same episode
    (start,) = await _events(db)
    assert start.event_kind == waits.INPUT_STARTED and start.team_slot_id == slot.id
    assert start.after_values["owner_member_id"] == session.member_id
    assert (start.after_values["awaited_actor_id"], start.after_values["awaited_member_id"]) == (
        (actor.id, None) if supervised else (None, leader.id))
    (wait,) = await _open(db, "input")
    assert (wait.awaited_actor_id, wait.awaited_member_id) == ((actor.id, None) if supervised else (None, leader.id))


async def test_a_cleared_screen_ends_the_input_wait_unattributed(db):
    team, _actor, slot, binding, _session, _leader = await wait_team(db)
    await record_wait(db, team, slot, binding, "question")
    await record_wait(db, team, slot, binding, "clear")
    await record_wait(db, team, slot, binding, "clear")
    (_start, end) = await _events(db)
    assert end.after_values["wait_outcome"] == "cleared" and end.after_values["answered_by_member_id"] is None
    assert await _open(db, "input") == []
    await record_wait(db, team, slot, binding, "sign_in")  # a new episode is a new wait
    assert len(await _events(db, waits.INPUT_STARTED)) == 2


async def test_a_provider_refusal_is_no_input_wait_and_ends_one(db):
    team, _actor, slot, binding, _session, _leader = await wait_team(db)
    await record_wait(db, team, slot, binding, "provider_refusal")
    assert await _events(db) == []
    await record_wait(db, team, slot, binding, "clear")
    await record_wait(db, team, slot, binding, "question")
    await record_wait(db, team, slot, binding, "provider_refusal")
    (_start, end) = await _events(db)
    assert end.after_values["wait_outcome"] == "refused"
    assert await _open(db, "input") == []
