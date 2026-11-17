"""#1066: the Sessions, Usage, Context and Plans responses name the user and folder they read.

#1065 made these four routers read the chosen harness user's home. Their answers did not say whose home that was, and
an empty list could mean "nothing there" or "cannot read it". A `scope` block on the response says which user and
which `.claude` folder it read, whether that folder exists, and whether the folder the view reads can be listed. Every
test builds two real home trees and reads them through the API.
"""
import os
import shutil
from pathlib import Path

import pytest

from app.config import settings
from app.models.schemas import ActiveSessionsResponse, PlanListResponse, SessionProjectListResponse, UsageSummaryResponse
from app.utils import path_utils

from tests.test_harness_scope import AGENT, HEADER, client, homes  # noqa: F401  (fixtures)
from tests.test_harness_scope_data_routes import API, data, write_plan  # noqa: F401  (fixtures and builders)

ROUTES = ["/sessions/projects", "/usage/summary", "/context/active", "/plans", "/plans?provider=codex-cli"]


def expected_scope(user: str, home: Path, *, exists: bool = True, readable: bool = True) -> dict:
    return {"user": user, "claude_dir": str(home / ".claude"), "exists": exists, "readable": readable}


async def read(client, route: str, user: str | None = None):
    response = await client.get(API + route, headers={HEADER: user} if user else {})
    assert response.status_code == 200, (route, response.status_code, response.text)
    return response.json()


# ---- every response names the user and the folder it read --------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_each_response_names_the_user_and_the_folder_it_read(data, client, route):
    controller = path_utils.controller_user_name()

    assert (await read(client, route, AGENT))["scope"] == expected_scope(AGENT, data.agent)
    assert (await read(client, route, controller))["scope"] == expected_scope(controller, data.controller)
    assert (await read(client, route))["scope"] == expected_scope(AGENT, data.agent)     # the agent user is the default


@pytest.mark.asyncio
async def test_switching_users_leaks_nothing_between_requests(data, client):
    controller = path_utils.controller_user_name()
    for user, home in ((AGENT, data.agent), (controller, data.controller), (AGENT, data.agent),
                       (controller, data.controller)):
        for route in ROUTES:
            assert (await read(client, route, user))["scope"] == expected_scope(user, home), (route, user)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_a_single_user_install_names_the_controller(data, client, monkeypatch, route):
    monkeypatch.setattr(settings, "harness_agent_user", None)
    controller = path_utils.controller_user_name()

    assert (await read(client, route))["scope"] == expected_scope(controller, data.controller)


# ---- unreadable is not empty ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("route,key", [("/sessions/projects", "projects"), ("/usage/summary", "summary"),
                                       ("/context/active", "sessions"), ("/plans", "plans"),
                                       ("/plans?provider=codex-cli", "plans")])
async def test_a_user_with_no_claude_folder_is_empty_not_unreadable(data, client, route, key):
    shutil.rmtree(data.agent / ".claude")
    shutil.rmtree(data.agent / ".codex")

    body = await read(client, route, AGENT)

    assert body["scope"] == expected_scope(AGENT, data.agent, exists=False, readable=True)
    assert body[key] in ([], {}) or (key == "summary" and body[key]["total_tokens"] == 0)


@pytest.mark.asyncio
async def test_an_empty_plans_folder_says_readable_and_an_unreadable_one_does_not(data, client):
    """The controller's own plans folder: empty is a readable answer, unreadable is another thing with the same list."""
    if os.geteuid() == 0:
        pytest.skip("root can read everywhere")
    controller = path_utils.controller_user_name()
    plans = data.controller / ".claude" / "plans"
    (plans / "ctl-one.md").unlink()

    empty = await read(client, "/plans", controller)
    assert empty["plans"] == [] and empty["scope"]["readable"] is True and empty["scope"]["exists"] is True

    write_plan(data.controller, "ctl-one")
    plans.chmod(0o000)
    try:
        unreadable = await read(client, "/plans", controller)
    finally:
        plans.chmod(0o755)
    assert unreadable["plans"] == []                     # the same list as the empty one ...
    assert unreadable["scope"] == expected_scope(controller, data.controller, readable=False)   # ... but not empty


@pytest.mark.asyncio
async def test_a_configured_plans_folder_is_not_judged_by_the_homes_own_folder(data, client, tmp_path):
    """Only the home's own plans folder is checked. A plansDirectory setting points elsewhere and is read as before."""
    import json
    if os.geteuid() == 0:
        pytest.skip("root can read everywhere")
    elsewhere = tmp_path / "configured-plans"
    elsewhere.mkdir()
    (elsewhere / "cfg-one.md").write_text("# Plan: cfg-one\n\nBody.\n", encoding="utf-8")
    settings_file = data.controller / ".claude" / "settings.json"
    config = json.loads(settings_file.read_text())
    config["plansDirectory"] = str(elsewhere)
    settings_file.write_text(json.dumps(config))
    plans = data.controller / ".claude" / "plans"
    plans.chmod(0o000)
    try:
        body = await read(client, "/plans", path_utils.controller_user_name())
    finally:
        plans.chmod(0o755)

    assert [p["slug"] for p in body["plans"]] == ["cfg-one"] and body["scope"]["readable"] is True


