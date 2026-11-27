"""Install and version status from fake CLI binaries on an isolated PATH."""
import stat

import pytest
from pydantic_settings import EnvSettingsSource

from app.services.providers import get_provider


@pytest.fixture
def isolated_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    return tmp_path


def _fake_cli(directory, name, body):
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_missing_copilot_binary_reports_not_installed(isolated_path):
    status = get_provider("copilot-cli").get_status()

    assert status["installed"] is False
    assert status["version"] is None
    assert status["binary_path"] is None
    assert status.get("unavailable_code") != "launcher_cli_missing"


def test_copilot_not_found_message_reports_not_installed(isolated_path):
    _fake_cli(
        isolated_path,
        "copilot",
        'echo "GitHub Copilot CLI was not found. Run copilot in a terminal to install it." >&2\nexit 1',
    )

    status = get_provider("copilot-cli").get_status()

    assert status["installed"] is False
    assert status["version"] is None
    assert status["binary_path"] is None
    assert status["unavailable_code"] == "launcher_cli_missing"


def test_installed_copilot_reports_its_version(isolated_path):
    binary = _fake_cli(isolated_path, "copilot", 'echo "GitHub Copilot CLI 1.4.2"')

    status = get_provider("copilot-cli").get_status()

    assert status["installed"] is True
    assert status["version"] == "1.4.2"
    assert status["binary_path"] == str(binary)


def test_failed_probe_without_not_found_message_keeps_binary_installed(isolated_path):
    _fake_cli(isolated_path, "copilot", 'echo "unexpected failure" >&2\nexit 3')

    status = get_provider("copilot-cli").get_status()

    assert status["installed"] is True
    assert status["binary_path"] == str(isolated_path / "copilot")


@pytest.mark.parametrize("agent_user", [None, "fixture-agent"])
def test_registry_reports_the_controller_search_and_configured_agent(isolated_path, monkeypatch, agent_user):
    from app.api.v1 import providers as providers_api
    from app.config import Settings, settings
    from app.utils.path_utils import controller_user_name

    if agent_user:
        monkeypatch.setenv("HARNESS_AGENT_USER", f" {agent_user} ")
    else:
        monkeypatch.delenv("HARNESS_AGENT_USER", raising=False)
    monkeypatch.setattr(settings, "harness_agent_user", EnvSettingsSource(Settings)().get("harness_agent_user"))

    response = providers_api.list_providers()

    assert response["installation_search"] == {
        "controller_user": controller_user_name(), "agent_user": agent_user, "path": str(isolated_path),
    }
    assert all(not provider["installed"] for provider in response["providers"])


def test_registry_reports_an_empty_path_without_searching_default_directories(monkeypatch):
    from app.api.v1 import providers as providers_api

    monkeypatch.setenv("PATH", "")

    response = providers_api.list_providers()

    assert response["installation_search"]["path"] == ""
    assert all(not provider["installed"] for provider in response["providers"])


def test_container_registry_preserves_the_launcher_state(isolated_path, monkeypatch):
    from app.api.v1 import providers as providers_api
    from app.services import runtime_environment

    _fake_cli(isolated_path, "copilot", 'echo "GitHub Copilot CLI was not found." >&2\nexit 1')
    monkeypatch.setattr(runtime_environment, "is_containerized", lambda: True)

    response = providers_api.list_providers()

    assert response["environment"]["agent_cli_warning"] == runtime_environment.CONTAINER_AGENT_CLI_WARNING
    copilot = next(p for p in response["providers"] if p["id"] == "copilot-cli")
    assert copilot["installed"] is False
    assert copilot["unavailable_code"] == "launcher_cli_missing"
