"""#1300 (part 1 of 2): waits on Supervisor decisions beyond the effort ceiling.

The ceiling notice starts the wait, keyed by its Mail id. Only evidence that the notice was resolved ends it: a ceiling
the asked step now fits under, an end-stopped disposition, the item's end or the attempt's replacement. Whoever acted
is the recorded actor; the answerer stays unknown.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select, text

from app.api.v1 import agent_teams
from app.models.database import FactoryAuditEvent, GithubWorkItem, MailMessage, TeamGithubScope
from app.models.schemas import TeamGithubScopeUpdate
from app.services import decision_wait_service as waits
from tests.agent_teams.test_effort_limit import (  # noqa: F401
    T0, _bind_supervisor, _ceiling_notices, _code, _extend, _limited, _scope_ceiling, _set, _tick, fx)
from tests.agent_teams.test_scope_amendments import db  # noqa: F401

pytestmark = pytest.mark.asyncio
CEILING = (waits.CEILING_STARTED, waits.CEILING_ENDED)


async def _events(fx, kinds=CEILING):
    async with fx.maker() as session:
        return (await session.scalars(select(FactoryAuditEvent).where(FactoryAuditEvent.event_kind.in_(kinds))
                                      .order_by(FactoryAuditEvent.id))).all()


async def _refused(fx, *, limit=60, ceiling=70, minutes=15, supervisor=True):
    """A Leader step beyond the ceiling: 10 minutes of room, 15 asked."""
    await _limited(fx, limit=limit)
    await _scope_ceiling(fx, ceiling)
    if supervisor:
        await _bind_supervisor(fx)
    assert await _code(_extend(fx, minutes)) == "extension_exceeds_ceiling"


async def _change_scope(fx, monkeypatch, **values):
    """The operator's scope update route, as the API runs it (no access-path context: the plain operator)."""
    monkeypatch.setattr(agent_teams, "_sync_github_jobs", AsyncMock())
    async with fx.maker() as session:
        await agent_teams.update_github_scope(fx.scope_id, TeamGithubScopeUpdate(**values), None, session, None)


# The start
async def test_the_ceiling_notice_starts_one_wait_on_the_supervisor_keyed_by_its_message_id(fx):
    await _refused(fx)
    assert await _code(_extend(fx, 12)) == "extension_exceeds_ceiling"  # the same attempt: no second wait
    (notice,) = await _ceiling_notices(fx)
    (start,) = await _events(fx)
    assert start.operation_id == f"ceiling_wait_started:message:{notice.id}"
    assert (start.item_id, start.fact_time, start.actor_kind, start.actor_member_id) == (
        fx.item_id, notice.created_at, "member", fx.leader)
    assert start.after_values == {"wait_message_id": notice.id, "request_kind": "effort_ceiling",
                                  "owner_member_id": fx.leader, "awaited_member_id": None,
                                  "awaited_role": "supervisor", "awaited_actor_id": 6}
    assert fx.nonce not in str(start.after_values)


async def test_no_supervisor_means_no_notice_and_no_wait(fx):
    await _refused(fx, supervisor=False)
    assert await _ceiling_notices(fx) == [] and await _events(fx) == []


async def test_the_notice_and_its_wait_commit_together(fx, monkeypatch):
    from app.services import github_effort_limit_service as effort

    async def interrupted(*_args, **_kwargs):
        raise RuntimeError("the audit write failed")

    monkeypatch.setattr(effort.decision_waits, "record_ceiling_wait_started", interrupted)
    await _refused(fx)
    assert await _ceiling_notices(fx) == [] and await _events(fx) == []


async def test_the_notice_is_no_mail_wait_and_the_leaders_effort_wait_stays_its_own(fx):
    await _refused(fx)
    mail = await _events(fx, (waits.MAIL_STARTED, waits.EFFORT_STARTED))
    assert mail == []  # #1240: the notice has no request_status, so it is counted once, here
    async with fx.maker() as session:
        (wait,) = [w for w in await waits.unended_decision_waits(session, now=T0) if w.wait_kind == "ceiling"]
    (notice,) = await _ceiling_notices(fx)
    assert (wait.request_id, wait.awaited_actor_id, wait.state) == (notice.id, 6, "open")


# Open and unknown
async def test_an_unanswered_wait_stays_open_with_its_age(fx):
    await _refused(fx)
    (notice,) = await _ceiling_notices(fx)
    later = notice.created_at + timedelta(minutes=40)
    async with fx.maker() as session:
        (wait,) = [w for w in await waits.unended_decision_waits(session, now=later) if w.wait_kind == "ceiling"]
    assert (wait.state, wait.age_seconds, wait.request_kind, wait.owner_member_id) == (
        "open", 40 * 60, "effort_ceiling", fx.leader)


