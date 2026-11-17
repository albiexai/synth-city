"""#1069: the Harnesses status shows the agent user's Claude Code version, not the controller's.

`AgentProvider.get_version()` runs `<claude on the controller's PATH> --version`. For a scoped non-controller user the
version is read from disk instead, through the ACTIVE launcher only (never a folder or package that merely exists),
with nothing executed: no `sudo` and no execution as the agent user. When it cannot be read the status says exactly
"version unknown (cannot read the agent user's install)". Every test uses temporary homes, never a real user's home.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import settings
from app.services.claude_install_version import UNKNOWN, read_installed_version
from app.utils import path_utils

from tests.test_harness_scope import AGENT, HEADER, client, homes  # noqa: F401  (fixtures)

API = "/api/v1"
CONTROLLER_VERSION = "9.9.9"
UNKNOWN_TEXT = "version unknown (cannot read the agent user's install)"


def native_install(home: Path, version: str) -> Path:
    """The native installer: ~/.local/bin/claude is a symlink to ~/.local/share/claude/versions/<version>."""
    binary = home / ".local" / "share" / "claude" / "versions" / version
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"\x7fELF not a real binary\n")
    launcher = home / ".local" / "bin" / "claude"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    if launcher.is_symlink() or launcher.exists():
        launcher.unlink()
    launcher.symlink_to(binary)
    return launcher


def npm_install(home: Path, version: object, *, prefix: str = ".npm-global", launch: bool = True) -> Path:
    """An npm install under the user's own prefix: <prefix>/bin/claude -> ../lib/node_modules/@anthropic-ai/claude-code/cli.js."""
    package = home / prefix / "lib" / "node_modules" / "@anthropic-ai" / "claude-code"
    package.mkdir(parents=True, exist_ok=True)
    (package / "cli.js").write_text("// not a real cli\n", encoding="utf-8")
    (package / "package.json").write_text(json.dumps({"name": "@anthropic-ai/claude-code", "version": version}),
                                          encoding="utf-8")
    launcher = home / prefix / "bin" / "claude"
    if launch:
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.symlink_to(Path("..") / "lib" / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js")
    return launcher


@pytest.fixture
def scoped(homes, monkeypatch):  # noqa: F811
    """Two-user install. No real program ever runs.

    The controller's own `--version` call goes through the standard library, which is one module object in every
    test run, so it is faked there (`shutil.which` and `subprocess.run`) and not on an app class that an earlier test
    may have replaced. Every program the status code tries to run is recorded in `homes.executed`.
    """
    monkeypatch.setattr(settings, "harness_agent_user", AGENT)
    executed: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
        executed.append([str(part) for part in argv])
        return subprocess.CompletedProcess(argv, 0, stdout=f"{CONTROLLER_VERSION} (fake)\n", stderr="")

    monkeypatch.setattr(shutil, "which", lambda name, *args, **kwargs: f"/fake-bin/{name}")
    monkeypatch.setattr(subprocess, "run", fake_run)
    homes.executed = executed
    return homes


async def claude_status(client, headers=None) -> dict:
    response = await client.get(API + "/providers/claude-code/status", headers=headers or {})
    assert response.status_code == 200, response.text
    return response.json()


async def listed_claude(client, headers=None) -> dict:
    response = await client.get(API + "/providers", headers=headers or {})
    assert response.status_code == 200, response.text
    return next(p for p in response.json()["providers"] if p["id"] == "claude-code")


# ---- the agent user's version, read from the active launcher ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_agent_users_native_version_is_shown_by_default_and_names_its_user(scoped, client):
    native_install(scoped.agent, "2.1.400")

    for status in (await claude_status(client), await listed_claude(client)):
        assert status["version"] == "2.1.400"
        assert status["version_user"] == AGENT
        assert status["version_source"] == "native"
        assert status["version_unknown_reason"] is None


@pytest.mark.asyncio
async def test_the_controllers_own_scope_keeps_its_existing_version_and_adds_nothing(scoped, client):
    native_install(scoped.agent, "2.1.400")
    controller = path_utils.controller_user_name()

    status = await claude_status(client, {HEADER: controller})

    assert status["version"] == CONTROLLER_VERSION
    assert not {"version_user", "version_source", "version_unknown_reason"} & set(status)


@pytest.mark.asyncio
async def test_a_single_user_install_keeps_the_controllers_version(scoped, client, monkeypatch):
    monkeypatch.setattr(settings, "harness_agent_user", None)
    native_install(scoped.agent, "2.1.400")

    status = await claude_status(client)

    assert status["version"] == CONTROLLER_VERSION and "version_user" not in status


@pytest.mark.asyncio
async def test_switching_users_leaks_nothing_between_requests(scoped, client):
    native_install(scoped.agent, "2.1.400")
    controller = path_utils.controller_user_name()

    seen = [(await claude_status(client, h))["version"] for h in ({HEADER: AGENT}, {HEADER: controller}, {}, {HEADER: controller})]

    assert seen == ["2.1.400", CONTROLLER_VERSION, "2.1.400", CONTROLLER_VERSION]


@pytest.mark.asyncio
async def test_an_npm_install_is_read_through_its_launcher(scoped, client):
    npm_install(scoped.agent, "2.1.350")

    status = await claude_status(client)

    assert status["version"] == "2.1.350" and status["version_source"] == "npm"


@pytest.mark.asyncio
async def test_the_active_launcher_wins_over_folders_and_packages_that_merely_exist(scoped, client):
    native_install(scoped.agent, "2.1.400")
    (scoped.agent / ".local" / "share" / "claude" / "versions" / "2.1.999").write_bytes(b"x")     # a newer, unlaunched one
    npm_install(scoped.agent, "3.0.0", launch=False)                                                  # a package nobody launches

    assert (await claude_status(client))["version"] == "2.1.400"


# ---- no authoritative active metadata: unknown, never a guess -------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", ["versions_folder_only", "package_only", "single_version_no_launcher"])
async def test_an_install_without_an_active_launcher_is_unknown(scoped, client, layout):
    if layout == "versions_folder_only":
        (scoped.agent / ".local" / "share" / "claude" / "versions").mkdir(parents=True)
        (scoped.agent / ".local" / "share" / "claude" / "versions" / "2.1.400").write_bytes(b"x")
        (scoped.agent / ".local" / "share" / "claude" / "versions" / "2.1.401").write_bytes(b"x")
    elif layout == "package_only":
        npm_install(scoped.agent, "2.1.350", launch=False)
    else:
        (scoped.agent / ".local" / "share" / "claude" / "versions").mkdir(parents=True)
        (scoped.agent / ".local" / "share" / "claude" / "versions" / "2.1.400").write_bytes(b"x")   # a lone version proves nothing

    status = await claude_status(client)

    assert status["version"] is None and status["version_unknown_reason"] == UNKNOWN_TEXT
    assert status["version_user"] == AGENT and status["version_source"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["regular_file", "dangling", "outside_shape", "not_a_version", "path_like"])
async def test_a_launcher_that_is_not_a_clean_active_native_install_is_unknown(scoped, client, case):
    launcher = scoped.agent / ".local" / "bin" / "claude"
    launcher.parent.mkdir(parents=True)
    versions = scoped.agent / ".local" / "share" / "claude" / "versions"
    versions.mkdir(parents=True)
    if case == "regular_file":
        launcher.write_text("#!/bin/sh\n")                                       # a wrapper, not a symlink
    elif case == "dangling":
        launcher.symlink_to(versions / "2.1.400")                                # the target does not exist
    elif case == "outside_shape":
        elsewhere = scoped.agent / "bin-elsewhere"
        elsewhere.write_bytes(b"x")
        launcher.symlink_to(elsewhere)                                           # not under claude/versions
    elif case == "not_a_version":
        (versions / "latest").write_bytes(b"x")
        launcher.symlink_to(versions / "latest")
    else:
        (versions / "2.1.4;rm").write_bytes(b"x")
        launcher.symlink_to(versions / "2.1.4;rm")

    status = await claude_status(client)

    assert status["version"] is None and status["version_unknown_reason"] == UNKNOWN_TEXT


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["latest", "2.1", "v2.1.3", "../2.1.3", 2, None, ["2.1.3"]])
async def test_a_package_version_that_is_not_a_plain_semantic_version_is_unknown(scoped, client, version):
    npm_install(scoped.agent, version)

    assert (await claude_status(client))["version_unknown_reason"] == UNKNOWN_TEXT


