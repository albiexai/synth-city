"""#1068: Memory has no project scope when no project is chosen, so it never reads or writes the backend's own directory.

With no `project_path`, the Memory service resolved the project CLAUDE.md, CLAUDE.local.md and the rules folder against
`Path.cwd()`, the working directory of the backend process: a controller path shown as "Project CLAUDE.md". Every test
makes such a directory the backend's working directory and puts files in it, so a fall-back to it shows as a wrong entry
or a file written there. A chosen project behaves as before, and the user entry names the scoped user's home.
"""
import json
from pathlib import Path

import pytest

from app.config import settings
from app.utils import path_utils

from tests.test_harness_scope import AGENT, HEADER, client, homes  # noqa: F401  (fixtures)

API = "/api/v1"


@pytest.fixture
def backend_cwd(tmp_path, monkeypatch):
    """The directory the backend was started in: it has a project CLAUDE.md, a local one and a rule of its own."""
    directory = tmp_path / "backend-cwd"
    (directory / ".claude" / "rules").mkdir(parents=True)
    (directory / "CLAUDE.md").write_text("# backend cwd project memory\n", encoding="utf-8")
    (directory / "CLAUDE.local.md").write_text("# backend cwd local memory\n", encoding="utf-8")
    (directory / ".claude" / "rules" / "cwd-rule.md").write_text("# cwd rule\n", encoding="utf-8")
    monkeypatch.chdir(directory)
    return directory


@pytest.fixture
def project(tmp_path):
    """A project the user chose, with its own memory and one rule."""
    directory = tmp_path / "chosen-project"
    (directory / ".claude" / "rules").mkdir(parents=True)
    (directory / "CLAUDE.md").write_text("# chosen project memory\n", encoding="utf-8")
    (directory / "CLAUDE.local.md").write_text("# chosen project local memory\n", encoding="utf-8")
    (directory / ".claude" / "rules" / "project-rule.md").write_text("# project rule\n", encoding="utf-8")
    return directory


def paths_under(files, directory: Path) -> list[str]:
    return [f["path"] for f in files if str(f["path"]).startswith(str(directory))]


# ---- no project chosen: nothing is read from the backend's directory ----------------------------------------------------


@pytest.mark.asyncio
async def test_with_no_project_the_hierarchy_has_only_the_managed_and_user_entries(homes, client, backend_cwd):  # noqa: F811
    response = await client.get(API + "/memory/hierarchy")

    assert response.status_code == 200, response.text
    files = response.json()["files"]
    assert {f["scope"] for f in files} == {"managed", "user"}
    assert paths_under(files, backend_cwd) == []           # no 'Project CLAUDE.md' at the backend's own path


@pytest.mark.asyncio
async def test_with_no_project_the_rules_list_is_empty_and_names_no_backend_folder(homes, client, backend_cwd):  # noqa: F811
    body = (await client.get(API + "/memory/rules")).json()

    assert body["rules"] == []
    assert body["rules_dir"] == ""                         # not '<backend cwd>/.claude/rules'


@pytest.mark.asyncio
async def test_with_no_project_a_rule_is_refused_and_nothing_is_written_to_the_backend_directory(
        homes, client, backend_cwd):  # noqa: F811
    before = sorted(p.name for p in (backend_cwd / ".claude" / "rules").iterdir())

    response = await client.post(API + "/memory/rules", json={"name": "new-rule", "content": "# new\n"})

    assert response.status_code == 400                       # the endpoint's existing answer to a failed create
    assert "project" in response.json()["detail"].lower()
    assert sorted(p.name for p in (backend_cwd / ".claude" / "rules").iterdir()) == before
    assert not (backend_cwd / ".claude" / "rules" / "new-rule.md").exists()


# ---- a chosen project behaves as before ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_chosen_project_has_its_own_entries_and_never_the_backend_directorys(homes, client, backend_cwd, project):  # noqa: F811
    files = (await client.get(API + "/memory/hierarchy", params={"project_path": str(project)})).json()["files"]

    assert {f["scope"] for f in files} == {"managed", "user", "project", "local", "rules"}
    assert paths_under(files, project) != [] and paths_under(files, backend_cwd) == []
    by_scope = {f["scope"]: f for f in files}
    assert by_scope["project"]["path"] == str(project / "CLAUDE.md") and by_scope["project"]["exists"] is True
    assert by_scope["local"]["path"] == str(project / "CLAUDE.local.md")
    assert by_scope["rules"]["name"] == "project-rule"


@pytest.mark.asyncio
async def test_a_chosen_projects_rules_are_listed_and_a_new_one_goes_to_that_project(homes, client, backend_cwd, project):  # noqa: F811
    params = {"project_path": str(project)}

    listed = (await client.get(API + "/memory/rules", params=params)).json()
    assert [r["name"] for r in listed["rules"]] == ["project-rule"]
    assert listed["rules_dir"] == str(project / ".claude" / "rules")

    created = await client.post(API + "/memory/rules", params=params, json={"name": "second", "content": "# second\n"})
    assert created.status_code == 200 and created.json()["success"] is True
    assert (project / ".claude" / "rules" / "second.md").exists()
    assert not (backend_cwd / ".claude" / "rules" / "second.md").exists()


@pytest.mark.asyncio
async def test_a_chosen_project_that_does_not_exist_yet_still_gets_its_preferred_location(homes, client, backend_cwd, tmp_path):  # noqa: F811
    missing = tmp_path / "not-created-yet"
    files = (await client.get(API + "/memory/hierarchy", params={"project_path": str(missing)})).json()["files"]

    project_entry = next(f for f in files if f["scope"] == "project")
    assert project_entry["path"] == str(missing / "CLAUDE.md") and project_entry["exists"] is False


# ---- the user entry names the scoped user's home ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_user_entry_is_the_agent_users_by_default_and_the_controllers_with_the_header(
        homes, client, backend_cwd, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "harness_agent_user", AGENT)
    controller = path_utils.controller_user_name()

    async def user_path(headers):
        files = (await client.get(API + "/memory/hierarchy", headers=headers)).json()["files"]
        return next(f["path"] for f in files if f["scope"] == "user")

    assert await user_path({}) == str(homes.agent / ".claude" / "CLAUDE.md")
    assert await user_path({HEADER: AGENT}) == str(homes.agent / ".claude" / "CLAUDE.md")
    assert await user_path({HEADER: controller}) == str(homes.controller / ".claude" / "CLAUDE.md")
    assert await user_path({}) == str(homes.agent / ".claude" / "CLAUDE.md")       # nothing leaked between requests


@pytest.mark.asyncio
async def test_a_single_user_install_names_the_controllers_home(homes, client, backend_cwd, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "harness_agent_user", None)

    files = (await client.get(API + "/memory/hierarchy")).json()["files"]

    assert next(f["path"] for f in files if f["scope"] == "user") == str(homes.controller / ".claude" / "CLAUDE.md")


# ---- Configuration was already right, and stays so ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_configuration_with_no_project_shows_no_backend_path_and_names_the_scoped_users_file(
        homes, client, backend_cwd, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "harness_agent_user", AGENT)
    (backend_cwd / ".claude" / "settings.json").write_text(json.dumps({"model": "cwd-model"}), encoding="utf-8")

    scopes = (await client.get(API + "/config/resolved")).json()["scopes"]

    assert scopes["project"]["path"] is None and scopes["project"]["exists"] is False
    assert scopes["local"]["path"] is None
    assert scopes["project"]["settings"] == {} and scopes["local"]["settings"] == {}
    assert scopes["user"]["path"] == str(homes.agent / ".claude" / "settings.json")
