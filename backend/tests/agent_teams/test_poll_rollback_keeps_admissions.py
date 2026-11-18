"""#1151: a poll whose stage rolls back keeps its admissions, and the next tick retries the rest (the #920 lesson).

The real watcher admits the labelled issues and the real run_repo_job owns the session. A failure is injected at a
chosen point; every assertion reads from a FRESH session, so only committed rows count. The negative controls at the
end patch behaviour in the test process only (no source edit) and show that these assertions do detect a loss, a
duplicate and a missing finding.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models.database  # noqa: F401
from app.database import Base
from app.models.database import (
    AgentTeamPreset,
    AgentTeamSlot,
    GithubWorkItem,
    OperationsFinding,
    TeamGithubScope,
)
from app.services import github_dispatch_scheduler as scheduler_module
from app.services.github_coordination_service import github_coordination_service
from app.services.github_dispatch_scheduler import GithubDispatchScheduler
from app.services.github_owner_followup_service import github_owner_followup_service
from app.services.github_watcher_service import github_watcher_service
from app.services.operations_health_service import operations_health_service

STAGES = ("dispatch_pending", "monitor_dispatched", "monitor_continuation", "verification", "monitor_recovery",
          "remind_held_leases")
LOCKED = OperationalError("UPDATE", {}, Exception("database is locked"))


@pytest_asyncio.fixture
async def maker(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'poll.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    made = async_sessionmaker(engine, expire_on_commit=False)
    # run_repo_job opens its own session, and the findings use their own: both go to this database.
    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", made)
    monkeypatch.setattr(operations_health_service, "_session", made)
    # Backlog assessment and owner follow-up are other stages, not under test here.
    monkeypatch.setattr(github_coordination_service, "reconcile", AsyncMock())
    monkeypatch.setattr(github_owner_followup_service, "poll", AsyncMock())
    yield made
    await engine.dispose()


async def _add_scope(maker, *, repo="r"):
    async with maker() as db:
        preset = AgentTeamPreset(name=f"team-{datetime.utcnow().timestamp()}", autonomy_enabled=True)
        db.add(preset)
        await db.flush()
        slot = AgentTeamSlot(preset_id=preset.id, position=0, display_name="Owner", provider="codex-cli",
                             repo_id=repo, repo_path=f"/tmp/{repo}", repo_name=repo, role="Leader")
        scope = TeamGithubScope(preset_id=preset.id, repo_owner="o", repo_name=repo, repo_path=f"/tmp/{repo}",
                                dispatch_label="claude-deck-ready", design_label="claude-deck-design",
                                base_ref="origin/master", enabled=True)
        db.add_all([slot, scope])
        await db.flush()
        preset.leader_slot_id = slot.id
        await db.commit()
        return scope.id


def _issue(number):
    return {"number": number, "title": f"Issue {number}", "html_url": f"https://github.com/o/r/issues/{number}",
            "updated_at": "2026-11-17T10:00:00Z", "labels": [{"name": "claude-deck-ready"}], "state": "open"}


class _Client:
    """Lists the labelled issues of the repo; every other read answers 'nothing'."""

    def __init__(self, *numbers):
        self.issues = [_issue(number) for number in numbers]
        self.list_error = None

    def label(self, number):
        self.issues.append(_issue(number))

    async def list_issues_with_label(self, owner, repo, label):
        if self.list_error is not None:
            raise self.list_error
        return list(self.issues)

    def __getattr__(self, name):
        async def nothing(*args, **kwargs):
            return {}
        return nothing


class _Stages:
    """Fake dispatch and verification collaborators: they record the stages that ran and fail on demand."""

    def __init__(self):
        self.calls = []
        self.fail = {}  # stage -> exception, consumed once

    def _run(self, stage, scope_id):
        self.calls.append((stage, scope_id))
        error = self.fail.pop((stage, scope_id), None) or self.fail.pop(stage, None)
        if error is not None:
            raise error

    async def dispatch_pending(self, db, scope, slots, **kwargs):
        self._run("dispatch_pending", scope.id)

    async def monitor_dispatched(self, db, scope, slots):
        self._run("monitor_dispatched", scope.id)

    async def monitor_continuation(self, db, scope, slots):
        self._run("monitor_continuation", scope.id)

    async def process_scope(self, db, scope, client=None):
        self._run("verification", scope.id)

    async def monitor_recovery(self, db, scope, slots):
        self._run("monitor_recovery", scope.id)

    async def remind_held_leases(self, db, scope):
        self._run("remind_held_leases", scope.id)


class _FakeScheduler:
    running = False

    def get_jobs(self):
        return []

    def add_job(self, *args, **kwargs):
        return None

    def remove_job(self, job_id):
        return None


def _build(client, monkeypatch):
    stages = _Stages()
    scheduler = GithubDispatchScheduler(scheduler=_FakeScheduler(), watcher=github_watcher_service,
                                        dispatch=stages, verification=stages)
    monkeypatch.setattr(scheduler_module, "github_client", client)
    return scheduler, stages


async def _tick(scheduler):
    await scheduler.run_repo_job("o", "r")


async def _items(maker):
    async with maker() as db:  # a fresh session: only committed rows are visible
        rows = (await db.execute(select(GithubWorkItem).order_by(GithubWorkItem.scope_id,
                                                                GithubWorkItem.issue_number))).scalars().all()
        return [(row.scope_id, row.issue_number, row.id, row.dispatch_status) for row in rows]


async def _findings(maker):
    async with maker() as db:
        rows = (await db.execute(select(OperationsFinding).where(
            OperationsFinding.kind == "poll_rolled_back"))).scalars().all()
        return [(row.status, row.occurrences, row.detail) for row in rows]


async def _polled(maker, scope_id):
    async with maker() as db:
        return (await db.get(TeamGithubScope, scope_id)).last_polled_at


def _stage_names(stages, scope_id):
    return [stage for stage, scope in stages.calls if scope == scope_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["monitor_dispatched", "verification"])
async def test_a_rollback_after_admission_keeps_the_admissions_and_the_next_tick_runs_the_rest(
        maker, monkeypatch, failing):
    scope_id = await _add_scope(maker)
    scheduler, stages = _build(_Client(1, 2), monkeypatch)
    stages.fail[failing] = LOCKED
    await _tick(scheduler)
    admitted = await _items(maker)
    assert [(scope, number, status) for scope, number, _id, status in admitted] == [
        (scope_id, 1, "pending"), (scope_id, 2, "pending")]
    assert await _polled(maker, scope_id) is not None
    findings = await _findings(maker)
    assert len(findings) == 1 and findings[0][0] == "open" and f"failed in {failing}" in findings[0][2]
    ran = _stage_names(stages, scope_id)
    assert ran[-1] == failing and set(STAGES[STAGES.index(failing) + 1:]).isdisjoint(ran)
    stages.calls.clear()
    await _tick(scheduler)
    assert await _items(maker) == admitted  # same rows and ids: nothing lost, nothing duplicated
    assert _stage_names(stages, scope_id) == list(STAGES)  # the failing stage and every later one ran
    assert [status for status, _count, _detail in await _findings(maker)] == ["resolved"]


@pytest.mark.asyncio
async def test_a_non_database_failure_after_admission_is_kept_the_same_way(maker, monkeypatch):
    await _add_scope(maker)
    scheduler, stages = _build(_Client(1, 2), monkeypatch)
    stages.fail["monitor_dispatched"] = httpx.HTTPStatusError(
        "bad gateway", request=httpx.Request("GET", "http://x"), response=httpx.Response(502))
    await _tick(scheduler)
    admitted = await _items(maker)
    assert [number for _scope, number, _id, _status in admitted] == [1, 2]
    assert len(await _findings(maker)) == 1
    await _tick(scheduler)
    assert await _items(maker) == admitted


@pytest.mark.asyncio
async def test_a_rollback_inside_the_watcher_after_the_admission_commit_keeps_admissions_and_the_poll_time(
        maker, monkeypatch):
    scope_id = await _add_scope(maker)
    scheduler, stages = _build(_Client(1, 2), monkeypatch)
    original = github_watcher_service._recheck_active_items
    broken = {"once": True}

    async def recheck(db, scope, client):
        if broken.pop("once", False):
            raise LOCKED
        return await original(db, scope, client)

    monkeypatch.setattr(github_watcher_service, "_recheck_active_items", recheck)
    await _tick(scheduler)
    admitted = await _items(maker)
    assert [number for _scope, number, _id, _status in admitted] == [1, 2]
    # The poll time must not advance over the work that was discarded.
    assert await _polled(maker, scope_id) is None
    assert stages.calls == []  # the failed watcher stops this tick before any later stage
    findings = await _findings(maker)
    assert len(findings) == 1 and "failed in poll_scope" in findings[0][2]
    await _tick(scheduler)
    assert await _items(maker) == admitted
    assert await _polled(maker, scope_id) is not None
    assert _stage_names(stages, scope_id) == list(STAGES)


@pytest.mark.asyncio
async def test_a_failed_issue_list_before_admission_admits_nothing_and_the_next_tick_admits_everything(
        maker, monkeypatch):
    scope_id = await _add_scope(maker)
    client = _Client(1, 2)
    scheduler, stages = _build(client, monkeypatch)
    client.list_error = httpx.HTTPStatusError(
        "bad gateway", request=httpx.Request("GET", "http://x"), response=httpx.Response(502))
    await _tick(scheduler)
    assert await _items(maker) == []
    assert await _polled(maker, scope_id) is None
    assert stages.calls == []
    assert len(await _findings(maker)) == 1
    client.list_error = None
    await _tick(scheduler)
    assert [number for _scope, number, _id, _status in await _items(maker)] == [1, 2]
    assert await _polled(maker, scope_id) is not None


@pytest.mark.asyncio
async def test_an_upsert_that_fails_after_the_first_issue_discards_the_whole_batch_and_the_next_tick_admits_both(
        maker, monkeypatch):
    await _add_scope(maker)
    scheduler, stages = _build(_Client(1, 2), monkeypatch)
    original = github_watcher_service._upsert_item
    broken = {"once": True}

    async def upsert(db, scope, issue):
        if issue["number"] == 2 and broken.pop("once", False):
            raise LOCKED
        return await original(db, scope, issue)

    monkeypatch.setattr(github_watcher_service, "_upsert_item", upsert)
    await _tick(scheduler)
    assert await _items(maker) == []  # issue 1 was staged but never committed: no half admission
    assert len(await _findings(maker)) == 1
    await _tick(scheduler)
    assert [number for _scope, number, _id, _status in await _items(maker)] == [1, 2]


@pytest.mark.asyncio
async def test_a_newly_labelled_issue_is_admitted_next_to_the_kept_ones(maker, monkeypatch):
    await _add_scope(maker)
    client = _Client(1, 2)
    scheduler, stages = _build(client, monkeypatch)
    stages.fail["monitor_dispatched"] = LOCKED
    await _tick(scheduler)
    kept = await _items(maker)
    client.label(3)
    await _tick(scheduler)
    after = await _items(maker)
    assert after[:2] == kept  # same ids and status
    assert [number for _scope, number, _id, _status in after] == [1, 2, 3]


@pytest.mark.asyncio
async def test_a_failure_in_one_scope_keeps_its_admissions_and_the_next_tick_reaches_the_other_scope(
        maker, monkeypatch):
    first = await _add_scope(maker)
    second = await _add_scope(maker)
    scheduler, stages = _build(_Client(1, 2), monkeypatch)
    stages.fail[("monitor_dispatched", first)] = LOCKED
    await _tick(scheduler)
    kept = await _items(maker)
    assert {scope for scope, *_rest in kept} == {first}  # the second scope was not reached this tick
    assert _stage_names(stages, second) == []
    await _tick(scheduler)
    after = await _items(maker)
    assert [row for row in after if row[0] == first] == kept
    assert [(scope, number) for scope, number, _id, _status in after if scope == second] == [(second, 1), (second, 2)]


# ---- negative controls: the assertions above can fail. Behaviour is patched in this process only. ----------------


@pytest.mark.asyncio
async def test_control_without_commits_the_admissions_are_lost_and_the_fresh_session_sees_it(maker, monkeypatch):
    await _add_scope(maker)
    scheduler, stages = _build(_Client(1, 2), monkeypatch)
    stages.fail["monitor_dispatched"] = LOCKED
    with monkeypatch.context() as patched:
        patched.setattr(AsyncSession, "commit", AsyncMock())
        await _tick(scheduler)
    assert await _items(maker) == []


@pytest.mark.asyncio
async def test_control_a_non_idempotent_admission_fails_the_next_tick_and_the_assertions_see_it(maker, monkeypatch):
    scope_id = await _add_scope(maker)
    scheduler, stages = _build(_Client(1, 2), monkeypatch)
    stages.fail["monitor_dispatched"] = LOCKED
    await _tick(scheduler)
    kept = await _items(maker)

    async def always_insert(db, scope, issue):
        db.add(GithubWorkItem(scope_id=scope.id, issue_number=issue["number"], issue_title=issue["title"],
                              issue_url=issue["html_url"], github_updated_at=datetime.utcnow(),
                              dispatch_status="pending"))

    monkeypatch.setattr(github_watcher_service, "_upsert_item", always_insert)
    stages.calls.clear()
    await _tick(scheduler)
    # The duplicate cannot be stored (one row for each scope and issue) and the tick fails in the watcher:
    # the later stages do not run and the finding records a second occurrence.
    assert _stage_names(stages, scope_id) == []
    assert (await _items(maker)) == kept
    assert (await _findings(maker))[0][1] == 2


@pytest.mark.asyncio
async def test_control_without_the_recorder_no_finding_exists_and_the_assertion_sees_it(maker, monkeypatch):
    await _add_scope(maker)
    scheduler, stages = _build(_Client(1, 2), monkeypatch)
    stages.fail["monitor_dispatched"] = LOCKED
    monkeypatch.setattr(operations_health_service, "record_poll_failure", AsyncMock())
    await _tick(scheduler)
    assert await _findings(maker) == []
