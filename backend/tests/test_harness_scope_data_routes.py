"""#1065: the Sessions, Usage, Context and Plans routers follow the chosen harness user, like the configuration ones.

#969 gave the scope to the configuration routers only, so these four always read the controller's home. Every test
builds two real home trees and reads them through the API, so a fall-back to the controller's home shows as a wrong
value. The cached data (session summaries, usage answers) is kept apart per home with no schema change.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import settings
from app.utils import path_utils

from tests.test_codex_plan_service import _write_rollout, _write_state_db
from tests.test_harness_scope import AGENT, HEADER, client, homes  # noqa: F401  (fixtures)

API = "/api/v1"


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_session(home: Path, folder: str, session_id: str, tokens: int, pairs: int = 1) -> None:
    """One transcript of `pairs` user and assistant messages (so 2 * pairs messages)."""
    directory = home / ".claude" / "projects" / folder
    directory.mkdir(parents=True, exist_ok=True)
    rows = []
    for index in range(pairs):
        rows += [
            {"type": "user", "timestamp": _stamp(), "sessionId": session_id, "cwd": "/synthetic/project",
             "message": {"role": "user", "content": f"hello {index} from {session_id}"}},
            {"type": "assistant", "timestamp": _stamp(), "sessionId": session_id,
             "message": {"role": "assistant", "model": "claude-sonnet-4-5",
                         "content": [{"type": "text", "text": "hi"}],
                         "usage": {"input_tokens": tokens, "output_tokens": 1, "cache_creation_input_tokens": 0,
                                   "cache_read_input_tokens": 0}}},
        ]
    (directory / f"{session_id}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def write_plan(home: Path, slug: str) -> None:
    plans = home / ".claude" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / f"{slug}.md").write_text(f"# Plan: {slug}\n\nBody of {slug}.\n", encoding="utf-8")


def write_codex_plan(home: Path, tag: str) -> None:
    codex = home / ".codex"
    project = home / f"{tag}-repo"
    project.mkdir(parents=True, exist_ok=True)
    rollout = codex / "sessions" / "2026" / "06" / "16" / f"rollout-{tag}.jsonl"
    _write_state_db(codex, thread_id=f"{tag}-thread", rollout_path=rollout, cwd=project)
    _write_rollout(rollout, [{
        "type": "response_item", "timestamp": "2026-06-16T10:00:00Z",
        "payload": {"type": "function_call", "name": "update_plan",
                    "arguments": json.dumps({"plan": [{"step": f"{tag} step", "status": "in_progress"}]})},
    }])


@pytest.fixture
def data(homes, monkeypatch):  # noqa: F811
    """The controller has one session, one plan and one Codex plan; the agent user has three, two and one."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(settings, "harness_agent_user", AGENT)
    write_session(homes.controller, "-ctl-alpha", "ctl-s1", 100)
    write_plan(homes.controller, "ctl-one")
    write_codex_plan(homes.controller, "ctl")
    write_session(homes.agent, "-agt-beta", "agt-s1", 1000)
    write_session(homes.agent, "-agt-beta", "agt-s2", 1000)
    write_session(homes.agent, "-agt-gamma", "agt-s3", 1000)
    write_plan(homes.agent, "agt-one")
    write_plan(homes.agent, "agt-two")
    write_codex_plan(homes.agent, "agt")
    return homes


async def snapshot(client, headers) -> dict:
    """What the four routers say for one user (the sessions list is read too, so the summaries get cached)."""
    async def get(url):
        response = await client.get(API + url, headers=headers)
        assert response.status_code == 200, (url, response.text)
        return response.json()

    sessions = await get("/sessions")
    projects = await get("/sessions/projects")
    stats = await get("/sessions/dashboard/stats")
    usage = (await get("/usage/summary"))["summary"]
    context = await get("/context/active")
    plans = await get("/plans")
    codex = await get("/plans?provider=codex-cli")
    return {
        "sessions": sorted(s["id"] for s in sessions["sessions"]),
        "folders": sorted(p["folder"] for p in projects["projects"]),
        "total_sessions": projects["total_sessions"],
        "stats_today": stats["sessions_today"],
        "input_tokens": usage["total_input_tokens"],
        "context": sorted(s["session_id"] for s in context["sessions"]),
        "plans": sorted(p["slug"] for p in plans["plans"]),
        "codex": sorted(p["title"] for p in codex["plans"]),
    }


