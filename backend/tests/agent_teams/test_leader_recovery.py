"""Leader recoveries keep the operator guards, audit facts and Supervisor notices."""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.v1.deps import require_operator
from app.config import settings
from app.models.database import (
    FactoryAuditEvent, GithubMaintenanceEvent, MailAgentSession, MailExternalActor, MailMessage,
)
from app.services import github_leader_recovery_service as lr
from app.services.github_end_stopped_item_service import EndStoppedItemRequest
from mcp_shim.work_remaining_protocol import public_generation
from tests.agent_teams.test_scope_amendments import db, healthy_attempt, submitted  # noqa: F401

try:
    from fastapi.routing import iter_route_contexts
except ImportError:  # FastAPI before the effective route contexts: included routes are already flat in `routes`
    iter_route_contexts = None

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


@pytest.mark.asyncio
async def test_only_current_leader_ends_stopped_work_and_sends_one_notice(db, ready, monkeypatch):
    attempt = ready
    attempt.item.dispatch_status = "escalated"
    await db.commit()
    monkeypatch.setattr(lr.github_workspace_service, "delegated_release_blocker", AsyncMock(return_value=None))
    request = EndStoppedItemRequest(operation_id="end-stopped", expected_scope_revision=attempt.item.active_scope_revision,
        expected_dispatch_generation=public_generation(attempt.item.id, attempt.nonce), outcome="deferred",
        target_milestone="vNext", reason="Work moved to the next milestone.")
    with pytest.raises(lr.LeaderRecoveryError, match="leader_only"):
        await lr.end_stopped(db, attempt.item, attempt.owner_session, request)
    first = await lr.end_stopped(db, attempt.item, attempt.leader_session, request)
    assert first["status"] == "ended" and first["notice"] == "sent"
    await db.refresh(attempt.revision)
    assert attempt.revision.status == "cancelled"
    again = await lr.end_stopped(db, attempt.item, attempt.leader_session, request)
    assert again["status"] == "already_ended" and again["notice"] == "already_sent"
    assert len(await _notices(db)) == 1


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

RECOVERY = "leader-recovery"
RECOVERY_ROUTES = [
    (f"{settings.api_v1_prefix}/agent-teams/github-work-items/{{item_id}}/leader-recovery/{tail}", ("POST",))
    for tail in ("cancel-revision", "finalize-merged/preview", "finalize-merged", "release-workspace", "end-stopped")
]


def _effective_routes(routes):
    """(full path, methods, effective dependant) of every real route reachable from `routes`.

    FastAPI 0.143 and later keep an included router as an `_IncludedRouter` node, so `router.routes` is no longer flat.
    `iter_route_contexts` is FastAPI's own flattening: it yields each real route with the path that includes every
    include prefix and the dependant that merges include-level and route-level dependencies. Before it existed the
    routes were already flat and effective. Mounts and WebSocket routes carry no methods and are skipped."""
    found = []
    for route in (routes if iter_route_contexts is None else iter_route_contexts(routes)):
        path, methods, dependant = (getattr(route, name, None) for name in ("path", "methods", "dependant"))
        if path is not None and methods is not None and dependant is not None:
            found.append((path, frozenset(methods), dependant))
    return found


def _depends_on(dependant, name):
    """True when the dependant, or any dependency under it at any depth, is a call whose name contains `name`."""
    return any(name in getattr(sub.call, "__name__", "") or _depends_on(sub, name) for sub in dependant.dependencies)


def _assert_recovery_exposure(routes, expected):
    effective = _effective_routes(routes)
    recovery = [(path, methods, dependant) for path, methods, dependant in effective if RECOVERY in path]
    assert sorted((path, tuple(sorted(methods))) for path, methods, _ in recovery) == sorted(expected)
    for path, _, dependant in recovery:
        assert not _depends_on(dependant, "require_operator"), f"{path} depends on require_operator"


def test_operator_only_actions_are_not_exposed_to_the_leader():
    import inspect
    from app.api.v1 import factory_delivery
    from app.main import app
    from mcp_shim import agent_mail_server as shim
    names = {n for n in dir(shim) if n.startswith("synth_city_leader_")}
    assert names == {"synth_city_leader_cancel_revision", "synth_city_leader_end_stopped", "synth_city_leader_finalize_merged",
                     "synth_city_leader_release_workspace"}
    source = inspect.getsource(lr)
    for forbidden in ("max_continuation", "continuation_enabled", "github_publish", "settings.", "approve("):
        assert forbidden not in source  # no policy, limit or approval setter is reachable from the Leader service
    # The exact recovery routes as the app exposes them, all behind the Leader session guard.
    _assert_recovery_exposure(app.routes, RECOVERY_ROUTES)
    # The test must have looked at the operator-protected group: every delivery route is among the effective routes,
    # under the agent-teams prefix, and depends on require_operator.
    effective = _effective_routes(app.routes)
    delivery = [route for route in factory_delivery.router.routes if getattr(route, "methods", None)]
    assert delivery
    for route in delivery:
        full = f"{settings.api_v1_prefix}/agent-teams{route.path}"
        matches = [dependant for path, methods, dependant in effective if path == full and methods == frozenset(route.methods)]
        assert matches, full
        assert all(_depends_on(dependant, "require_operator") for dependant in matches), full


# --- nested routes: the same assertion, run on small trees built with the real include_router --------------------

RECOVERY_PATH = "/github-work-items/{item_id}/leader-recovery/nested"


def _transitive_guard(operator=Depends(require_operator)):  # reaches require_operator only through this dependency
    return operator


