"""The provider-refusal notices to the Leader and the Supervisor, naming the member and its work item (#1092).

A refusal episode (#1091) sends one notice to the Leader and one to the Supervisor. Neither wakes anybody. The
Supervisor reads only strictly shaped system notices, so the notice has its own predicate; a notice that predicate
rejects never reaches the inbox (the #1060 lesson). Every test goes through the real `_record_input_waits` and
reads the result through `supervisor_inbox` and `require_supervisor_thread`.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.database import GithubWorkItem, MailAgentSession, MailMessage, MailReceipt
from app.models.database import MailTeamMember, MailWakeAttempt
from app.services.agent_team_supervisor_service import (
    is_supervisor_message, require_supervisor_thread, supervisor_inbox,
)

from tests.agent_teams.test_native_input_wait import record_wait, wait_team

REFUSAL = "provider_refusal"
NOW = datetime(2026, 11, 17, 12, 0, 0)


async def add_item(db, slot, *, status="dispatched", issue=1092, title="Notify on a provider refusal"):
    item = GithubWorkItem(issue_number=issue, issue_title=title, scope_id=1, github_updated_at=NOW,
                          issue_url=f"https://github.com/albiexai/synth-city/issues/{issue}",
                          dispatch_status=status, owner_slot_id=slot.id)
    db.add(item)
    await db.commit()
    return item


async def refusal_notices(db):
    rows = (await db.scalars(select(MailMessage).order_by(MailMessage.id))).all()
    return [m for m in rows if isinstance(m.payload, dict) and m.payload.get("notice") == REFUSAL]


async def generic_notices(db):
    rows = (await db.scalars(select(MailMessage).order_by(MailMessage.id))).all()
    return [m for m in rows if isinstance(m.payload, dict) and m.payload.get("notice") == "native_input_wait"]


def for_supervisor(notices):
    return [m for m in notices if m.audience_type == "team_preset"]


def for_leader(notices, leader):
    return [m for m in notices if m.recipient_member_id == leader.id]


async def worker_member(db, slot):
    return await db.scalar(select(MailTeamMember).where(MailTeamMember.team_slot_id == slot.id))


# ---- the two notices -----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_refusal_sends_one_notice_to_the_supervisor_and_one_to_the_leader(db):
    team, actor, slot, binding, _, leader = await wait_team(db, supervised=True)
    await add_item(db, slot)

    await record_wait(db, team, slot, binding, REFUSAL)

    notices = await refusal_notices(db)
    assert len(notices) == 2
    assert len(for_supervisor(notices)) == 1 and len(for_leader(notices, leader)) == 1
    assert await generic_notices(db) == []


@pytest.mark.asyncio
async def test_the_supervisor_notice_has_the_strict_shape_and_names_the_member_and_the_work_item(db):
    team, actor, slot, binding, _, _ = await wait_team(db, supervised=True)
    item = await add_item(db, slot)
    member = await worker_member(db, slot)

    await record_wait(db, team, slot, binding, REFUSAL)

    notice = for_supervisor(await refusal_notices(db))[0]
    assert notice.subject == "Provider refusal"
    assert notice.payload == {"notice": REFUSAL, "member_id": member.id, "slot_id": slot.id,
                              "work_item_id": item.id, "issue_number": 1092, "supervisor_actor_id": actor.id}
    assert notice.sender_member_id is None and notice.sender_actor_id is None and notice.recipient_member_id is None
    assert notice.thread_root_id is None and notice.audience_id == str(team.id)
    assert "Worker" in notice.body_markdown and "#1092" in notice.body_markdown
    assert "Notify on a provider refusal" in notice.body_markdown
    assert "synthetic/project" not in notice.body_markdown


@pytest.mark.asyncio
async def test_the_supervisor_reads_it_through_the_real_inbox_and_thread_gate(db):
    """A raw row is not delivery. The Supervisor reads through supervisor_inbox and require_supervisor_thread."""
    team, actor, slot, binding, _, _ = await wait_team(db, supervised=True)
    await add_item(db, slot)
    await record_wait(db, team, slot, binding, REFUSAL)
    notice = for_supervisor(await refusal_notices(db))[0]

    inbox = await supervisor_inbox(db, actor)

    assert [m.id for m in inbox if m.id == notice.id] == [notice.id]
    assert is_supervisor_message(notice) is True
    await require_supervisor_thread(db, actor, notice)
    with pytest.raises(PermissionError):
        await supervisor_inbox(db, SimpleNamespace(id=actor.id + 99))
    with pytest.raises(PermissionError):
        await require_supervisor_thread(db, SimpleNamespace(id=actor.id + 99), notice)


@pytest.mark.asyncio
async def test_the_leader_notice_names_the_member_and_the_work_item_and_reaches_the_leader(db):
    team, actor, slot, binding, _, leader = await wait_team(db, supervised=True)
    item = await add_item(db, slot)
    member = await worker_member(db, slot)

    await record_wait(db, team, slot, binding, REFUSAL)

    notice = for_leader(await refusal_notices(db), leader)[0]
    assert notice.subject == "Provider refusal" and notice.audience_id == str(leader.id)
    assert notice.sender_member_id is None and notice.sender_type == "system"
    assert notice.payload == {"notice": REFUSAL, "member_id": member.id, "slot_id": slot.id,
                              "work_item_id": item.id, "issue_number": 1092}
    assert "Worker" in notice.body_markdown and "#1092" in notice.body_markdown
    assert "synthetic/project" not in notice.body_markdown
    assert await db.scalar(select(MailReceipt.id).where(
        MailReceipt.message_id == notice.id, MailReceipt.member_id == leader.id))


@pytest.mark.asyncio
@pytest.mark.parametrize("supervised", [True, False])
async def test_a_refusal_notice_wakes_nobody(db, supervised):
    """The notices are plain rows: no wake attempt is recorded, and nothing is sent to a pane."""
    team, actor, slot, binding, _, leader = await wait_team(db, supervised=supervised)
    await record_wait(db, team, slot, binding, REFUSAL)

    assert len(await refusal_notices(db)) == (2 if supervised else 1)
    assert (await db.scalars(select(MailWakeAttempt))).all() == []


@pytest.mark.asyncio
async def test_an_unsupervised_team_sends_the_leader_notice_only(db):
    team, actor, slot, binding, _, leader = await wait_team(db, supervised=False)
    await add_item(db, slot)

    await record_wait(db, team, slot, binding, REFUSAL)

    notices = await refusal_notices(db)
    assert len(notices) == 1 and notices[0].recipient_member_id == leader.id
    assert "supervisor_actor_id" not in notices[0].payload
    assert for_supervisor(notices) == []
    assert await generic_notices(db) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("supervised", [True, False])
async def test_the_binding_keeps_the_id_of_the_supervisor_notice_or_else_the_leaders(db, supervised):
    team, actor, slot, binding, _, leader = await wait_team(db, supervised=supervised)

    await record_wait(db, team, slot, binding, REFUSAL)
    await db.refresh(binding)

    notices = await refusal_notices(db)
    expected = for_supervisor(notices)[0] if supervised else for_leader(notices, leader)[0]
    assert binding.waiting_input_notice_id == expected.id


# ---- the work item ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["dispatched", "verifying"])
async def test_an_active_work_item_is_named(db, status):
    team, actor, slot, binding, _, _ = await wait_team(db, supervised=True)
    await add_item(db, slot, status=status, issue=1234, title="Some other issue")

    await record_wait(db, team, slot, binding, REFUSAL)

    for notice in await refusal_notices(db):
        assert notice.payload["issue_number"] == 1234 and "#1234" in notice.body_markdown


@pytest.mark.asyncio
@pytest.mark.parametrize("setup", ["none", "merged", "other_slot"])
async def test_a_member_without_an_active_work_item_is_named_with_no_work_item(db, setup):
    team, actor, slot, binding, _, leader = await wait_team(db, supervised=True)
    if setup == "merged":
        await add_item(db, slot, status="merged")
    elif setup == "other_slot":
        await add_item(db, SimpleNamespace(id=leader_slot_id(team)), status="dispatched")

    await record_wait(db, team, slot, binding, REFUSAL)

    notices = await refusal_notices(db)
    assert len(notices) == 2
    for notice in notices:
        assert notice.payload["work_item_id"] is None and notice.payload["issue_number"] is None
        assert "no active work item" in notice.body_markdown
    assert is_supervisor_message(for_supervisor(notices)[0]) is True


def leader_slot_id(team):
    return team.leader_slot_id


def test_the_active_statuses_are_the_ones_the_wake_followup_notice_uses():
    from app.services import provider_refusal_notice_service as notices
    from app.services.wake_followup_service import ACTIVE_ITEM_STATUSES
    assert tuple(notices.ACTIVE_ITEM_STATUSES) == tuple(ACTIVE_ITEM_STATUSES)


# ---- one pair per episode ------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_repeat_adds_nothing_and_each_new_episode_adds_one_pair(db):
    team, actor, slot, binding, _, leader = await wait_team(db, supervised=True)

    await record_wait(db, team, slot, binding, REFUSAL)
    await record_wait(db, team, slot, binding, REFUSAL)
    assert len(await refusal_notices(db)) == 2

    await record_wait(db, team, slot, binding, "clear")
    await record_wait(db, team, slot, binding, REFUSAL)
    notices = await refusal_notices(db)
    assert len(notices) == 4
    keys = [m.delivery_key for m in notices]
    assert all(keys) and len(set(keys)) == 4          # a key per episode and per recipient


@pytest.mark.asyncio
async def test_a_question_wait_that_becomes_a_refusal_is_a_new_episode_with_its_own_pair(db):
    team, actor, slot, binding, _, leader = await wait_team(db, supervised=True)

    await record_wait(db, team, slot, binding, "question")
    await record_wait(db, team, slot, binding, REFUSAL)
    await record_wait(db, team, slot, binding, REFUSAL)

    assert len(await generic_notices(db)) == 1 and (await generic_notices(db))[0].payload["reason"] == "question"
    assert len(await refusal_notices(db)) == 2


@pytest.mark.asyncio
async def test_a_refusal_that_becomes_a_question_gets_the_generic_notice_for_the_question(db):
    team, actor, slot, binding, _, _ = await wait_team(db, supervised=True)

    await record_wait(db, team, slot, binding, REFUSAL)
    await record_wait(db, team, slot, binding, "question")

    assert len(await refusal_notices(db)) == 2
    generic = await generic_notices(db)
    assert [m.payload["reason"] for m in generic] == ["question"]
    assert is_supervisor_message(generic[0]) is True


@pytest.mark.asyncio
async def test_the_same_episode_written_twice_keeps_exactly_one_pair(db):
    """The delivery key is the second guard: a second writer for the same episode adds nothing and does not fail."""
    from app.services.provider_refusal_notice_service import send_provider_refusal_notices
    team, actor, slot, binding, _, leader = await wait_team(db, supervised=True)
    started = datetime(2026, 11, 17, 12, 0, 0)

    first = await send_provider_refusal_notices(db, preset_id=team.id, slot_id=slot.id, binding_id=binding.id,
                                                started_at=started, supervisor_actor_id=actor.id,
                                                leader_slot_id=team.leader_slot_id)
    await db.commit()
    second = await send_provider_refusal_notices(db, preset_id=team.id, slot_id=slot.id, binding_id=binding.id,
                                                 started_at=started, supervisor_actor_id=actor.id,
                                                 leader_slot_id=team.leader_slot_id)
    await db.commit()

    assert first is not None and second is None
    assert len(await refusal_notices(db)) == 2


# ---- who can be told -----------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_supervised_team_without_a_leader_member_still_tells_the_supervisor(db):
    team, actor, slot, binding, _, leader = await wait_team(db, supervised=True)
    await db.delete(leader)
    await db.commit()

    await record_wait(db, team, slot, binding, REFUSAL)

    notices = await refusal_notices(db)
    assert len(notices) == 1 and for_supervisor(notices) == notices
    await require_supervisor_thread(db, actor, notices[0])


@pytest.mark.asyncio
async def test_a_team_with_nobody_to_tell_leaves_the_episode_unclaimed(db):
    """As before: an unsupervised team with no Leader member records nothing, so a later observation can try again."""
    team, actor, slot, binding, _, leader = await wait_team(db, supervised=False)
    await db.delete(leader)
    await db.commit()

    await record_wait(db, team, slot, binding, REFUSAL)
    await db.refresh(binding)

    assert await refusal_notices(db) == [] and binding.waiting_input_reason is None


@pytest.mark.asyncio
async def test_a_closed_session_stores_no_refusal_and_sends_no_notice(db):
    """The authority fences are the ones the generic notice had."""
    from sqlalchemy import update
    team, actor, slot, binding, session, _ = await wait_team(db, supervised=True)
    await db.execute(update(MailAgentSession).where(MailAgentSession.id == session.id).values(
        closed_at=datetime.utcnow()))
    await db.commit()

    await record_wait(db, team, slot, binding, REFUSAL)
    await db.refresh(binding)

    assert await refusal_notices(db) == [] and binding.waiting_input_reason is None


@pytest.mark.asyncio
async def test_an_older_observation_cannot_send_a_notice(db):
    team, actor, slot, binding, _, _ = await wait_team(db, supervised=True)
    now = datetime.now(timezone.utc)
    await record_wait(db, team, slot, binding, "clear", now=now)
    await record_wait(db, team, slot, binding, REFUSAL, now=now - timedelta(seconds=30))

    assert await refusal_notices(db) == []


# ---- the predicates -------------------------------------------------------------------------------------------------


def copy_of(notice, **changes):
    values = {c.name: getattr(notice, c.name) for c in notice.__table__.columns}
    values["payload"] = dict(notice.payload)
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize("name,change", [
    ("subject", {"subject": "Waiting for input"}),
    ("subject case", {"subject": "provider refusal"}),
    ("sender member", {"sender_member_id": 1}),    # a member message passes the other branch; see below
    ("sender actor", {"sender_actor_id": 1}),
    ("recipient", {"recipient_member_id": 1}),
    ("not a system sender", {"sender_type": "member"}),
    ("thread reply", {"thread_root_id": 1}),
    ("audience", {"audience_type": "member"}),
    ("kind", {"kind": "question"}),
    ("payload is not a dict", {"payload": ["provider_refusal"]}),
    ("payload without supervisor", {"payload_drop": "supervisor_actor_id"}),
    ("payload without member", {"payload_drop": "member_id"}),
    ("payload without issue key", {"payload_drop": "issue_number"}),
    ("extra key", {"payload_add": ("surprise", 1)}),
    ("wrong notice", {"payload_set": ("notice", "native_input_wait")}),
    ("bool member", {"payload_set": ("member_id", True)}),
    ("zero slot", {"payload_set": ("slot_id", 0)}),
    ("text member", {"payload_set": ("member_id", "7")}),
    ("negative supervisor", {"payload_set": ("supervisor_actor_id", -1)}),
    ("text issue", {"payload_set": ("issue_number", "1092")}),
    ("zero work item", {"payload_set": ("work_item_id", 0)}),
])
async def test_the_supervisor_predicate_refuses_anything_off_shape(db, name, change):
    team, actor, slot, binding, _, _ = await wait_team(db, supervised=True)
    await add_item(db, slot)
    await record_wait(db, team, slot, binding, REFUSAL)
    notice = for_supervisor(await refusal_notices(db))[0]
    change = dict(change)
    payload = dict(notice.payload)
    if "payload_drop" in change:
        payload.pop(change.pop("payload_drop"))
        change["payload"] = payload
    if "payload_add" in change:
        key, value = change.pop("payload_add")
        payload[key] = value
        change["payload"] = payload
    if "payload_set" in change:
        key, value = change.pop("payload_set")
        payload[key] = value
        change["payload"] = payload
    if name == "sender member":
        # A message from a member is a different kind of thing; it must not pass as this system notice.
        assert is_supervisor_message(copy_of(notice, **change)) is True
        from app.services import agent_team_supervisor_service as service
        assert service._is_provider_refusal_notice(copy_of(notice, **change)) is False
        return

    assert is_supervisor_message(copy_of(notice, **change)) is False


@pytest.mark.asyncio
async def test_the_work_item_and_issue_may_be_absent_but_not_wrong(db):
    team, actor, slot, binding, _, _ = await wait_team(db, supervised=True)
    await record_wait(db, team, slot, binding, REFUSAL)
    notice = for_supervisor(await refusal_notices(db))[0]
    assert notice.payload["work_item_id"] is None and notice.payload["issue_number"] is None

    assert is_supervisor_message(notice) is True


@pytest.mark.asyncio
async def test_the_generic_waiting_notice_no_longer_accepts_a_refusal(db):
    """A refusal has its own notice. The generic predicate accepts only the three waits for input."""
    team, actor, slot, binding, _, _ = await wait_team(db, supervised=True)
    await record_wait(db, team, slot, binding, "question")
    generic = (await generic_notices(db))[0]

    assert is_supervisor_message(generic) is True
    for reason in ("trust", "sign_in"):
        assert is_supervisor_message(copy_of(generic, payload={**generic.payload, "reason": reason})) is True
    assert is_supervisor_message(copy_of(generic, payload={**generic.payload, "reason": REFUSAL})) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["question", "trust", "sign_in"])
async def test_a_wait_for_input_still_gets_the_generic_notice_and_no_refusal_notice(db, reason):
    team, actor, slot, binding, _, _ = await wait_team(db, supervised=True)

    await record_wait(db, team, slot, binding, reason)

    generic = await generic_notices(db)
    assert len(generic) == 1 and generic[0].subject == "Waiting for input" and generic[0].payload["reason"] == reason
    assert await refusal_notices(db) == []
