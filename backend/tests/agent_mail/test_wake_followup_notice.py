"""Notify the Leader and the Supervisor after a re-wake that also got no activity (#1060).

Same evidence rule as the re-wake (#1059): a notice needs positive evidence of no activity, which is no
Mail read, ack or sent message since the re-wake and a known native idle state from before it. Unknown
stays unknown.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base
from app.models.database import (
    AgentTeamPreset, AgentTeamSlot, GithubWorkItem, MailMessage, MailReceipt, MailWakeAttempt,
)
from app.services.agent_mail_service import agent_mail_service
from app.services.agent_status_service import agent_status_service
from app.services.agent_team_supervisor_service import (
    is_supervisor_message, require_supervisor_thread, supervisor_inbox,
)
from app.services.wake_followup_service import wake_followup_service

NOW = datetime(2026, 11, 17, 12, 0, 0)
REWAKE_AT = NOW - timedelta(seconds=300)
SUPERVISOR_ID = 6


def aware(value):
    return value.replace(tzinfo=timezone.utc)


def idle(since):
    return SimpleNamespace(activity_state="idle", idle_since=since)


async def team(db, *, supervisor=SUPERVISOR_ID, work_item=True):
    """A preset with a Leader slot and a builder slot, and optionally the builder's active work item."""
    preset = AgentTeamPreset(name="v4.3", supervisor_actor_id=supervisor)
    db.add(preset)
    await db.commit()
    slots = {}
    for name in ("leader", "builder"):
        slot = AgentTeamSlot(preset_id=preset.id, display_name=f"{name} slot", provider="claude-code",
                             repo_id=f"r-{name}", repo_name=name, repo_path=f"/tmp/{name}")
        db.add(slot)
        await db.commit()
        slots[name] = slot
    preset.leader_slot_id = slots["leader"].id
    await db.commit()
    members = {name: await agent_mail_service.get_or_create_slot_member(db, slot) for name, slot in slots.items()}
    if work_item:
        db.add(GithubWorkItem(issue_number=1060, issue_title="Notify after a re-wake", scope_id=1,
                              issue_url="https://github.com/albiexai/synth-city/issues/1060", github_updated_at=NOW,
                              dispatch_status="dispatched", owner_slot_id=slots["builder"].id))
        await db.commit()
    return SimpleNamespace(preset=preset, slots=slots, members=members, builder=members["builder"],
                           leader=members["leader"])


async def delivered_rewake(db, member_id, *, original=7, at=REWAKE_AT, result="delivered"):
    row = MailWakeAttempt(member_id=member_id, actor_type="server", source="rewake",
                          reason_code=f"rewake:{original}", correlation_id=uuid4().hex,
                          unread_count=1, pending_count=0, result=result, created_at=at)
    db.add(row)
    await db.commit()
    return row


async def notices(db):
    return list((await db.scalars(select(MailMessage).where(
        MailMessage.delivery_key.like("rewake-notice:%")).order_by(MailMessage.id))).all())


@pytest.fixture
def native(monkeypatch):
    states = {}

    async def fake_for_members(db, members, *, activities=None):
        return {member.id: states[member.id] for member in members if member.id in states}

    monkeypatch.setattr(agent_status_service, "for_members", fake_for_members)
    return states


@pytest.fixture
def no_wakes(monkeypatch):
    """The notice must never wake anybody. A wake would start another follow-up and could chain."""
    nudge = AsyncMock(return_value=[])
    wake = AsyncMock()
    monkeypatch.setattr(agent_mail_service, "auto_nudge_members", nudge)
    monkeypatch.setattr(agent_mail_service, "_wake_member", wake)
    return SimpleNamespace(nudge=nudge, wake=wake)