def _nested_tree(*, child_dependencies=(), include_dependencies=(), route_dependencies=(), path=RECOVERY_PATH, levels=1):
    child = APIRouter(dependencies=list(child_dependencies))

    @child.post(path, dependencies=list(route_dependencies))
    def nested_endpoint():
        return {}

    tree = child
    for index in range(levels):
        outer = APIRouter()
        outer.include_router(tree, dependencies=list(include_dependencies) if index == 0 else [])
        tree = outer
    return tree.routes


NESTED = [(RECOVERY_PATH, ("POST",))]


def test_nested_routes_guarded_by_the_child_router_are_found_and_fail_the_exposure_check():
    routes = _nested_tree(child_dependencies=[Depends(require_operator)])
    assert [path for path, _, _ in _effective_routes(routes)] == [RECOVERY_PATH]
    with pytest.raises(AssertionError, match="depends on require_operator"):
        _assert_recovery_exposure(routes, NESTED)


def test_nested_routes_guarded_by_the_include_are_found_and_fail_the_exposure_check():
    routes = _nested_tree(include_dependencies=[Depends(require_operator)])
    with pytest.raises(AssertionError, match="depends on require_operator"):
        _assert_recovery_exposure(routes, NESTED)


def test_nested_routes_two_levels_deep_are_found_and_fail_the_exposure_check():
    routes = _nested_tree(include_dependencies=[Depends(require_operator)], levels=2)
    with pytest.raises(AssertionError, match="depends on require_operator"):
        _assert_recovery_exposure(routes, NESTED)


def test_nested_routes_that_reach_the_guard_only_through_another_dependency_fail_the_exposure_check():
    routes = _nested_tree(route_dependencies=[Depends(_transitive_guard)])
    with pytest.raises(AssertionError, match="depends on require_operator"):
        _assert_recovery_exposure(routes, NESTED)


def test_nested_routes_with_an_extra_unguarded_recovery_route_fail_the_inventory():
    routes = _nested_tree()
    with pytest.raises(AssertionError):
        _assert_recovery_exposure(routes, [])           # one recovery route that the inventory does not expect


def test_nested_routes_without_the_guard_and_with_the_expected_inventory_pass():
    _assert_recovery_exposure(_nested_tree(), NESTED)                                    # not over-strict
    _assert_recovery_exposure(_nested_tree(path="/github-work-items/{item_id}/other"), [])   # no recovery route at all


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


# --- #997: HOLD after the fence's own reads, and a claim record that every admitted execution keeps ----------------

@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "finalize", "release"])
async def test_a_hold_raised_during_the_fences_own_reads_stops_the_action(db, releasable, monkeypatch, action):
    """The fence checks HOLD first and then awaits more reads; a HOLD that arrives during them must still stop it."""
    a = releasable
    if action != "release":
        a.item.dispatch_status = "verifying"
        await db.commit()
    revoker, config_writer = AsyncMock(), AsyncMock()
    monkeypatch.setattr(lr.github_workspace_service, "revoke_push_token", revoker)
    monkeypatch.setattr(lr.github_workspace_service, "remove_managed_worktree_config", config_writer)
    real, calls = lr._refuse_observation_pause, {"n": 0}

    async def last_read(*args, **kwargs):
        result = await real(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] >= 2:   # the entry check is the first call; the fence's is the second, its last await
            monkeypatch.setattr(lr, "hold_code", lambda: "operator_hold")
        return result
    monkeypatch.setattr(lr, "_refuse_observation_pause", last_read)
    with pytest.raises(lr.LeaderRecoveryError) as error:
        await _act(db, a, action)
    assert error.value.code == "operator_hold" and calls["n"] >= 2
    revoker.assert_not_called()
    config_writer.assert_not_called()
    assert await _notices(db) == []
    await db.refresh(a.item)
    await db.refresh(a.workspace)
    if action == "release":
        assert a.workspace.leased_item_id == a.item.id
    elif action == "finalize":
        assert a.item.dispatch_status == "verifying"
    else:
        await db.refresh(a.revision)
        assert a.revision.status == "submitted"


def _race_call(action, a, read, digest, d, i, s, reason=REASON):
    if action == "finalize":
        return lr.finalize_merged(d, i, s, read_pull=read, operation_id="race-1", expected_context_sha256=digest,
                                  reason=reason)
    return lr.cancel_revision(d, i, s, revision_number=1, dispatch_nonce=a.nonce, operation_id="race-1", reason=reason)


async def _race_ledger(db, action):
    await db.rollback()
    return (await db.execute(select(GithubMaintenanceEvent).where(
        GithubMaintenanceEvent.operation_id == f"leader-recovery:{action}:race-1"))).scalars().all()