@pytest.mark.asyncio
async def test_a_configured_plans_folder_that_cannot_be_listed_is_never_inspected(data, client, tmp_path):
    """The boundary: a path chosen by a setting is not opened to judge it, so its readability is not reported."""
    import json
    if os.geteuid() == 0:
        pytest.skip("root can read everywhere")
    elsewhere = tmp_path / "configured-plans"
    elsewhere.mkdir()
    (elsewhere / "cfg-one.md").write_text("# Plan: cfg-one\n\nBody.\n", encoding="utf-8")
    settings_file = data.controller / ".claude" / "settings.json"
    config = json.loads(settings_file.read_text())
    config["plansDirectory"] = str(elsewhere)
    settings_file.write_text(json.dumps(config))
    elsewhere.chmod(0o000)
    try:
        body = await read(client, "/plans", path_utils.controller_user_name())
    finally:
        elsewhere.chmod(0o755)

    assert body["plans"] == []
    assert body["scope"] == expected_scope(path_utils.controller_user_name(), data.controller)   # not judged: readable


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["controller", "agent"])
async def test_codex_plans_are_judged_by_the_codex_folder_not_by_an_unrelated_claude_folder(data, client, who):
    """V1's finding: readable Codex data was reported unreadable because .claude, which that view never reads, was not."""
    if os.geteuid() == 0:
        pytest.skip("root can read everywhere")
    user = path_utils.controller_user_name() if who == "controller" else AGENT
    home = data.controller if who == "controller" else data.agent
    claude = home / ".claude"
    claude.chmod(0o000)
    try:
        body = await read(client, "/plans?provider=codex-cli", user)
    finally:
        claude.chmod(0o755)

    assert [p["title"] for p in body["plans"]] != []                       # the response carries real Codex plans
    assert body["scope"] == expected_scope(user, home, exists=True, readable=True)   # so they are readable


@pytest.mark.asyncio
async def test_a_missing_codex_folder_is_empty_and_readable_not_unreadable(data, client):
    shutil.rmtree(data.controller / ".codex")

    body = await read(client, "/plans?provider=codex-cli", path_utils.controller_user_name())

    assert body["plans"] == [] and body["scope"]["readable"] is True


@pytest.mark.asyncio
async def test_the_claude_plans_view_still_depends_on_the_claude_folder(data, client):
    """No change to the four Claude-side views: their data lives under .claude, so an unreadable one still fails."""
    if os.geteuid() == 0:
        pytest.skip("root can read everywhere")
    claude = data.controller / ".claude"
    claude.chmod(0o000)
    try:
        response = await client.get(API + "/plans", headers={HEADER: path_utils.controller_user_name()})
    finally:
        claude.chmod(0o755)

    assert response.status_code == 500


# ---- what existed before is unchanged ----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_controllers_own_unreadable_folders_still_fail_as_they_did(data, client):
    """No 200 is invented for them: a 500 stays a 500 and the unhandled context error stays unhandled."""
    if os.geteuid() == 0:
        pytest.skip("root can read everywhere")
    controller = path_utils.controller_user_name()
    projects = data.controller / ".claude" / "projects"
    projects.chmod(0o000)
    try:
        for route in ("/sessions/projects", "/usage/summary"):
            response = await client.get(API + route, headers={HEADER: controller})
            assert response.status_code == 500, (route, response.status_code)
        with pytest.raises(PermissionError):
            await client.get(API + "/context/active", headers={HEADER: controller})
    finally:
        projects.chmod(0o755)
    codex = data.controller / ".codex"
    codex.chmod(0o000)
    try:
        response = await client.get(API + "/plans?provider=codex-cli", headers={HEADER: controller})
        assert response.status_code == 500
    finally:
        codex.chmod(0o755)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["/sessions/projects", "/usage/summary", "/context/active", "/plans"])
async def test_another_users_unreadable_home_still_answers_403(data, client, route):
    if os.geteuid() == 0:
        pytest.skip("root can read everywhere")
    claude = data.agent / ".claude"
    claude.chmod(0o000)
    try:
        response = await client.get(API + route, headers={HEADER: AGENT})
        assert response.status_code == 403, (route, response.status_code)
        assert response.json()["detail"].startswith("The controller cannot read ")
    finally:
        claude.chmod(0o755)


@pytest.mark.asyncio
async def test_the_harness_scope_listing_is_unchanged(data, client):
    body = (await client.get(API + "/harness-scope")).json()

    assert set(body) == {"users", "default_user"}
    assert all(set(u) == {"name", "is_controller", "is_default", "claude_dir", "exists", "readable", "writable"}
               for u in body["users"])


@pytest.mark.parametrize("model,kwargs", [
    (SessionProjectListResponse, {"projects": [], "total_sessions": 0}),
    (ActiveSessionsResponse, {"sessions": []}),
    (PlanListResponse, {"plans": [], "total": 0}),
])
def test_the_scope_field_is_optional_so_other_builders_of_these_responses_still_validate(model, kwargs):
    assert model(**kwargs).scope is None


def test_the_usage_summary_response_validates_without_a_scope():
    from app.models.schemas import UsageSummary

    summary = UsageSummary(total_cost=0.0, total_input_tokens=0, total_output_tokens=0, total_cache_creation_tokens=0,
                           total_cache_read_tokens=0, total_tokens=0, project_count=0, session_count=0, models_used=[])

    assert UsageSummaryResponse(summary=summary).scope is None