@pytest.mark.asyncio
async def test_a_silent_rewake_notifies_the_leader_and_the_supervisor_once_each(db, native, no_wakes):
    t = await team(db)
    await delivered_rewake(db, t.builder.id)
    native[t.builder.id] = idle(aware(REWAKE_AT - timedelta(minutes=10)))

    await wake_followup_service.tick(db, NOW)

    found = await notices(db)
    assert sorted(m.delivery_key for m in found) == ["rewake-notice:7:leader", "rewake-notice:7:supervisor"]
    leader = next(m for m in found if m.delivery_key.endswith(":leader"))
    supervisor = next(m for m in found if m.delivery_key.endswith(":supervisor"))
    assert leader.recipient_member_id == t.leader.id
    assert (supervisor.audience_type, supervisor.audience_id) == ("team_preset", str(t.preset.id))
    assert supervisor.payload["supervisor_actor_id"] == SUPERVISOR_ID
    for message in (leader, supervisor):
        assert t.builder.display_name in message.body_markdown
        assert "#1060" in message.body_markdown and "Notify after a re-wake" in message.body_markdown
        assert message.sender_type == "system"


@pytest.mark.asyncio
async def test_the_notice_wakes_nobody(db, native, no_wakes):
    t = await team(db)
    await delivered_rewake(db, t.builder.id)
    native[t.builder.id] = idle(aware(REWAKE_AT - timedelta(minutes=10)))
    before = len(list((await db.scalars(select(MailWakeAttempt))).all()))

    await wake_followup_service.tick(db, NOW)

    assert len(await notices(db)) == 2
    no_wakes.nudge.assert_not_called()
    no_wakes.wake.assert_not_called()
    assert len(list((await db.scalars(select(MailWakeAttempt))).all())) == before


@pytest.mark.asyncio
async def test_a_second_tick_adds_no_second_notice(db, native, no_wakes):
    t = await team(db)
    await delivered_rewake(db, t.builder.id)
    native[t.builder.id] = idle(aware(REWAKE_AT - timedelta(minutes=10)))

    await wake_followup_service.tick(db, NOW)
    await wake_followup_service.tick(db, NOW + timedelta(seconds=30))

    assert len(await notices(db)) == 2