@pytest.mark.asyncio
@pytest.mark.parametrize("db", ["concurrent"], indirect=True)
@pytest.mark.parametrize("action", ["cancel", "finalize"])
async def test_a_replay_that_commits_between_the_proof_read_and_the_cleanup_keeps_its_ledger_row(db, ready, monkeypatch, action):
    """A is refused with no effect; its replay B, already admitted, completes before A's cleanup runs."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.database import GithubWorkItem
    a = ready
    a.item.dispatch_status = "verifying"
    await db.commit()
    item_id, session_id = a.item.id, a.leader_session.id
    read = _merged_reader(a)
    digest = (await lr.finalize_preview(db, a.item, a.leader_session, read_pull=read))["context_sha256"] if action == "finalize" else None
    other = async_sessionmaker(db.bind, expire_on_commit=False)()
    real_fence, real_unclaim = lr._assert_authority, lr._unclaim
    state = {"fence": 0, "cleanup": 0, "replay": None}

    async def fence(*args, **kwargs):
        state["fence"] += 1
        if state["fence"] == 1:   # only the original claimant's first fence refuses
            raise lr.LeaderRecoveryError("operator_hold")
        return await real_fence(*args, **kwargs)

    async def unclaim(*args, **kwargs):
        state["cleanup"] += 1
        if state["cleanup"] == 1:  # the replay was admitted by the claim; it runs to completion right now
            item2 = await other.get(GithubWorkItem, item_id)
            session2 = await other.get(MailAgentSession, session_id)
            state["replay"] = await _race_call(action, a, read, digest, other, item2, session2)
        return await real_unclaim(*args, **kwargs)
    monkeypatch.setattr(lr, "_assert_authority", fence)
    monkeypatch.setattr(lr, "_unclaim", unclaim)
    try:
        first = await asyncio.gather(_race_call(action, a, read, digest, db, a.item, a.leader_session), return_exceptions=True)
        assert state["cleanup"] >= 1 and isinstance(state["replay"], dict), state   # the interleaving really happened
        assert len(await _race_ledger(db, action)) == 1   # the winner's ledger row survives the loser's cleanup
        item2 = await other.get(GithubWorkItem, item_id)
        session2 = await other.get(MailAgentSession, session_id)
        third = await _race_call(action, a, read, digest, other, item2, session2)
        assert isinstance(third, dict)
        item2 = await other.get(GithubWorkItem, item_id, populate_existing=True)   # rollbacks expired the rows
        session2 = await other.get(MailAgentSession, session_id, populate_existing=True)
        with pytest.raises(lr.LeaderRecoveryError) as conflict:   # the same id with another reason is still refused
            await _race_call(action, a, read, digest, other, item2, session2, reason="A different reason.")
        assert conflict.value.code == "operation_conflict"
    finally:
        await other.close()
    assert len(await _notices(db)) == 1 and first


@pytest.mark.asyncio
@pytest.mark.parametrize("db", ["concurrent"], indirect=True)
@pytest.mark.parametrize("action", ["cancel", "finalize"])
async def test_an_admitted_replay_cannot_commit_after_the_claim_it_rests_on_was_cleaned_up(db, ready, monkeypatch, action):
    """A is refused and frees the id while B, admitted as its replay, has not reached its commit; B must refuse."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.database import GithubWorkItem
    a = ready
    a.item.dispatch_status = "verifying"
    await db.commit()
    item_id, session_id = a.item.id, a.leader_session.id
    read = _merged_reader(a)
    digest = (await lr.finalize_preview(db, a.item, a.leader_session, read_pull=read))["context_sha256"] if action == "finalize" else None
    target, name = ((lr, "finalize_merged_item") if action == "finalize"
                    else (lr.github_approval_service, "cancel_active_continuation"))
    real, real_fence = getattr(target, name), lr._assert_authority
    gates = [asyncio.Event(), asyncio.Event()]
    entered = [asyncio.Event(), asyncio.Event()]
    state = {"calls": 0, "fence": 0}

    async def paused(*args, **kwargs):
        index = state["calls"]
        state["calls"] += 1
        entered[index].set()
        await gates[index].wait()
        return await real(*args, **kwargs)

    async def fence(*args, **kwargs):
        state["fence"] += 1
        if state["fence"] == 1:   # A's fence refuses with no effect
            raise lr.LeaderRecoveryError("operator_hold")
        return await real_fence(*args, **kwargs)
    monkeypatch.setattr(target, name, paused)
    monkeypatch.setattr(lr, "_assert_authority", fence)
    other = async_sessionmaker(db.bind, expire_on_commit=False)()
    try:
        item2 = await other.get(GithubWorkItem, item_id)
        session2 = await other.get(MailAgentSession, session_id)
        first = asyncio.ensure_future(_race_call(action, a, read, digest, db, a.item, a.leader_session))
        await entered[0].wait()                         # A claimed the id and stopped at its action
        second = asyncio.ensure_future(_race_call(action, a, read, digest, other, item2, session2))
        await entered[1].wait()                         # B was admitted as A's replay and stopped at its action
        gates[0].set()
        await asyncio.gather(first, return_exceptions=True)   # A is refused and frees the id
        gates[1].set()
        outcome = (await asyncio.gather(second, return_exceptions=True))[0]
    finally:
        await other.close()
    assert isinstance(outcome, lr.LeaderRecoveryError), outcome   # B must not commit on a claim that no longer exists
    assert await _notices(db) == []
    await db.rollback()
    await db.refresh(a.item)
    if action == "finalize":
        assert a.item.dispatch_status == "verifying"
    else:
        await db.refresh(a.revision)
        assert a.revision.status == "submitted"


