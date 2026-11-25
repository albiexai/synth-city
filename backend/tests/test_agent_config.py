"""Run the restricted helper against disposable accounts, workspaces and process records."""
from __future__ import annotations

import io
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest
import tomlkit

from app.services import team_workspace_trust as trust

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "product-factory" / "agent-config"


@pytest.fixture
def helper(tmp_path, monkeypatch, capsys):
    main = runpy.run_path(str(SCRIPT))["main"]
    work_root = tmp_path / "work"
    workspace = work_root / "member"
    workspace.mkdir(parents=True)
    home = tmp_path / "agent"
    home.mkdir()
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setitem(main.__globals__, "WORK_ROOT", work_root)
    monkeypatch.setitem(main.__globals__, "PROC", proc)
    monkeypatch.setitem(main.__globals__, "agent_home", lambda: home)
    monkeypatch.setitem(main.__globals__, "load_writer", lambda: trust)

    def run(provider="codex-cli", folder=workspace, *, payload=None, argv=()):
        if payload is None:
            payload = json.dumps({"operation": "folder_trust", "provider": provider, "folder": str(folder)}).encode()
        status = main(list(argv), io.BytesIO(payload))
        emitted = capsys.readouterr()
        assert not emitted.err
        assert len(emitted.out.splitlines()) == 1
        return status, json.loads(emitted.out)

    return run, workspace, home, proc


@pytest.mark.parametrize("provider", ["codex-cli", "claude-code"])
def test_only_folder_trust_changes_and_one_private_backup_is_kept(helper, provider):
    run, workspace, home, _ = helper
    if provider == "codex-cli":
        config = home / ".codex" / "config.toml"
        config.parent.mkdir()
        original = '# keep this comment\nmodel = "fixture"\n[projects."/unrelated"]\ntrust_level = "untrusted"\n'
        config.write_text(original, encoding="utf-8")
    else:
        config = home / ".claude.json"
        original = json.dumps({"fixture": True, "projects": {"/unrelated": {"allowedTools": ["Read"]}}})
        config.write_text(original, encoding="utf-8")
    config.chmod(0o640)

    assert run(provider) == (0, {"ok": True, "changed": True})
    data = config.read_text(encoding="utf-8")
    if provider == "codex-cli":
        assert "# keep this comment" in data
        result = tomlkit.parse(data)
        assert result["model"] == "fixture"
        assert result["projects"]["/unrelated"]["trust_level"] == "untrusted"
        assert result["projects"][str(workspace)]["trust_level"] == "trusted"
    else:
        assert json.loads(data) == {
            "fixture": True,
            "projects": {"/unrelated": {"allowedTools": ["Read"]}, str(workspace): {"hasTrustDialogAccepted": True}},
        }
    assert config.stat().st_mode & 0o777 == 0o640
    backup = config.with_name(config.name + ".synth-city.bak")
    assert backup.read_text(encoding="utf-8") == original
    assert backup.stat().st_mode & 0o777 == 0o600
    assert run(provider) == (0, {"ok": True, "changed": False})
    assert backup.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("payload", [
    b'{"operation":"other","provider":"codex-cli","folder":"/fixture"}',
    b'{"operation":"folder_trust","provider":"antigravity-cli","folder":"/fixture"}',
    b'{"operation":"folder_trust","provider":"codex-cli","folder":false}',
    b'{"operation":"folder_trust","provider":"codex-cli","folder":"/fixture","extra":"/unexpected"}',
    b'{"operation":"folder_trust","operation":"folder_trust","provider":"codex-cli","folder":"/fixture"}',
    b'{} {}',
    b' ' * 25000,
])
def test_rejects_requests_outside_the_typed_allowlist(helper, payload):
    run, _, home, _ = helper
    assert run(payload=payload) == (1, {"ok": False, "error": "agent_config_refused"})
    assert list(home.iterdir()) == []


def test_rejects_arguments_and_untrusted_paths(helper):
    run, workspace, home, _ = helper
    outside = home.parent / "outside"
    outside.mkdir()
    link = workspace.parent / "alias"
    link.symlink_to(workspace, target_is_directory=True)
    nested = workspace / "nested"
    nested.mkdir()
    for status, body in [
        run(argv=["--anything"]), run(folder=outside), run(folder=link), run(folder=nested / ".."),
    ]:
        assert (status, body) == (1, {"ok": False, "error": "agent_config_refused"})
    assert list(home.iterdir()) == []