async def test_a_deleted_item_makes_its_wait_unknown(fx):
    await _refused(fx)
    async with fx.maker() as session:
        await session.execute(text("DELETE FROM github_work_items WHERE id = :id"), {"id": fx.item_id})
        await session.commit()
        (wait,) = [w for w in await waits.unended_decision_waits(session, now=T0) if w.wait_kind == "ceiling"]
    assert (wait.state, wait.age_seconds, wait.reason) == ("unknown", None, "work item deleted")


# A ceiling the asked step now fits under
async def test_a_ceiling_raised_enough_ends_the_wait_with_its_actor_and_evidence(fx, monkeypatch):
    await _refused(fx)
    await _change_scope(fx, monkeypatch, max_item_effort_minutes=75)
    await _change_scope(fx, monkeypatch, max_item_effort_minutes=90)  # once
    start, end = await _events(fx)
    async with fx.maker() as session:
        policy = await session.scalar(select(FactoryAuditEvent).where(FactoryAuditEvent.event_kind == "policy_change"))
    assert end.operation_id == start.operation_id.replace("started", "ended", 1)
    assert end.item_context_key == start.item_context_key and end.fact_time >= start.fact_time
    assert (end.actor_kind, end.actor_member_id) == ("operator", None)  # the operator, never assumed a person
    assert end.after_values == {
        "wait_message_id": start.after_values["wait_message_id"], "wait_outcome": "ceiling_raised",
        "answered_by_member_id": None, "max_item_effort_minutes": 75}
    assert end.before_values == {"max_item_effort_minutes": 70} and end.correlation_id == policy.correlation_id


@pytest.mark.parametrize("values", [{"max_item_effort_minutes": 74}, {"max_item_effort_minutes": 65},
                                    {"max_concurrent_dispatched": 3}])
async def test_a_change_that_does_not_fit_the_asked_step_leaves_the_wait_open(fx, monkeypatch, values):
    await _refused(fx)
    await _change_scope(fx, monkeypatch, **values)
    assert [e.event_kind for e in await _events(fx)] == [waits.CEILING_STARTED]


async def test_the_room_counts_extensions_granted_since_the_notice(fx, monkeypatch):
    await _refused(fx)
    await _set(fx, effort_extension_minutes=10)  # the Leader used the old room after the notice
    await _change_scope(fx, monkeypatch, max_item_effort_minutes=80)  # 10 minutes of room: 15 still don't fit
    assert [e.event_kind for e in await _events(fx)] == [waits.CEILING_STARTED]
    await _change_scope(fx, monkeypatch, max_item_effort_minutes=85)
    assert [e.event_kind for e in await _events(fx)] == list(CEILING)


async def test_another_scopes_change_leaves_the_wait_open(fx, monkeypatch):
    await _refused(fx)
    async with fx.maker() as session:
        scope = await session.get(TeamGithubScope, fx.scope_id)
        other = TeamGithubScope(preset_id=scope.preset_id, repo_owner="example", repo_name="other",
                                repo_path="/tmp/other-scope", base_ref=scope.base_ref, max_item_effort_minutes=70)
        session.add(other)
        await session.commit()
        other_id = other.id
    monkeypatch.setattr(agent_teams, "_sync_github_jobs", AsyncMock())
    async with fx.maker() as session:
        await agent_teams.update_github_scope(
            other_id, TeamGithubScopeUpdate(max_item_effort_minutes=120), None, session, None)
    assert [e.event_kind for e in await _events(fx)] == [waits.CEILING_STARTED]


async def _replace_notice(fx, *, delivery_key, payload):
    """The notice's Mail row is gone and its id reused (SQLite reuses a deleted row's id)."""
    (notice,) = await _ceiling_notices(fx)
    async with fx.maker() as session:
        await session.execute(delete(MailMessage).where(MailMessage.id == notice.id))
        session.add(MailMessage(id=notice.id, kind="message", sender_type="system", audience_type="team_preset",
                                audience_id="1", subject="Other", body_markdown="x", payload=payload,
                                delivery_key=delivery_key))
        await session.commit()


@pytest.mark.parametrize("key,payload", [
    ("github-effort-ceiling:999:other-attempt:operator", {"requested_minutes": 1}),  # another item's notice
    (None, {}),  # this item's key, but no asked step
])
async def test_a_notice_that_cannot_be_read_is_no_evidence(fx, monkeypatch, key, payload):
    await _refused(fx)
    await _replace_notice(fx, delivery_key=key or f"github-effort-ceiling:{fx.item_id}:{fx.nonce}:operator",
                          payload=payload)
    await _change_scope(fx, monkeypatch, max_item_effort_minutes=120)
    await _tick(fx, T0 + timedelta(minutes=1))
    assert [e.event_kind for e in await _events(fx)] == [waits.CEILING_STARTED]


