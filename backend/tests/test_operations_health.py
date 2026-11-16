"""#941: the factory finds its own stuck states and silent background failures.

Four detectors open a finding, keep it while the condition lasts, and resolve it when it clears. Each opened finding
reaches the Supervisor once, in one transaction with the finding; /health reports it as degraded with reason codes.
Detection uses sessions of its own, against a file database here, so a second connection really is a second writer.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models.database  # noqa: F401
from app.api.v1.operations import list_operations_findings
from app.api.v1.router import health_check
from app.database import Base
from app.main import health as root_health
from app.models.database import (
    AgentTeamPreset, GithubWorkItem, MailMessage, MailReceipt, OperationsFinding, TeamGithubScope,
)
from app.services import github_dispatch_scheduler as scheduler_module
from app.services import operations_health_service as ops_module
from app.services.agent_team_supervisor_service import is_supervisor_message, supervisor_inbox
from app.services.github_dispatch_scheduler import GithubDispatchScheduler
from app.services.github_verification_service import github_verification_service
from app.services.github_watcher_service import GithubWatcherService
from app.services.operations_health_service import (
    ADMISSION_MISSING, ITEM_SKIPPED, MISSING_ADMISSION_POLLS, POLL_ROLLED_BACK, SKIPPED_POLLS, VERIFYING_AFTER_MERGE,
    VERIFYING_AFTER_MERGE_MINUTES, OperationsHealthService,
)


@pytest_asyncio.fixture
async def ops(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ops.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    service = OperationsHealthService()
    service._session = maker
    # The wired singleton (scheduler, watcher, /health) uses the same database and starts from empty counters.
    monkeypatch.setattr(ops_module.operations_health_service, "_session", maker)
    monkeypatch.setattr(ops_module.operations_health_service, "_skips", {})
    monkeypatch.setattr(ops_module.operations_health_service, "_missing", {})
    yield SimpleNamespace(maker=maker, service=service, engine=engine)
    await engine.dispose()


async def _seed(ops, *, supervisor=6, status="verifying", pr_number=5, age_minutes=VERIFYING_AFTER_MERGE_MINUTES + 1):
    async with ops.maker() as db:
        preset = AgentTeamPreset(name="T", description="", created_by="t", supervisor_actor_id=supervisor)
        db.add(preset)
        await db.flush()
        scope = TeamGithubScope(preset_id=preset.id, repo_owner="o", repo_name="r", repo_path="/tmp/r")
        db.add(scope)
        await db.flush()
        item = GithubWorkItem(
            scope_id=scope.id, issue_number=1, issue_title="x", issue_url="u", github_updated_at=datetime.utcnow(),
            dispatch_status=status, dispatch_head_ref="deck/slot-1/issue-1/attempt", dispatch_base_ref="origin/master",
            pr_number=pr_number, updated_at=datetime.utcnow() - timedelta(minutes=age_minutes))
        db.add(item)
        await db.commit()
        return SimpleNamespace(preset_id=preset.id, scope_id=scope.id, item_id=item.id)


class _Client:
    """A GitHub stand-in: every PR is merged (or not) with a merged_at the given minutes ago, unless numbers say otherwise."""

    def __init__(self, merged=True, error=None, merged_minutes_ago=60, with_merged_at=True, merged_numbers=None):
        self.merged, self.error, self.calls = merged, error, 0
        self.merged_minutes_ago, self.with_merged_at, self.merged_numbers = merged_minutes_ago, with_merged_at, merged_numbers
        self.read_numbers = []

    async def get_pull(self, owner, repo, number):
        self.calls += 1
        self.read_numbers.append(number)
        if self.error:
            raise self.error
        merged = self.merged if self.merged_numbers is None else number in self.merged_numbers
        pull = {"number": number, "merged": merged, "state": "closed" if merged else "open"}
        if merged and self.with_merged_at:
            stamp = datetime.utcnow() - timedelta(minutes=self.merged_minutes_ago)
            pull["merged_at"] = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        return pull


async def _findings(ops, kind=None, status=None):
    async with ops.maker() as db:
        query = select(OperationsFinding).order_by(OperationsFinding.id)
        if kind:
            query = query.where(OperationsFinding.kind == kind)
        if status:
            query = query.where(OperationsFinding.status == status)
        return list((await db.scalars(query)).all())


async def _notices(ops):
    async with ops.maker() as db:
        rows = (await db.scalars(select(MailMessage).order_by(MailMessage.id))).all()
        return [m for m in rows if (m.payload or {}).get("notice") == "operations_finding"]


# ---- detector 1: still verifying after the PR merged ----

@pytest.mark.asyncio
async def test_verifying_after_merge_opens_one_finding_and_one_notice_and_resolves(ops):
    seeded = await _seed(ops)
    client = _Client(merged=True)
    for _ in range(4):  # repeated polls
        await ops.service.sweep_verifying_after_merge(seeded.scope_id, "o", "r", client)

    [finding] = await _findings(ops, VERIFYING_AFTER_MERGE, "open")
    assert finding.subject == f"work_item:{seeded.item_id}" and finding.work_item_id == seeded.item_id
    assert finding.occurrences == 4 and finding.notified_at is not None
    assert str(seeded.item_id) in finding.detail and "PR #5" in finding.detail
    [notice] = await _notices(ops)
    assert notice.audience_type == "team_preset" and notice.payload["supervisor_actor_id"] == 6
    assert notice.payload["kind"] == VERIFYING_AFTER_MERGE and notice.payload["finding_id"] == finding.id

    async with ops.maker() as db:  # the item settles: the finding clears
        await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == seeded.item_id).values(dispatch_status="merged"))
        await db.commit()
    await ops.service.sweep_verifying_after_merge(seeded.scope_id, "o", "r", client)
    assert await _findings(ops, status="open") == [] and len(await _findings(ops, status="resolved")) == 1

    async with ops.maker() as db:  # the same wedge again opens a new episode and tells the operator again
        await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == seeded.item_id).values(
            dispatch_status="verifying", updated_at=datetime.utcnow() - timedelta(hours=1)))
        await db.commit()
    await ops.service.sweep_verifying_after_merge(seeded.scope_id, "o", "r", client)
    assert len(await _findings(ops, status="open")) == 1 and len(await _findings(ops)) == 2
    assert len(await _notices(ops)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["recent_merge", "not_merged", "unreadable_pr", "no_pr", "other_status", "no_merged_at"])
async def test_verifying_after_merge_stays_quiet_when_a_condition_is_not_met(ops, case):
    # An old updated_at proves nothing: only GitHub's merged_at starts the clock.
    seeded = await _seed(
        ops, age_minutes=600, pr_number=None if case == "no_pr" else 5,
        status="ready_for_review" if case == "other_status" else "verifying")
    client = _Client(
        merged=case != "not_merged", error=RuntimeError("down") if case == "unreadable_pr" else None,
        merged_minutes_ago=2 if case == "recent_merge" else 60, with_merged_at=case != "no_merged_at")
    await ops.service.sweep_verifying_after_merge(seeded.scope_id, "o", "r", client)
    assert await _findings(ops) == [] and await _notices(ops) == []


@pytest.mark.asyncio
async def test_the_clock_starts_at_the_merge_not_at_the_items_last_change(ops):
    seeded = await _seed(ops, age_minutes=1)  # the item changed a minute ago; the PR merged long before
    await ops.service.sweep_verifying_after_merge(seeded.scope_id, "o", "r", _Client(merged_minutes_ago=VERIFYING_AFTER_MERGE_MINUTES + 1))
    assert len(await _findings(ops, VERIFYING_AFTER_MERGE, "open")) == 1


@pytest.mark.asyncio
async def test_a_merge_just_under_the_threshold_opens_nothing_yet(ops):
    seeded = await _seed(ops)
    await ops.service.sweep_verifying_after_merge(seeded.scope_id, "o", "r", _Client(merged_minutes_ago=VERIFYING_AFTER_MERGE_MINUTES - 1))
    assert await _findings(ops) == []


@pytest.mark.asyncio
async def test_the_merged_sweep_reads_at_most_eight_pull_requests_a_poll(ops):
    seeded = await _seed(ops)
    async with ops.maker() as db:
        for number in range(2, 14):
            db.add(GithubWorkItem(
                scope_id=seeded.scope_id, issue_number=number, issue_title="x", issue_url="u",
                github_updated_at=datetime.utcnow(), dispatch_status="verifying", pr_number=100 + number,
                dispatch_head_ref="h", dispatch_base_ref="origin/master", updated_at=datetime.utcnow() - timedelta(hours=1)))
        await db.commit()
    client = _Client(merged=True)
    await ops.service.sweep_verifying_after_merge(seeded.scope_id, "o", "r", client)
    assert client.calls == 8


async def _add_items(ops, scope_id, numbers):
    async with ops.maker() as db:
        for number in numbers:
            db.add(GithubWorkItem(
                scope_id=scope_id, issue_number=number, issue_title="x", issue_url="u",
                github_updated_at=datetime.utcnow(), dispatch_status="verifying", pr_number=100 + number,
                dispatch_head_ref="h", dispatch_base_ref="origin/master", updated_at=datetime.utcnow()))
        await db.commit()


@pytest.mark.asyncio
async def test_the_merged_sweep_rotates_so_the_first_eight_cannot_starve_the_rest(ops):
    seeded = await _seed(ops, pr_number=None)  # the seeded item has no PR; the twelve below do
    await _add_items(ops, seeded.scope_id, range(2, 14))
    client = _Client(merged_numbers={113})  # only the LAST candidate's PR is merged
    await ops.service.sweep_verifying_after_merge(seeded.scope_id, "o", "r", client)
    assert client.read_numbers == [102, 103, 104, 105, 106, 107, 108, 109] and await _findings(ops) == []
    await ops.service.sweep_verifying_after_merge(seeded.scope_id, "o", "r", client)  # the next poll continues
    assert [f.subject for f in await _findings(ops, VERIFYING_AFTER_MERGE, "open")] == [
        f"work_item:{(await _item_ids(ops))[113]}"]
    assert set(client.read_numbers) == {100 + n for n in range(2, 14)}  # all twelve were read within two polls


async def _item_ids(ops):
    async with ops.maker() as db:
        return {pr: item_id for item_id, pr in (await db.execute(select(GithubWorkItem.id, GithubWorkItem.pr_number))).all()}


@pytest.mark.asyncio
async def test_the_sweep_cursors_are_kept_per_scope(ops):
    first = await _seed(ops, pr_number=None)
    async with ops.maker() as db:
        scope = TeamGithubScope(preset_id=first.preset_id, repo_owner="o", repo_name="other", repo_path="/tmp/o")
        db.add(scope)
        await db.commit()
        second_scope = scope.id
    await _add_items(ops, first.scope_id, range(2, 12))
    await _add_items(ops, second_scope, range(20, 24))
    client_a, client_b = _Client(merged_numbers=set()), _Client(merged_numbers=set())
    await ops.service.sweep_verifying_after_merge(first.scope_id, "o", "r", client_a)
    await ops.service.sweep_verifying_after_merge(second_scope, "o", "other", client_b)
    assert client_a.read_numbers == [102, 103, 104, 105, 106, 107, 108, 109]
    assert client_b.read_numbers == [120, 121, 122, 123]  # the second scope starts from its own beginning


# ---- detector 2: skipped by verification poll after poll ----

@pytest.mark.asyncio
async def test_a_skipped_item_opens_a_finding_at_the_threshold_and_resolves_when_processed(ops):
    seeded = await _seed(ops)
    for _ in range(SKIPPED_POLLS - 1):
        await ops.service.record_verification_skips(seeded.scope_id, {seeded.item_id})
    assert await _findings(ops) == []  # below the threshold: nothing yet

    for _ in range(3):  # the threshold poll and later ones
        await ops.service.record_verification_skips(seeded.scope_id, {seeded.item_id})
    [finding] = await _findings(ops, ITEM_SKIPPED, "open")
    assert finding.work_item_id == seeded.item_id and len(await _notices(ops)) == 1

    await ops.service.record_verification_skips(seeded.scope_id, set())  # verification processed it again
    assert await _findings(ops, status="open") == []
    for _ in range(SKIPPED_POLLS - 1):  # the count starts again from zero
        await ops.service.record_verification_skips(seeded.scope_id, {seeded.item_id})
    assert await _findings(ops, status="open") == []
    await ops.service.record_verification_skips(seeded.scope_id, {seeded.item_id})
    assert len(await _findings(ops, status="open")) == 1 and len(await _notices(ops)) == 2


@pytest.mark.asyncio
async def test_alternating_scopes_each_reach_the_skip_threshold_and_do_not_reset_each_other(ops):
    first = await _seed(ops)
    async with ops.maker() as db:
        scope = TeamGithubScope(preset_id=first.preset_id, repo_owner="o", repo_name="other", repo_path="/tmp/o")
        db.add(scope)
        await db.flush()
        item = GithubWorkItem(scope_id=scope.id, issue_number=2, issue_title="x", issue_url="u",
                              github_updated_at=datetime.utcnow(), dispatch_status="verifying")
        db.add(item)
        await db.commit()
        second_scope, second_item = scope.id, item.id
    for _ in range(SKIPPED_POLLS):  # the scopes are polled in turn; each skips its own item every time
        await ops.service.record_verification_skips(first.scope_id, {first.item_id})
        await ops.service.record_verification_skips(second_scope, {second_item})
    assert sorted(f.work_item_id for f in await _findings(ops, ITEM_SKIPPED, "open")) == sorted([first.item_id, second_item])
    assert len(await _notices(ops)) == 2


@pytest.mark.asyncio
async def test_one_scope_clearing_its_item_does_not_clear_another_scopes_count(ops):
    first = await _seed(ops)
    for _ in range(SKIPPED_POLLS - 1):
        await ops.service.record_verification_skips(first.scope_id, {first.item_id})
    await ops.service.record_verification_skips(first.scope_id + 1000, set())  # another scope's poll processed nothing
    await ops.service.record_verification_skips(first.scope_id, {first.item_id})
    assert len(await _findings(ops, ITEM_SKIPPED, "open")) == 1


@pytest.mark.asyncio
async def test_verification_reports_the_items_it_skipped_in_each_poll(ops):
    async with ops.maker() as db:
        scope = await db.get(TeamGithubScope, (await _seed(ops)).scope_id)
        item = await db.scalar(select(GithubWorkItem))
        github_verification_service.skipped_item_ids = {999}
        await github_verification_service.process_scope(db, scope, client=_Client())
        assert github_verification_service.skipped_item_ids == set()  # reset at the start of each poll
        github_verification_service._log_inconsistent_once(item)
        github_verification_service._log_inconsistent_once(item)  # already logged once, still counted this poll
        assert github_verification_service.skipped_item_ids == {item.id}


# ---- detector 3: a ready issue with no work item ----

@pytest.mark.asyncio
async def test_a_labeled_issue_without_a_work_item_opens_a_finding_and_resolves_when_admitted(ops):
    seeded = await _seed(ops)
    for _ in range(MISSING_ADMISSION_POLLS - 1):
        await ops.service.record_admissions(seeded.scope_id, {1, 7})
    assert await _findings(ops) == []  # issue 1 has an item; issue 7 is not yet over the threshold

    for _ in range(3):
        await ops.service.record_admissions(seeded.scope_id, {1, 7})
    [finding] = await _findings(ops, ADMISSION_MISSING, "open")
    assert finding.subject == f"scope:{seeded.scope_id}:issue:7" and "#7" in finding.detail and len(await _notices(ops)) == 1

    async with ops.maker() as db:  # the admission finally lands
        db.add(GithubWorkItem(scope_id=seeded.scope_id, issue_number=7, issue_title="x", issue_url="u",
                              github_updated_at=datetime.utcnow(), dispatch_status="pending"))
        await db.commit()
    await ops.service.record_admissions(seeded.scope_id, {1, 7})
    assert await _findings(ops, status="open") == [] and len(await _findings(ops, status="resolved")) == 1


@pytest.mark.asyncio
async def test_the_same_issue_number_in_two_scopes_opens_two_findings_and_two_notices(ops):
    first = await _seed(ops)
    async with ops.maker() as db:
        scope = TeamGithubScope(preset_id=first.preset_id, repo_owner="o", repo_name="other", repo_path="/tmp/o")
        db.add(scope)
        await db.commit()
        second = scope.id
    for _ in range(MISSING_ADMISSION_POLLS):
        await ops.service.record_admissions(first.scope_id, {9})
        await ops.service.record_admissions(second, {9})
    assert sorted(f.subject for f in await _findings(ops, ADMISSION_MISSING, "open")) == sorted(
        [f"scope:{first.scope_id}:issue:9", f"scope:{second}:issue:9"])
    assert len(await _notices(ops)) == 2
    async with ops.maker() as db:  # admitting it in one scope resolves only that scope's finding
        db.add(GithubWorkItem(scope_id=second, issue_number=9, issue_title="x", issue_url="u",
                              github_updated_at=datetime.utcnow(), dispatch_status="pending"))
        await db.commit()
    await ops.service.record_admissions(first.scope_id, {9})
    await ops.service.record_admissions(second, {9})
    assert [f.subject for f in await _findings(ops, ADMISSION_MISSING, "open")] == [f"scope:{first.scope_id}:issue:9"]


@pytest.mark.asyncio
async def test_the_watcher_reports_what_it_listed_after_its_admissions_commit(ops, monkeypatch):
    seeded = await _seed(ops)
    watcher = GithubWatcherService()
    monkeypatch.setattr(watcher, "_upsert_item", _async_noop)  # an admission that never lands
    for name in ("_recheck_active_items", "_reconcile_closed_issues", "_reconcile_provisional_results"):
        monkeypatch.setattr(watcher, name, _async_noop)
    from app.services import github_watcher_service as watcher_module
    monkeypatch.setattr(watcher_module.github_dispatch_service, "promote_deferred_retries", _async_noop)
    monkeypatch.setattr(watcher_module.agent_team_campaign_service, "refresh_scope", _async_noop)

    class Lister:
        async def list_issues_with_label(self, owner, repo, label):
            return [{"number": 7, "title": "x", "html_url": "u", "updated_at": "2026-11-15T00:00:00Z", "labels": []}]

    async with ops.maker() as db:
        scope = await db.get(TeamGithubScope, seeded.scope_id)
        for _ in range(MISSING_ADMISSION_POLLS):
            await watcher.poll_scope(db, scope, Lister())
    assert [f.subject for f in await _findings(ops, ADMISSION_MISSING, "open")] == [f"scope:{seeded.scope_id}:issue:7"]


async def _async_noop(*args, **kwargs):
    return None


# ---- detector 4: a poll whose writes were rolled back ----

@pytest.mark.asyncio
async def test_a_failed_poll_opens_one_finding_with_no_error_text_and_a_good_poll_resolves_it(ops):
    await _seed(ops)
    error = RuntimeError("token ghp_secret in the message")
    for _ in range(3):
        await ops.service.record_poll_failure("o", "r", error)
    [finding] = await _findings(ops, POLL_ROLLED_BACK, "open")
    assert finding.subject == "repo:o/r" and finding.occurrences == 3
    assert "RuntimeError" in finding.detail and "ghp_secret" not in finding.detail  # the class name, never the text
    [notice] = await _notices(ops)
    assert "ghp_secret" not in notice.body_markdown and "ghp_secret" not in str(notice.payload)

    await ops.service.record_poll_success("o", "r")
    assert await _findings(ops, status="open") == []
    await ops.service.record_poll_failure("o", "r", error)
    assert len(await _findings(ops, status="open")) == 1 and len(await _notices(ops)) == 2  # a new episode


@pytest.mark.asyncio
async def test_the_scheduler_job_records_a_failed_run_and_a_later_good_run(ops, monkeypatch, caplog):
    seeded = await _seed(ops)
    scheduler = GithubDispatchScheduler()
    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", ops.maker)
    monkeypatch.setattr(scheduler, "sync_jobs", _async_noop)
    outcome = {"fail": True}

    async def run_once(db, owner, repo, **kwargs):
        if outcome["fail"]:
            raise RuntimeError("closed issue broke the poll")

    monkeypatch.setattr(scheduler, "run_repo_once", run_once)
    with caplog.at_level(logging.ERROR):
        await scheduler.run_repo_job("o", "r")
    assert [f.kind for f in await _findings(ops, status="open")] == [POLL_ROLLED_BACK]
    outcome["fail"] = False
    await scheduler.run_repo_job("o", "r")
    assert await _findings(ops, status="open") == [] and seeded.scope_id


# ---- /health, the findings API, the Supervisor inbox ----

@pytest.mark.asyncio
async def test_health_is_degraded_with_reason_codes_while_a_finding_is_open_and_keeps_its_status(ops):
    seeded = await _seed(ops)
    healthy = await health_check()
    assert healthy["status"] == "ok" and healthy["health"] == "ok" and healthy["reasons"] == []
    # The root liveness body keeps its exact shape while nothing is wrong.
    assert set(await root_health()) == {"name", "version", "status"}

    await ops.service.record_poll_failure("o", "r", RuntimeError("x"))
    await ops.service.record_admissions(seeded.scope_id, {9})
    await ops.service.record_admissions(seeded.scope_id, {9})
    await ops.service.record_admissions(seeded.scope_id, {9})
    degraded = await health_check()
    assert degraded["status"] == "ok"  # the deploy gate reads status; it must not change
    assert degraded["health"] == "degraded" and degraded["reasons"] == [ADMISSION_MISSING, POLL_ROLLED_BACK]
    assert degraded["open_findings"] == 2
    root = await root_health()
    assert root["status"] == "running" and root["health"] == "degraded" and root["reasons"] == degraded["reasons"]
    assert root["open_findings"] == 2

    await ops.service.record_poll_success("o", "r")
    assert (await health_check())["reasons"] == [ADMISSION_MISSING]


@pytest.mark.asyncio
async def test_health_still_answers_when_the_findings_cannot_be_read(ops, monkeypatch):
    def broken():
        raise RuntimeError("database is gone")

    monkeypatch.setattr(ops_module.operations_health_service, "_session", broken)
    for body in (await health_check(), await root_health()):
        assert body["status"] in {"ok", "running"}  # the status values never change
        assert body["health"] == "degraded"
        assert body["reasons"] == ["findings_unreadable"] and body["open_findings"] is None


@pytest.mark.asyncio
async def test_an_initialized_database_that_lost_the_findings_table_is_unreadable_not_healthy(ops):
    """Only a database the application never initialized may be read as 'no findings'."""
    from sqlalchemy import text
    await _seed(ops)
    await ops.service.record_poll_failure("o", "r", RuntimeError("fixture"))
    assert (await ops.service.health())["health"] == "degraded"
    async with ops.engine.begin() as connection:
        await connection.execute(text("DROP TABLE operations_findings"))
    for body in (await ops.service.health(), await health_check(), await root_health()):
        assert body["health"] == "degraded" and body["reasons"] == ["findings_unreadable"]
        assert body["open_findings"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("present", [
    ["agent_team_presets"],                                 # V1's case: one application table, not github_work_items
    ["projects", "mail_messages", "team_github_scopes"],    # a few tables of a real schema
    ["github_work_items"],                                  # the table the first check looked for
])
async def test_any_application_table_without_the_findings_table_is_unreadable_not_healthy(tmp_path, monkeypatch, present):
    """A partial or broken schema is not a fresh database."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'partial.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: [Base.metadata.tables[name].create(sync) for name in present])
    monkeypatch.setattr(ops_module.operations_health_service, "_session", async_sessionmaker(engine, expire_on_commit=False))
    for body in (await health_check(), await root_health()):
        assert body["health"] == "degraded" and body["reasons"] == ["findings_unreadable"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_database_with_only_tables_that_are_not_the_applications_counts_as_empty(tmp_path, monkeypatch):
    from sqlalchemy import text
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'foreign.db'}")
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE somebody_elses_table (id INTEGER)"))
    monkeypatch.setattr(ops_module.operations_health_service, "_session", async_sessionmaker(engine, expire_on_commit=False))
    body = await health_check()
    assert body["health"] == "ok" and body["reasons"] == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_store_that_does_not_exist_yet_is_not_a_finding_and_keeps_the_exact_shape(tmp_path, monkeypatch):
    """A database init_db has not touched has no findings; it must not degrade a fresh installation."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
    monkeypatch.setattr(ops_module.operations_health_service, "_session", async_sessionmaker(engine, expire_on_commit=False))
    body = await health_check()
    assert body["health"] == "ok" and body["reasons"] == [] and body["open_findings"] == 0
    assert set(await root_health()) == {"name", "version", "status"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_the_findings_api_lists_open_findings_first_then_recent_resolved(ops):
    seeded = await _seed(ops)
    await ops.service.record_poll_failure("o", "r", RuntimeError("x"))
    await ops.service.record_poll_success("o", "r")
    await ops.service.record_poll_failure("o", "r", RuntimeError("x"))
    for _ in range(MISSING_ADMISSION_POLLS):
        await ops.service.record_admissions(seeded.scope_id, {9})
    async with ops.maker() as db:
        listed = await list_operations_findings(limit=10, db=db)
    assert listed.health == "degraded" and listed.open_findings == 2
    assert [f.status for f in listed.findings] == ["open", "open", "resolved"]
    assert {f.kind for f in listed.findings} == {POLL_ROLLED_BACK, ADMISSION_MISSING}
    assert all(not hasattr(f, "payload") for f in listed.findings)


@pytest.mark.asyncio
async def test_the_bound_supervisor_sees_exactly_one_notice_per_finding_and_nobody_else_does(ops):
    seeded = await _seed(ops)
    for _ in range(3):
        await ops.service.record_poll_failure("o", "r", RuntimeError("x"))
    async with ops.maker() as db:
        seen = await supervisor_inbox(db, SimpleNamespace(id=6))
        assert [m.payload["kind"] for m in seen] == [POLL_ROLLED_BACK]
        with pytest.raises(PermissionError):
            await supervisor_inbox(db, SimpleNamespace(id=9))
        db.add(AgentTeamPreset(name="Other", description="", created_by="t", supervisor_actor_id=7))
        await db.commit()
        assert await supervisor_inbox(db, SimpleNamespace(id=7)) == []
        assert (await db.execute(select(func.count()).select_from(MailReceipt))).scalar_one() == 0  # no member receipts
    assert seeded.preset_id


@pytest.mark.asyncio
async def test_no_notice_is_sent_without_a_bound_supervisor_but_the_finding_is_kept(ops):
    seeded = await _seed(ops, supervisor=None)
    await ops.service.record_admissions(seeded.scope_id, {9})
    await ops.service.record_admissions(seeded.scope_id, {9})
    await ops.service.record_admissions(seeded.scope_id, {9})
    [finding] = await _findings(ops, status="open")
    assert finding.notified_at is None and await _notices(ops) == []


def _row(**overrides):
    base = dict(
        kind="message", audience_type="team_preset", audience_id="1", thread_root_id=None, sender_type="system",
        sender_member_id=None, sender_actor_id=None, recipient_member_id=None, subject="Operations finding",
        payload={"notice": "operations_finding", "finding_id": 3, "kind": POLL_ROLLED_BACK, "subject": "repo:o/r",
                 "detail": "A poll failed.", "supervisor_actor_id": 6})
    base.update(overrides)
    return SimpleNamespace(**base)


def test_the_supervisor_recognizer_accepts_only_the_exact_notice_shape():
    assert is_supervisor_message(_row()) is True
    good = _row().payload
    for bad in (
        _row(payload={**good, "extra": 1}),
        _row(payload={k: v for k, v in good.items() if k != "detail"}),
        _row(payload={**good, "kind": "something_else"}),
        _row(payload={**good, "finding_id": 0}),
        _row(payload={**good, "finding_id": "3"}),
        _row(payload={**good, "supervisor_actor_id": True}),
        _row(payload={**good, "subject": ""}),
        _row(payload={**good, "notice": "other"}),
        _row(subject="Something else"),
        _row(sender_type="member"),
        _row(sender_actor_id=6),
        _row(recipient_member_id=4),
        _row(thread_root_id=2),
        _row(audience_type="work_item"),
        _row(kind="broadcast"),
    ):
        assert is_supervisor_message(bad) is False, bad.payload


# ---- atomicity and isolation ----

class _Boom(RuntimeError):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["notice_row", "commit"])
async def test_a_failure_while_opening_leaves_no_finding_and_the_next_poll_opens_it_once(ops, monkeypatch, boundary):
    seeded = await _seed(ops)
    service = ops.service
    armed = {"left": 1}
    if boundary == "notice_row":
        original = service._add_operator_notice

        async def failing(*args, **kwargs):
            if armed["left"]:
                armed["left"] = 0
                raise _Boom("notice row")
            return await original(*args, **kwargs)

        monkeypatch.setattr(service, "_add_operator_notice", failing)
    else:
        real_maker = service._session

        def flaky_session():
            session = real_maker()
            real_commit = session.commit

            async def commit():
                if armed["left"]:
                    armed["left"] = 0
                    raise _Boom("commit")
                return await real_commit()

            session.commit = commit
            return session

        monkeypatch.setattr(service, "_session", flaky_session)

    await service.record_admissions(seeded.scope_id, {9})
    await service.record_admissions(seeded.scope_id, {9})
    await service.record_admissions(seeded.scope_id, {9})  # the failing attempt: swallowed, logged, nothing half-done
    assert await _findings(ops) == [] and await _notices(ops) == []

    await service.record_admissions(seeded.scope_id, {9})  # the next poll succeeds
    await service.record_admissions(seeded.scope_id, {9})
    assert len(await _findings(ops, status="open")) == 1 and len(await _notices(ops)) == 1


@pytest.mark.asyncio
async def test_detection_failures_never_reach_the_caller(ops, monkeypatch, caplog):
    seeded = await _seed(ops)

    def broken():
        raise RuntimeError("database is gone")

    monkeypatch.setattr(ops.service, "_session", broken)
    with caplog.at_level(logging.WARNING):
        await ops.service.sweep_verifying_after_merge(seeded.scope_id, "o", "r", _Client())
        await ops.service.record_verification_skips(seeded.scope_id, {1})
        await ops.service.record_admissions(seeded.scope_id, {1})
        await ops.service.record_poll_failure("o", "r", RuntimeError("x"))
        await ops.service.record_poll_success("o", "r")
    assert len([r for r in caplog.records if "operations detection failed" in r.getMessage()]) == 5


@pytest.mark.asyncio
async def test_detection_does_not_wait_on_the_polls_own_open_transaction(ops):
    """The poll's session may hold the write lock; detection runs after its commit, in its own session."""
    seeded = await _seed(ops)
    async with ops.maker() as caller:
        await caller.execute(update(GithubWorkItem).where(GithubWorkItem.id == seeded.item_id).values(issue_title="y"))
        await caller.commit()  # the scheduler commits before it calls detection
        await ops.service.record_admissions(seeded.scope_id, {9})
    assert await _findings(ops) == []
