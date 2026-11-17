"""Wake follow-up: re-wake a member once when a delivered wake gets no activity (#1059).

A member is re-woken only on positive evidence of no activity: no Mail read, ack or sent message after
the wake, and a known native idle state from before the wake. Unknown stays unknown.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.database import MailMessage, MailReceipt, MailTeamMember, MailWakeAttempt
from app.services.agent_mail_service import MailWakeError, agent_mail_service
from app.services.agent_status_service import agent_status_service
from app.services.wake_followup_service import wake_followup_service

NOW = datetime(2026, 11, 17, 12, 0, 0)
WAKE_AT = NOW - timedelta(seconds=300)


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc)


@pytest.fixture
def rewakes(monkeypatch):
    """Replace the transport. The fake writes the audit row the real wake writes."""
    calls = []

    async def fake_wake(db, member_id, now, *args, source="auto_nudge", reason_code=None, **options):
        calls.append((member_id, source, reason_code))
        db.add(MailWakeAttempt(
            member_id=member_id, actor_type="server", source=source, reason_code=reason_code or source,
            correlation_id=uuid4().hex, result="attempted"))
        await db.commit()
        return {"method": "tmux"}

    monkeypatch.setattr(agent_mail_service, "_wake_member", fake_wake)
    return calls


@pytest.fixture
def native(monkeypatch):
    """Native activity per member id. A member that is not listed has no observation."""
    states: dict[int, SimpleNamespace] = {}

    async def fake_for_members(db, members, *, activities=None):
        return {member.id: states[member.id] for member in members if member.id in states}

    monkeypatch.setattr(agent_status_service, "for_members", fake_for_members)
    return states


async def member(db, name="b2"):
    row = MailTeamMember(identity_key=f"slot:{name}", repo_id="r", repo_path=f"/tmp/{name}",
                         repo_name=name, display_name=name)
    db.add(row)
    await db.commit()
    return row


async def delivered_wake(db, member_id, *, at=WAKE_AT, source="auto_nudge", result="delivered", unread=1, pending=0):
    row = MailWakeAttempt(member_id=member_id, actor_type="server", source=source, reason_code=source,
                          correlation_id=uuid4().hex, unread_count=unread, pending_count=pending,
                          result=result, created_at=at)
    db.add(row)
    await db.commit()
    return row


async def receipt(db, member_id, *, read_at=None, acked_at=None):
    message = MailMessage(body_markdown="Approval decision.", created_at=WAKE_AT - timedelta(seconds=5))
    db.add(message)
    await db.flush()
    db.add(MailReceipt(message_id=message.id, member_id=member_id, read_at=read_at, acked_at=acked_at))
    await db.commit()


def idle(since):
    return SimpleNamespace(activity_state="idle", idle_since=since)


async def rewake_rows(db):
    return list((await db.scalars(select(MailWakeAttempt).where(MailWakeAttempt.source == "rewake"))).all())


@pytest.mark.asyncio
async def test_known_idle_before_the_wake_gets_exactly_one_rewake(db, rewakes, native):
    b2 = await member(db)
    wake = await delivered_wake(db, b2.id)
    native[b2.id] = idle(aware(WAKE_AT - timedelta(minutes=10)))

    await wake_followup_service.tick(db, NOW)

    assert rewakes == [(b2.id, "rewake", f"rewake:{wake.id}")]
    assert len(await rewake_rows(db)) == 1


@pytest.mark.asyncio
async def test_a_second_tick_adds_no_second_wake(db, rewakes, native):
    b2 = await member(db)
    await delivered_wake(db, b2.id)
    native[b2.id] = idle(aware(WAKE_AT - timedelta(minutes=10)))

    await wake_followup_service.tick(db, NOW)
    await wake_followup_service.tick(db, NOW + timedelta(seconds=30))

    assert len(rewakes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["read_at", "acked_at"])
async def test_mail_read_or_acked_after_the_wake_means_the_member_acted(db, rewakes, native, field):
    b2 = await member(db)
    await delivered_wake(db, b2.id)
    await receipt(db, b2.id, **{field: WAKE_AT + timedelta(seconds=20)})
    native[b2.id] = idle(aware(WAKE_AT - timedelta(minutes=10)))

    await wake_followup_service.tick(db, NOW)

    assert rewakes == []


@pytest.mark.asyncio
async def test_mail_read_before_the_wake_is_not_activity_after_it(db, rewakes, native):
    b2 = await member(db)
    await delivered_wake(db, b2.id)
    await receipt(db, b2.id, read_at=WAKE_AT - timedelta(seconds=60))
    native[b2.id] = idle(aware(WAKE_AT - timedelta(minutes=10)))

    await wake_followup_service.tick(db, NOW)

    assert len(rewakes) == 1


@pytest.mark.asyncio
async def test_a_message_sent_after_the_wake_means_the_member_acted(db, rewakes, native):
    b2 = await member(db)
    await delivered_wake(db, b2.id)
    db.add(MailMessage(body_markdown="Plan question.", sender_member_id=b2.id,
                       created_at=WAKE_AT + timedelta(seconds=40)))
    await db.commit()
    native[b2.id] = idle(aware(WAKE_AT - timedelta(minutes=10)))

    await wake_followup_service.tick(db, NOW)

    assert rewakes == []


@pytest.mark.asyncio
async def test_native_working_means_the_member_acted(db, rewakes, native):
    b2 = await member(db)
    await delivered_wake(db, b2.id)
    native[b2.id] = SimpleNamespace(activity_state="working", idle_since=None)

    await wake_followup_service.tick(db, NOW)

    assert rewakes == []


@pytest.mark.asyncio
async def test_native_idle_since_after_the_wake_means_a_turn_finished_after_it(db, rewakes, native):
    b2 = await member(db)
    await delivered_wake(db, b2.id)
    native[b2.id] = idle(aware(WAKE_AT + timedelta(seconds=90)))

    await wake_followup_service.tick(db, NOW)

    assert rewakes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [
    SimpleNamespace(activity_state="unknown", idle_since=None),
    SimpleNamespace(activity_state="stopped", idle_since=None),
    SimpleNamespace(activity_state="idle", idle_since=None),
    None,
])
async def test_unknown_native_evidence_stays_unknown_and_wakes_nobody(db, rewakes, native, status):
    b2 = await member(db)
    await delivered_wake(db, b2.id)
    if status is not None:
        native[b2.id] = status

    await wake_followup_service.tick(db, NOW)

    assert rewakes == []


@pytest.mark.asyncio
async def test_an_unknown_attempt_stays_a_candidate_and_wakes_once_when_the_state_becomes_known(db, rewakes, native):
    b2 = await member(db)
    await delivered_wake(db, b2.id)
    native[b2.id] = SimpleNamespace(activity_state="unknown", idle_since=None)
    await wake_followup_service.tick(db, NOW)
    assert rewakes == []

    native[b2.id] = idle(aware(WAKE_AT - timedelta(minutes=10)))
    await wake_followup_service.tick(db, NOW + timedelta(seconds=60))
    await wake_followup_service.tick(db, NOW + timedelta(seconds=120))

    assert len(rewakes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [
    {"at": NOW - timedelta(seconds=30)},                 # younger than the delay
    {"at": NOW - timedelta(seconds=7200)},               # older than the maximum age
    {"source": "rewake"},                                # a re-wake is never re-woken
    {"result": "refused"},                               # never delivered
    {"unread": 0, "pending": 0},                         # nothing was waiting for the member
])
async def test_attempts_outside_the_rule_are_not_candidates(db, rewakes, native, options):
    b2 = await member(db)
    await delivered_wake(db, b2.id, **options)
    native[b2.id] = idle(aware(NOW - timedelta(hours=3)))

    await wake_followup_service.tick(db, NOW)

    assert rewakes == []


@pytest.mark.asyncio
async def test_the_follow_up_can_be_switched_off(db, rewakes, native, monkeypatch):
    b2 = await member(db)
    await delivered_wake(db, b2.id)
    native[b2.id] = idle(aware(WAKE_AT - timedelta(minutes=10)))
    monkeypatch.setattr(settings, "wake_followup_enabled", False)

    await wake_followup_service.tick(db, NOW)

    assert rewakes == []


@pytest.mark.asyncio
async def test_a_refused_rewake_is_recorded_and_not_repeated(db, native, monkeypatch):
    b2 = await member(db)
    await delivered_wake(db, b2.id)
    native[b2.id] = idle(aware(WAKE_AT - timedelta(minutes=10)))
    calls = []

    async def refusing_wake(db, member_id, now, *args, source="auto_nudge", reason_code=None, **options):
        calls.append(member_id)
        db.add(MailWakeAttempt(member_id=member_id, actor_type="server", source=source,
                               reason_code=reason_code, correlation_id=uuid4().hex, result="refused",
                               failure_code="wake_target_unbound"))
        await db.commit()
        raise MailWakeError("wake_target_unbound")

    monkeypatch.setattr(agent_mail_service, "_wake_member", refusing_wake)

    await wake_followup_service.tick(db, NOW)
    await wake_followup_service.tick(db, NOW + timedelta(seconds=30))

    assert calls == [b2.id]


@pytest.mark.asyncio
async def test_a_failure_while_reading_native_state_does_not_raise_or_wake(db, rewakes, monkeypatch):
    b2 = await member(db)
    await delivered_wake(db, b2.id)

    async def broken(db, members, *, activities=None):
        raise OSError("observation unavailable")

    monkeypatch.setattr(agent_status_service, "for_members", broken)

    await wake_followup_service.tick(db, NOW)

    assert rewakes == []


@pytest.mark.asyncio
async def test_the_scheduler_runs_the_follow_up_after_its_stages(db, monkeypatch):
    from app.services.github_dispatch_scheduler import GithubDispatchScheduler

    ticks = []

    async def fake_tick(session, now=None):
        ticks.append(session)
        return []

    monkeypatch.setattr(wake_followup_service, "tick", fake_tick)

    await GithubDispatchScheduler().run_repo_once(db, "owner", "repo", client=object())

    assert ticks == [db]