CONTROLLER = {
    "sessions": ["ctl-s1"], "folders": ["-ctl-alpha"], "total_sessions": 1, "stats_today": 1, "input_tokens": 100,
    "context": ["ctl-s1"], "plans": ["ctl-one"], "codex": ["ctl-repo - ctl step"],
}
AGENT_EXPECTED = {
    "sessions": ["agt-s1", "agt-s2", "agt-s3"], "folders": ["-agt-beta", "-agt-gamma"], "total_sessions": 3,
    "stats_today": 3, "input_tokens": 3000, "context": ["agt-s1", "agt-s2", "agt-s3"],
    "plans": ["agt-one", "agt-two"], "codex": ["agt-repo - agt step"],
}


def assert_same(actual: dict, expected: dict) -> None:
    for key, value in expected.items():
        assert actual[key] == value, (key, actual[key], value)


# ---- every route follows the chosen user -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_agent_user_is_the_default_scope_of_the_four_routers(data, client):
    assert_same(await snapshot(client, {}), AGENT_EXPECTED)


@pytest.mark.asyncio
async def test_the_header_switches_users_on_all_four_routers_and_nothing_leaks_between_requests(data, client):
    controller = path_utils.controller_user_name()
    for user, expected in ((AGENT, AGENT_EXPECTED), (controller, CONTROLLER), (AGENT, AGENT_EXPECTED),
                           (controller, CONTROLLER)):
        assert_same(await snapshot(client, {HEADER: user}), expected)


@pytest.mark.asyncio
async def test_a_single_user_install_reads_the_controllers_home_unchanged(data, client, monkeypatch):
    monkeypatch.setattr(settings, "harness_agent_user", None)
    assert_same(await snapshot(client, {}), CONTROLLER)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["/sessions/projects", "/usage/summary", "/context/active", "/plans",
                                   "/plans?provider=codex-cli"])
async def test_a_user_outside_the_allowed_list_is_refused_on_each_route(data, client, route):
    response = await client.get(API + route, headers={HEADER: "somebody-else"})

    assert response.status_code == 400, response.text


# ---- a cached session row is used only for the file it was built from (V1's review of #1110) ------------------------


async def total_messages(client, user) -> int:
    response = await client.get(API + "/sessions/dashboard/stats", headers={HEADER: user})
    assert response.status_code == 200, response.text
    return response.json()["total_messages"]


@pytest.mark.asyncio
async def test_the_statistics_never_use_another_homes_cached_row_for_the_same_session_id(homes, client, monkeypatch):
    """The same folder and session id in two homes, with different transcripts: one cache row, never the wrong home's."""
    monkeypatch.setattr(settings, "harness_agent_user", AGENT)
    controller = path_utils.controller_user_name()
    write_session(homes.controller, "-same", "dup", 10, pairs=1)      # 2 messages
    write_session(homes.agent, "-same", "dup", 10, pairs=2)           # 4 messages
    assert (await client.get(API + "/sessions", headers={HEADER: controller})).status_code == 200  # warm only this one

    assert await total_messages(client, AGENT) == 4          # not the controller's cached 2
    assert await total_messages(client, controller) == 2     # and back: the row now describes the other file again
    assert await total_messages(client, AGENT) == 4


@pytest.mark.asyncio
async def test_a_session_that_grew_since_it_was_cached_is_counted_with_its_current_messages(homes, client, monkeypatch):
    monkeypatch.setattr(settings, "harness_agent_user", None)
    write_session(homes.controller, "-grow", "g1", 10, pairs=1)
    assert (await client.get(API + "/sessions")).status_code == 200
    assert await total_messages(client, path_utils.controller_user_name()) == 2

    write_session(homes.controller, "-grow", "g1", 10, pairs=3)       # the same file, now longer

    assert await total_messages(client, path_utils.controller_user_name()) == 6


