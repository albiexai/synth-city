"""Run the restricted helper against disposable accounts, workspaces and process records."""
from __future__ import annotations

import io
import json
import os
import runpy
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomlkit

from app.services import agent_config_client, agent_config_settings
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
    policy = agent_config_settings.AgentConfigSettings(agent_config_work_root=str(work_root))
    monkeypatch.setitem(main.__globals__, "PROC", proc)
    monkeypatch.setitem(main.__globals__, "agent_home", lambda _name: home)
    monkeypatch.setitem(main.__globals__, "load_writer", lambda: trust)

    def run(provider="codex-cli", folder=workspace, *, payload=None, argv=(), settings=policy):
        monkeypatch.setitem(main.__globals__, "load_policy", lambda: settings)
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


def print_sudoers(installation, policy=None):
    productctl = installation / "bin" / "productctl"
    settings_module = installation / "libexec" / "agent_config_settings.py"
    productctl.parent.mkdir(parents=True)
    settings_module.parent.mkdir()
    productctl.write_bytes(SCRIPT.with_name("productctl").read_bytes())
    settings_source = Path(agent_config_settings.__file__).read_text(encoding="utf-8")
    fixture_read = "raise FileNotFoundError" if policy is None else f"return {json.dumps(policy).encode()!r}"
    settings_module.write_text(settings_source + f"\n\ndef _read_policy():\n    {fixture_read}\n", encoding="utf-8")
    result = subprocess.run([sys.executable, str(productctl), "print-agent-config-sudoers"],
                            check=True, capture_output=True, text=True, cwd=installation)
    assert result.stderr == ""
    return result.stdout


def test_operator_command_prints_only_the_exact_default_sudoers_line(tmp_path):
    assert print_sudoers(tmp_path / "installation") == (
        'deckproductctl ALL=(deckproduct) NOPASSWD: /opt/albiex-synth-city-product/libexec/agent-config ""\n'
    )


def test_overrides_share_the_sudo_target_and_bound_helper_work_root(helper, tmp_path, monkeypatch):
    run, original, _, _ = helper
    work_root = tmp_path / "alternate-work"
    workspace = work_root / "member"
    workspace.mkdir(parents=True)
    helper_path = "/opt/claude-deck-product/libexec/agent-config"
    values = {"agent_config_helper_path": helper_path, "agent_config_agent_user": "fixture-agent",
              "agent_config_controller_user": "fixture-controller", "agent_config_work_root": str(work_root)}
    assert print_sudoers(tmp_path / "alternate-install", values) == (
        f'fixture-controller ALL=(fixture-agent) NOPASSWD: {helper_path} ""\n'
    )
    monkeypatch.setattr(agent_config_settings, "_read_policy", lambda: json.dumps(values).encode())
    policy = agent_config_settings.load_agent_config_settings()

    def run_command(argv, **kwargs):
        assert argv == ["/usr/bin/sudo", "-n", "-u", "fixture-agent", "--", helper_path]
        assert "AGENT_CONFIG_WORK_ROOT" not in kwargs["env"]
        assert json.loads(kwargs["input"]) == {"operation": "folder_trust", "provider": "codex-cli",
                                              "folder": str(workspace)}
        return subprocess.CompletedProcess(argv, 0, '{"ok": true, "changed": true}\n', "")

    monkeypatch.setattr(agent_config_client.subprocess, "run", run_command)
    assert agent_config_client.prepare_workspace_trust_via_helper("codex-cli", str(workspace)) is True
    assert run(folder=workspace, settings=policy) == (0, {"ok": True, "changed": True})
    assert run(folder=original, settings=policy) == (1, {"ok": False, "error": "agent_config_refused"})


@pytest.mark.parametrize(("field", "value"), [
    ("agent_config_agent_user", "root"), ("agent_config_controller_user", "ALL"),
    ("agent_config_agent_user", "#0"), ("agent_config_helper_path", "/opt/helpers/*"),
    ("agent_config_helper_path", "/opt/helper\nALL ALL=NOPASSWD: ALL"),
    pytest.param("agent_config_helper_path", "/opt/helper with space", id="helper-space"),
    ("agent_config_work_root", "relative/work"),
    pytest.param("agent_config_work_root", "/fixture/work*", id="work-root-wildcard"),
    pytest.param("agent_config_work_root", "/fixture/work\0", id="work-root-nul"),
    pytest.param("agent_config_work_root", "/fixture/work\r", id="work-root-cr"),
    pytest.param("agent_config_work_root", "/fixture/work\n", id="work-root-lf"),
    pytest.param("agent_config_work_root", "/fixture/work/", id="noncanonical-work-root"),
    pytest.param("agent_config_work_root", "/fixture/work/../other", id="parent-traversal"),
])
def test_agent_config_settings_reject_unsafe_sudoers_fields_and_work_roots(field, value, monkeypatch):
    monkeypatch.setattr(agent_config_settings, "_read_policy", lambda: json.dumps({field: value}).encode())
    with pytest.raises(ValueError):
        agent_config_settings.load_agent_config_settings()


@pytest.mark.parametrize(("work_root", "valid"), [
    pytest.param("/", False, id="literal-root"),
    pytest.param("//", False, id="alternate-root"),
    pytest.param("///", False, id="repeated-separators"),
    pytest.param("/./", False, id="dot-root"),
    pytest.param("//fixture/work", False, id="alternate-anchor"),
    pytest.param("/fixture/alternate-work", True, id="named-work-root"),
])
def test_agent_config_work_root_requires_a_canonical_absolute_directory(monkeypatch, work_root, valid):
    monkeypatch.setattr(agent_config_settings, "_read_policy",
                        lambda: json.dumps({"agent_config_work_root": work_root}).encode())
    if valid:
        assert agent_config_settings.load_agent_config_settings().agent_config_work_root == work_root
    else:
        with pytest.raises(ValueError, match="Invalid agent-config path"):
            agent_config_settings.load_agent_config_settings()


