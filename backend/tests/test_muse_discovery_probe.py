"""#965: the read-only probe of first-session discovery. Every test reads fake descriptors; no real PID is used and
no helper process is started."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

import app.services.muse_activity_service as muse
import app.services.providers.muse_cli as muse_cli

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "muse-discovery-probe.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "muse_sessions"
SID = "01a13be9-b3fa-79f3-941d-e49637f03184"
OTHER = "01a13be9-ffff-7fff-bfff-ffffffffffff"
CWD = "/tmp/muse-fixture-ws"


@pytest.fixture(scope="module")
def probe():
    spec = importlib.util.spec_from_file_location("muse_discovery_probe", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _session(root: Path, sid: str = SID, cwd: str = CWD) -> Path:
    folder = root / "sessions" / "2026" / "10" / "14" / sid
    folder.mkdir(parents=True)
    (folder / ".session.lock").write_text("")
    text = (FIXTURES / "turn_completed.jsonl").read_text().replace(SID, sid).replace(CWD, cwd)
    (folder / "session.jsonl").write_text(text)
    return folder


def _process(tmp_path, monkeypatch, *targets, uid=None):
    """A fake process: its data root, its descriptor directory and its owner."""
    root = tmp_path / "owner-data"
    monkeypatch.setattr(muse_cli, "_process_environ", lambda pid: {b"MUSE_DATA_DIR": str(root).encode()})
    fds = tmp_path / "fd"
    fds.mkdir()
    for number, target in enumerate(targets, start=7):
        (fds / str(number)).symlink_to(target)
    monkeypatch.setattr(muse, "_process_fd_dir", lambda pid: fds)
    monkeypatch.setattr(muse, "_process_uid", lambda pid: os.getuid() if uid is None else uid)
    return root


def test_a_held_session_is_discovered_and_nothing_else_is_reported(probe, tmp_path, monkeypatch):
    root = tmp_path / "owner-data"
    folder = _session(root)
    _process(tmp_path, monkeypatch, folder / ".session.lock")
    facts = probe.observe(4100, CWD, SID)
    assert facts["fd_listable"] is True and facts["fd_failure"] is None
    assert facts["lock_descriptors"] == 1 and facts["locks_under_sessions_root"] == 1
    assert facts["discovered"] is True and facts["matches_expected"] is True
    assert facts["target_present"] is True and facts["target_uid_differs"] is False


def test_an_expected_id_that_does_not_match_is_reported_as_a_mismatch(probe, tmp_path, monkeypatch):
    folder = _session(tmp_path / "owner-data")
    _process(tmp_path, monkeypatch, folder / ".session.lock")
    facts = probe.observe(4100, CWD, OTHER)
    assert facts["discovered"] is True and facts["matches_expected"] is False


def test_without_an_expected_id_there_is_no_match_fact(probe, tmp_path, monkeypatch):
    folder = _session(tmp_path / "owner-data")
    _process(tmp_path, monkeypatch, folder / ".session.lock")
    assert probe.observe(4100, CWD)["matches_expected"] is None


def test_a_process_of_another_user_is_reported_as_a_different_owner(probe, tmp_path, monkeypatch):
    folder = _session(tmp_path / "owner-data")
    _process(tmp_path, monkeypatch, folder / ".session.lock", uid=os.getuid() + 1)
    assert probe.observe(4100, CWD)["target_uid_differs"] is True


def test_two_held_locks_are_counted_and_nothing_is_discovered(probe, tmp_path, monkeypatch):
    root = tmp_path / "owner-data"
    first, second = _session(root), _session(root, OTHER)
    _process(tmp_path, monkeypatch, first / ".session.lock", second / ".session.lock")
    facts = probe.observe(4100, CWD)
    assert facts["lock_descriptors"] == 2 and facts["locks_under_sessions_root"] == 2
    assert facts["discovered"] is False


def test_a_lock_outside_the_sessions_root_is_counted_apart(probe, tmp_path, monkeypatch):
    folder = _session(tmp_path / "elsewhere")
    _process(tmp_path, monkeypatch, folder / ".session.lock")
    facts = probe.observe(4100, CWD)
    assert facts["lock_descriptors"] == 1 and facts["locks_under_sessions_root"] == 0
    assert facts["discovered"] is False


def test_another_workspace_is_not_discovered(probe, tmp_path, monkeypatch):
    folder = _session(tmp_path / "owner-data", cwd="/tmp/another-workspace")
    _process(tmp_path, monkeypatch, folder / ".session.lock")
    facts = probe.observe(4100, CWD)
    assert facts["lock_descriptors"] == 1 and facts["discovered"] is False


def test_an_unreadable_log_is_not_discovered(probe, tmp_path, monkeypatch):
    folder = _session(tmp_path / "owner-data")
    (folder / "session.jsonl").unlink()
    _process(tmp_path, monkeypatch, folder / ".session.lock")
    assert probe.observe(4100, CWD)["discovered"] is False


@pytest.mark.parametrize("error,failure", [
    (PermissionError("xyzzy /proc/9/fd"), "permission_denied"),
    (FileNotFoundError("xyzzy /proc/9/fd"), "not_found"),
    (OSError("xyzzy /proc/9/fd"), "unreadable"),
])
def test_a_descriptor_directory_that_cannot_be_listed_is_reported_by_kind_only(
        probe, tmp_path, monkeypatch, error, failure):
    _process(tmp_path, monkeypatch)

    def refuse(path):
        raise error

    monkeypatch.setattr(probe, "_scandir", refuse)
    facts = probe.observe(4100, CWD)
    assert facts["fd_listable"] is False and facts["fd_failure"] == failure
    assert facts["lock_descriptors"] == 0 and facts["discovered"] is False
    assert facts["target_present"] is (failure != "not_found")
    assert "/proc" not in json.dumps(facts) and "xyzzy" not in json.dumps(facts)


def test_the_capability_names_come_from_the_status_masks(probe):
    both = "Name:\tx\nCapInh:\t0\nCapPrm:\t0000000000080004\nCapEff:\t0000000000080004\nCapBnd:\tff\nCapAmb:\t0000000000080004\n"
    none = "CapEff:\t0000000000000000\nCapAmb:\t0000000000000000\n"
    only_ptrace = "CapEff:\t0000000000080000\nCapAmb:\t0000000000000000\n"
    assert probe.capabilities(both) == {"effective": ["cap_dac_read_search", "cap_sys_ptrace"],
                                        "ambient": ["cap_dac_read_search", "cap_sys_ptrace"]}
    assert probe.capabilities(none) == {"effective": [], "ambient": []}
    assert probe.capabilities(only_ptrace)["effective"] == ["cap_sys_ptrace"]
    assert probe.capabilities("garbage") == {"effective": [], "ambient": []}


def test_the_output_never_holds_a_path_an_id_or_command_text(probe, tmp_path, monkeypatch, capsys):
    folder = _session(tmp_path / "owner-data")
    _process(tmp_path, monkeypatch, folder / ".session.lock")
    probe.main(["--pid", "4100", "--cwd", CWD, "--expect-session-id", SID])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    for secret in (str(tmp_path), SID, OTHER, CWD, "/proc", ".session.lock", "owner-data"):
        assert secret not in out
    assert parsed["discovered"] is True and parsed["matches_expected"] is True


def test_a_usage_error_is_the_only_nonzero_exit(probe, capsys):
    with pytest.raises(SystemExit) as caught:
        probe.main(["--pid", "not-a-number", "--cwd", CWD])
    assert caught.value.code != 0
    with pytest.raises(SystemExit) as caught:
        probe.main(["--cwd", CWD])
    assert caught.value.code != 0


def test_an_unexpected_failure_still_exits_zero_and_names_only_its_kind(probe, tmp_path, monkeypatch, capsys):
    _process(tmp_path, monkeypatch)

    def explode(pid, cwd):
        raise RuntimeError("secret detail /proc/9/fd " + SID)

    monkeypatch.setattr(probe, "discover_muse_session", explode)
    assert probe.main(["--pid", "4100", "--cwd", CWD]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["discovered"] is False and "secret" not in out and SID not in out


def test_the_probe_writes_nothing_and_signals_nothing():
    source = SCRIPT.read_text()
    for forbidden in ("os.kill", "signal.", "subprocess", "socket", "urllib", "requests", 'open(', "write(", "unlink",
                      "os.remove", "shutil", "os.system"):
        assert forbidden not in source, forbidden


# --- #1014 review 5928160001: no traceback, and so no path or session name, can leave the probe -----------------------

def test_a_symlink_loop_in_a_lock_target_never_escapes_main(probe, tmp_path, monkeypatch, capsys):
    """A real loop of symlinks in a descriptor target, driven through main()."""
    first, second = tmp_path / "loop-a", tmp_path / "loop-b"
    first.symlink_to(second)
    second.symlink_to(first)
    _process(tmp_path, monkeypatch, first / SID / ".session.lock")
    assert probe.main(["--pid", "4100", "--cwd", CWD, "--expect-session-id", SID]) == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    for secret in (str(tmp_path), "loop-a", "loop-b", SID, CWD, ".session.lock", "Traceback"):
        assert secret not in out
    assert parsed["discovered"] is False and parsed["fd_listable"] is True


def test_an_unexpected_error_while_counting_locks_is_reported_by_kind_with_conservative_facts(
        probe, tmp_path, monkeypatch, capsys):
    _process(tmp_path, monkeypatch)

    def explode(entries, root):
        raise RuntimeError("secret /proc/9/fd " + SID)

    monkeypatch.setattr(probe, "_count_locks", explode)
    facts = probe.observe(4100, CWD)
    assert facts["lock_count_error"] == "RuntimeError"
    assert facts["lock_descriptors"] == 0 and facts["locks_under_sessions_root"] == 0
    assert "secret" not in json.dumps(facts) and SID not in json.dumps(facts)


def test_main_prints_a_fixed_shape_object_and_exits_zero_when_observe_itself_fails(probe, monkeypatch, capsys):
    def explode(pid, cwd, expect=None):
        raise RuntimeError("secret /proc/9/fd " + SID)

    monkeypatch.setattr(probe, "observe", explode)
    assert probe.main(["--pid", "4100", "--cwd", CWD]) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {"probe_error": "RuntimeError", "version": 1}
    assert "secret" not in out and SID not in out and "Traceback" not in out


def test_main_has_no_unguarded_observation_call(probe):
    import inspect
    source = inspect.getsource(probe.main)
    assert "except Exception" in source and source.index("except Exception") > source.index("observe(")
