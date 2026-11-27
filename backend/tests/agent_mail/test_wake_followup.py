"""Wake follow-up: re-wake a member once when a delivered wake gets no activity (#1059).

A member is re-woken only on positive evidence of no activity: no Mail read, ack or sent message after
the wake, and a known native idle state from before the wake. Unknown stays unknown.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base
from app.models.database import AgentTeamPreset, MailMessage, MailReceipt, MailTeamMember, MailWakeAttempt
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

    async def fake_wake(db, member_id, now, *args, source="auto_nudge", reason_code=None,
                        delivery_guard=None, **options):
        row = MailWakeAttempt(
            member_id=member_id, actor_type="server", source=source, reason_code=reason_code or source,
            correlation_id=uuid4().hex, result="attempted")
        db.add(row)
        await db.commit()
        # The real wake asks the guard after the audit row and just before the transport.
        if delivery_guard is not None and not await delivery_guard():
            row.result, row.failure_code = "refused", "wake_session_mismatch"
            await db.commit()
            raise MailWakeError("wake_session_mismatch")
        calls.append((member_id, source, reason_code))
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


async def member(db, name="b2", *, preset_id=None):
    row = MailTeamMember(identity_key=f"slot:{name}", repo_id="r", repo_path=f"/tmp/{name}",
                         repo_name=name, display_name=name, team_preset_id=preset_id)
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
    """The re-wakes that reached the wake. A claim row only reserves the attempt."""
    return list((await db.scalars(select(MailWakeAttempt).where(
        MailWakeAttempt.source == "rewake", MailWakeAttempt.result != "claimed"))).all())


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
@pytest.mark.parametrize("installation_enabled", [False, True])
async def test_teams_use_their_own_wake_policy_and_clear_to_defaults(
    db, rewakes, native, monkeypatch, installation_enabled,
):
    monkeypatch.setattr(settings, "wake_followup_enabled", installation_enabled)
    monkeypatch.setattr(settings, "wake_followup_delay_seconds", 120)
    enabled = AgentTeamPreset(name="Enabled", wake_followup_enabled=True, wake_followup_delay_seconds=60)
    disabled = AgentTeamPreset(name="Disabled", wake_followup_enabled=False, wake_followup_delay_seconds=30)
    delayed = AgentTeamPreset(name="Delayed", wake_followup_delay_seconds=180)
    inherited = AgentTeamPreset(name="Inherited")
    presets = [enabled, disabled, delayed, inherited]
    db.add_all(presets)
    await db.commit()
    expected = {}
    for preset in presets:
        candidate = await member(db, preset.name, preset_id=preset.id)
        await delivered_wake(db, candidate.id)
        newest = await delivered_wake(db, candidate.id, at=NOW - timedelta(seconds=60))
        native[candidate.id] = idle(aware(WAKE_AT - timedelta(minutes=10)))
        expected[preset.name] = (candidate.id, "rewake", f"rewake:{newest.id}")

    await wake_followup_service.tick(db, NOW - timedelta(seconds=1))
    assert rewakes == []
    await wake_followup_service.tick(db, NOW)
    assert rewakes == [expected["Enabled"]]
    await wake_followup_service.tick(db, NOW + timedelta(seconds=60))
    assert rewakes == ([expected["Enabled"], expected["Inherited"]]
                       if installation_enabled else [expected["Enabled"]])

    disabled.wake_followup_enabled = None
    delayed.wake_followup_delay_seconds = None
    await db.commit()
    await wake_followup_service.tick(db, NOW + timedelta(seconds=60))
    assert set(rewakes[1:]) == ({expected[name] for name in ("Disabled", "Delayed", "Inherited")}
                                if installation_enabled else set())


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), [("wake_followup_enabled", False), ("wake_followup_delay_seconds", 301)])
async def test_policy_change_during_delivery_guard_cancels_the_rewake(db, rewakes, monkeypatch, field, value):
    preset = AgentTeamPreset(name="Team", wake_followup_enabled=True)
    db.add(preset)
    await db.commit()
    candidate = await member(db, preset_id=preset.id)
    await delivered_wake(db, candidate.id)
    reads = []

    async def changes_policy(session, members, *, activities=None):
        reads.append(1)
        if len(reads) == 2:
            await session.execute(update(AgentTeamPreset).where(AgentTeamPreset.id == preset.id)
                                  .values({field: value}).execution_options(synchronize_session=False))
            await session.commit()
        return {candidate.id: idle(aware(WAKE_AT - timedelta(minutes=10)))}

    monkeypatch.setattr(agent_status_service, "for_members", changes_policy)
    await wake_followup_service.tick(db, NOW)

    assert len(reads) == 2
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


@pytest.mark.asyncio
async def test_two_concurrent_ticks_send_one_follow_up(tmp_path, rewakes, monkeypatch):
    """Two repo jobs sweep at once. Both see the attempt unclaimed unless the claim is serialized and durable."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/mail.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as setup:
        b2 = await member(setup)
        await delivered_wake(setup, b2.id)

    async def slow_for_members(db, members, *, activities=None):
        await asyncio.sleep(0.05)  # lets the other tick reach the same point
        return {m.id: idle(aware(WAKE_AT - timedelta(minutes=10))) for m in members}

    monkeypatch.setattr(agent_status_service, "for_members", slow_for_members)

    async def sweep():
        async with maker() as session:
            return await wake_followup_service.tick(session, NOW)

    await asyncio.gather(sweep(), sweep())
    await engine.dispose()

    assert rewakes == [(b2.id, "rewake", "rewake:1")]


