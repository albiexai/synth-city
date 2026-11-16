"""The Leader runs three standard recoveries with the operator guards, one audit fact and one Supervisor notice."""
import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.database import (
    FactoryAuditEvent, MailAgentSession, MailExternalActor, MailMessage,
)
from app.services import github_leader_recovery_service as lr
from tests.agent_teams.test_scope_amendments import db, healthy_attempt, submitted  # noqa: F401

REASON = "Standard recovery run by the Leader."


async def _session(db, member):
    return await db.scalar(select(MailAgentSession).where(MailAgentSession.member_id == member.id))


async def _bind_supervisor(db, a):
    actor = MailExternalActor(actor_key="supervisor-fixture", display_name="Supervisor", token_hash="x")
    db.add(actor)
    await db.flush()
    a.preset.supervisor_actor_id = actor.id
    await db.commit()
    return actor


async def _notices(db):
    return (await db.execute(select(MailMessage).where(
        MailMessage.delivery_key.like("leader-recovery:%")))).scalars().all()


async def _cancel(db, a, session, **over):
    values = dict(revision_number=1, dispatch_nonce=a.nonce, operation_id="op-1", reason=REASON)
    values.update(over)
    await db.refresh(a.item)
    await db.refresh(session)  # a rollback expires loaded rows; a request loads them fresh
    return await lr.cancel_revision(db, a.item, session, **values)


@pytest_asyncio.fixture
async def ready(db, healthy_attempt):
    a = healthy_attempt
    a.nonce = a.item.dispatch_nonce
    a.supervisor = await _bind_supervisor(db, a)
    a.revision = await submitted(a, db)
    a.leader_session = await _session(db, a.leader)
    a.owner_session = await _session(db, a.owner)
    return a


# --- cancel ---------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_applies_the_operator_guards_writes_one_audit_fact_and_one_supervisor_notice(db, ready):
    a = ready
    result = await _cancel(db, a, a.leader_session)
    assert result["status"] == "cancelled" and result["notice"] == "sent"
    assert a.item.dispatch_status == "escalated" and a.revision.status == "superseded"
    facts = (await db.execute(select(FactoryAuditEvent).where(
        FactoryAuditEvent.event_kind == "recovery_cancellation",
        FactoryAuditEvent.action_outcome == "applied"))).scalars().all()
    assert len(facts) == 1  # the existing action audit, never a duplicate
    notice = (await _notices(db))
    assert len(notice) == 1
    n = notice[0]
    assert n.sender_member_id == a.leader.id and n.audience_type == "team_preset"
    assert n.audience_id == str(a.preset.id) and n.payload["supervisor_actor_id"] == a.supervisor.id
    assert "lease" not in str(n.payload).lower() and a.item.dispatch_nonce not in n.body_markdown


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["owner", "nonce", "revision", "no_supervisor", "hold", "reason"])
async def test_cancel_refuses_each_guard_without_a_notice_or_a_change(db, ready, monkeypatch, refusal):
    a, session, over = ready, ready.leader_session, {}
    if refusal == "owner":
        session = a.owner_session
    elif refusal == "nonce":
        over["dispatch_nonce"] = "stale"
    elif refusal == "revision":
        over["revision_number"] = 9
    elif refusal == "no_supervisor":
        a.preset.supervisor_actor_id = None
        await db.commit()
    elif refusal == "hold":
        monkeypatch.setattr(lr, "hold_code", lambda: "operator_hold")
    else:
        over["reason"] = "  "
    with pytest.raises(lr.LeaderRecoveryError):
        await _cancel(db, a, session, **over)
    await db.refresh(a.revision)
    assert a.revision.status == "submitted" and await _notices(db) == []


@pytest.mark.asyncio
async def test_cancel_replay_is_exact_and_a_conflicting_replay_is_refused(db, ready):
    a = ready
    await _cancel(db, a, a.leader_session)
    again = await _cancel(db, a, a.leader_session)
    assert again["notice"] == "already_sent" and len(await _notices(db)) == 1
    with pytest.raises(lr.LeaderRecoveryError) as error:
        await _cancel(db, a, a.leader_session, reason="A different reason under the same operation")
    assert error.value.code == "operation_conflict" and len(await _notices(db)) == 1