@pytest.mark.asyncio
async def test_a_wrapper_script_at_one_prefix_does_not_hide_the_real_launcher_at_the_other(scoped, client):
    """The search goes on past a launcher that is not a symlink: a regular file is not the active install."""
    native_install(scoped.agent, "2.1.400")
    wrapper = scoped.agent / ".npm-global" / "bin" / "claude"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/bin/sh\nexec something\n")

    status = await claude_status(client)

    assert status["version"] == "2.1.400" and status["version_source"] == "native"


@pytest.mark.asyncio
async def test_an_npm_launcher_that_resolves_into_another_package_is_unknown(scoped, client):
    """The launcher must lead into the claude-code package; another package's version says nothing about it."""
    other = scoped.agent / ".npm-global" / "lib" / "node_modules" / "some-other-tool"
    other.mkdir(parents=True)
    (other / "cli.js").write_text("// other\n", encoding="utf-8")
    (other / "package.json").write_text(json.dumps({"name": "some-other-tool", "version": "1.2.3"}), encoding="utf-8")
    launcher = scoped.agent / ".npm-global" / "bin" / "claude"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(Path("..") / "lib" / "node_modules" / "some-other-tool" / "cli.js")

    assert (await claude_status(client))["version_unknown_reason"] == UNKNOWN_TEXT