@pytest.mark.asyncio
async def test_a_release_claim_is_kept_once_the_credential_revoker_may_have_run(db, releasable, monkeypatch):
    """Conservative interim: an uncertain revocation keeps its binding; a corrected request needs a new id."""
    a = releasable
    from app.services.github_workspace_service import GithubWorkspaceCredentialRevokeError
    revoker = AsyncMock(side_effect=GithubWorkspaceCredentialRevokeError("revoke result unknown"))
    monkeypatch.setattr(lr.github_workspace_service, "revoke_push_token", revoker)
    same = dict(workspace_id=a.workspace.id, expected_leased_at=a.workspace_leased_at)   # plain values: rollbacks expire rows
    with pytest.raises(lr.LeaderRecoveryError):
        await _release(db, a, a.leader_session, **same)
    revoker.assert_called()
    await db.rollback()
    claim = await db.scalar(select(GithubMaintenanceEvent).where(
        GithubMaintenanceEvent.operation_id == "leader-recovery:release:rel-1"))
    assert claim is not None                                 # the binding is not lost
    await db.refresh(a.workspace)
    with pytest.raises(lr.LeaderRecoveryError) as conflict:  # a corrected request must not reuse the id
        await _release(db, a, a.leader_session, reason="A corrected reason.", **same)
    assert conflict.value.code == "operation_conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "finalize", "release"])
async def test_a_hold_raised_during_the_entry_checks_own_reads_claims_nothing(db, releasable, monkeypatch, action):
    a = releasable
    if action != "release":
        a.item.dispatch_status = "verifying"
        await db.commit()
    real = lr._refuse_observation_pause

    async def last_read(*args, **kwargs):
        result = await real(*args, **kwargs)
        monkeypatch.setattr(lr, "hold_code", lambda: "operator_hold")   # arrives during the entry check's last read
        return result
    monkeypatch.setattr(lr, "_refuse_observation_pause", last_read)
    with pytest.raises(lr.LeaderRecoveryError) as error:
        await _act(db, a, action)
    assert error.value.code == "operator_hold"
    await db.rollback()
    assert (await db.execute(select(GithubMaintenanceEvent))).scalars().all() == []   # no ledger row, so the id is free


# --- #998: the release execution marker and the retry protocol --------------------------------------------------

class _Crash(BaseException):
    """A process-level stop: it is not an Exception, so no cleanup after it may be assumed."""


async def _claim_outcome(db, op="rel-1"):
    await db.rollback()
    return await db.scalar(select(GithubMaintenanceEvent.outcome).where(
        GithubMaintenanceEvent.operation_id == f"leader-recovery:release:{op}"))


def _same_lease(a):
    return dict(workspace_id=a.workspace.id, expected_leased_at=a.workspace_leased_at)   # plain values: rollbacks expire rows


async def _again(db, a, same, **over):
    await db.rollback()
    await db.refresh(a.workspace)
    return await _release(db, a, a.leader_session, **same, **over)


def _first_fence_refuses(monkeypatch, times=1):
    real, state = lr._assert_authority, {"n": 0}

    async def fence(*args, **kwargs):
        state["n"] += 1
        if state["n"] <= times:
            raise lr.LeaderRecoveryError("operator_hold")
        return await real(*args, **kwargs)
    monkeypatch.setattr(lr, "_assert_authority", fence)
    return state


async def _release_audits(db):
    await db.rollback()
    return (await db.execute(select(FactoryAuditEvent).where(
        FactoryAuditEvent.event_kind == "workspace_release", FactoryAuditEvent.action_outcome == "applied"))).scalars().all()


