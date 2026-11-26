"""Run the restricted helper against disposable accounts, workspaces and process records."""
from __future__ import annotations

import io
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

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
@pytest.mark.parametrize("existing_backup", [False, True])
def test_only_folder_trust_changes_and_one_private_backup_is_kept(helper, provider, existing_backup):
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
    backup = config.with_name(config.name + ".synth-city.bak")
    if existing_backup:
        backup.write_text("previous backup", encoding="utf-8")
        backup.chmod(0o644)

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
    assert backup.read_text(encoding="utf-8") == original
    assert backup.stat().st_mode & 0o777 == 0o600
    assert run(provider) == (0, {"ok": True, "changed": False})
    assert backup.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("case", ["operation", "provider", "folder", "extra", "duplicate", "trailing", "oversized"])
def test_rejects_requests_outside_the_typed_allowlist(helper, case):
    run, workspace, home, _ = helper
    valid = {"operation": "folder_trust", "provider": "codex-cli", "folder": str(workspace)}
    encoded = json.dumps(valid).encode()
    helper_module = runpy.run_path(str(SCRIPT))
    payload = {
        "operation": json.dumps({**valid, "operation": "other"}).encode(),
        "provider": json.dumps({**valid, "provider": "antigravity-cli"}).encode(),
        "folder": json.dumps({**valid, "folder": False}).encode(),
        "extra": json.dumps({**valid, "extra": "unexpected"}).encode(),
        "duplicate": b'{"operation": "folder_trust", ' + encoded[1:],
        "trailing": encoded + b" {}",
        "oversized": encoded.ljust(helper_module["MAX_REQUEST_BYTES"] + 1, b" "),
    }[case]

    # Assert the parser rejects the request before any writer-side checks can
    # mask a missing allowlist guard.
    with pytest.raises(ValueError):
        helper_module["read_request"](io.BytesIO(payload))
    assert run(payload=payload) == (1, {"ok": False, "error": "agent_config_refused"})
    assert list(home.iterdir()) == []


@pytest.mark.parametrize(("field", "value"), [("device", False), ("inode", 0), ("extra", "unexpected")])
def test_dispatch_git_trust_parser_rejects_invalid_fields(field, value):
    read_request = runpy.run_path(str(SCRIPT))["read_request"]
    payload = {"operation": "dispatch_git_trust", "folder": "/fixture/dispatch-1", "repo": "/fixture/repo",
               "device": 0, "inode": 2}
    payload[field] = value

    with pytest.raises(ValueError):
        read_request(io.BytesIO(json.dumps(payload).encode()))


def test_refuses_a_home_owned_by_another_user(helper, monkeypatch):
    run, _, home, _ = helper
    binding = home.stat()
    fstat = os.fstat

    def stat_with_wrong_home_owner(fd):
        info = fstat(fd)
        if (info.st_dev, info.st_ino) == (binding.st_dev, binding.st_ino):
            return SimpleNamespace(st_uid=os.getuid() + 1)
        return info

    monkeypatch.setattr(trust.os, "fstat", stat_with_wrong_home_owner)
    assert run() == (1, {"ok": False, "error": "agent_config_refused"})
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


@pytest.mark.parametrize("case", ["valid", "changed-inode", "non-dispatch", "different-repo", "changed-after-probe"])
def test_dispatch_git_trust_accepts_only_the_owned_exact_worktree(helper, monkeypatch, case):
    run, workspace, home, _ = helper
    root = workspace.parent
    repo = root / "repo"
    destination = root / ("dispatch-1" if case != "non-dispatch" else "some-worktree")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "-q", "--allow-empty", "-m", "fixture"], check=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(destination)], check=True)
    config = home / ".gitconfig"
    original = "[user]\n\tname = existing value\n"
    if case == "changed-after-probe":
        # Exercise the idempotent path. It never reaches the pre-replace check.
        original += f'[safe]\n\tdirectory = "{destination}"\n'
        lstat = Path.lstat
        read_config = trust._read_restricted
        stale = False

        def read_after_probe(*args):
            nonlocal stale
            content = read_config(*args)
            stale = True
            return content

        def observed_lstat(path):
            info = lstat(path)
            if stale and path == destination:
                return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino + 1)
            return info

        monkeypatch.setattr(trust, "_read_restricted", read_after_probe)
        monkeypatch.setattr(Path, "lstat", observed_lstat)
    config.write_text(original, encoding="utf-8")
    identity = destination.stat()
    other = root / "other"
    if case == "different-repo":
        subprocess.run(["git", "init", "-q", str(other)], check=True)
    payload = json.dumps({
        "operation": "dispatch_git_trust", "folder": str(destination),
        "repo": str(other if case == "different-repo" else repo),
        "device": identity.st_dev, "inode": identity.st_ino + int(case == "changed-inode"),
    }).encode()

    if case != "valid":
        assert run(payload=payload) == (1, {"ok": False, "error": "agent_config_refused"})
        assert config.read_text(encoding="utf-8") == original
        return
    assert run(payload=payload) == (0, {"ok": True, "changed": True})
    assert run(payload=payload) == (0, {"ok": True, "changed": False})
    values = subprocess.check_output(["git", "config", "--file", str(config), "--get-all", "safe.directory"],
                                     text=True).splitlines()
    assert values == [str(destination)]
    assert "existing value" in config.read_text(encoding="utf-8")


@pytest.mark.parametrize("parent_name", ["other-parent", "literal*", "literal\r", "literal\n"])
def test_dispatch_git_trust_checks_sibling_and_literal_path_before_probing(helper, monkeypatch, parent_name):
    run, workspace, home, _ = helper
    parent = workspace.parent / parent_name
    destination = parent / "dispatch-1"
    repo_parent = workspace.parent if parent_name == "other-parent" else parent
    repo = repo_parent / "repo"
    destination.mkdir(parents=True)
    repo.mkdir()
    identity = destination.stat()

    def unexpected_probe(*_args, **_kwargs):
        pytest.fail("A non-sibling or non-literal path reached the Git trust probe")

    monkeypatch.setattr(trust.subprocess, "run", unexpected_probe)
    payload = json.dumps({"operation": "dispatch_git_trust", "folder": str(destination), "repo": str(repo),
                          "device": identity.st_dev, "inode": identity.st_ino}).encode()

    assert run(payload=payload) == (1, {"ok": False, "error": "agent_config_refused"})
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
def test_claude_trust_applies_to_validated_primary_checkout(helper, outside, monkeypatch):
    run, workspace, home, _ = helper
    primary_root = home.parent if outside else workspace.parent
    primary = primary_root / "primary"
    subprocess.run(["git", "init", "-q", str(primary)], check=True)
    subprocess.run(["git", "-C", str(primary), "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "-q", "--allow-empty", "-m", "fixture"], check=True)
    subprocess.run(["git", "-C", str(primary), "worktree", "add", "-q", "--detach", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(primary), "config", "core.fsmonitor", "true"], check=True)
    run_git = subprocess.run

    def isolated_git(argv, **kwargs):
        assert argv[0] == "/usr/bin/git"
        config_overrides = [value for flag, value in zip(argv, argv[1:]) if flag == "-c"]
        assert config_overrides == [f"safe.directory={workspace}", "core.fsmonitor=false"]
        assert kwargs["env"] == {
            "PATH": "/usr/bin:/bin", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        }
        return run_git(argv, **kwargs)

    monkeypatch.setattr(trust.subprocess, "run", isolated_git)
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