@pytest.mark.parametrize("link_target", ["config_dir", "config_file", "backup"])
def test_rejects_symbolic_links_in_configuration_paths(helper, link_target):
    run, _, home, _ = helper
    other = home.parent / "other"
    other.mkdir()
    target = other / "config.toml"
    target.write_text('model = "unchanged"\n', encoding="utf-8")
    config_dir = home / ".codex"
    if link_target == "config_dir":
        config_dir.symlink_to(other, target_is_directory=True)
    else:
        config_dir.mkdir()
        config = config_dir / "config.toml"
        if link_target == "config_file":
            config.symlink_to(target)
        else:
            config.write_text('model = "fixture"\n', encoding="utf-8")
            config.with_name("config.toml.synth-city.bak").symlink_to(target)
    assert run() == (1, {"ok": False, "error": "agent_config_refused"})
    assert target.read_text(encoding="utf-8") == 'model = "unchanged"\n'


@pytest.mark.parametrize(("name", "argv"), [
    ("claude", b"/usr/bin/claude\x00--session-id\x00fixture\x00"),
    ("node", b"node\x00/test/node_modules/@anthropic-ai/claude-code/cli.js\x00"),
])
def test_a_live_claude_session_defers_only_claude_trust(helper, name, argv):
    run, _, home, proc = helper
    process = proc / "812"
    process.mkdir()
    uid = os.getuid()
    (process / "status").write_text(f"Name:\t{name}\nState:\tS (sleeping)\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    (process / "cmdline").write_bytes(argv)
    assert run("claude-code") == (75, {"ok": False, "error": "agent_config_busy"})
    assert list(home.iterdir()) == []
    assert run("codex-cli") == (0, {"ok": True, "changed": True})


@pytest.mark.parametrize("outside", [False, True])
def test_claude_trust_applies_to_validated_primary_checkout(helper, outside):
    run, workspace, home, _ = helper
    primary_root = home.parent if outside else workspace.parent
    primary = primary_root / "primary"
    subprocess.run(["git", "init", "-q", str(primary)], check=True)
    subprocess.run(["git", "-C", str(primary), "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "-q", "--allow-empty", "-m", "fixture"], check=True)
    subprocess.run(["git", "-C", str(primary), "worktree", "add", "-q", "--detach", str(workspace)], check=True)
    if outside:
        assert run("claude-code") == (1, {"ok": False, "error": "agent_config_refused"})
        assert list(home.iterdir()) == []
    else:
        assert run("claude-code") == (0, {"ok": True, "changed": True})
        assert json.loads((home / ".claude.json").read_text(encoding="utf-8")) == {
            "projects": {str(primary): {"hasTrustDialogAccepted": True}},
        }


def test_claude_refuses_a_symbolic_link_for_the_primary_checkout(helper, monkeypatch):
    run, workspace, home, _ = helper
    link = workspace.parent / "linked-primary"
    link.symlink_to(workspace, target_is_directory=True)
    listing = subprocess.CompletedProcess([], 0, f"worktree {link}\0HEAD abc\0\0", "")
    monkeypatch.setattr(trust.subprocess, "run", lambda *_args, **_kwargs: listing)
    assert run("claude-code") == (1, {"ok": False, "error": "agent_config_refused"})
    assert list(home.iterdir()) == []


def test_claude_appearing_before_config_replacement_leaves_no_file(helper, monkeypatch):
    run, _, home, proc = helper
    original_fsync = os.fsync
    uid = os.getuid()

    def start_claude(fd):
        original_fsync(fd)
        process = proc / "812"
        process.mkdir()
        (process / "status").write_text(f"Name:\tclaude\nState:\tS\nUid:\t{uid} {uid} {uid} {uid}\n")

    monkeypatch.setattr(trust.os, "fsync", start_claude)
    assert run("claude-code") == (75, {"ok": False, "error": "agent_config_busy"})
    assert list(home.iterdir()) == []


def test_unreadable_claude_process_state_defers_the_write(helper):
    run, _, home, proc = helper
    process = proc / "812"
    process.mkdir()
    (process / "status").write_text("State unavailable\n")
    assert run("claude-code") == (75, {"ok": False, "error": "agent_config_busy"})
    assert list(home.iterdir()) == []


def test_operator_command_prints_only_the_exact_sudoers_line():
    result = subprocess.run(
        [sys.executable, str(SCRIPT.with_name("productctl")), "print-agent-config-sudoers"],
        capture_output=True, text=True, check=True,
    )
    assert result.stderr == ""
    assert result.stdout == 'deckproductctl ALL=(deckproduct) NOPASSWD: /opt/albiex-synth-city-product/libexec/agent-config ""\n'