# An end-stopped disposition: the "stop the work" option
async def test_ending_stopped_work_ends_the_wait_with_its_actor_and_outcome(db, tmp_path, monkeypatch):
    from tests.agent_teams.test_end_stopped_work_item import (
        OPERATOR,
        _api,
        _body,
        _stopped,
    )

    preset, _scope, item, workspace = await _stopped(db, tmp_path)
    await db.delete(workspace)
    notice = MailMessage(kind="message", sender_type="system", audience_type="team_preset",
                         audience_id=str(preset.id), subject="Effort ceiling reached", body_markdown="x",
                         payload={"requested_minutes": 15, "supervisor_actor_id": 6},
                         delivery_key=f"github-effort-ceiling:{item.id}:attempt-7:operator")
    db.add(notice)
    await waits.record_ceiling_wait_started(db, notice, item_id=item.id, leader_member_id=None, source="test")
    await db.commit()
    async with _api(db, monkeypatch) as client:
        for _ in range(2):  # a replay adds nothing
            response = await client.post(f"/presets/{preset.id}/work-items/{item.id}/end-stopped",
                                         json=_body(item.id, "deferred"), headers=OPERATOR)
            assert response.status_code == 200
    ends = (await db.scalars(select(FactoryAuditEvent).where(FactoryAuditEvent.event_kind == waits.CEILING_ENDED))).all()
    stopped = await db.scalar(select(FactoryAuditEvent).where(FactoryAuditEvent.event_kind == "stopped_work_item_ended"))
    (end,) = ends
    assert (end.actor_kind, end.actor_member_id) == ("operator", None)
    assert end.after_values == {"wait_message_id": notice.id, "wait_outcome": "work_stopped",
                                "answered_by_member_id": None, "outcome": "deferred"}
    assert end.correlation_id == stopped.operation_id


# The item's end and the attempt's replacement
@pytest.mark.parametrize("status", ["merged", "cancelled"])
async def test_an_ended_item_ends_the_wait_unattributed(fx, status):
    await _refused(fx)
    await _set(fx, dispatch_status=status)
    seen = (await _ceiling_notices(fx))[0].created_at + timedelta(minutes=3)
    await _tick(fx, seen)
    await _tick(fx, seen + timedelta(minutes=1))  # once
    _start, end = await _events(fx)
    assert (end.actor_kind, end.fact_time) == ("system", seen)
    assert end.after_values["wait_outcome"] == "item_terminal" and end.after_values["answered_by_member_id"] is None
    assert end.after_values["dispatch_status"] == status


async def test_an_escalated_item_keeps_its_wait_open(fx):
    await _refused(fx)
    await _set(fx, dispatch_status="escalated")
    await _tick(fx, T0 + timedelta(minutes=3))
    assert [e.event_kind for e in await _events(fx)] == [waits.CEILING_STARTED]


async def test_a_replaced_attempt_ends_its_wait_unattributed(fx):
    await _refused(fx)
    await _tick(fx, T0 + timedelta(minutes=1))  # the same attempt: still open
    assert [e.event_kind for e in await _events(fx)] == [waits.CEILING_STARTED]
    await _set(fx, dispatch_nonce="the-next-attempt")
    seen = (await _ceiling_notices(fx))[0].created_at + timedelta(minutes=5)
    await _tick(fx, seen)
    await _tick(fx, seen + timedelta(minutes=1))  # once
    _start, end = await _events(fx)
    assert (end.actor_kind, end.fact_time, end.after_values["wait_outcome"]) == ("system", seen, "attempt_ended")
    assert end.after_values == {"wait_message_id": _start.after_values["wait_message_id"],
                                "wait_outcome": "attempt_ended", "answered_by_member_id": None}


async def test_a_mail_id_reused_by_another_item_is_another_wait(fx):
    await _refused(fx)
    (notice,) = await _ceiling_notices(fx)
    async with fx.maker() as session:
        item = await session.get(GithubWorkItem, fx.item_id)
        other = GithubWorkItem(scope_id=item.scope_id, issue_number=item.issue_number + 1, issue_title="Other",
                               issue_url="https://example.invalid/issues/2", github_updated_at=item.github_updated_at,
                               dispatch_status="dispatched", dispatch_nonce="other-attempt")
        session.add(other)
        await session.flush()
        await session.execute(delete(MailMessage).where(MailMessage.id == notice.id))
        reused = MailMessage(id=notice.id, kind="message", sender_type="system", audience_type="team_preset",
                             audience_id="1", subject="Effort ceiling reached", body_markdown="x",
                             payload={"requested_minutes": 5, "supervisor_actor_id": 6},
                             delivery_key=f"github-effort-ceiling:{other.id}:other-attempt:operator")
        session.add(reused)
        await waits.record_ceiling_wait_started(session, reused, item_id=other.id, leader_member_id=fx.leader,
                                                source="test")
        await session.commit()
        other_id = other.id
    starts = [e for e in await _events(fx) if e.event_kind == waits.CEILING_STARTED]
    assert [e.item_id for e in starts] == [fx.item_id, other_id]
    assert starts[0].item_context_key != starts[1].item_context_key