@pytest.mark.asyncio
@pytest.mark.parametrize("db", ["concurrent"], indirect=True)
async def test_the_executing_marker_is_committed_before_the_revoker_runs(db, releasable, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    a = releasable
    seen = []
    other = async_sessionmaker(db.bind, expire_on_commit=False)()

    async def revoker(*args, **kwargs):
        seen.append(await other.scalar(select(GithubMaintenanceEvent.outcome).where(
            GithubMaintenanceEvent.operation_id == "leader-recovery:release:rel-1")))
    monkeypatch.setattr(lr.github_workspace_service, "revoke_push_token", AsyncMock(side_effect=revoker))
    try:
        result = await _release(db, a, a.leader_session)
    finally:
        await other.close()
    assert result["status"] == "released"
    assert len(seen) == 1 and seen[0].startswith("executing:") and not seen[0].endswith(":u"), seen
    final = await _claim_outcome(db)
    assert final is not None and final.startswith("done:") and final.removeprefix("done:") == seen[0].removeprefix("executing:")


@pytest.mark.asyncio
async def test_a_refusal_before_any_external_effect_frees_the_id_for_a_corrected_request(db, releasable, monkeypatch):
    a = releasable
    revoker = AsyncMock()
    monkeypatch.setattr(lr.github_workspace_service, "revoke_push_token", revoker)
    same = _same_lease(a)
    _first_fence_refuses(monkeypatch)
    with pytest.raises(lr.LeaderRecoveryError) as error:
        await _release(db, a, a.leader_session)
    assert error.value.code == "operator_hold"
    revoker.assert_not_called()
    assert await _claim_outcome(db) is None                   # the attempt provably never started: the id is free
    result = await _again(db, a, same, reason="A corrected reason.")   # the same id, now with a corrected request
    assert result["status"] == "released"
    assert len(await _release_audits(db)) == 1 and len(await _notices(db)) == 1


@pytest.mark.asyncio
async def test_an_unknown_attempt_a_refused_retry_and_a_cleanup_never_release_the_binding(db, releasable, monkeypatch):
    from app.services.github_workspace_service import GithubWorkspaceCredentialRevokeError
    a = releasable
    revoker = AsyncMock(side_effect=GithubWorkspaceCredentialRevokeError("revoke result unknown"))
    monkeypatch.setattr(lr.github_workspace_service, "revoke_push_token", revoker)
    same = _same_lease(a)
    with pytest.raises(lr.LeaderRecoveryError):                 # attempt A: the revoker's result is unknown
        await _release(db, a, a.leader_session)
    first = await _claim_outcome(db)
    assert first.startswith("executing:") and not first.endswith(":u")
    with pytest.raises(lr.LeaderRecoveryError) as conflict:     # a corrected request must not reuse the id
        await _again(db, a, same, reason="A corrected reason.")
    assert conflict.value.code == "operation_conflict"
    calls = revoker.await_count
    _first_fence_refuses(monkeypatch)                           # the exact retry R adopts, then is refused before its revoker
    with pytest.raises(lr.LeaderRecoveryError) as refused:
        await _again(db, a, same)
    assert refused.value.code == "operation_hold" or refused.value.code == "operator_hold"
    adopted = await _claim_outcome(db)
    assert adopted is not None and adopted.startswith("executing:") and adopted.endswith(":u") and adopted != first
    assert revoker.await_count == calls                         # R never started its own revoker
    with pytest.raises(lr.LeaderRecoveryError) as conflict_again:   # the inherited uncertainty still binds the id
        await _again(db, a, same, reason="Another corrected reason.")
    assert conflict_again.value.code == "operation_conflict"
    monkeypatch.setattr(lr.github_workspace_service, "revoke_push_token", AsyncMock())
    done = await _again(db, a, same)                            # the exact retry completes
    assert done["status"] == "released"
    assert len(await _release_audits(db)) == 1 and len(await _notices(db)) == 1


@pytest.mark.asyncio
async def test_a_revoker_that_refuses_on_the_retry_keeps_the_binding_and_sends_no_notice(db, releasable, monkeypatch):
    from app.services.github_workspace_service import GithubWorkspaceCredentialRevokeError
    a = releasable
    revoker = AsyncMock(side_effect=GithubWorkspaceCredentialRevokeError("revoke refused"))
    monkeypatch.setattr(lr.github_workspace_service, "revoke_push_token", revoker)
    same, item_id = _same_lease(a), a.item.id
    for _ in range(2):
        with pytest.raises(lr.LeaderRecoveryError) as error:
            await _again(db, a, same)
        assert error.value.status_code == 503
    outcome = await _claim_outcome(db)
    assert outcome.startswith("executing:") and outcome.endswith(":u")
    assert await _notices(db) == [] and await _release_audits(db) == []
    await db.refresh(a.workspace)
    assert a.workspace.leased_item_id == item_id


@pytest.mark.asyncio
async def test_a_legacy_plain_claim_is_adopted_only_as_inherited_uncertainty(db, releasable, monkeypatch):
    a = releasable
    monkeypatch.setattr(lr.github_workspace_service, "revoke_push_token", AsyncMock())
    same = _same_lease(a)
    item_id = a.item.id
    digest = lr._digest("release", item_id, same["workspace_id"], same["expected_leased_at"].isoformat(), REASON)
    db.add(GithubMaintenanceEvent(operation_id="leader-recovery:release:rel-1", work_item_id=item_id,
                                  outcome="claimed", request_sha256=digest))
    await db.commit()
    _first_fence_refuses(monkeypatch)
    with pytest.raises(lr.LeaderRecoveryError):                 # an exact retry refused before its own revoker
        await _again(db, a, same)
    outcome = await _claim_outcome(db)
    assert outcome is not None and outcome.startswith("executing:") and outcome.endswith(":u")   # never clean again
    with pytest.raises(lr.LeaderRecoveryError) as conflict:
        await _again(db, a, same, reason="A corrected reason.")
    assert conflict.value.code == "operation_conflict"
    assert (await _again(db, a, same))["status"] == "released"


@pytest.mark.asyncio
async def test_a_crash_after_the_marker_keeps_the_binding(db, releasable, monkeypatch):
    a = releasable
    monkeypatch.setattr(lr.github_workspace_service, "revoke_push_token", AsyncMock(side_effect=_Crash()))
    same = _same_lease(a)
    with pytest.raises(_Crash):
        await _release(db, a, a.leader_session)
    outcome = await _claim_outcome(db)
    assert outcome is not None and outcome.startswith("executing:")   # absence of a success audit never proves no effect
    with pytest.raises(lr.LeaderRecoveryError) as conflict:
        await _again(db, a, same, reason="A corrected reason.")
    assert conflict.value.code == "operation_conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize("db", ["concurrent"], indirect=True)
async def test_a_claimant_paused_before_its_marker_answers_as_the_replay_that_released(db, releasable, monkeypatch):
    """A claimed the id and stops before its marker; its exact replay B adopts the row and releases the lease."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.database import GithubWorkItem
    a = releasable
    monkeypatch.setattr(lr.github_workspace_service, "revoke_push_token", AsyncMock())
    workspace_id, leased_at, item_id, session_id = a.workspace.id, a.workspace_leased_at, a.item.id, a.leader_session.id
    real = lr._mark_executing
    entered, gate, state = asyncio.Event(), asyncio.Event(), {"first": True}

    async def paused(*args, **kwargs):
        if state["first"]:
            state["first"] = False
            entered.set()
            await gate.wait()
        return await real(*args, **kwargs)
    monkeypatch.setattr(lr, "_mark_executing", paused)
    other = async_sessionmaker(db.bind, expire_on_commit=False)()
    try:
        item2 = await other.get(GithubWorkItem, item_id)
        session2 = await other.get(MailAgentSession, session_id)
        first = asyncio.ensure_future(lr.release_workspace(db, a.item, a.leader_session, workspace_id=workspace_id,
            expected_leased_at=leased_at, operation_id="rel-1", reason=REASON))
        await asyncio.wait_for(entered.wait(), 10)   # a missing marker step fails here instead of hanging
        assert await other.scalar(select(GithubMaintenanceEvent.outcome).where(
            GithubMaintenanceEvent.operation_id == "leader-recovery:release:rel-1")) == "claimed:r2"   # version 2 clean row
        second = await lr.release_workspace(other, item2, session2, workspace_id=workspace_id,
            expected_leased_at=leased_at, operation_id="rel-1", reason=REASON)
        gate.set()
        outcome = (await asyncio.gather(first, return_exceptions=True))[0]
    finally:
        await other.close()
    assert second["status"] == "released"
    assert isinstance(outcome, dict) and outcome["status"] == "already_released", outcome
    assert len(await _release_audits(db)) == 1 and len(await _notices(db)) == 1
    assert (await _claim_outcome(db)).startswith("done:")           # the release committed with its done row


@pytest.mark.asyncio
@pytest.mark.parametrize("db", ["concurrent"], indirect=True)
async def test_a_clean_attempt_never_reverts_a_marker_that_another_attempt_adopted(db, releasable, monkeypatch):
    """A marks and stops before its lease write; B adopts A's row and its revoker result is unknown; A is then refused."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.database import GithubWorkItem
    from app.services.github_workspace_service import GithubWorkspaceCredentialRevokeError
    a = releasable
    monkeypatch.setattr(lr.github_workspace_service, "revoke_push_token",
                        AsyncMock(side_effect=GithubWorkspaceCredentialRevokeError("revoke result unknown")))
    workspace_id, leased_at, item_id, session_id = a.workspace.id, a.workspace_leased_at, a.item.id, a.leader_session.id
    real_release, real_fence = lr.github_workspace_service.force_release_acquisition, lr._assert_authority
    entered, gate, state = asyncio.Event(), asyncio.Event(), {"release": 0, "fence": 0}

    async def release(*args, **kwargs):
        state["release"] += 1
        if state["release"] == 1:         # A stops after its marker, before its lease write opens
            entered.set()
            await gate.wait()
        return await real_release(*args, **kwargs)

    async def fence(*args, **kwargs):
        state["fence"] += 1
        if state["fence"] == 2:           # the first fence call is B's; the second is A's: A is refused before its revoker
            raise lr.LeaderRecoveryError("operator_hold")
        return await real_fence(*args, **kwargs)
    monkeypatch.setattr(lr.github_workspace_service, "force_release_acquisition", release)
    monkeypatch.setattr(lr, "_assert_authority", fence)
    other = async_sessionmaker(db.bind, expire_on_commit=False)()
    try:
        item2 = await other.get(GithubWorkItem, item_id)
        session2 = await other.get(MailAgentSession, session_id)
        first = asyncio.ensure_future(lr.release_workspace(db, a.item, a.leader_session, workspace_id=workspace_id,
            expected_leased_at=leased_at, operation_id="rel-1", reason=REASON))
        await asyncio.wait_for(entered.wait(), 10)
        with pytest.raises(lr.LeaderRecoveryError) as unknown:   # B adopts A's row; its revoker result is unknown
            await lr.release_workspace(other, item2, session2, workspace_id=workspace_id,
                expected_leased_at=leased_at, operation_id="rel-1", reason=REASON)
        assert unknown.value.status_code == 503
        gate.set()
        refused = (await asyncio.gather(first, return_exceptions=True))[0]
    finally:
        await other.close()
    assert isinstance(refused, lr.LeaderRecoveryError) and state["fence"] == 2, (refused, state)
    outcome = await _claim_outcome(db)
    assert outcome is not None and outcome.startswith("executing:") and outcome.endswith(":u")   # B's binding survives A


async def _stale_replay_across_a_free_and_a_reuse(db, a, monkeypatch, *, corrected_reason):
    """B is admitted as A's replay and pauses before its reload; A is refused cleanly and frees the id; C reuses it."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.database import GithubWorkItem
    monkeypatch.setattr(lr.github_workspace_service, "revoke_push_token", AsyncMock())
    workspace_id, leased_at, item_id, session_id = a.workspace.id, a.workspace_leased_at, a.item.id, a.leader_session.id
    real_mark, real_reload, real_fence = lr._mark_executing, lr._reload, lr._assert_authority
    a_marking, b_reloading = asyncio.Event(), asyncio.Event()
    gate_a, gate_b = asyncio.Event(), asyncio.Event()
    state = {"mark": 0, "fence": 0}
    other = async_sessionmaker(db.bind, expire_on_commit=False)()

    async def mark(*args, **kwargs):
        state["mark"] += 1
        if state["mark"] == 1:              # A claimed the id and stops before its marker
            a_marking.set()
            await gate_a.wait()
        return await real_mark(*args, **kwargs)

    async def reload(session, *rows):
        if session is other:                # B, the replay, stops before it reads the lease again
            b_reloading.set()
            await gate_b.wait()
        return await real_reload(session, *rows)

    async def fence(*args, **kwargs):
        state["fence"] += 1
        if state["fence"] == 1:             # A's fence refuses before its revoker: a clean, no-effect refusal
            raise lr.LeaderRecoveryError("operator_hold")
        return await real_fence(*args, **kwargs)
    monkeypatch.setattr(lr, "_mark_executing", mark)
    monkeypatch.setattr(lr, "_reload", reload)
    monkeypatch.setattr(lr, "_assert_authority", fence)
    try:
        item2 = await other.get(GithubWorkItem, item_id)
        session2 = await other.get(MailAgentSession, session_id)
        call = lambda d, i, s, reason: lr.release_workspace(d, i, s, workspace_id=workspace_id,
            expected_leased_at=leased_at, operation_id="rel-1", reason=reason)
        first = asyncio.ensure_future(call(db, a.item, a.leader_session, REASON))
        await asyncio.wait_for(a_marking.wait(), 10)
        second = asyncio.ensure_future(call(other, item2, session2, REASON))     # admitted as A's replay
        await asyncio.wait_for(b_reloading.wait(), 10)
        gate_a.set()
        assert isinstance((await asyncio.gather(first, return_exceptions=True))[0], lr.LeaderRecoveryError)   # A: clean refusal
        assert await _claim_outcome(db) is None                                  # the id was freed
        await db.refresh(a.item)
        await db.refresh(a.leader_session)
        reused = await lr.release_workspace(db, a.item, a.leader_session, workspace_id=workspace_id,
            expected_leased_at=leased_at, operation_id="rel-1", reason=corrected_reason)   # C reuses the id and releases
        assert reused["status"] == "released"
        gate_b.set()
        return (await asyncio.gather(second, return_exceptions=True))[0]
    finally:
        await other.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("db", ["concurrent"], indirect=True)
async def test_an_old_replay_cannot_speak_for_a_release_by_a_corrected_request(db, releasable, monkeypatch):
    outcome = await _stale_replay_across_a_free_and_a_reuse(db, releasable, monkeypatch, corrected_reason="A corrected reason.")
    assert isinstance(outcome, lr.LeaderRecoveryError) and outcome.code == "recovery_context_changed", outcome
    assert len(await _release_audits(db)) == 1
    notices = await _notices(db)
    assert len(notices) == 1 and "A corrected reason." in notices[0].body_markdown   # only the corrected request's notice


@pytest.mark.asyncio
@pytest.mark.parametrize("db", ["concurrent"], indirect=True)
async def test_an_old_replay_of_the_same_request_after_a_reuse_answers_with_the_one_notice(db, releasable, monkeypatch):
    outcome = await _stale_replay_across_a_free_and_a_reuse(db, releasable, monkeypatch, corrected_reason=REASON)
    assert isinstance(outcome, dict) and outcome["status"] == "already_released", outcome
    assert len(await _release_audits(db)) == 1 and len(await _notices(db)) == 1   # the keyed notice dedupes


@pytest.mark.asyncio
async def test_repeated_fences_write_one_done_row_and_a_foreign_done_row_stops_the_release(db, releasable, monkeypatch):
    a = releasable
    revoker = AsyncMock()
    monkeypatch.setattr(lr.github_workspace_service, "revoke_push_token", revoker)
    real, state = lr._assert_authority, {"calls": 0}

    async def counting(*args, **kwargs):
        state["calls"] += 1
        return await real(*args, **kwargs)
    monkeypatch.setattr(lr, "_assert_authority", counting)
    same, item_id = _same_lease(a), a.item.id
    assert (await _release(db, a, a.leader_session))["status"] == "released"
    assert state["calls"] >= 2                                 # the fence repeats; the done row is written once
    outcome = await _claim_outcome(db)
    assert outcome.startswith("done:") and not outcome.endswith(":u")
    # A second lease: the repeated fence meets a done row that is not this attempt's own and must refuse.
    await db.rollback()
    await db.refresh(a.workspace)
    a.workspace.leased_item_id, a.workspace.leased_at, a.workspace.lease_token = item_id, a.workspace_leased_at, "synthetic-lease-2"
    await db.commit()
    from sqlalchemy import update as sql_update
    state["calls"] = 0

    async def foreign(*args, **kwargs):
        state["calls"] += 1
        result = await real(*args, **kwargs)
        if state["calls"] == 1:                                # inside the writer transaction, after the first fence
            await db.execute(sql_update(GithubMaintenanceEvent).where(
                GithubMaintenanceEvent.operation_id == "leader-recovery:release:rel-2").values(outcome="done:someone-else"))
        return result
    monkeypatch.setattr(lr, "_assert_authority", foreign)
    with pytest.raises(lr.LeaderRecoveryError) as error:
        await _release(db, a, a.leader_session, operation_id="rel-2", reason=REASON, **{k: v for k, v in same.items()})
    assert error.value.code == "recovery_context_changed"
    assert revoker.await_count == 1                            # the second release stopped before its own revoker
    assert len(await _release_audits(db)) == 1                 # and committed nothing
    await db.rollback()
    await db.refresh(a.workspace)
    assert a.workspace.leased_item_id == item_id


@pytest.mark.asyncio
async def test_a_legacy_plain_claimed_row_with_the_committed_release_answers_with_the_keyed_notice_only(db, releasable, monkeypatch):
    a = releasable
    monkeypatch.setattr(lr.github_workspace_service, "revoke_push_token", AsyncMock())
    same = _same_lease(a)
    assert (await _release(db, a, a.leader_session))["status"] == "released"
    await db.rollback()
    row = await db.scalar(select(GithubMaintenanceEvent).where(GithubMaintenanceEvent.operation_id == "leader-recovery:release:rel-1"))
    row.outcome = "claimed"                                     # the #997 interim wrote only plain 'claimed' rows
    await db.commit()
    again = await _again(db, a, same)
    assert again["status"] == "already_released"
    assert len(await _notices(db)) == 1 and len(await _release_audits(db)) == 1


@pytest.mark.asyncio
async def test_an_executing_row_whose_lease_was_released_under_another_id_refuses_and_sends_nothing(db, releasable, monkeypatch):
    from app.services.github_workspace_service import GithubWorkspaceCredentialRevokeError
    a = releasable
    same = _same_lease(a)
    monkeypatch.setattr(lr.github_workspace_service, "revoke_push_token",
                        AsyncMock(side_effect=GithubWorkspaceCredentialRevokeError("revoke result unknown")))
    with pytest.raises(lr.LeaderRecoveryError):                 # rel-1: unknown, the row stays executing
        await _release(db, a, a.leader_session)
    monkeypatch.setattr(lr.github_workspace_service, "revoke_push_token", AsyncMock())
    assert (await _again(db, a, same, operation_id="rel-2"))["status"] == "released"   # another operation id releases the lease
    notices_before = len(await _notices(db))
    with pytest.raises(lr.LeaderRecoveryError) as error:        # the exact retry of rel-1 must not speak for it
        await _again(db, a, same)
    assert error.value.code == "recovery_context_changed"
    assert len(await _notices(db)) == notices_before and len(await _release_audits(db)) == 1


@pytest.mark.asyncio
async def test_a_clean_version_2_row_is_never_answered_as_a_legacy_replay(db, releasable, monkeypatch):
    a = releasable
    monkeypatch.setattr(lr.github_workspace_service, "revoke_push_token", AsyncMock())
    same = _same_lease(a)
    item_id = a.item.id
    assert (await _again(db, a, same, operation_id="rel-2"))["status"] == "released"   # the lease is released under rel-2
    digest = lr._digest("release", item_id, same["workspace_id"], same["expected_leased_at"].isoformat(), REASON)
    db.add(GithubMaintenanceEvent(operation_id="leader-recovery:release:rel-1", work_item_id=item_id,
                                  outcome="claimed:r2", request_sha256=digest))
    await db.commit()
    notices_before = len(await _notices(db))
    with pytest.raises(lr.LeaderRecoveryError) as error:
        await _again(db, a, same)
    assert error.value.code == "recovery_context_changed" and len(await _notices(db)) == notices_before


@pytest.mark.asyncio
@pytest.mark.parametrize("db", ["concurrent"], indirect=True)
async def test_an_attempt_that_completes_after_its_committed_marker_was_adopted_sends_one_notice(db, releasable, monkeypatch):
    """A commits its marker and stops before its lease write; B adopts it and is refused before its revoker; A completes."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.database import GithubWorkItem
    a = releasable
    revoker = AsyncMock()
    monkeypatch.setattr(lr.github_workspace_service, "revoke_push_token", revoker)
    workspace_id, leased_at, item_id, session_id = a.workspace.id, a.workspace_leased_at, a.item.id, a.leader_session.id
    real_release, real_fence = lr.github_workspace_service.force_release_acquisition, lr._assert_authority
    entered, gate, state = asyncio.Event(), asyncio.Event(), {"release": 0, "fence": 0}

    async def release(*args, **kwargs):
        state["release"] += 1
        if state["release"] == 1:         # A: its marker is committed; it stops before its lease write opens
            entered.set()
            await gate.wait()
        return await real_release(*args, **kwargs)

    async def fence(*args, **kwargs):
        state["fence"] += 1
        if state["fence"] == 1:           # the first fence call is B's: B is refused before its own revoker
            raise lr.LeaderRecoveryError("operator_hold")
        return await real_fence(*args, **kwargs)
    monkeypatch.setattr(lr.github_workspace_service, "force_release_acquisition", release)
    monkeypatch.setattr(lr, "_assert_authority", fence)
    other = async_sessionmaker(db.bind, expire_on_commit=False)()
    call = lambda d, i, s: lr.release_workspace(d, i, s, workspace_id=workspace_id, expected_leased_at=leased_at,
                                               operation_id="rel-1", reason=REASON)
    try:
        item2 = await other.get(GithubWorkItem, item_id)
        session2 = await other.get(MailAgentSession, session_id)
        first = asyncio.ensure_future(call(db, a.item, a.leader_session))
        await asyncio.wait_for(entered.wait(), 10)
        a_marker = await other.scalar(select(GithubMaintenanceEvent.outcome).where(
            GithubMaintenanceEvent.operation_id == "leader-recovery:release:rel-1"))
        assert a_marker.startswith("executing:") and not a_marker.endswith(":u")
        with pytest.raises(lr.LeaderRecoveryError) as refused:           # B adopts A's marker, then is refused at its fence
            await call(other, item2, session2)
        assert refused.value.code == "operator_hold"
        assert (await other.scalar(select(GithubMaintenanceEvent.outcome).where(
            GithubMaintenanceEvent.operation_id == "leader-recovery:release:rel-1"))).endswith(":u")   # B's adoption stands
        gate.set()
        completed = (await asyncio.gather(first, return_exceptions=True))[0]
        assert isinstance(completed, dict) and completed["status"] == "released", completed
        item2 = await other.get(GithubWorkItem, item_id, populate_existing=True)
        session2 = await other.get(MailAgentSession, session_id, populate_existing=True)
        replay = await call(other, item2, session2)                      # a later exact replay
    finally:
        await other.close()
    assert replay["status"] == "already_released"
    assert revoker.await_count == 1                                      # only A's revoker ran
    assert len(await _release_audits(db)) == 1 and len(await _notices(db)) == 1
    assert await _claim_outcome(db) == "done:" + a_marker.removeprefix("executing:")   # A's own done row
