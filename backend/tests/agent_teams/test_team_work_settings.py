import pytest

from app.config import settings
from app.models.database import AgentTeamPreset
from tests.agent_teams.test_github_workspace_api import OPERATOR_HEADERS, client, operator_token  # noqa: F401


@pytest.mark.asyncio
async def test_work_settings_http_protection_validation_and_history(client, db, monkeypatch):
    teams = [AgentTeamPreset(name=name) for name in ("A", "B")]
    db.add_all(teams)
    await db.commit()
    path, other = [f"/api/v1/agent-teams/presets/{team.id}" for team in teams]
    patch = {"mail_request_unanswered_seconds": 90, "github_team_idle_timeout_seconds": 60,
             "github_publish_summaries_enabled": False}
    headers = OPERATOR_HEADERS

    assert (await client.get(f"{path}/work-settings")).status_code == 401
    assert (await client.patch(path, json=patch)).status_code == 401
    for name, value in zip(patch, (59, "60", 0)):
        assert (await client.patch(path, headers=headers, json={name: value})).status_code == 422
    assert (await client.patch(path, headers=headers, json=patch)).status_code == 200
    assert (await client.patch(path, headers=headers, json={"name": "Renamed", **patch})).status_code == 200
    assert (await client.patch(other, headers=headers, json={next(iter(patch)): 600})).status_code == 200
    cleared = {name: None for name in patch}
    assert (await client.patch(path, headers=headers, json=cleared)).status_code == 200
    monkeypatch.setattr(settings, "github_publish_summaries_enabled", True)

    response = await client.get(f"{path}/work-settings", headers=headers, params={"limit": 1})
    current = response.json()
    assert response.status_code == 200
    assert current["overrides"] == cleared
    assert current["defaults"] == {name: getattr(settings, name) for name in patch}
    assert current["history"][0]["changes"] == {
        name: {"before": value, "after": None} for name, value in patch.items()
    }
    previous = await client.get(f"{path}/work-settings", headers=headers,
                                params={"before_id": current["next_before_id"]})
    entries = previous.json()["history"]
    assert len(entries) == 1
    assert entries[0]["actor_kind"] == "operator"
    assert entries[0]["changes"] == {name: {"before": None, "after": value} for name, value in patch.items()}
