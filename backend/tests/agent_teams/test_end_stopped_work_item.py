from datetime import datetime, timedelta
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from mcp_shim.work_remaining_protocol import public_generation
from sqlalchemy import select, update

from app.api.v1 import factory_maintenance
from app.config import settings
from app.database import get_db
from app.models.database import AgentTeamPreset, FactoryAuditEvent, GithubWorkItem, GithubWorkspace, TeamGithubScope
from app.services.github_client import github_client
from app.services.github_coordination_service import github_coordination_service
from app.services.github_operator_attention_service import github_operator_attention_service
from app.services.github_workspace_service import github_workspace_service
from tests.agent_teams.test_scope_amendments import db  # noqa: F401


async def _stopped(db, tmp_path):
    preset = AgentTeamPreset(name="Test team", autonomy_enabled=True)
    db.add(preset)
    await db.flush()
    scope = TeamGithubScope(preset_id=preset.id, repo_owner="example", repo_name="repo",
                            repo_path=str(tmp_path), base_ref="origin/release/test", enabled=True)
    db.add(scope)
    await db.flush()
    item = GithubWorkItem(scope_id=scope.id, issue_number=7, issue_title="Stopped item",
                          issue_url="https://example.invalid/issues/7", github_updated_at=datetime.utcnow(),
                          dispatch_status="escalated", escalation_reason="abandoned_by_operator",
                          dispatch_nonce="attempt-7")
    db.add(item)
    await db.flush()
    workspace = GithubWorkspace(scope_id=scope.id, kind="primary", path=str(tmp_path),
                                leased_item_id=item.id, leased_at=datetime.utcnow(), lease_token="test-lease")
    db.add(workspace)
    await db.commit()
    return preset, scope, item, workspace


def _api(db, monkeypatch):
    monkeypatch.setattr(settings, "operator_token", "test-director")
    app = FastAPI()
    app.include_router(factory_maintenance.router)

    async def database():
        yield db

    app.dependency_overrides[get_db] = database
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _body(item_id, outcome):
    evidence = {"target_milestone": "v4.4"} if outcome == "deferred" else {"superseding_pr_number": 42}
    return {"operation_id": "end-test", "expected_dispatch_generation": public_generation(item_id, "attempt-7"),
            "expected_scope_revision": 0, "outcome": outcome,
            "reason": "Tracking moved to the recorded destination.", **evidence}


OPERATOR = {"X-Synth-City-Operator-Token": "test-director"}


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["deferred", "superseded"])
async def test_ending_stopped_work_resolves_attention_and_releases_its_lease(
        db, tmp_path, monkeypatch, outcome):
    preset, scope, item, workspace = await _stopped(db, tmp_path)
    url = f"/presets/{preset.id}/work-items/{item.id}/end-stopped"
    body = _body(item.id, outcome)
    clean = AsyncMock(return_value=None)
    monkeypatch.setattr(github_workspace_service, "delegated_release_blocker", clean)
    pull = {"number": 42, "state": "closed", "merged": True, "merged_at": "2026-11-01T00:00:00Z",
            "merge_commit_sha": "a" * 40,
            "base": {"ref": "release/test", "repo": {"full_name": "example/repo"}}}
    monkeypatch.setattr(github_client, "get_pull", AsyncMock(return_value=pull))
    before = await github_operator_attention_service.dispatch_snapshot(db, [scope])
    assert [row["kind"] for row in before["actions"]] == ["inspect_attempt"]
    async with _api(db, monkeypatch) as client:
        response = await client.post(url, json=body, headers=OPERATOR)
        assert response.status_code == 200, response.text
        assert response.json()["outcome"] == outcome
        assert (await client.post(url, json=body, headers=OPERATOR)).json()["status"] == "already_ended"
    await db.refresh(item)
    await db.refresh(workspace)
    assert item.dispatch_status == "cancelled"
    assert item.escalation_reason is None
    assert workspace.leased_item_id is None
    await db.refresh(scope)
    assert (await github_operator_attention_service.dispatch_snapshot(db, [scope]))["actions"] == []
    event = (await db.scalars(select(FactoryAuditEvent).where(
        FactoryAuditEvent.event_kind == "stopped_work_item_ended"))).one()
    assert event.actor_kind == "operator"
    assert event.after_values["outcome"] == outcome
    assert event.sanitized_reason == body["reason"]


@pytest.mark.asyncio
async def test_ending_refuses_unauthorized_stale_and_unpublished_work(db, tmp_path, monkeypatch):
    preset, _, item, workspace = await _stopped(db, tmp_path)
    url = f"/presets/{preset.id}/work-items/{item.id}/end-stopped"
    body = _body(item.id, "deferred")
    clean = AsyncMock(return_value="unpublished commits in the primary workspace")
    monkeypatch.setattr(github_workspace_service, "delegated_release_blocker", clean)
    async with _api(db, monkeypatch) as client:
        assert (await client.post(url, json=body)).status_code == 401
        stale = {**body, "expected_dispatch_generation": public_generation(item.id, "previous-attempt")}
        assert (await client.post(url, json=stale, headers=OPERATOR)).status_code == 409
        clean.assert_not_awaited()
        refused = await client.post(url, json=body, headers=OPERATOR)
        assert refused.status_code == 409
        assert refused.json()["detail"] == "workspace_not_clean"
    await db.refresh(item)
    await db.refresh(workspace)
    assert item.dispatch_status == "escalated"
    assert workspace.leased_item_id == item.id


