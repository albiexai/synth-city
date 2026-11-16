"""The Leader runs three standard recoveries with the operator guards, one audit fact and one Supervisor notice."""
import asyncio
from datetime import datetime, timedelta
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
    a.workspace_leased_at = a.workspace.leased_at
    monkeypatch.setattr(lr.github_workspace_service, "delegated_release_blocker", AsyncMock(return_value=None))
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
        monkeypatch.setattr(lr.github_workspace_service, "delegated_release_blocker",
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
    monkeypatch.setattr(lr.github_workspace_service, "delegated_release_blocker",
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


# --- review 5919432941: one complete correction batch ------------------------------------------------------

import subprocess

from app.models.database import GithubOwnerObservationPause


async def _pause(db, a, status):
    db.add(GithubOwnerObservationPause(work_item_id=a.item.id, wait_key=f"wait-{status}", authority_sha256="x",
                                       status=status, deadline=datetime.utcnow() + timedelta(minutes=5)))
    await db.commit()


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                           *args], check=True, capture_output=True, text=True).stdout.strip()


async def _act(db, a, action, over=None):
    over = over or {}
    if action == "cancel":
        return await _cancel(db, a, a.leader_session, **over)
    if action == "release":
        return await _release(db, a, a.leader_session, **over)
    read = _merged_reader(a)
    if not hasattr(a, "digest"):  # the digest of the first read binds the replay; a terminal item has no preview
        a.digest = (await lr.finalize_preview(db, a.item, a.leader_session, read_pull=read))["context_sha256"]
    await db.refresh(a.item)
    await db.refresh(a.leader_session)
    return await lr.finalize_merged(db, a.item, a.leader_session, read_pull=read, operation_id="fin-9",
                                    expected_context_sha256=a.digest, reason=REASON)


@pytest.fixture
def primary_release(db, releasable, monkeypatch):
    """Real inspection of a real disposable repository: nothing about the probe is mocked."""
    monkeypatch.undo()  # drop the releasable fixture's probe mock
    return releasable


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["waiting", "paused"])
@pytest.mark.parametrize("action", ["cancel", "finalize", "release"])
async def test_a_recorded_observation_pause_refuses_every_delegated_action(db, releasable, action, status):
    a = releasable
    if action != "release":
        a.item.dispatch_status = "verifying"
        await db.commit()
    await _pause(db, a, status)
    with pytest.raises(lr.LeaderRecoveryError) as error:
        await _act(db, a, action)
    assert error.value.code == "observation_pause_active" and await _notices(db) == []
    await db.refresh(a.item)
    assert a.item.dispatch_status != "merged" or action == "release"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["hold", "supervisor", "autonomy", "pause"])
async def test_authority_that_changes_during_the_awaited_pull_read_stops_the_finalize_write(db, ready, monkeypatch, change):
    a = ready
    flipped = {"done": False}

    async def read():
        pull = await _merged_reader(a)()
        if not flipped["done"] and flipped.setdefault("calls", 0) >= 1:
            flipped["done"] = True
            if change == "hold":
                monkeypatch.setattr(lr, "hold_code", lambda: "operator_hold")
            elif change == "supervisor":
                a.preset.supervisor_actor_id = None
                await db.commit()
            elif change == "pause":
                await _pause(db, a, "waiting")
            else:
                a.preset.autonomy_enabled = False
                await db.commit()
        flipped["calls"] += 1
        return pull
    digest = (await lr.finalize_preview(db, a.item, a.leader_session, read_pull=_merged_reader(a)))["context_sha256"]
    flipped["calls"] = 1
    with pytest.raises(lr.LeaderRecoveryError):
        await lr.finalize_merged(db, a.item, a.leader_session, read_pull=read, operation_id="fin-3",
                                 expected_context_sha256=digest, reason=REASON)
    await db.refresh(a.item)
    assert a.item.dispatch_status != "merged" and await _notices(db) == []