@pytest.mark.parametrize("values", [
    pytest.param({"unexpected": "fixture"}, id="unknown-key"),
    pytest.param({"agent_config_work_root": 21}, id="non-text-value"),
])
def test_agent_config_policy_rejects_invalid_schema_before_building_settings(monkeypatch, values):
    monkeypatch.setattr(agent_config_settings, "_read_policy", lambda: json.dumps(values).encode())
    with pytest.raises(ValueError, match="Invalid agent-config policy"):
        agent_config_settings.load_agent_config_settings()


def test_agent_config_policy_rejects_an_agent_alias_for_uid_zero(monkeypatch):
    monkeypatch.setattr(agent_config_settings.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=0))
    with pytest.raises(ValueError, match="Invalid agent-config account"):
        agent_config_settings.AgentConfigSettings(agent_config_agent_user="fixture-agent")


@pytest.mark.parametrize("fault", ["owner", "policy_write", "parent_write", "symlink", "nonregular", "oversize"])
def test_policy_requires_a_root_controlled_bounded_regular_file(tmp_path, monkeypatch, fault):
    path = tmp_path / "policy.json"
    padding = " " * agent_config_settings._MAX_POLICY_BYTES if fault == "oversize" else ""
    path.write_text("{}" + padding, encoding="utf-8")
    observed_path = path
    if fault == "symlink":
        observed_path = tmp_path / "policy-link.json"
        observed_path.symlink_to(path)
    monkeypatch.setattr(agent_config_settings, "POLICY_PATH", observed_path)
    parent = tmp_path.stat()
    fstat = os.fstat

    def observed_stat(fd):
        info = fstat(fd)
        mode = info.st_mode & ~0o022
        if stat.S_ISREG(info.st_mode):
            if fault == "nonregular":
                mode = stat.S_IFIFO | stat.S_IMODE(mode)
            return SimpleNamespace(st_uid=int(fault == "owner"), st_mode=mode | (0o020 if fault == "policy_write" else 0))
        if fault == "parent_write" and (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino):
            mode |= 0o020
        return SimpleNamespace(st_uid=0, st_mode=mode)

    monkeypatch.setattr(os, "fstat", observed_stat)
    with pytest.raises((ValueError, OSError)):
        agent_config_settings.load_agent_config_settings()


def test_helper_uses_the_alternate_installations_isolated_python(tmp_path, monkeypatch):
    program = runpy.run_path(str(SCRIPT))
    helper = tmp_path / "alternate" / "libexec" / "agent-config"
    python = str(helper.parent.parent / "venv" / "bin" / "python")
    calls = []
    monkeypatch.setattr(os, "execv", lambda path, argv: calls.append((path, argv)))
    program["use_install_python"](helper)
    assert calls == [(python, [python, "-I", str(helper)])]


def test_helper_home_uses_the_configured_unprivileged_account(tmp_path, monkeypatch):
    program = runpy.run_path(str(SCRIPT))
    calls = []

    def passwd(name):
        calls.append(name)
        return SimpleNamespace(pw_uid=812, pw_dir=str(tmp_path))

    monkeypatch.setattr(program["pwd"], "getpwnam", passwd)
    monkeypatch.setattr(os, "getuid", lambda: 812)
    monkeypatch.setattr(os, "geteuid", lambda: 812)
    assert program["agent_home"]("alternate-agent") == tmp_path
    assert calls == ["alternate-agent"]
    monkeypatch.setattr(os, "geteuid", lambda: 813)
    with pytest.raises(ValueError):
        program["agent_home"]("alternate-agent")


def test_installed_helper_refuses_a_policy_for_another_executable(tmp_path, monkeypatch, capsys):
    main = runpy.run_path(str(SCRIPT))["main"]
    policy = agent_config_settings.AgentConfigSettings(agent_config_helper_path=str(tmp_path / "agent-config"))
    monkeypatch.setitem(main.__globals__, "load_policy", lambda: policy)

    def unexpected_bootstrap(_path):
        pytest.fail("A mismatched helper reached the installation bootstrap")

    monkeypatch.setitem(main.__globals__, "use_install_python", unexpected_bootstrap)
    request = {"operation": "folder_trust", "provider": "codex-cli", "folder": str(tmp_path)}
    assert main([], io.BytesIO(json.dumps(request).encode()), installed=True) == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"ok": False, "error": "agent_config_refused"}
    assert output.err == ""


def test_installed_main_uses_isolated_python_before_reading_the_request(monkeypatch, capsys):
    main = runpy.run_path(str(SCRIPT))["main"]
    helper = SCRIPT.resolve()
    policy = agent_config_settings.AgentConfigSettings(agent_config_helper_path=str(helper))
    monkeypatch.setitem(main.__globals__, "load_policy", lambda: policy)
    called = []

    def read_request(_stdin):
        assert called == [helper]
        raise ValueError("Synthetic invalid request")

    monkeypatch.setitem(main.__globals__, "use_install_python", called.append)
    monkeypatch.setitem(main.__globals__, "read_request", read_request)
    assert main([], io.BytesIO(b""), installed=True) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "agent_config_refused"}