@pytest.mark.asyncio
async def test_a_crash_after_the_action_and_before_the_notice_is_retryable_with_the_same_operation(db, ready, monkeypatch):
    a = ready
    real = lr._insert_notice
    monkeypatch.setattr(lr, "_insert_notice", AsyncMock(side_effect=RuntimeError("crash before the notice")))
    with pytest.raises(lr.LeaderRecoveryError) as error:
        await _cancel(db, a, a.leader_session)
    assert error.value.code == "recovery_notice_failed" and error.value.status_code == 502
    await db.refresh(a.revision)
    assert a.revision.status == "superseded" and await _notices(db) == []  # the action stays; the failure is visible
    failed = (await db.execute(select(FactoryAuditEvent).where(
        FactoryAuditEvent.correlation_id.like("leader-recovery:%")))).scalars().all()
    assert failed and all(f.action_outcome in {"uncertain", "rejected"} for f in failed)
    monkeypatch.setattr(lr, "_insert_notice", real)
    retried = await _cancel(db, a, a.leader_session)
    assert retried["notice"] == "sent" and len(await _notices(db)) == 1
    await _cancel(db, a, a.leader_session)
    assert len(await _notices(db)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("db", ["concurrent"], indirect=True)
async def test_concurrent_duplicate_notices_insert_exactly_one_row(db, ready):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    a = ready
    other = async_sessionmaker(db.bind, expire_on_commit=False)()
    try:
        args = dict(preset_id=a.preset.id, leader_member_id=a.leader.id, supervisor_actor_id=a.supervisor.id,
                    key="leader-recovery:cancel:1:op-1:digest", subject="s", body="b", payload={})
        sent = await asyncio.gather(lr._insert_notice(db, **args), lr._insert_notice(other, **args))
    finally:
        await other.close()
    assert sorted(sent) == [False, True] and len(await _notices(db)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("db", ["concurrent"], indirect=True)
async def test_concurrent_duplicate_cancel_requests_cancel_once_and_notify_once(db, ready):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.database import GithubWorkItem
    a = ready
    other = async_sessionmaker(db.bind, expire_on_commit=False)()
    try:
        item2 = await other.get(GithubWorkItem, a.item.id)
        session2 = await other.get(MailAgentSession, a.leader_session.id)
        results = await asyncio.gather(
            _cancel(db, a, a.leader_session),
            lr.cancel_revision(other, item2, session2, revision_number=1, dispatch_nonce=a.nonce,
                               operation_id="op-1", reason=REASON),
            return_exceptions=True)
    finally:
        await other.close()
    assert any(isinstance(r, dict) and r["status"] == "cancelled" for r in results), results
    for r in results:  # the loser is a replay or a clean refusal, never a second action or a second notice
        assert isinstance(r, (dict, lr.LeaderRecoveryError)), r
    assert len(await _notices(db)) == 1


# --- release a clean workspace -------------------------------------------------------------------------------

async def _release(db, a, session, **over):
    values = dict(workspace_id=a.workspace.id, expected_leased_at=a.workspace.leased_at, operation_id="rel-1",
                  reason=REASON)
    values.update(over)
    await db.refresh(a.item)
    await db.refresh(session)
    return await lr.release_workspace(db, a.item, session, **values)


@pytest_asyncio.fixture
async def releasable(db, ready, monkeypatch):
    a = ready
    a.item.dispatch_status = "merged"
    await db.commit()
    monkeypatch.setattr(lr.github_workspace_service, "release_blocker", AsyncMock(return_value=None))
    return a


@pytest.mark.asyncio
async def test_release_clears_exactly_the_inspected_lease_and_notifies_once(db, releasable):
    a = releasable
    result = await _release(db, a, a.leader_session)
    assert result["status"] == "released" and result["notice"] == "sent"
    await db.refresh(a.workspace)
    assert a.workspace.leased_item_id is None and len(await _notices(db)) == 1
    again = await _release(db, a, a.leader_session)
    assert again["status"] == "already_released" and again["notice"] == "already_sent"
    assert len(await _notices(db)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["owner", "dirty", "not_terminal", "lease_changed", "other_item",
                                     "no_supervisor", "reason"])
async def test_release_refuses_each_guard_and_keeps_the_lease(db, releasable, monkeypatch, refusal):
    a, session, over = releasable, releasable.leader_session, {}
    if refusal == "owner":
        session = a.owner_session
    elif refusal == "dirty":
        monkeypatch.setattr(lr.github_workspace_service, "release_blocker",
                            AsyncMock(return_value="uncommitted or untracked changes"))
    elif refusal == "not_terminal":
        a.item.dispatch_status = "dispatched"
        await db.commit()
    elif refusal == "lease_changed":
        over["expected_leased_at"] = a.workspace.leased_at - timedelta(seconds=5)
    elif refusal == "other_item":
        a.workspace.leased_item_id = a.item.id + 100
        await db.commit()
    elif refusal == "no_supervisor":
        a.preset.supervisor_actor_id = None
        await db.commit()
    else:
        over["reason"] = ""
    with pytest.raises(lr.LeaderRecoveryError):
        await _release(db, a, session, **over)
    await db.refresh(a.workspace)
    assert a.workspace.lease_token == "synthetic-lease" and await _notices(db) == []


@pytest.mark.asyncio
async def test_a_replay_is_proved_by_the_recorded_release_never_by_a_missing_lease_alone(db, releasable):
    """A claimed operation whose lease vanished without a recorded release is a changed lease, not a replay."""
    from app.models.database import GithubMaintenanceEvent
    a = releasable
    leased_at, workspace_id, item_id = a.workspace.leased_at, a.workspace.id, a.item.id
    digest = lr._digest("release", item_id, workspace_id, leased_at.isoformat(), REASON)
    db.add(GithubMaintenanceEvent(operation_id="leader-recovery:release:rel-1", work_item_id=item_id,
                                  outcome="claimed", request_sha256=digest))
    a.workspace.leased_item_id = None  # cleared with no release audit fact
    await db.commit()
    with pytest.raises(lr.LeaderRecoveryError) as error:
        await _release(db, a, a.leader_session, expected_leased_at=leased_at)
    assert error.value.code == "workspace_lease_changed" and await _notices(db) == []


@pytest.mark.asyncio
async def test_an_unknown_tree_state_is_not_clean(db, releasable, monkeypatch):
    a = releasable
    monkeypatch.setattr(lr.github_workspace_service, "release_blocker",
                        AsyncMock(return_value="workspace status could not be determined"))
    with pytest.raises(lr.LeaderRecoveryError) as error:
        await _release(db, a, a.leader_session)
    assert error.value.code == "workspace_not_clean"


# --- finalize a merged item ---------------------------------------------------------------------------------

def _merged_reader(a):
    async def read_pull():
        return dict(a.pull, state="closed", merged=True, merged_at="2026-11-16T10:00:00Z",
                    merge_commit_sha="c" * 40)
    return read_pull


@pytest.mark.asyncio
async def test_finalize_is_digest_bound_replay_safe_and_notifies_once(db, ready):
    a = ready
    read = _merged_reader(a)
    preview = await lr.finalize_preview(db, a.item, a.leader_session, read_pull=read)
    result = await lr.finalize_merged(db, a.item, a.leader_session, read_pull=read, operation_id="fin-1",
        expected_context_sha256=preview["context_sha256"], reason=REASON)
    assert result["status"] == "finalized" and result["notice"] == "sent"
    await db.refresh(a.item)
    assert a.item.dispatch_status == "merged" and len(await _notices(db)) == 1
    again = await lr.finalize_merged(db, a.item, a.leader_session, read_pull=read, operation_id="fin-1",
        expected_context_sha256=preview["context_sha256"], reason=REASON)
    assert again["status"] == "already_finalized" and len(await _notices(db)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["owner", "not_merged", "stale_digest", "no_supervisor", "reason"])
async def test_finalize_refuses_each_guard_without_a_notice(db, ready, refusal):
    a, session, read = ready, ready.leader_session, _merged_reader(ready)
    digest = (await lr.finalize_preview(db, a.item, session, read_pull=read))["context_sha256"]
    over = dict(expected_context_sha256=digest, reason=REASON)
    if refusal == "owner":
        session = a.owner_session
    elif refusal == "not_merged":
        async def read():  # noqa: F811
            return dict(a.pull, state="open", merged=False, merged_at=None)
    elif refusal == "stale_digest":
        over["expected_context_sha256"] = "0" * 64
    elif refusal == "no_supervisor":
        a.preset.supervisor_actor_id = None
        await db.commit()
    else:
        over["reason"] = " "
    with pytest.raises((lr.LeaderRecoveryError, ValueError)):
        await lr.finalize_merged(db, a.item, session, read_pull=read, operation_id="fin-2", **over)
    await db.refresh(a.item)
    assert a.item.dispatch_status != "merged" and await _notices(db) == []


# --- exposure --------------------------------------------------------------------------------------------------

def test_operator_only_actions_are_not_exposed_to_the_leader():
    import inspect
    from app.api.v1 import agent_teams
    from mcp_shim import agent_mail_server as shim
    names = {n for n in dir(shim) if n.startswith("synth_city_leader_")}
    assert names == {"synth_city_leader_cancel_revision", "synth_city_leader_finalize_merged",
                     "synth_city_leader_release_workspace"}
    source = inspect.getsource(lr)
    for forbidden in ("max_continuation", "continuation_enabled", "github_publish", "settings.", "approve("):
        assert forbidden not in source  # no policy, limit or approval setter is reachable from the Leader service
    routes = {(r.path, tuple(sorted(r.methods))) for r in agent_teams.router.routes
              if "leader-recovery" in r.path}
    assert len(routes) == 4
    for route in agent_teams.router.routes:
        if "leader-recovery" in route.path:
            assert "require_operator" not in str(route.dependant.dependencies)
