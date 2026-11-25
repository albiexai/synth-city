"""Retrospective check history from the real PR verification reader."""
from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import text

from app import database
from app.services.github_check_observation import GithubCheckObservationError
from app.services.github_client import GithubClient
from app.services.github_verification_service import github_verification_service
from tests.agent_teams.test_github_check_observation import API, SHA, check, job, observe, run
from tests.agent_teams.test_github_verification_service import (
    _diagnostic_revision, _item, _owner, _reported_client, _scope,
)

CREATED = "2026-11-25T12:00:00Z"
STARTED = "2026-11-25T12:01:00Z"
COMPLETED = "2026-11-25T12:03:00Z"
WORKFLOW = ".github/workflows/feature.yml"


async def history(db):
    table = await db.scalar(text(
        "SELECT name FROM sqlite_master WHERE name = 'github_check_run_history'"
    ))
    assert table == "github_check_run_history", "PR check history is not installed"
    rows = await db.execute(text("SELECT * FROM github_check_run_history"))
    return {row["check_run_id"]: dict(row) for row in rows.mappings()}


async def tracked_client(db, monkeypatch, http, repo="repo", required=("CI", "github-actions")):
    name, app = required
    scope = await _scope(db, repo_owner="owner", repo_name=repo, delivery_policy={
        "required_checks": [{"name": name, "app_slug": app}],
    })
    item = await _item(db, scope, dispatch_status="verifying", pr_number=5)
    pull = _reported_client(scope, item, 5).pull
    pull["head"]["sha"] = SHA
    await db.commit()
    client = GithubClient(http=http, token="polling-token")

    async def get_pull(*args, **kwargs):
        return pull

    monkeypatch.setattr(client, "get_pull", get_pull)
    return scope, item, client


@pytest.mark.asyncio
async def test_pr_poll_keeps_each_raw_completed_run_once_with_exact_job_metadata(db, monkeypatch):
    external = check(200, app=23)
    optional = check(201, app=23, name="Optional", status="in_progress", conclusion=None)
    real = [
        check(99, suite=9, conclusion="failure"), check(conclusion="failure"), check(101), external,
    ]
    for row in real:
        row.update(started_at=STARTED, completed_at=COMPLETED)
    api = API(
        [*real, optional],
        [{**run(9), "name": "Former workflow", "path": ".github/workflows/former.yml"},
         {**run(attempt=2), "name": "Feature", "path": WORKFLOW + "@work"}],
        {10: [{**job(101, attempt=2), "created_at": CREATED}]},
        {10: [{**job(conclusion="failure"), "created_at": "2026-11-25T11:59:00Z"}, job(101, attempt=2)]},
        latest=[real[2], external, optional],
    )
    effective = await observe(api)
    assert {row.get("id") for row in effective} == {101, 200, 201, None}
    expected_requests = [str(request.url) for request in api.requests]
    api.requests.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(api), base_url="https://api.github.com") as http:
        scope, item, client = await tracked_client(db, monkeypatch, http)
        await github_verification_service._verify_item(db, scope, item, client)
        assert [str(request.url) for request in api.requests] == expected_requests
        before = await history(db)
        assert set(before) == {99, 100, 101, 200}
        assert {row["repository"] for row in before.values()} == {"owner/repo"}
        assert {row["scope_id"] for row in before.values()} == {scope.id}
        assert {row["head_sha"] for row in before.values()} == {SHA}
        current = before[101]
        assert (current["check_name"], current["app_slug"], current["conclusion"], current["required"]) == (
            "CI", "github-actions", "success", 1,
        )
        assert datetime.fromisoformat(current["started_at"]) == datetime(2026, 11, 25, 12, 1)
        assert datetime.fromisoformat(current["completed_at"]) == datetime(2026, 11, 25, 12, 3)
        assert datetime.fromisoformat(current["job_created_at"]) == datetime(2026, 11, 25, 12)
        assert (current["run_attempt"], current["workflow_name"], current["workflow_path"]) == (2, "Feature", WORKFLOW)
        assert before[100]["conclusion"] == "failure"
        assert before[100]["run_attempt"] == 1
        assert datetime.fromisoformat(before[100]["job_created_at"]) == datetime(2026, 11, 25, 11, 59)
        assert before[99]["job_created_at"] is None and before[99]["run_attempt"] is None
        assert before[99]["workflow_name"] == "Former workflow"
        assert before[200]["app_slug"] == "external" and before[200]["required"] == 0
        for field in ("job_created_at", "run_attempt", "workflow_name", "workflow_path"):
            assert before[200][field] is None

        optional.update(status="completed", conclusion="success", completed_at=COMPLETED)
        external["completed_at"] = "2026-11-25T12:04:00Z"
        await github_verification_service._verify_item(db, scope, item, client)
        after = await history(db)
        assert set(after) == {99, 100, 101, 200, 201}
        assert {key: after[key] for key in before} == before
        assert after[201]["required"] == 0
        assert item.last_verified_sha == SHA
        assert item.status_note == "PR #5 is draft. The owner must mark it ready."


