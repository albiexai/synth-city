"""#988: no unauthenticated projection emits the dispatch nonce as a field; a public generation stands in for it."""
import re

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import update

from app.api.v1 import agent_teams, github_work_progress as progress_api
from app.database import get_db
from app.models.database import GithubAttemptScopeRevision, GithubWorkItem
from app.services.github_work_progress_service import GithubWorkProgressService
from mcp_shim.work_remaining_protocol import public_generation
from tests.agent_teams.test_github_work_progress import Client, Observer, db, seed  # noqa: F401  (fixtures and helpers)

NONCE = "public-dispatch-id"
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _app(db, monkeypatch, *routers, service=None):
    app = FastAPI()
    for router in routers:
        app.include_router(router, prefix="/agent-teams")
    async def session(): yield db
    app.dependency_overrides[get_db] = session
    if service is not None:
        monkeypatch.setattr(progress_api, "github_work_progress_service", service)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _without_branch_strings(text: str, head_ref: str) -> str:
    """The branch name embeds the nonce and is public on GitHub; that exposure is reported separately."""
    return text.replace(head_ref, "<branch>")


@pytest.mark.asyncio
async def test_the_unauthenticated_progress_response_has_no_nonce_field_and_names_the_dispatch_by_generation(db, monkeypatch):
    item, scope, preset, workspace = await seed(db)
    item_id = item.id
    http = _app(db, monkeypatch, progress_api.router, service=GithubWorkProgressService(Observer(), Client()))
    async with http:
        response = await http.get(f"/agent-teams/github-work-items/{item_id}/progress")
    body = response.json()
    assert response.status_code == 200
    assert body["dispatch_nonce"] is None
    assert HEX64.match(body["dispatch_generation"]) and body["dispatch_generation"] == public_generation(item_id, NONCE)
    assert NONCE not in response.text and "private-lease-value" not in response.text and "/safe/repo" not in response.text


@pytest.mark.asyncio
async def test_with_a_realistic_branch_name_the_nonce_appears_only_inside_the_branch_strings(db, monkeypatch):
    item, scope, preset, workspace = await seed(db)
    item_id = item.id
    branch = f"deck/slot-1/issue-1-{NONCE}"
    await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == item_id).values(dispatch_head_ref=branch))
    await db.commit()

    class BranchObserver(Observer):
        async def read(self, context):
            from app.services.github_work_progress_observation import LocalProgressObservation
            from tests.agent_teams.test_github_work_progress import SHA
            return LocalProgressObservation(SHA, branch)

    class BranchClient(Client):
        async def get_ref(self, *args, **kwargs):
            return {"ref": f"refs/heads/{branch}", "object": {"sha": self.sha, "type": "commit"}}
    http = _app(db, monkeypatch, progress_api.router, service=GithubWorkProgressService(BranchObserver(), BranchClient()))
    async with http:
        response = await http.get(f"/agent-teams/github-work-items/{item_id}/progress")
    body = response.json()
    assert body["dispatch_nonce"] is None
    assert NONCE not in _without_branch_strings(response.text, branch)   # the residual is the branch name alone


@pytest.mark.asyncio
async def test_the_public_work_item_list_has_no_nonce_field_and_carries_the_generation(db, monkeypatch):
    item, scope, preset, workspace = await seed(db)
    item_id, preset_id = item.id, preset.id
    http = _app(db, monkeypatch, agent_teams.router)
    async with http:
        response = await http.get(f"/agent-teams/presets/{preset_id}/github-work-items")
    assert response.status_code == 200, response.text
    row = next(i for i in response.json()["items"] if i["id"] == item_id)
    assert row["dispatch_nonce"] is None
    assert row["dispatch_generation"] == public_generation(item_id, NONCE)
    assert NONCE not in response.text and "private-lease-value" not in response.text


@pytest.mark.asyncio
async def test_the_other_public_team_reads_emit_neither_the_nonce_nor_a_lease_value(db, monkeypatch):
    item, scope, preset, workspace = await seed(db)
    preset_id = preset.id
    http = _app(db, monkeypatch, agent_teams.router)
    async with http:
        for path in (f"/agent-teams/presets/{preset_id}", f"/agent-teams/presets/{preset_id}/summary",
                     f"/agent-teams/presets/{preset_id}/activity", f"/agent-teams/presets/{preset_id}/github-scopes",
                     "/agent-teams/presets"):
            response = await http.get(path)
            assert response.status_code in {200, 404}, (path, response.status_code)
            assert NONCE not in response.text and "private-lease-value" not in response.text, path


@pytest.mark.asyncio
async def test_the_authenticated_revision_response_carries_the_generation_beside_the_nonce(db):
    item, scope, preset, workspace = await seed(db)
    item_id, owner_slot_id = item.id, item.owner_slot_id
    revision = GithubAttemptScopeRevision(work_item_id=item_id, dispatch_nonce=NONCE, revision=1,
        owner_slot_id=owner_slot_id, owner_member_id=1, phase="implementation", execution_target="workspace",
        summary="s", allowed_paths=["a.py"], allowed_actions=["edit_tests"], allowed_commands=[], prohibited_actions=[],
        tool_fallbacks={}, baseline_head_sha="a" * 40, baseline_tree_sha="b" * 40, originating_escalation_reason="",
        expected_workspace_id=1, expected_lease_token_hash="h", max_failed_heads=1, failed_head_count=0, status="active")
    db.add(revision)
    await db.flush()
    response = agent_teams._scope_revision_response(revision)
    assert response.dispatch_nonce == NONCE                       # the authenticated route keeps it for operator actions
    assert response.dispatch_generation == public_generation(item_id, NONCE)