@pytest.mark.asyncio
async def test_a_malformed_package_file_is_unknown(scoped, client):
    launcher = npm_install(scoped.agent, "2.1.350")
    (launcher.parent.parent / "lib" / "node_modules" / "@anthropic-ai" / "claude-code" / "package.json").write_text("{not json")

    assert (await claude_status(client))["version_unknown_reason"] == UNKNOWN_TEXT


@pytest.mark.asyncio
@pytest.mark.parametrize("package_json", ["[]", "5", "null", '"2.1.350"', "true", '["2.1.350"]'])
async def test_a_package_file_that_is_valid_json_but_not_an_object_is_unknown_and_not_an_error(scoped, client, package_json):
    launcher = npm_install(scoped.agent, "2.1.350")
    (launcher.parent.parent / "lib" / "node_modules" / "@anthropic-ai" / "claude-code" / "package.json").write_text(package_json)

    for status in (await claude_status(client), await listed_claude(client)):      # 200 with the message, never a server error
        assert status["version"] is None
        assert status["version_unknown_reason"] == UNKNOWN_TEXT


@pytest.mark.asyncio
async def test_an_empty_package_object_is_unknown(scoped, client):
    launcher = npm_install(scoped.agent, "2.1.350")
    (launcher.parent.parent / "lib" / "node_modules" / "@anthropic-ai" / "claude-code" / "package.json").write_text("{}")

    assert (await claude_status(client))["version_unknown_reason"] == UNKNOWN_TEXT


# ---- two supported launchers: only an agreement is a version ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_launchers_that_disagree_are_unknown(scoped, client):
    """Two symlinks alone do not say which one runs, so neither version is asserted (the search order is arbitrary)."""
    native_install(scoped.agent, "2.1.400")
    npm_install(scoped.agent, "2.0.1")

    status = await claude_status(client)

    assert status["version"] is None and status["version_source"] is None
    assert status["version_unknown_reason"] == UNKNOWN_TEXT
    assert "2.1.400" not in json.dumps(status) and "2.0.1" not in json.dumps(status)