@pytest.mark.asyncio
async def test_mail_read_while_native_state_is_read_cancels_the_follow_up(db, rewakes, monkeypatch):
    b2 = await member(db)
    await delivered_wake(db, b2.id)

    async def reading_member(session, members, *, activities=None):
        # The member acts while the observation runs.
        await receipt(session, b2.id, read_at=WAKE_AT + timedelta(seconds=45))
        return {b2.id: idle(aware(WAKE_AT - timedelta(minutes=10)))}

    monkeypatch.setattr(agent_status_service, "for_members", reading_member)

    await wake_followup_service.tick(db, NOW)

    assert rewakes == []


@pytest.mark.asyncio
async def test_native_work_that_starts_before_delivery_cancels_the_follow_up(db, rewakes, monkeypatch):
    b2 = await member(db)
    await delivered_wake(db, b2.id)
    reads = []

    async def starts_working(session, members, *, activities=None):
        reads.append(1)
        if len(reads) == 1:
            return {b2.id: idle(aware(WAKE_AT - timedelta(minutes=10)))}
        return {b2.id: SimpleNamespace(activity_state="working", idle_since=None)}

    monkeypatch.setattr(agent_status_service, "for_members", starts_working)

    await wake_followup_service.tick(db, NOW)

    assert len(reads) == 2
    assert rewakes == []


@pytest.mark.asyncio
async def test_the_newest_wake_sets_the_delay_not_an_older_unanswered_one(db, rewakes, native):
    b2 = await member(db)
    await delivered_wake(db, b2.id, at=WAKE_AT)
    newest = await delivered_wake(db, b2.id, at=NOW - timedelta(seconds=10))
    native[b2.id] = idle(aware(WAKE_AT - timedelta(minutes=10)))

    await wake_followup_service.tick(db, NOW)
    assert rewakes == []

    await wake_followup_service.tick(db, NOW + timedelta(seconds=200))
    assert rewakes == [(b2.id, "rewake", f"rewake:{newest.id}")]


@pytest.mark.asyncio
async def test_a_claim_is_taken_once_even_when_the_first_read_was_stale(db):
    """Another process can claim between this sweep's read and its claim. Only one claimant gets a row."""
    b2 = await member(db)
    wake = await delivered_wake(db, b2.id)

    first = await wake_followup_service._claim(db, b2.id, wake)
    second = await wake_followup_service._claim(db, b2.id, wake)

    assert (first, second) == (True, False)
    rows = list((await db.scalars(select(MailWakeAttempt).where(
        MailWakeAttempt.reason_code == f"rewake:{wake.id}"))).all())
    assert [row.result for row in rows] == ["claimed"]


@pytest.mark.asyncio
async def test_mail_read_during_the_guards_own_native_read_cancels_the_follow_up(db, rewakes, monkeypatch):
    """The guard awaits a second native read. Mail read during it must still cancel the follow-up."""
    b2 = await member(db)
    wake = await delivered_wake(db, b2.id)
    await receipt(db, b2.id)  # other unread Mail stays, so an empty inbox cannot mask the activity
    reads = []

    async def reads_mail_during_second_read(session, members, *, activities=None):
        reads.append(1)
        if len(reads) == 2:
            await receipt(session, b2.id, read_at=WAKE_AT + timedelta(seconds=60))
        return {b2.id: idle(aware(WAKE_AT - timedelta(minutes=10)))}

    monkeypatch.setattr(agent_status_service, "for_members", reads_mail_during_second_read)

    await wake_followup_service.tick(db, NOW)

    assert len(reads) == 2
    assert rewakes == []
    claims = list((await db.scalars(select(MailWakeAttempt).where(
        MailWakeAttempt.reason_code == f"rewake:{wake.id}", MailWakeAttempt.result == "cancelled"))).all())
    assert len(claims) == 1


@pytest.mark.asyncio
async def test_the_mail_activity_check_is_one_statement_over_receipts_and_messages(db):
    """Two statements leave a gap: a read committed between them is missed. One statement has none."""
    b2 = await member(db)
    seen = []

    def record(connection, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(db.bind.sync_engine, "before_cursor_execute", record)
    try:
        await wake_followup_service._mail_activity_since(db, b2.id, WAKE_AT)
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", record)

    assert len(seen) == 1
    assert "mail_receipts" in seen[0] and "mail_messages" in seen[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("setup,expected", [
    ("nothing", False),
    ("read_after", True),
    ("acked_after", True),
    ("sent_after", True),
    ("read_before_only", False),
    ("sent_by_other_member", False),
])
async def test_the_mail_activity_check_answers_each_kind_of_activity(db, setup, expected):
    b2 = await member(db)
    other = await member(db, "b1")
    if setup == "read_after":
        await receipt(db, b2.id, read_at=WAKE_AT + timedelta(seconds=5))
    elif setup == "acked_after":
        await receipt(db, b2.id, acked_at=WAKE_AT + timedelta(seconds=5))
    elif setup == "sent_after":
        db.add(MailMessage(body_markdown="Update.", sender_member_id=b2.id, created_at=WAKE_AT + timedelta(seconds=5)))
        await db.commit()
    elif setup == "read_before_only":
        await receipt(db, b2.id, read_at=WAKE_AT - timedelta(seconds=5))
    elif setup == "sent_by_other_member":
        db.add(MailMessage(body_markdown="Update.", sender_member_id=other.id, created_at=WAKE_AT + timedelta(seconds=5)))
        await db.commit()

    assert await wake_followup_service._mail_activity_since(db, b2.id, WAKE_AT) is expected