@pytest.mark.asyncio
async def test_two_concurrent_ticks_send_one_notice_each(tmp_path, monkeypatch, no_wakes):
    """Two repository jobs sweep at once. The unique delivery key and the sweep lock keep it to one each."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/mail.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as setup:
        t = await team(setup)
        await delivered_rewake(setup, t.builder.id)
        builder_id = t.builder.id

    async def slow_for_members(db, members, *, activities=None):
        await asyncio.sleep(0.05)
        return {m.id: idle(aware(REWAKE_AT - timedelta(minutes=10))) for m in members}

    monkeypatch.setattr(agent_status_service, "for_members", slow_for_members)

    async def sweep():
        async with maker() as session:
            await wake_followup_service.tick(session, NOW)

    await asyncio.gather(sweep(), sweep())
    async with maker() as check:
        keys = sorted(m.delivery_key for m in await notices(check))
    await engine.dispose()

    assert builder_id and keys == ["rewake-notice:7:leader", "rewake-notice:7:supervisor"]


@pytest.mark.asyncio
@pytest.mark.parametrize("activity", ["read", "acked", "sent"])
async def test_mail_activity_after_the_rewake_means_no_notice(db, native, no_wakes, activity):
    t = await team(db)
    await delivered_rewake(db, t.builder.id)
    later = REWAKE_AT + timedelta(seconds=30)
    if activity == "sent":
        db.add(MailMessage(body_markdown="Plan question.", sender_member_id=t.builder.id, created_at=later))
    else:
        message = MailMessage(body_markdown="Decision.", created_at=REWAKE_AT - timedelta(seconds=5))
        db.add(message)
        await db.flush()
        db.add(MailReceipt(message_id=message.id, member_id=t.builder.id,
                           **{"read_at" if activity == "read" else "acked_at": later}))
    await db.commit()
    native[t.builder.id] = idle(aware(REWAKE_AT - timedelta(minutes=10)))

    await wake_followup_service.tick(db, NOW)

    assert await notices(db) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [
    SimpleNamespace(activity_state="working", idle_since=None),
    SimpleNamespace(activity_state="idle", idle_since=aware(REWAKE_AT + timedelta(seconds=90))),
    SimpleNamespace(activity_state="unknown", idle_since=None),
    SimpleNamespace(activity_state="stopped", idle_since=None),
    SimpleNamespace(activity_state="idle", idle_since=None),
    None,
])
async def test_native_evidence_that_is_not_known_idle_before_the_rewake_sends_no_notice(
        db, native, no_wakes, status):
    t = await team(db)
    await delivered_rewake(db, t.builder.id)
    if status is not None:
        native[t.builder.id] = status

    await wake_followup_service.tick(db, NOW)

    assert await notices(db) == []


@pytest.mark.asyncio
async def test_an_unknown_state_that_becomes_known_idle_gets_one_notice_then(db, native, no_wakes):
    t = await team(db)
    await delivered_rewake(db, t.builder.id)
    native[t.builder.id] = SimpleNamespace(activity_state="unknown", idle_since=None)
    await wake_followup_service.tick(db, NOW)
    assert await notices(db) == []

    native[t.builder.id] = idle(aware(REWAKE_AT - timedelta(minutes=10)))
    await wake_followup_service.tick(db, NOW + timedelta(seconds=60))
    await wake_followup_service.tick(db, NOW + timedelta(seconds=120))

    assert len(await notices(db)) == 2


@pytest.mark.asyncio
async def test_mail_read_during_the_native_read_cancels_the_notice(db, no_wakes, monkeypatch):
    t = await team(db)
    await delivered_rewake(db, t.builder.id)

    async def reads_mail_meanwhile(session, members, *, activities=None):
        message = MailMessage(body_markdown="Decision.", created_at=REWAKE_AT - timedelta(seconds=5))
        session.add(message)
        await session.flush()
        session.add(MailReceipt(message_id=message.id, member_id=t.builder.id,
                                read_at=REWAKE_AT + timedelta(seconds=60)))
        await session.commit()
        return {t.builder.id: idle(aware(REWAKE_AT - timedelta(minutes=10)))}

    monkeypatch.setattr(agent_status_service, "for_members", reads_mail_meanwhile)

    await wake_followup_service.tick(db, NOW)

    assert await notices(db) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [
    {"at": NOW - timedelta(seconds=30)},      # younger than the delay
    {"at": NOW - timedelta(seconds=7200)},    # older than the maximum age
    {"result": "claimed"},                    # reserved, never delivered
    {"result": "refused"},                    # the member never got the text
    {"result": "cancelled"},
])
async def test_a_rewake_outside_the_rule_sends_no_notice(db, native, no_wakes, options):
    t = await team(db)
    await delivered_rewake(db, t.builder.id, **options)
    native[t.builder.id] = idle(aware(NOW - timedelta(hours=3)))

    await wake_followup_service.tick(db, NOW)

    assert await notices(db) == []


@pytest.mark.asyncio
async def test_no_active_work_item_is_said_plainly(db, native, no_wakes):
    t = await team(db, work_item=False)
    await delivered_rewake(db, t.builder.id)
    native[t.builder.id] = idle(aware(REWAKE_AT - timedelta(minutes=10)))

    await wake_followup_service.tick(db, NOW)

    found = await notices(db)
    assert len(found) == 2
    assert all("no active work item" in m.body_markdown for m in found)


@pytest.mark.asyncio
async def test_a_team_without_a_supervisor_notifies_the_leader_only(db, native, no_wakes):
    t = await team(db, supervisor=None)
    await delivered_rewake(db, t.builder.id)
    native[t.builder.id] = idle(aware(REWAKE_AT - timedelta(minutes=10)))

    await wake_followup_service.tick(db, NOW)

    assert [m.delivery_key for m in await notices(db)] == ["rewake-notice:7:leader"]


@pytest.mark.asyncio
async def test_the_notice_can_be_switched_off(db, native, no_wakes, monkeypatch):
    t = await team(db)
    await delivered_rewake(db, t.builder.id)
    native[t.builder.id] = idle(aware(REWAKE_AT - timedelta(minutes=10)))
    monkeypatch.setattr(settings, "wake_followup_enabled", False)

    await wake_followup_service.tick(db, NOW)

    assert await notices(db) == []


async def written_supervisor_notice(db, native):
    t = await team(db)
    await delivered_rewake(db, t.builder.id)
    native[t.builder.id] = idle(aware(REWAKE_AT - timedelta(minutes=10)))
    await wake_followup_service.tick(db, NOW)
    row = next(m for m in await notices(db) if m.delivery_key.endswith(":supervisor"))
    return t, row


@pytest.mark.asyncio
async def test_the_supervisor_notice_reaches_the_bound_supervisors_inbox_and_thread_gate(db, native, no_wakes):
    """A raw row is not delivery. The Supervisor reads through supervisor_inbox and require_supervisor_thread."""
    t, row = await written_supervisor_notice(db, native)
    bound = SimpleNamespace(id=SUPERVISOR_ID)

    inbox = await supervisor_inbox(db, bound)

    assert [m.id for m in inbox if m.id == row.id] == [row.id]
    assert [m.subject for m in inbox if m.id == row.id] == ["Re-wake without activity"]
    assert is_supervisor_message(row) is True
    await require_supervisor_thread(db, bound, row)


@pytest.mark.asyncio
async def test_another_actor_cannot_read_the_notice(db, native, no_wakes):
    t, row = await written_supervisor_notice(db, native)
    other = SimpleNamespace(id=SUPERVISOR_ID + 1)

    with pytest.raises(PermissionError):
        await supervisor_inbox(db, other)
    with pytest.raises(PermissionError):
        await require_supervisor_thread(db, other, row)


@pytest.mark.asyncio
async def test_the_supervisor_notice_has_a_fixed_subject_and_an_exact_payload(db, native, no_wakes):
    t, row = await written_supervisor_notice(db, native)

    assert row.subject == "Re-wake without activity"
    assert set(row.payload) == {"notice", "member_id", "slot_id", "attempt_id", "issue_number",
                                "supervisor_actor_id"}
    assert row.payload["notice"] == "wake_followup_silent"
    assert row.payload["member_id"] == t.builder.id and row.payload["attempt_id"] == 7
    assert row.payload["issue_number"] == 1060 and row.payload["supervisor_actor_id"] == SUPERVISOR_ID
    assert t.builder.display_name in row.body_markdown


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"extra_key": True},
    {"notice": "something_else"},
    {"subject": "No activity after a re-wake: builder slot"},
    {"recipient_member_id": "member"},
    {"sender_actor_id": 6},
    {"sender_type": "member"},
    {"member_id": 0},
    {"member_id": True},
    {"slot_id": "1"},
    {"issue_number": "1060"},
    {"attempt_id": -1},
    {"supervisor_actor_id": "6"},
    {"audience_type": "member"},
])
async def test_a_changed_shape_is_not_a_supervisor_message(db, native, no_wakes, change):
    """The gate is not broadened: any deviation from the strict shape is refused."""
    t, row = await written_supervisor_notice(db, native)
    payload = dict(row.payload)
    fields = {"subject": row.subject, "sender_member_id": row.sender_member_id, "sender_actor_id": None,
              "recipient_member_id": row.recipient_member_id, "sender_type": row.sender_type,
              "audience_type": row.audience_type}
    for key, value in change.items():
        if key == "extra_key":
            payload["extra"] = 1
        elif key in payload:
            payload[key] = value
        elif key in ("sender_member_id", "recipient_member_id"):
            fields[key] = t.builder.id
        else:
            fields[key] = value
    if "notice" in change:
        payload["notice"] = change["notice"]
    changed = SimpleNamespace(kind="message", thread_root_id=None, payload=payload, **fields)

    assert is_supervisor_message(changed) is False


@pytest.mark.asyncio
async def test_a_notice_without_a_slot_or_work_item_is_still_a_supervisor_message(db, native, no_wakes):
    t, row = await written_supervisor_notice(db, native)
    payload = dict(row.payload, slot_id=None, issue_number=None)
    changed = SimpleNamespace(kind="message", thread_root_id=None, payload=payload, subject=row.subject,
                              sender_member_id=None, sender_actor_id=None, recipient_member_id=None,
                              sender_type="system", audience_type="team_preset")

    assert is_supervisor_message(changed) is True
