"""#969: the global scope of a Harnesses page is a chosen OS user's home, not the controller's.

Single-user installs keep reading the controller's home. With harness_agent_user set, the default is the agent
user's home, the header can pick the controller instead, and no other user can be named. Every test builds two
real home trees and reads them through the API, so a fall-back to the controller's home shows as a wrong count.
"""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models.database  # noqa: F401
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.utils import path_utils

AGENT = "agentuser"
HEADER = "X-Harness-Scope-User"


def _populate(home: Path, tag: str, *, skills=0, agents=0, commands=0, servers=0) -> None:
    claude = home / ".claude"
    for index in range(skills):
        skill = claude / "skills" / f"{tag}-skill-{index}"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: {tag}-skill-{index}\ndescription: {tag} skill\n---\nBody\n")
    for index in range(agents):
        (claude / "agents").mkdir(parents=True, exist_ok=True)
        (claude / "agents" / f"{tag}-agent-{index}.md").write_text(
            f"---\nname: {tag}-agent-{index}\ndescription: {tag} agent\n---\nBody\n")
    for index in range(commands):
        (claude / "commands").mkdir(parents=True, exist_ok=True)
        (claude / "commands" / f"{tag}-command-{index}.md").write_text(f"---\ndescription: {tag}\n---\nBody\n")
    claude.mkdir(parents=True, exist_ok=True)
    (claude / "CLAUDE.md").write_text(f"# {tag} memory\n")
    (claude / "settings.json").write_text(json.dumps({
        "model": f"{tag}-model",
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": f"echo {tag}"}]}]},
    }))
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {f"{tag}-server-{i}": {"command": "true"} for i in range(servers)}}))


@pytest.fixture
def homes(tmp_path, monkeypatch):
    controller, agent = tmp_path / "controller", tmp_path / "agent"
    controller.mkdir(), agent.mkdir()
    _populate(controller, "ctl")  # the controller has nothing: the page used to say "None yet"
    _populate(agent, "agt", skills=1, agents=2, commands=1, servers=3)
    monkeypatch.setenv("HOME", str(controller))
    monkeypatch.setattr(settings, "operator_token", "")
    monkeypatch.setattr(
        path_utils.pwd, "getpwnam",
        lambda name: SimpleNamespace(pw_dir=str(agent)) if name == AGENT else (_ for _ in ()).throw(KeyError(name)),
    )
    return SimpleNamespace(controller=controller, agent=agent)


@pytest_asyncio.fixture
async def client():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _override():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_db] = _override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    await engine.dispose()


async def _counts(client, headers=None) -> dict:
    api = "/api/v1"
    get = lambda url: client.get(api + url, headers=headers or {})  # noqa: E731
    skills, agents, commands, servers = (await get("/agents/skills"), await get("/agents"),
                                         await get("/commands"), await get("/mcp/servers"))
    for response in (skills, agents, commands, servers):
        assert response.status_code == 200, response.text
    return {
        "skills": len([s for s in skills.json()["skills"] if s["location"] == "user"]),
        "agents": len([a for a in agents.json()["agents"] if a["scope"] == "user"]),
        "commands": len([c for c in commands.json()["commands"] if c["scope"] == "user"]),
        "servers": len([s for s in servers.json()["servers"] if s["scope"] == "user"]),
    }


@pytest.mark.asyncio
async def test_a_single_user_install_reads_the_controllers_home_unchanged(homes, client):
    _populate(homes.controller, "ctl", skills=2, agents=1, commands=3, servers=1)
    assert settings.harness_agent_user is None
    assert await _counts(client) == {"skills": 2, "agents": 1, "commands": 3, "servers": 1}
    body = (await client.get("/api/v1/harness-scope")).json()
    assert [u["name"] for u in body["users"]] == [path_utils.controller_user_name()]
    assert body["default_user"] == path_utils.controller_user_name()
    assert body["users"][0]["claude_dir"] == str(homes.controller / ".claude")


@pytest.mark.asyncio
async def test_an_agent_user_is_the_default_global_scope_on_a_two_user_install(homes, client, monkeypatch):
    monkeypatch.setattr(settings, "harness_agent_user", AGENT)
    # Fails if the resolution falls back to the controller's home (which has 0 of each).
    assert await _counts(client) == {"skills": 1, "agents": 2, "commands": 1, "servers": 3}
    body = (await client.get("/api/v1/harness-scope")).json()
    assert body["default_user"] == AGENT
    by_name = {u["name"]: u for u in body["users"]}
    assert by_name[AGENT]["claude_dir"] == str(homes.agent / ".claude") and by_name[AGENT]["is_default"]
    assert by_name[path_utils.controller_user_name()]["claude_dir"] == str(homes.controller / ".claude")
    assert not by_name[path_utils.controller_user_name()]["is_default"]