@pytest.mark.asyncio
async def test_ending_preserves_a_real_unpublished_commit(db, tmp_path, monkeypatch):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), "-c", "user.name=Test",
                                        "-c", "user.email=test@example.invalid", *args], text=True).strip()

    git("init", "-b", "release/test")
    git("commit", "--allow-empty", "-m", "Tracked base")
    git("update-ref", "refs/remotes/origin/release/test", "HEAD")
    git("commit", "--allow-empty", "-m", "Unpublished work")
    unpublished = git("rev-parse", "HEAD")
    preset, _, item, workspace = await _stopped(db, tmp_path)
    url = f"/presets/{preset.id}/work-items/{item.id}/end-stopped"
    async with _api(db, monkeypatch) as client:
        response = await client.post(url, json=_body(item.id, "deferred"), headers=OPERATOR)
    assert response.status_code == 409 and response.json()["detail"] == "workspace_not_clean"
    await db.refresh(workspace)
    await db.refresh(item)
    assert workspace.leased_item_id == item.id and item.dispatch_status == "escalated"
    assert git("rev-parse", "HEAD") == unpublished


@pytest.mark.asyncio
async def test_end_without_a_lease_retires_an_old_leader_human_action(db, tmp_path, monkeypatch):
    preset, _, item, workspace = await _stopped(db, tmp_path)
    await db.delete(workspace)
    await db.commit()
    old_assessment = {"enabled": True, "assessment_current": True, "version": 8, "repo": "example/repo",
                      "status": "assessed", "last_assessed_at": datetime.utcnow(),
                      "observation_expires_at": datetime.utcnow() + timedelta(minutes=1), "entries": [{
                          "issue_number": item.issue_number, "disposition": "human_decision_blocked",
                          "reason": "scope_clarification", "required_actor": "operator",
                          "evidence_issue_numbers": [item.issue_number], "human_actions": [
                              {"kind": "scope_clarification", "readiness": "requested"}]}]}
    monkeypatch.setattr(github_coordination_service, "summary", AsyncMock(return_value=old_assessment))
    monkeypatch.setattr(github_coordination_service, "state", AsyncMock(return_value=SimpleNamespace(version=8)))
    url = f"/presets/{preset.id}/work-items/{item.id}/end-stopped"
    async with _api(db, monkeypatch) as client:
        response = await client.post(url, json=_body(item.id, "deferred"), headers=OPERATOR)
    assert response.status_code == 200 and response.json()["workspace_released"] is False
    summary = await github_operator_attention_service.summary(db, preset.id)
    assert summary["actions"] == [] and summary["coverage_complete"] is True


@pytest.mark.asyncio
async def test_superseded_refuses_a_pr_on_another_target(db, tmp_path, monkeypatch):
    preset, _, item, workspace = await _stopped(db, tmp_path)
    pull = {"number": 42, "state": "closed", "merged": True, "merged_at": "2026-11-01T00:00:00Z",
            "merge_commit_sha": "a" * 40, "base": {"ref": "other", "repo": {"full_name": "example/repo"}}}
    monkeypatch.setattr(github_client, "get_pull", AsyncMock(return_value=pull))
    url = f"/presets/{preset.id}/work-items/{item.id}/end-stopped"
    async with _api(db, monkeypatch) as client:
        response = await client.post(url, json=_body(item.id, "superseded"), headers=OPERATOR)
    assert response.status_code == 409 and response.json()["detail"] == "superseding_pr_not_merged_into_target"
    await db.refresh(item)
    await db.refresh(workspace)
    assert item.dispatch_status == "escalated" and workspace.leased_item_id == item.id


@pytest.mark.asyncio
async def test_ending_preserves_the_lease_if_retry_wins_during_inspection(db, tmp_path, monkeypatch):
    preset, _, item, workspace = await _stopped(db, tmp_path)
    item_id = item.id
    url = f"/presets/{preset.id}/work-items/{item_id}/end-stopped"

    async def inspect_then_retry(*args):
        await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == item_id).values(
            dispatch_status="dispatched", dispatch_nonce="new-attempt"))
        await db.commit()
        return None

    monkeypatch.setattr(github_workspace_service, "delegated_release_blocker", inspect_then_retry)
    async with _api(db, monkeypatch) as client:
        response = await client.post(url, json=_body(item_id, "deferred"), headers=OPERATOR)
    assert response.status_code == 409 and response.json()["detail"] == "end_stopped_item_context_changed"
    await db.refresh(item)
    await db.refresh(workspace)
    assert item.dispatch_status == "dispatched" and item.dispatch_nonce == "new-attempt"
    assert workspace.leased_item_id == item_id
