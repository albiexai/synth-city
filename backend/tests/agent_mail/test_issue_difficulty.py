"""Authenticated difficulty writes through the normal Factory entry points."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, update

from app.models.database import AgentTeamPreset, FactoryAuditEvent, MailAgentSession, TeamGithubScope
from app.services.github_app_auth_service import github_app_auth_service
from app.services.github_client import github_client
from app.utils import peer_process
from tests.agent_mail.test_api import _dispatch_approval_fixture, client  # noqa: F401
from tests.agent_mail.test_external_api import _actor_token

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def difficulty_world(client, db, monkeypatch):
    monkeypatch.setattr(peer_process, "pane_is_alive", lambda _pid, _start: True)
    item, members, tokens = await _dispatch_approval_fixture(db)
    scope = await db.get(TeamGithubScope, item.scope_id)
    team = await db.get(AgentTeamPreset, scope.preset_id)
    team.autonomy_enabled = True
    await db.execute(update(MailAgentSession).where(MailAgentSession.member_id == members[0].id).values(
        wake_enabled=True,
    ))
    item.issue_url = "https://github.com/o/approval/issues/91"
    await db.commit()

    labels = {"bug", "ready"}
    writes = []
    remote = SimpleNamespace(fail_delete=False, concurrent_label=None, authorization=[])

    def github(request):
        path = request.url.path
        remote.authorization.append(request.headers.get("authorization"))
        issue = "/repos/o/approval/issues/91"
        if request.method == "GET" and path == issue:
            return httpx.Response(200, json={
                "number": 91, "html_url": "https://github.com/o/approval/issues/91", "state": "open",
                "updated_at": "2026-11-27T00:00:00Z", "labels": [{"name": name} for name in sorted(labels)],
            })
        if request.method == "POST" and path == issue + "/labels":
            added = json.loads(request.content)["labels"]
            writes.append(("add", added))
            labels.update(added)
            if remote.concurrent_label:
                labels.add(remote.concurrent_label)
        elif request.method == "DELETE" and path.startswith(issue + "/labels/"):
            name = path.split("/")[-1]
            writes.append(("remove", name))
            if remote.fail_delete:
                return httpx.Response(503)
            labels.discard(name)
        else:
            raise AssertionError(f"Unexpected GitHub request {request.method} {path}")
        return httpx.Response(200, json=[{"name": name} for name in sorted(labels)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(github), base_url="https://api.github.com") as transport:
        monkeypatch.setattr(github_client, "_http", transport)
        yield SimpleNamespace(
            item=item, scope=scope, team=team, leader=members[0], labels=labels, writes=writes, remote=remote,
            leader_headers={"X-Synth-City-Session-Token": tokens[0]},
            owner_headers={"X-Synth-City-Session-Token": tokens[1]},
            director_headers={"X-Synth-City-Operator-Token": "test-director-key"},
            mail_path=f"/api/v1/agent-mail/work-items/{item.id}/difficulty",
            director_path=f"/api/v1/agent-teams/github-work-items/{item.id}/difficulty",
            supervisor_path=f"/api/v1/external/agent-mail/work-items/{item.id}/difficulty",
        )


async def write(client, path, headers, difficulty):
    return await client.put(path, headers=headers, json={"difficulty": difficulty})


async def test_leader_write_is_audited_and_visible(client, db, difficulty_world):
    w = difficulty_world
    denied = await write(client, w.mail_path, w.owner_headers, "high")
    assert denied.status_code == 403 and denied.json()["detail"] == "current_leader_required"
    assert w.writes == []

    applied = await write(client, w.mail_path, w.leader_headers, "medium")
    expected = {"difficulty": "medium", "source": "leader", "actor_reference": f"member:{w.leader.id}"}
    assert applied.status_code == 200 and applied.json() == expected
    assert (await write(client, w.mail_path, w.leader_headers, "medium")).json() == expected
    assert w.labels == {"bug", "ready", "difficulty:medium"}
    assert w.writes == [("add", ["difficulty:medium"])]
    detail = await client.get(f"/api/v1/factory/work-items/{w.item.id}")
    assert detail.status_code == 200
    assert detail.json()["work_item"]["item"]["difficulty_provenance"] == expected
    events = (await db.scalars(select(FactoryAuditEvent).where(
        FactoryAuditEvent.event_kind == "issue_difficulty_changed", FactoryAuditEvent.action_outcome == "applied"))).all()
    assert len(events) == 1 and events[0].actor_member_id == w.leader.id
    assert events[0].sanitized_reason == "Difficulty changed from unset to medium."


@pytest.mark.parametrize("source", ["director", "supervisor"])
async def test_privileged_override_wins_even_at_a_lower_tier(client, db, difficulty_world, source):
    w = difficulty_world
    assert (await write(client, w.mail_path, w.leader_headers, "high")).status_code == 200
    if source == "supervisor":
        token, actor = await _actor_token(client, "difficulty-supervisor", "Supervisor")
        headers = {"Authorization": f"Bearer {token}"}
        assert (await write(client, w.supervisor_path, headers, "low")).status_code == 403
        w.team.supervisor_actor_id = actor["id"]
        await db.commit()
        path, reference = w.supervisor_path, f"external_actor:{actor['id']}"
    else:
        path, headers, reference = w.director_path, w.director_headers, "shared-operator-credential"
        assert (await write(client, path, {**headers, **w.leader_headers}, "low")).status_code == 403
    override = await write(client, path, headers, "low")
    assert override.status_code == 200 and override.json() == {
        "difficulty": "low", "source": source, "actor_reference": reference,
    }
    assert w.labels == {"bug", "ready", "difficulty:low"}
    before = w.writes.copy()
    refused = await write(client, w.mail_path, w.leader_headers, "medium")
    assert refused.status_code == 409 and refused.json()["detail"] == "difficulty_override_protected"
    assert w.writes == before


async def test_external_label_is_protected_without_claiming_an_author(client, db, difficulty_world):
    w = difficulty_world
    w.labels.add("difficulty:high")
    w.item.issue_difficulty = "high"
    await db.commit()
    refused = await write(client, w.mail_path, w.leader_headers, "medium")
    assert refused.status_code == 409 and refused.json()["detail"] == "difficulty_override_protected"
    assert w.writes == []
    detail = await client.get(f"/api/v1/factory/work-items/{w.item.id}")
    assert detail.json()["work_item"]["item"]["difficulty_provenance"] == {
        "difficulty": "high", "source": "github", "actor_reference": None,
    }
    assert (await write(client, w.director_path, w.director_headers, "low")).status_code == 200
    assert w.labels == {"bug", "ready", "difficulty:low"}


async def test_partial_github_write_clears_applied_provenance(client, db, difficulty_world):
    w = difficulty_world
    assert (await write(client, w.mail_path, w.leader_headers, "high")).status_code == 200
    w.remote.fail_delete = True
    result = await write(client, w.mail_path, w.leader_headers, "low")
    assert result.status_code == 502 and result.json()["detail"] == "difficulty_write_uncertain"
    await db.refresh(w.item)
    assert w.item.issue_difficulty_provenance is None and w.item.issue_difficulty == "high"
    outcomes = (await db.scalars(select(FactoryAuditEvent.action_outcome).where(
        FactoryAuditEvent.event_kind == "issue_difficulty_changed").order_by(FactoryAuditEvent.id))).all()
    assert outcomes == ["applied", "uncertain"]

async def test_same_issue_concurrent_external_label_is_preserved(client, db, difficulty_world):
    w = difficulty_world
    w.remote.concurrent_label = "difficulty:high"
    result = await write(client, w.mail_path, w.leader_headers, "low")
    assert result.status_code == 502 and result.json()["detail"] == "difficulty_write_uncertain"
    assert w.labels == {"bug", "ready", "difficulty:high", "difficulty:low"}
    await db.refresh(w.item)
    assert w.item.issue_difficulty_provenance is None and w.item.issue_difficulty == "high"
    refused = await write(client, w.mail_path, w.leader_headers, "medium")
    assert refused.status_code == 409 and refused.json()["detail"] == "difficulty_override_protected"


async def test_uses_installation_token_for_the_scoped_repository(client, db, difficulty_world, monkeypatch):
    w = difficulty_world
    w.scope.github_auth_mode = "app"
    w.scope.github_app_installation_id = 101
    await db.commit()
    mint = AsyncMock(return_value="test-installation-token")
    monkeypatch.setattr(github_app_auth_service, "mint_repository_token", mint)
    result = await write(client, w.mail_path, w.leader_headers, "medium")
    assert result.status_code == 200
    mint.assert_awaited_once_with(101, "o", "approval", purpose="issue_difficulty")
    assert w.remote.authorization == ["Bearer test-installation-token"] * 3

async def test_rejects_stale_leader_and_wrong_issue_before_github_access(client, db, difficulty_world, monkeypatch):
    w = difficulty_world
    monkeypatch.setattr(peer_process, "pane_is_alive", lambda _pid, _start: False)
    refused = await write(client, w.mail_path, w.leader_headers, "low")
    assert refused.status_code == 401 and refused.json()["detail"] == "session_token_stale"

    w.item.issue_url = "https://github.com/o/different/issues/91"
    await db.commit()
    refused = await write(client, w.director_path, w.director_headers, "low")
    assert refused.status_code == 409 and refused.json()["detail"] == "difficulty_issue_mismatch"
    assert w.remote.authorization == []