@pytest.mark.asyncio
async def test_an_item_that_stops_being_terminal_during_the_clean_probe_keeps_its_lease(db, releasable, monkeypatch):
    a = releasable

    async def probe(*_args):
        a.item.dispatch_status = "dispatched"
        await db.commit()
        return None
    monkeypatch.setattr(lr.github_workspace_service, "delegated_release_blocker", probe)
    item_id = a.item.id
    with pytest.raises(lr.LeaderRecoveryError):
        await _release(db, a, a.leader_session)
    await db.refresh(a.workspace)
    assert a.workspace.leased_item_id == item_id and await _notices(db) == []


@pytest.mark.asyncio
async def test_a_hold_raised_during_the_cancel_head_read_stops_the_cancellation(db, ready, monkeypatch):
    a = ready
    real = lr.github_approval_service._active_cancellation_head_sha

    async def head(*args, **kwargs):
        monkeypatch.setattr(lr, "hold_code", lambda: "operator_hold")
        return await real(*args, **kwargs)
    monkeypatch.setattr(lr.github_approval_service, "_active_cancellation_head_sha", head)
    with pytest.raises(lr.LeaderRecoveryError):
        await _cancel(db, a, a.leader_session)
    await db.refresh(a.revision)
    assert a.revision.status == "submitted" and await _notices(db) == []