@pytest.mark.asyncio
async def test_the_header_switches_between_the_two_users_and_nothing_leaks_across_requests(homes, client, monkeypatch):
    monkeypatch.setattr(settings, "harness_agent_user", AGENT)
    controller = path_utils.controller_user_name()
    empty = {"skills": 0, "agents": 0, "commands": 0, "servers": 0}
    agent = {"skills": 1, "agents": 2, "commands": 1, "servers": 3}
    assert await _counts(client, {HEADER: controller}) == empty
    assert await _counts(client, {HEADER: AGENT}) == agent
    assert await _counts(client, {HEADER: controller}) == empty
    assert await _counts(client) == agent  # a request without the header never inherits the last one
    assert path_utils.get_user_home() == homes.controller  # and nothing outside a request sees the override


@pytest.mark.asyncio
async def test_other_surfaces_follow_the_chosen_user(homes, client, monkeypatch):
    monkeypatch.setattr(settings, "harness_agent_user", AGENT)
    for user, tag in ((AGENT, "agt"), (path_utils.controller_user_name(), "ctl")):
        headers = {HEADER: user}
        hooks = (await client.get("/api/v1/hooks", headers=headers)).json()["hooks"]
        assert [h["command"] for h in hooks] == [f"echo {tag}"], user
        hierarchy = await client.get("/api/v1/memory/hierarchy", headers=headers)
        user_file = next(f for f in hierarchy.json()["files"] if f["scope"] == "user")
        assert user_file["path"] == str((homes.agent if tag == "agt" else homes.controller) / ".claude" / "CLAUDE.md"), user
        config = await client.get("/api/v1/config", headers=headers)
        assert f"{tag}-model" in json.dumps(config.json()), user


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["root", "someone-else", "../agent", AGENT + " "])
async def test_a_user_outside_the_allowed_list_is_refused(homes, client, monkeypatch, name):
    monkeypatch.setattr(settings, "harness_agent_user", AGENT)
    response = await client.get("/api/v1/agents/skills", headers={HEADER: name})
    assert response.status_code == 400
    # With no agent user configured the agent's name is not allowed either.
    monkeypatch.setattr(settings, "harness_agent_user", None)
    assert (await client.get("/api/v1/agents/skills", headers={HEADER: AGENT})).status_code == 400


@pytest.mark.asyncio
async def test_a_configured_user_that_does_not_exist_is_an_error_not_a_fall_back(homes, client, monkeypatch):
    monkeypatch.setattr(settings, "harness_agent_user", "nobody-here")
    response = await client.get("/api/v1/agents/skills")
    assert response.status_code == 503 and "nobody-here" in response.json()["detail"]
    assert (await client.get("/api/v1/harness-scope")).status_code == 503


@pytest.mark.asyncio
async def test_the_agent_user_is_the_controller_user_when_they_are_the_same(homes, client, monkeypatch):
    monkeypatch.setattr(settings, "harness_agent_user", path_utils.controller_user_name())
    body = (await client.get("/api/v1/harness-scope")).json()
    assert len(body["users"]) == 1 and body["users"][0]["is_default"]


@pytest.mark.asyncio
async def test_a_home_the_controller_cannot_write_answers_403_and_names_it(homes, client, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("root can write everywhere")
    monkeypatch.setattr(settings, "harness_agent_user", AGENT)
    agents = homes.agent / ".claude" / "agents"
    agents.chmod(0o555)
    try:
        response = await client.post("/api/v1/agents", json={"name": "new", "scope": "user", "prompt": "Body"})
        assert response.status_code == 403, response.text
        assert str(agents) in response.json()["detail"] and AGENT in response.json()["detail"]
        # A read of the same home still works: only the write is refused.
        assert (await client.get("/api/v1/agents")).status_code == 200
    finally:
        agents.chmod(0o755)


@pytest.mark.asyncio
async def test_the_harness_scope_listing_reports_an_unwritable_claude_directory(homes, client, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("root can write everywhere")
    monkeypatch.setattr(settings, "harness_agent_user", AGENT)
    claude = homes.agent / ".claude"
    claude.chmod(0o555)
    try:
        scope = {u["name"]: u for u in (await client.get("/api/v1/harness-scope")).json()["users"]}
        assert scope[AGENT]["readable"] is True and scope[AGENT]["writable"] is False
    finally:
        claude.chmod(0o755)


@pytest.mark.asyncio
async def test_backups_and_the_controllers_own_data_never_follow_the_scope(homes, client, monkeypatch):
    from app.services.backup_service import get_backup_storage_dir

    monkeypatch.setattr(settings, "harness_agent_user", AGENT)
    token = path_utils.set_scope_home(homes.agent)
    try:
        assert get_backup_storage_dir() == homes.controller / ".albiex-synth-city" / "backups"
    finally:
        path_utils._scope_home.reset(token)