@pytest.mark.asyncio
async def test_external_required_check_is_classified_per_repository(db, monkeypatch):
    api = API([check(100, app=23, name="Build elsewhere")])
    async with httpx.AsyncClient(transport=httpx.MockTransport(api), base_url="https://api.github.com") as http:
        for repo, required in (("RepoA", "Build elsewhere"), ("RepoB", "Another check")):
            scope, item, client = await tracked_client(db, monkeypatch, http, repo, (required, "external"))
            await github_verification_service._verify_item(db, scope, item, client)
    rows = await db.execute(text(
        "SELECT repository, check_run_id, required FROM github_check_run_history ORDER BY repository"
    ))
    assert rows.all() == [("owner/repoa", 100, 1), ("owner/repob", 100, 0)]


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_head", [False, True])
async def test_partial_read_defers_actions_until_metadata_can_be_recorded(db, monkeypatch, foreign_head):
    rows = [check(), check(200, app=23)]
    if foreign_head:
        rows.append({**check(300, app=23), "head_sha": "b" * 40})
    api = API(rows, [{**run(), "name": "Feature", "path": WORKFLOW}], jobs={10: []})
    async with httpx.AsyncClient(transport=httpx.MockTransport(api), base_url="https://api.github.com") as http:
        scope, item, client = await tracked_client(db, monkeypatch, http)
        reason = "check_head_mismatch" if foreign_head else "incomplete_current_jobs"
        with pytest.raises(GithubCheckObservationError, match=reason):
            await github_verification_service._verify_item(db, scope, item, client)
        await db.rollback()
        assert set(await history(db)) == {200}
        await db.refresh(scope)
        await db.refresh(item)
        api.checks = rows[:2]
        api.jobs = {10: [{**job(), "created_at": CREATED}]}
        await github_verification_service._verify_item(db, scope, item, client)
    completed = await history(db)
    assert set(completed) == {100, 200}
    row = completed[100]
    assert datetime.fromisoformat(row["job_created_at"]) == datetime(2026, 11, 25, 12)
    assert (row["run_attempt"], row["workflow_name"], row["workflow_path"]) == (1, "Feature", WORKFLOW)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["diagnostic", "human_review", "merge_wait", "stall_fallback"])
async def test_existing_poll_paths_record_history(db, monkeypatch, stage):
    api = API([check(200, app=23)])
    verification = github_verification_service
    async with httpx.AsyncClient(transport=httpx.MockTransport(api), base_url="https://api.github.com") as http:
        scope, item, client = await tracked_client(db, monkeypatch, http, required=("CI", "external"))
        monkeypatch.setattr(client, "get_combined_status_for_ref", AsyncMock(return_value={"state": "success"}))
        if stage == "diagnostic":
            slot, member = await _owner(db, scope)
            item.dispatch_status, item.attempt_phase = "dispatched", "diagnostic"
            item.dispatch_nonce, item.owner_slot_id, item.active_scope_revision = "diagnostic", slot.id, 1
            revision, _ = await _diagnostic_revision(db, scope, item, slot, member)
            await verification._observe_diagnostic_checks(db, scope, item, revision, client)
        elif stage == "human_review":
            item.last_verified_sha = SHA
            await verification._process_review_item(db, scope, item, client)
        elif stage == "merge_wait":
            assert not await verification._wait_for_merge_checks(db, scope, item, client, SHA)
        else:
            item.merge_wait_head_sha = SHA
            item.status_note = "Auto-merge blocked: GitHub checks stalled for 60 minutes on head."
            pull = await client.get_pull("owner", "repo", 5)
            assert await verification._cleared_fallback_note(db, scope, item, client, pull)
    assert set(await history(db)) == {200}


@pytest.mark.asyncio
async def test_history_write_failure_does_not_change_verification(db, monkeypatch, caplog):
    api = API([check()], [run()], jobs={10: [job()]})
    await db.execute(text("DROP TABLE IF EXISTS github_check_run_history"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(api), base_url="https://api.github.com") as http:
        scope, item, client = await tracked_client(db, monkeypatch, http)
        await github_verification_service._verify_item(db, scope, item, client)
    assert "github_check_history_write_failed" in caplog.text
    assert item.last_verified_sha == SHA


@pytest.mark.asyncio
async def test_installation_sets_check_history_coverage_before_any_poll(db, monkeypatch):
    monkeypatch.setattr(database, "engine", db.bind)
    await database.init_db()
    coverage_table = "github_check_run_history_coverage"
    assert await db.scalar(text("SELECT name FROM sqlite_master WHERE name = :name"), {"name": coverage_table})
    started = await db.scalar(text("SELECT started_at FROM github_check_run_history_coverage"))
    assert started is not None
    await db.commit()
    await database.init_db()
    assert (await db.scalars(text("SELECT started_at FROM github_check_run_history_coverage"))).all() == [started]
