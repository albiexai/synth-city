"""Install and version status from fake CLI binaries on an isolated PATH."""
import stat

import pytest

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