@pytest.mark.asyncio
async def test_two_launchers_that_agree_are_that_version(scoped, client):
    native_install(scoped.agent, "2.1.400")
    npm_install(scoped.agent, "2.1.400")

    status = await claude_status(client)

    assert status["version"] == "2.1.400"
    assert status["version_unknown_reason"] is None


@pytest.mark.asyncio
async def test_a_live_launcher_whose_version_cannot_be_read_next_to_a_valid_one_is_unknown(scoped, client):
    """The unreadable launcher might be the one that runs, so the valid one is not asserted either."""
    native_install(scoped.agent, "2.1.400")
    launcher = npm_install(scoped.agent, "2.0.1")
    (launcher.parent.parent / "lib" / "node_modules" / "@anthropic-ai" / "claude-code" / "package.json").write_text("[]")

    assert (await claude_status(client))["version_unknown_reason"] == UNKNOWN_TEXT


@pytest.mark.asyncio
async def test_an_agent_user_without_an_install_is_unknown_with_the_exact_message(scoped, client):
    status = await claude_status(client)

    assert status["version"] is None and status["version_unknown_reason"] == UNKNOWN_TEXT
    assert UNKNOWN == UNKNOWN_TEXT


@pytest.mark.asyncio
async def test_an_install_the_controller_cannot_read_is_unknown_with_the_exact_message(scoped, client):
    if os.geteuid() == 0:
        pytest.skip("root can read everywhere")
    native_install(scoped.agent, "2.1.400")
    local = scoped.agent / ".local"
    local.chmod(0o000)
    try:
        status = await claude_status(client)
    finally:
        local.chmod(0o755)

    assert status["version"] is None and status["version_unknown_reason"] == UNKNOWN_TEXT


# ---- nothing is executed, other providers and refusals are unchanged -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_is_executed_to_find_the_agent_users_version(scoped, client):
    """The agent user's version comes from disk. The only programs the status code runs are the controller's own."""
    import app.services.claude_install_version as reader

    native_install(scoped.agent, "2.1.400")

    assert (await claude_status(client))["version"] == "2.1.400"
    assert (await listed_claude(client))["version"] == "2.1.400"

    assert scoped.executed, "the controller's own --version still runs, as it always did"
    assert all(argv[0].startswith("/fake-bin/") for argv in scoped.executed)               # only the controller's PATH lookups
    assert [part for argv in scoped.executed for part in argv if str(scoped.agent) in part] == []
    assert not hasattr(reader, "subprocess")                                              # the reader cannot run anything


@pytest.mark.asyncio
async def test_other_providers_are_unchanged(scoped, client):
    native_install(scoped.agent, "2.1.400")

    providers = (await client.get(API + "/providers")).json()["providers"]

    others = [p for p in providers if p["id"] != "claude-code"]
    assert others and all("version_user" not in p and p["version"] in (CONTROLLER_VERSION, None) for p in others)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["/providers", "/providers/claude-code/status"])
async def test_a_user_outside_the_allowed_list_is_refused(scoped, client, route):
    response = await client.get(API + route, headers={HEADER: "somebody-else"})

    assert response.status_code == 400, response.text


# ---- the reader on its own ---------------------------------------------------------------------------------------------------------


def test_the_reader_returns_the_version_and_its_source(tmp_path):
    native_install(tmp_path, "2.1.400")

    found = read_installed_version(tmp_path)

    assert (found.version, found.source) == ("2.1.400", "native")


def test_the_reader_never_raises_for_a_missing_home(tmp_path):
    found = read_installed_version(tmp_path / "no-such-home")

    assert (found.version, found.source) == (None, None)