def _prepare_primary(a, repo_state):
    a.workspace.kind = "primary"
    base = _git(a.repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines()
    a.scope.base_ref = next(b for b in base if b != a.item.dispatch_head_ref)
    if repo_state == "dirty":
        (a.repo / "scratch.txt").write_text("not committed\n")
    elif repo_state == "unpublished":
        (a.repo / "work.txt").write_text("committed but not on the base\n")
        _git(a.repo, "add", ".")
        _git(a.repo, "commit", "-qm", "unpublished")
    elif repo_state == "missing":
        a.workspace.path = str(a.repo) + "-gone"


@pytest.mark.asyncio
@pytest.mark.parametrize("repo_state", ["dirty", "unpublished", "missing"])
async def test_a_primary_workspace_is_really_inspected_and_unclean_ones_are_refused(db, releasable, monkeypatch, repo_state):
    a = releasable
    monkeypatch.undo()
    _prepare_primary(a, repo_state)
    await db.commit()
    with pytest.raises(lr.LeaderRecoveryError) as error:
        await _release(db, a, a.leader_session)
    assert error.value.code == "workspace_not_clean"
    await db.refresh(a.workspace)
    assert a.workspace.leased_item_id == a.item.id and await _notices(db) == []


@pytest.mark.asyncio
async def test_a_clean_primary_workspace_is_released_after_the_real_inspection(db, releasable, monkeypatch):
    a = releasable
    monkeypatch.undo()
    _prepare_primary(a, "clean")
    await db.commit()
    result = await _release(db, a, a.leader_session)
    assert result["status"] == "released"


@pytest.mark.asyncio
async def test_finalize_records_the_authenticated_leader_once_even_on_replay(db, ready):
    a = ready
    leader_id, session_id = a.leader.id, a.leader_session.id
    await _act(db, a, "finalize")
    await _act(db, a, "finalize")
    facts = (await db.execute(select(FactoryAuditEvent).where(
        FactoryAuditEvent.event_kind == "leader_recovery_finalize"))).scalars().all()
    assert len(facts) == 1
    assert (facts[0].actor_kind, facts[0].actor_member_id, facts[0].actor_session_id) == (
        "member", leader_id, session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("db", ["concurrent"], indirect=True)
@pytest.mark.parametrize("action", ["finalize", "release"])
async def test_the_losing_concurrent_claim_is_a_clean_replay_or_refusal(db, releasable, monkeypatch, action):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.database import GithubWorkItem
    a = releasable
    if action == "finalize":
        a.item.dispatch_status = "verifying"
        await db.commit()
    barrier = asyncio.Barrier(2)
    real = lr._claim

    async def synced(*args, **kwargs):
        await barrier.wait()
        return await real(*args, **kwargs)
    monkeypatch.setattr(lr, "_claim", synced)
    other = async_sessionmaker(db.bind, expire_on_commit=False)()
    try:
        item2 = await other.get(GithubWorkItem, a.item.id)
        session2 = await other.get(MailAgentSession, a.leader_session.id)
        if action == "release":
            call = lambda d, i, s: lr.release_workspace(d, i, s, workspace_id=a.workspace.id,  # noqa: E731
                expected_leased_at=a.workspace.leased_at, operation_id="rel-c", reason=REASON)
        else:
            read = _merged_reader(a)
            digest = (await lr.finalize_preview(db, a.item, a.leader_session, read_pull=read))["context_sha256"]
            call = lambda d, i, s: lr.finalize_merged(d, i, s, read_pull=read, operation_id="fin-c",  # noqa: E731
                expected_context_sha256=digest, reason=REASON)
        results = await asyncio.gather(call(db, a.item, a.leader_session), call(other, item2, session2),
                                       return_exceptions=True)
    finally:
        await other.close()
    for r in results:
        assert isinstance(r, (dict, lr.LeaderRecoveryError)), r  # never an uncaught ORM error
    assert any(isinstance(r, dict) for r in results) and len(await _notices(db)) == 1


@pytest.mark.asyncio
async def test_a_residual_race_is_a_clean_409_not_an_uncaught_orm_error(db, ready, monkeypatch):
    from fastapi import HTTPException
    from sqlalchemy.exc import MissingGreenlet
    from app.api.v1 import agent_teams
    a = ready
    item_id = a.item.id
    monkeypatch.setattr(lr, "cancel_revision", AsyncMock(side_effect=MissingGreenlet("expired row")))
    with pytest.raises(HTTPException) as error:
        await agent_teams.leader_cancel_revision(item_id, lr.CancelRevisionRequest(revision_number=1,
            dispatch_nonce=a.nonce, operation_id="op-9", reason=REASON), a.leader_session, db)
    assert error.value.status_code == 409 and error.value.detail == "recovery_context_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["hold", "leader_session"])
async def test_the_fence_runs_before_the_credential_revoker_and_the_config_writer(db, releasable, monkeypatch, change):
    """Authority that changes during the last awaited probe stops the release before ANY external effect."""
    a = releasable
    revoker, config_writer = AsyncMock(), AsyncMock()
    monkeypatch.setattr(lr.github_workspace_service, "revoke_push_token", revoker)
    monkeypatch.setattr(lr.github_workspace_service, "remove_managed_worktree_config", config_writer)
    session_id, item_id = a.leader_session.id, a.item.id

    async def probe(*_args):
        if change == "hold":
            monkeypatch.setattr(lr, "hold_code", lambda: "operator_hold")
        else:
            session = await db.get(MailAgentSession, session_id)
            session.closed_at = datetime.utcnow()
            await db.commit()
        return None
    monkeypatch.setattr(lr.github_workspace_service, "delegated_release_blocker", probe)
    with pytest.raises(lr.LeaderRecoveryError):
        await _release(db, a, a.leader_session)
    revoker.assert_not_called()
    config_writer.assert_not_called()
    await db.refresh(a.workspace)
    assert a.workspace.leased_item_id == item_id and await _notices(db) == []


@pytest.mark.asyncio
async def test_the_operator_default_release_path_is_unchanged_without_a_fence(db, releasable, monkeypatch):
    """No before_commit: the shared release core behaves exactly as before for the operator and the scheduler."""
    a = releasable
    ok = await lr.github_workspace_service.force_release_acquisition(db, workspace_id=a.workspace.id,
        scope_id=a.scope.id, item_id=a.item.id, expected_leased_at=a.workspace.leased_at, lease_token="synthetic-lease")
    assert ok is True
    await db.refresh(a.workspace)
    assert a.workspace.leased_item_id is None


@pytest.mark.asyncio
async def test_authority_that_changes_during_the_external_effects_stops_the_commit(db, releasable, monkeypatch):
    """The second fence, right before the commit: a HOLD raised while the credential is revoked keeps the lease row."""
    a = releasable
    item_id = a.item.id

    async def write_config(*_args, **_kwargs):  # the last external mutation: the hold arrives while it runs
        monkeypatch.setattr(lr, "hold_code", lambda: "operator_hold")
    restorer = AsyncMock()
    monkeypatch.setattr(lr.github_workspace_service, "revoke_push_token", AsyncMock())
    monkeypatch.setattr(lr.github_workspace_service, "snapshot_worktree_config", AsyncMock(return_value={"snap": 1}))
    monkeypatch.setattr(lr.github_workspace_service, "remove_managed_worktree_config", AsyncMock(side_effect=write_config))
    monkeypatch.setattr(lr.github_workspace_service, "restore_worktree_config", restorer)
    with pytest.raises(lr.LeaderRecoveryError) as error:
        await _release(db, a, a.leader_session)
    assert error.value.code == "operator_hold"
    restorer.assert_called_once()  # the config removal ran, so the snapshot is restored on rollback
    await db.refresh(a.workspace)
    assert a.workspace.leased_item_id == item_id and await _notices(db) == []  # the database write was rolled back


@pytest.mark.asyncio
async def test_a_replay_that_loses_the_final_compare_and_clear_is_reported_as_already_released(db, releasable, monkeypatch):
    a = releasable
    await _release(db, a, a.leader_session)  # a real release: claim row, applied audit fact, one notice
    await db.refresh(a.workspace)
    # the same lease is visible again to the replay (a stale read), but the compare-and-clear finds nothing to clear
    a.workspace.leased_item_id, a.workspace.leased_at, a.workspace.lease_token = (
        a.item.id, a.workspace_leased_at, "synthetic-lease")
    await db.commit()
    monkeypatch.setattr(lr.github_workspace_service, "force_release_acquisition", AsyncMock(return_value=False))
    again = await _release(db, a, a.leader_session, expected_leased_at=a.workspace_leased_at)
    assert again["status"] == "already_released" and len(await _notices(db)) == 1


# --- review 5920192577: residual findings ---------------------------------------------------------------------

from app.models.database import AgentTeamPreset, GithubMaintenanceEvent


async def _replace_leader_from_another_session(db, a):
    """Another connection commits a replacement Leader slot; the request's own session never sees it by itself."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    other = async_sessionmaker(db.bind, expire_on_commit=False)()
    try:
        preset = await other.get(AgentTeamPreset, a.preset.id)
        preset.leader_slot_id = a.item_owner_slot_id  # a different slot than the current Leader's
        await other.commit()
    finally:
        await other.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("db", ["concurrent"], indirect=True)
@pytest.mark.parametrize("action", ["finalize", "release", "cancel"])
async def test_a_leader_replaced_by_another_session_during_the_probe_stops_the_action(db, releasable, monkeypatch, action):
    a = releasable
    a.item_owner_slot_id = a.item.owner_slot_id
    flipped = {"n": 0}

    async def flip():
        if not flipped["n"]:
            flipped["n"] = 1
            await _replace_leader_from_another_session(db, a)
    if action == "release":
        async def probe(*_args):
            await flip()
            return None
        monkeypatch.setattr(lr.github_workspace_service, "delegated_release_blocker", probe)
    elif action == "cancel":
        a.item.dispatch_status = "verifying"
        await db.commit()
        real = lr.github_approval_service._active_cancellation_head_sha

        async def head(*args, **kwargs):
            await flip()
            return await real(*args, **kwargs)
        monkeypatch.setattr(lr.github_approval_service, "_active_cancellation_head_sha", head)
    else:
        a.item.dispatch_status = "verifying"
        await db.commit()
        base = _merged_reader(a)
        calls = {"n": 0}

        async def read():
            calls["n"] += 1
            if calls["n"] >= 2:  # the digest read is done; this is the action's own second, final read
                await flip()
            return await base()
        a.read = read
    with pytest.raises(lr.LeaderRecoveryError):
        if action == "finalize":
            digest = (await lr.finalize_preview(db, a.item, a.leader_session, read_pull=_merged_reader(a)))["context_sha256"]
            await db.refresh(a.item)
            await db.refresh(a.leader_session)
            await lr.finalize_merged(db, a.item, a.leader_session, read_pull=a.read, operation_id="fin-L",
                                     expected_context_sha256=digest, reason=REASON)
        else:
            await _act(db, a, action)
    await db.refresh(a.item)
    assert await _notices(db) == []
    if action == "release":
        await db.refresh(a.workspace)
        assert a.workspace.leased_item_id == a.item.id
    else:
        assert a.item.dispatch_status != "merged"


@pytest.mark.asyncio
async def test_a_hold_raised_in_the_revoker_stops_the_config_writer_and_skips_a_needless_restore(db, releasable, monkeypatch):
    a = releasable
    item_id = a.item.id
    writer, restorer = AsyncMock(), AsyncMock()

    async def revoke(*_args, **_kwargs):
        monkeypatch.setattr(lr, "hold_code", lambda: "operator_hold")
    monkeypatch.setattr(lr.github_workspace_service, "revoke_push_token", AsyncMock(side_effect=revoke))
    monkeypatch.setattr(lr.github_workspace_service, "snapshot_worktree_config", AsyncMock(return_value={"snap": 1}))
    monkeypatch.setattr(lr.github_workspace_service, "remove_managed_worktree_config", writer)
    monkeypatch.setattr(lr.github_workspace_service, "restore_worktree_config", restorer)
    with pytest.raises(lr.LeaderRecoveryError) as error:
        await _release(db, a, a.leader_session)
    assert error.value.code == "operator_hold"
    writer.assert_not_called()      # the next external mutation never started
    restorer.assert_not_called()    # nothing was touched, so nothing is restored
    await db.refresh(a.workspace)
    assert a.workspace.leased_item_id == item_id and await _notices(db) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("db", ["concurrent"], indirect=True)
@pytest.mark.parametrize("action", ["release", "finalize", "cancel"])
async def test_a_claimant_that_loses_to_its_replay_keeps_the_ledger_and_a_third_request_replays(db, releasable, monkeypatch, action):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.database import GithubWorkItem
    a = releasable
    if action != "release":
        a.item.dispatch_status = "verifying"
        await db.commit()
    workspace_id, item_id, session_id = a.workspace.id, a.item.id, a.leader_session.id  # plain values: rollbacks expire rows
    target, name = {
        "release": (lr.github_workspace_service, "force_release_acquisition"),
        "finalize": (lr, "finalize_merged_item"),
        "cancel": (lr.github_approval_service, "cancel_active_continuation"),
    }[action]
    real = getattr(target, name)
    entered, gate, state = asyncio.Event(), asyncio.Event(), {"first": True}

    async def paused(*args, **kwargs):
        if state["first"]:           # the original claimant stops between its claim and its action
            state["first"] = False
            entered.set()
            await gate.wait()
        return await real(*args, **kwargs)
    monkeypatch.setattr(target, name, paused)
    other = async_sessionmaker(db.bind, expire_on_commit=False)()
    try:
        item2 = await other.get(GithubWorkItem, item_id)
        session2 = await other.get(MailAgentSession, session_id)
        read = _merged_reader(a)
        digest = None
        if action == "finalize":
            digest = (await lr.finalize_preview(db, a.item, a.leader_session, read_pull=read))["context_sha256"]

        def call(d, i, s):
            if action == "release":
                return lr.release_workspace(d, i, s, workspace_id=workspace_id,
                    expected_leased_at=a.workspace_leased_at, operation_id="race-1", reason=REASON)
            if action == "finalize":
                return lr.finalize_merged(d, i, s, read_pull=read, operation_id="race-1",
                    expected_context_sha256=digest, reason=REASON)
            return lr.cancel_revision(d, i, s, revision_number=1, dispatch_nonce=a.nonce,
                operation_id="race-1", reason=REASON)
        first = asyncio.ensure_future(call(db, a.item, a.leader_session))
        await entered.wait()
        second = await call(other, item2, session2)   # the replay completes the whole action and its notice
        gate.set()
        first_result = await asyncio.gather(first, return_exceptions=True)
        assert isinstance(first_result[0], dict), first_result  # the losing claimant answers as the replay outcome
        ledger = (await db.execute(select(GithubMaintenanceEvent).where(
            GithubMaintenanceEvent.operation_id == f"leader-recovery:{ {'release': 'release', 'finalize': 'finalize', 'cancel': 'cancel'}[action] }:race-1"))).scalars().all()
        assert len(ledger) == 1                                 # the shared claim record survives
        third = await call(other, item2, session2)
        assert isinstance(third, dict) and isinstance(second, dict)
    finally:
        await other.close()
    assert len(await _notices(db)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("db", ["concurrent"], indirect=True)
async def test_a_claimant_that_finds_the_lease_gone_after_its_reload_answers_as_the_replay(db, releasable, monkeypatch):
    """The other losing path: the replay finishes between the original claim and the claimant's reload."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.database import GithubWorkItem
    a = releasable
    workspace_id, item_id, session_id, leased_at = a.workspace.id, a.item.id, a.leader_session.id, a.workspace_leased_at
    real = lr._claim
    entered, gate, state = asyncio.Event(), asyncio.Event(), {"first": True}

    async def paused_claim(*args, **kwargs):
        result = await real(*args, **kwargs)
        if state["first"]:
            state["first"] = False
            entered.set()
            await gate.wait()
        return result
    monkeypatch.setattr(lr, "_claim", paused_claim)
    other = async_sessionmaker(db.bind, expire_on_commit=False)()
    try:
        item2 = await other.get(GithubWorkItem, item_id)
        session2 = await other.get(MailAgentSession, session_id)

        def call(d, i, s):
            return lr.release_workspace(d, i, s, workspace_id=workspace_id, expected_leased_at=leased_at,
                                        operation_id="race-2", reason=REASON)
        first = asyncio.ensure_future(call(db, a.item, a.leader_session))
        await entered.wait()
        await call(other, item2, session2)
        gate.set()
        result = (await asyncio.gather(first, return_exceptions=True))[0]
        assert isinstance(result, dict) and result["status"] == "already_released", result
        third = await call(other, item2, session2)
        assert third["status"] == "already_released"
    finally:
        await other.close()
    assert len(await _notices(db)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["cancel", "finalize"])
async def test_abandon_keeps_the_claim_when_the_action_is_recorded_and_frees_it_otherwise(db, ready, kind):
    a = ready
    ctx = lr._ctx(a.item, a.leader_session)
    op = "op-1" if kind == "cancel" else "fin-9"
    proof = dict(revision_number=1) if kind == "cancel" else {}
    if kind == "cancel":
        digest = lr._digest("cancel", ctx.item_id, 1, a.nonce, REASON)
        await _cancel(db, a, a.leader_session)
    else:
        digest = None
        await _act(db, a, "finalize")
    await db.refresh(a.item)
    ctx = lr._ctx(a.item, a.leader_session)
    claim = f"leader-recovery:{kind}:{op}"
    assert await lr._abandon(db, kind, ctx, op, False, **proof) is True   # recorded: the claim stays
    assert await db.scalar(select(GithubMaintenanceEvent.id).where(GithubMaintenanceEvent.operation_id == claim)) is not None
    # an operation id whose action never happened is freed again
    db.add(GithubMaintenanceEvent(operation_id=f"leader-recovery:{kind}:never", work_item_id=ctx.item_id,
                                  outcome="claimed", request_sha256="x"))
    await db.commit()
    never = dict(revision_number=9) if kind == "cancel" else {}  # a revision that was never cancelled
    assert await lr._abandon(db, kind, ctx, "never", False, **never) is False
    assert await db.scalar(select(GithubMaintenanceEvent.id).where(
        GithubMaintenanceEvent.operation_id == f"leader-recovery:{kind}:never")) is None