@pytest.mark.asyncio
async def test_an_unchanged_session_is_not_scanned_again_by_the_statistics(homes, client, monkeypatch):
    """The refresh costs a scan only for a row whose file changed or belongs to another home."""
    from app.services.session_service import SessionService

    monkeypatch.setattr(settings, "harness_agent_user", None)
    write_session(homes.controller, "-calm", "c1", 10, pairs=1)
    assert (await client.get(API + "/sessions")).status_code == 200
    scans = []
    original = SessionService.scan_for_listing

    async def counting(self, path):
        scans.append(path)
        return await original(self, path)

    monkeypatch.setattr(SessionService, "scan_for_listing", counting)

    assert await total_messages(client, path_utils.controller_user_name()) == 2
    assert scans == []


# ---- a Plans folder that cannot be listed answers 403, not an empty list (V1's review of #1110) ------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["/plans", "/plans/stats", "/plans/search?q=agt", "/plans/agt-one.md"])
async def test_an_unreadable_plans_folder_answers_403_and_names_it(data, client, route):
    if os.geteuid() == 0:
        pytest.skip("root can read everywhere")
    plans = data.agent / ".claude" / "plans"
    plans.chmod(0o000)
    try:
        response = await client.get(API + route, headers={HEADER: AGENT})
        assert response.status_code == 403, (route, response.status_code, response.text)
        detail = response.json()["detail"]
        assert detail.startswith("The controller cannot read ") and detail.endswith(f"It belongs to {AGENT}."), detail
        assert str(plans) in detail
    finally:
        plans.chmod(0o755)


@pytest.mark.asyncio
async def test_only_the_homes_own_plans_folder_is_opened_never_a_configured_one(data, client, tmp_path):
    """A plansDirectory setting points at a folder the request's settings chose. It is read as before, not probed."""
    if os.geteuid() == 0:
        pytest.skip("root can read everywhere")
    elsewhere = tmp_path / "configured-plans"
    elsewhere.mkdir()
    (elsewhere / "cfg-one.md").write_text("# Plan: cfg-one\n\nBody.\n", encoding="utf-8")
    settings_file = data.agent / ".claude" / "settings.json"
    config = json.loads(settings_file.read_text())
    config["plansDirectory"] = str(elsewhere)
    settings_file.write_text(json.dumps(config))
    plans = data.agent / ".claude" / "plans"
    plans.chmod(0o000)          # the home's own folder is unreadable, but it is not the one that is configured
    try:
        response = await client.get(API + "/plans", headers={HEADER: AGENT})
        assert response.status_code == 200, response.text
        assert [p["slug"] for p in response.json()["plans"]] == ["cfg-one"]
    finally:
        plans.chmod(0o755)


@pytest.mark.asyncio
async def test_the_controllers_own_unreadable_plans_folder_behaves_as_before(data, client):
    """No change for the controller: an unreadable folder of its own still lists as empty."""
    if os.geteuid() == 0:
        pytest.skip("root can read everywhere")
    plans = data.controller / ".claude" / "plans"
    plans.chmod(0o000)
    try:
        headers = {HEADER: path_utils.controller_user_name()}
        for route in ("/plans", "/plans/stats"):
            response = await client.get(API + route, headers=headers)
            assert response.status_code == 200, (route, response.status_code, response.text)
        assert (await client.get(API + "/plans", headers=headers)).json()["total"] == 0
    finally:
        plans.chmod(0o755)


# ---- authorization is as it was ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_usage_write_still_needs_the_director_key_and_the_reads_stay_open(data, client, monkeypatch):
    """The scope adds no authorization to Sessions, Context and Plans, and Usage keeps the key it had for writes."""
    monkeypatch.setattr(settings, "operator_token", "director-key")
    for route in ("/sessions/projects", "/usage/summary", "/context/active", "/plans"):
        assert (await client.get(API + route, headers={HEADER: AGENT})).status_code == 200, route

    refused = await client.post(API + "/usage/cache/invalidate", headers={HEADER: AGENT})
    allowed = await client.post(API + "/usage/cache/invalidate",
                                headers={HEADER: AGENT, "X-Synth-City-Operator-Token": "director-key"})

    assert refused.status_code in (401, 403), refused.text
    assert allowed.status_code == 200, allowed.text


# ---- the cached data is kept apart per home ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_session_statistics_count_only_the_chosen_homes_cached_sessions(data, client):
    controller = path_utils.controller_user_name()
    await snapshot(client, {HEADER: AGENT})            # caches the agent user's three summaries
    await snapshot(client, {HEADER: controller})       # and the controller's one

    for user, today in ((controller, 1), (AGENT, 3), (controller, 1)):
        stats = (await client.get(API + "/sessions/dashboard/stats", headers={HEADER: user})).json()
        assert stats["sessions_today"] == today and stats["total_sessions"] == today, (user, stats)
        assert stats["most_active_project"] is not None


