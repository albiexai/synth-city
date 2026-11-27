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
@pytest.mark.parametrize("suffix", ["g1", NONCE, "0123456789abcdef"])
async def test_real_branch_lookup_keeps_the_nonce_out_of_public_refs_and_urls(db, monkeypatch, suffix):
    item, scope, preset, workspace = await seed(db)
    item_id, preset_id = item.id, preset.id
    branch = f"deck/slot-1/issue-1-{suffix}"
    await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == item_id).values(dispatch_head_ref=branch))
    await db.commit()
    looked_up = []

    class BranchObserver(Observer):
        async def read(self, context):
            from app.services.github_work_progress_observation import LocalProgressObservation
            from tests.agent_teams.test_github_work_progress import SHA
            return LocalProgressObservation(SHA, branch)

    class BranchClient(Client):
        async def get_ref(self, owner, repo, ref, **kwargs):
            looked_up.append(ref)
            return {"ref": f"refs/heads/{branch}", "object": {"sha": self.sha, "type": "commit"}}

    http = _app(db, monkeypatch, progress_api.router, agent_teams.router,
                service=GithubWorkProgressService(BranchObserver(), BranchClient()))
    async with http:
        response = await http.get(f"/agent-teams/github-work-items/{item_id}/progress")
        cached = await http.get(f"/agent-teams/github-work-items/{item_id}/progress")
        listing = await http.get(f"/agent-teams/presets/{preset_id}/github-work-items")
    body = response.json()
    row = next(i for i in listing.json()["items"] if i["id"] == item_id)
    assert response.status_code == cached.status_code == listing.status_code == 200
    assert body["dispatch_nonce"] is None
    assert looked_up == [branch]
    assert body["publication"]["state"] == "current"
    public_ref = branch if suffix == "g1" else None
    assert row["dispatch_head_ref"] == public_ref
    expected_url = f"https://github.com/{scope.repo_owner}/{scope.repo_name}/tree/{public_ref}" if public_ref else None
    assert body["publication"]["destination_url"] == expected_url
    assert cached.json()["publication"]["destination_url"] == expected_url
    for public_response in (response, cached, listing):
        assert NONCE not in public_response.text
        if public_ref is None:
            assert suffix not in public_response.text


@pytest.mark.asyncio
async def test_observed_legacy_url_stays_private_if_the_attempt_changes_during_checks(db, monkeypatch):
    from app.services.github_ci_watch_service import github_ci_watch_service
    from app.services.github_work_progress_observation import LocalProgressObservation
    from tests.agent_teams.test_github_work_progress import SHA

    item, _, _, _ = await seed(db)
    item_id = item.id
    legacy_ref = f"deck/slot-1/issue-1-{NONCE}"
    item.dispatch_head_ref = legacy_ref
    await db.commit()

    class BranchObserver(Observer):
        async def read(self, context):
            return LocalProgressObservation(SHA, legacy_ref)

    class BranchClient(Client):
        async def get_ref(self, *args, **kwargs):
            return {"ref": f"refs/heads/{legacy_ref}", "object": {"sha": SHA, "type": "commit"}}

    async def change_attempt(session, current):
        await session.execute(update(GithubWorkItem).where(GithubWorkItem.id == item_id).values(
            dispatch_nonce="replacement-binding", dispatch_head_ref="deck/slot-1/issue-1-g2"))
        await session.commit()

    monkeypatch.setattr(github_ci_watch_service, "view", change_attempt)
    service = GithubWorkProgressService(BranchObserver(), BranchClient())
    async with _app(db, monkeypatch, progress_api.router, service=service) as http:
        response = await http.get(f"/agent-teams/github-work-items/{item_id}/progress")
    assert response.status_code == 200
    body = response.json()
    assert body["dispatch_generation"] == public_generation(item_id, "replacement-binding")
    assert body["publication"]["destination_url"] is None
    assert NONCE not in response.text


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