@pytest.mark.asyncio
async def test_a_usage_answer_cached_for_one_home_is_not_served_for_another(data, client):
    controller = path_utils.controller_user_name()
    seen = []
    for user in (AGENT, controller, AGENT, controller):   # the second read of each user is a cache hit
        seen.append((await client.get(API + "/usage/summary", headers={HEADER: user})).json()["summary"][
            "total_input_tokens"])

    assert seen == [3000, 100, 3000, 100]


@pytest.mark.asyncio
async def test_the_usage_cache_key_has_the_home_only_for_another_users_home(data):
    from app.services.usage_service import UsageService

    service = UsageService()
    plain = await service.get_cache_key("summary", "/some/project", days=3)
    assert "home:" not in plain                                    # a single-user install keeps its cache valid
    token = path_utils.set_scope_home(Path.home())
    try:
        assert await service.get_cache_key("summary", "/some/project", days=3) == plain   # the controller: unchanged
    finally:
        path_utils._scope_home.reset(token)
    token = path_utils.set_scope_home(data.agent)
    try:
        scoped = await service.get_cache_key("summary", "/some/project", days=3)
    finally:
        path_utils._scope_home.reset(token)
    assert scoped != plain and f"home:{data.agent}" in scoped


# ---- the Codex plans follow the scope too ------------------------------------------------------------------------------


def test_the_codex_plan_home_follows_a_non_controller_scope_and_nothing_else(data, monkeypatch):
    from app.services.codex_plan_service import CodexPlanService
    from app.services.providers.codex_cli import get_codex_home

    assert CodexPlanService().codex_home == get_codex_home() == data.controller / ".codex"
    monkeypatch.setenv("CODEX_HOME", str(data.controller / "custom-codex"))
    assert CodexPlanService().codex_home == data.controller / "custom-codex"      # the controller's setting is kept
    token = path_utils.set_scope_home(Path.home())
    try:
        assert CodexPlanService().codex_home == data.controller / "custom-codex"  # the controller's own scope too
    finally:
        path_utils._scope_home.reset(token)
    token = path_utils.set_scope_home(data.agent)
    try:
        # The controller's CODEX_HOME is the controller's: it must not point the agent user's scope at it.
        assert CodexPlanService().codex_home == data.agent / ".codex"
        assert CodexPlanService(codex_home=data.controller / "explicit").codex_home == data.controller / "explicit"
    finally:
        path_utils._scope_home.reset(token)


# ---- a home the controller cannot read -----------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["/sessions/projects", "/sessions", "/sessions/dashboard/stats", "/usage/summary",
                                   "/context/active", "/plans"])
async def test_a_home_the_controller_cannot_read_answers_403_and_names_it(data, client, route):
    if os.geteuid() == 0:
        pytest.skip("root can read everywhere")
    claude = data.agent / ".claude"
    claude.chmod(0o000)
    try:
        response = await client.get(API + route, headers={HEADER: AGENT})
        assert response.status_code == 403, (route, response.status_code, response.text)
        detail = response.json()["detail"]
        assert detail.startswith("The controller cannot read ") and detail.endswith(f"It belongs to {AGENT}."), detail
        assert str(data.agent) in detail
        # The controller's own home still works, and so does the same route for it.
        assert (await client.get(API + route, headers={HEADER: path_utils.controller_user_name()})).status_code == 200
    finally:
        claude.chmod(0o755)


@pytest.mark.asyncio
async def test_the_codex_plans_of_a_home_the_controller_cannot_read_answer_403_too(data, client):
    if os.geteuid() == 0:
        pytest.skip("root can read everywhere")
    codex = data.agent / ".codex"
    codex.chmod(0o000)
    try:
        response = await client.get(API + "/plans?provider=codex-cli", headers={HEADER: AGENT})
        assert response.status_code == 403, (response.status_code, response.text)
        assert str(codex) in response.json()["detail"] and AGENT in response.json()["detail"]
    finally:
        codex.chmod(0o755)
