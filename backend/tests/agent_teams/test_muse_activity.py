from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.muse_activity_service import discover_muse_session, muse_state_from_lines

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "muse_sessions"
SID = "01a13be9-b3fa-79f3-941d-e49637f03184"
CWD = "/tmp/muse-fixture-ws"
NOW = datetime(2026, 10, 14, 17, 50, tzinfo=timezone.utc)
START = NOW - timedelta(hours=1)


def state(name, **kwargs):
    lines = (FIX / f"{name}.jsonl").read_bytes().splitlines(keepends=True)
    values = {"session_id": SID, "cwd": CWD, "now": NOW, "started_at": START, **kwargs}
    return muse_state_from_lines(lines, **values)[:2]


@pytest.mark.parametrize("name,expected", [
    ("turn_completed", ("idle", "native_turn_completed")),
    ("tool_turn_working", ("working", "native_progress")),
    ("question_waiting", ("waiting_input", "native_question_open")),
])
def test_states(name, expected):
    assert state(name) == expected


def test_answered_question_is_not_waiting():
    assert state("question_answered")[0] == "idle"


def test_wrong_session_or_workspace_is_a_mismatch():
    assert state("turn_completed", session_id="01a13be9-0000-7000-8000-000000000000")[1] == "session_mismatch"
    assert state("turn_completed", cwd="/elsewhere")[1] == "session_mismatch"


def test_events_before_process_start_are_ignored():
    assert state("turn_completed", started_at=NOW - timedelta(seconds=1))[0] == "unknown"


def test_partial_last_line_is_incomplete():
    lines = (FIX / "turn_completed.jsonl").read_bytes().splitlines(keepends=True)
    lines[-1] = lines[-1].rstrip(b"\n")[:-5]
    assert muse_state_from_lines(lines, SID, CWD, NOW, START)[:2] == ("unknown", "observation_incomplete")


def test_discovery_uses_this_process_lock_only(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions" / "2026" / "10" / "14"
    for sid in (SID, "01a13be9-ffff-7fff-bfff-ffffffffffff"):
        folder = sessions / sid
        folder.mkdir(parents=True)
        (folder / ".session.lock").write_text("")
        text = (FIX / "turn_completed.jsonl").read_text()
        (folder / "session.jsonl").write_text(text.replace(SID, sid))
    import app.services.providers.muse_cli as cli
    monkeypatch.setattr(cli, "_process_environ", lambda pid: {b"MUSE_DATA_DIR": str(tmp_path).encode()})
    fds = tmp_path / "fd"
    fds.mkdir()
    (fds / "7").symlink_to(sessions / SID / ".session.lock")
    import app.services.muse_activity_service as m
    monkeypatch.setattr(m, "_process_fd_dir", lambda pid: fds)
    monkeypatch.setattr(m, "_process_uid", lambda pid: __import__("os").getuid())  # never a host PID
    assert discover_muse_session(123, CWD) == SID


def test_answered_question_mid_turn_is_working_not_waiting():
    # The turn goes on after the answer; only the settle event can clear the question.
    lines = (FIX / "question_answered.jsonl").read_bytes().splitlines(keepends=True)
    settled = next(i for i, line in enumerate(lines) if b"user_input_prompt_settled" in line)
    assert muse_state_from_lines(lines[:settled + 2], SID, CWD, NOW, START)[:2] == ("working", "native_progress")
    assert muse_state_from_lines(lines[:settled], SID, CWD, NOW, START)[:2] == ("waiting_input", "native_question_open")


def _dispatch(monkeypatch, session_id, observed=None, discovered=None):
    from app.services import agent_activity_service as activity
    monkeypatch.setattr(activity, "_process", lambda _: ("S", "27"))
    monkeypatch.setattr(activity, "discover_muse_session", lambda pid, cwd: discovered)
    monkeypatch.setattr(activity, "observe_muse", lambda *args, **kwargs: observed)
    binding = activity.ActivityBinding(41000, "27", CWD, tmux_target="%8", binding_id=1)
    metadata = {}
    result = activity._observe(1, "muse-cli", session_id, [binding], False, datetime.now(timezone.utc),
                               provenance=metadata)
    return result, metadata


def test_first_launch_discovers_the_session_and_asks_for_resume(monkeypatch):
    result, metadata = _dispatch(monkeypatch, None, discovered=SID)
    assert (result.state, result.reason) == ("unknown", "restart_needed_for_observation")
    assert metadata["discovered_session_id"] == SID


def test_no_discoverable_session_is_unknown(monkeypatch):
    result, metadata = _dispatch(monkeypatch, None, discovered=None)
    assert result.reason == "session_identity_unavailable" and "discovered_session_id" not in metadata


def test_open_question_becomes_a_bound_wait(monkeypatch):
    result, metadata = _dispatch(monkeypatch, SID, observed=("waiting_input", "native_question_open", None))
    assert result.waiting_input is not None and result.waiting_input.reason == "question"
    assert (result.state, result.reason) == ("idle", "native_input_wait")
    assert metadata["input_wait_reason"] == "question" and metadata["input_wait_binding_id"] == 1


def test_a_finished_turn_clears_the_wait(monkeypatch):
    result, metadata = _dispatch(monkeypatch, SID, observed=("idle", "native_turn_completed", None))
    assert result.waiting_input is None and result.state == "idle"
    assert metadata["input_wait_reason"] == "clear"


@pytest.mark.asyncio
async def test_discovered_muse_identity_switches_the_slot_to_resume(db):
    from app.models.database import AgentTeamPreset, AgentTeamSlot
    from app.services import agent_activity_service as activity
    preset = AgentTeamPreset(name="muse team")
    db.add(preset)
    await db.flush()
    slots = [AgentTeamSlot(preset_id=preset.id, position=i, display_name=f"M{i}", provider="muse-cli",
                           repo_id=f"r{i}", repo_path=f"/w{i}", repo_name=f"w{i}", launch_mode="plain",
                           launch_options={"model": "muse-wonder-1.0"}) for i in (1, 2)]
    db.add_all(slots)
    await db.commit()
    first, second = ((slot.id, "muse-cli", None) for slot in slots)
    current = {first[0]: first, second[0]: second}
    assert await activity._save_codex_identities(db, [first], [{"discovered_session_id": SID}], current)
    await db.refresh(slots[0])
    assert slots[0].launch_mode == "resume"
    assert slots[0].launch_options == {"model": "muse-wonder-1.0", "session_id": SID, "use_last": False}
    # The same id cannot be bound to a second slot.
    assert not await activity._save_codex_identities(db, [second], [{"discovered_session_id": SID}], current)
    await db.refresh(slots[1])
    assert slots[1].launch_mode == "plain" and "session_id" not in slots[1].launch_options


def test_a_log_larger_than_the_tail_keeps_its_binding(tmp_path):
    # The metadata and opened records are written once, at the head; a long session pushes them out of the tail.
    import json
    import os
    import app.services.muse_activity_service as m

    lines = (FIX / "turn_completed.jsonl").read_bytes().splitlines(keepends=True)
    started = next(i for i, line in enumerate(lines) if b'"prompt"' in line)
    filler = json.loads(lines[started])
    filler["payload"]["event"] = {"kind": "output", "text": "x" * 4096}
    filler_line = (json.dumps(filler) + "\n").encode()
    count = m._TAIL_BYTES // len(filler_line) + 10
    path = tmp_path / "session.jsonl"
    path.write_bytes(b"".join(lines[:started]) + filler_line * count + b"".join(lines[started:]))

    tail, _offset, failure = m._read_lines(path, os.getuid())

    assert failure is None
    assert muse_state_from_lines(tail, SID, CWD, NOW, START)[:2] == ("idle", "native_turn_completed")


def test_an_out_of_range_timestamp_is_invalid_not_an_error():
    import json

    lines = (FIX / "turn_completed.jsonl").read_bytes().splitlines(keepends=True)
    row = json.loads(lines[-1])
    row["recorded_at"] = 10**25
    lines[-1] = (json.dumps(row) + "\n").encode()
    with pytest.raises(ValueError):
        muse_state_from_lines(lines, SID, CWD, NOW, START)


# #746: a Muse member can run as another OS user than the controller. Its sessions root
# comes from the observed process, never from the controller's own environment or home.
def _owner(monkeypatch, tmp_path, environ=None):
    import app.services.providers.muse_cli as cli
    owner_home = tmp_path / "agent-home"
    monkeypatch.setenv("MUSE_DATA_DIR", str(tmp_path / "controller-muse"))
    monkeypatch.setenv("HOME", str(tmp_path / "controller-home"))
    monkeypatch.setattr(cli, "_owner_home", lambda pid: owner_home)

    def read(pid):
        if environ is None:
            raise PermissionError("another user's environ")
        return environ
    monkeypatch.setattr(cli, "_process_environ", read)
    return cli, owner_home


def test_the_root_is_the_process_owners_default_when_its_environ_is_unreadable(tmp_path, monkeypatch):
    cli, owner_home = _owner(monkeypatch, tmp_path)
    assert cli.muse_data_dir_for_process(4100) == owner_home / ".local" / "share" / "muse"


# Muse 1.4.20 wrote its sessions under XDG_DATA_HOME/muse with both variables set (#746 E2).
# MUSE_DATA_DIR alone was not observed; it is honoured when XDG_DATA_HOME is unset.
@pytest.mark.parametrize("environ,expected", [
    ({b"MUSE_DATA_DIR": b"/srv/agent/muse", b"XDG_DATA_HOME": b"/srv/xdg", b"HOME": b"/srv/home"}, "/srv/xdg/muse"),
    ({b"MUSE_DATA_DIR": b"/srv/agent/muse", b"XDG_DATA_HOME": b"/srv/xdg"}, "/srv/xdg/muse"),
    ({b"XDG_DATA_HOME": b"/srv/xdg", b"HOME": b"/srv/home"}, "/srv/xdg/muse"),
    ({b"MUSE_DATA_DIR": b"/srv/agent/muse", b"HOME": b"/srv/home"}, "/srv/agent/muse"),
    ({b"MUSE_DATA_DIR": b"/srv/agent/muse"}, "/srv/agent/muse"),
    ({b"HOME": b"/srv/home"}, "/srv/home/.local/share/muse"),
])
def test_the_root_honours_the_processs_own_environment(tmp_path, monkeypatch, environ, expected):
    cli, _owner_home = _owner(monkeypatch, tmp_path, environ)
    assert cli.muse_data_dir_for_process(4100) == Path(expected)


def test_an_environ_without_any_root_falls_back_to_the_owners_home(tmp_path, monkeypatch):
    cli, owner_home = _owner(monkeypatch, tmp_path, {b"PATH": b"/usr/bin"})
    assert cli.muse_data_dir_for_process(4100) == owner_home / ".local" / "share" / "muse"


@pytest.mark.parametrize("environ", [
    {b"XDG_DATA_HOME": b"relative/dir", b"MUSE_DATA_DIR": b"/srv/agent/muse"},
    {b"MUSE_DATA_DIR": b"relative/dir", b"HOME": b"/srv/home"},
    {b"HOME": b"relative/dir"},
])
def test_a_relative_process_root_is_refused(tmp_path, monkeypatch, environ):
    # The chosen step's value is relative: fail closed rather than skip to a later one.
    cli, _owner_home = _owner(monkeypatch, tmp_path, environ)
    with pytest.raises(ValueError):
        cli.muse_data_dir_for_process(4100)


def _owner_session(root, sid=SID, cwd=CWD):
    folder = root / "sessions" / "2026" / "10" / "14" / sid
    folder.mkdir(parents=True)
    (folder / ".session.lock").write_text("")
    (folder / "session.jsonl").write_text(
        (FIX / "turn_completed.jsonl").read_text().replace(SID, sid).replace(CWD, cwd))
    return folder


def _fds(tmp_path, monkeypatch, *targets):
    import app.services.muse_activity_service as m
    fds = tmp_path / "fd"
    fds.mkdir()
    for number, target in enumerate(targets, start=7):
        (fds / str(number)).symlink_to(target)
    monkeypatch.setattr(m, "_process_fd_dir", lambda pid: fds)
    monkeypatch.setattr(m, "_process_uid", lambda pid: __import__("os").getuid())  # never a host PID


def test_cross_user_discovery_uses_the_owners_xdg_root_when_the_environ_is_readable(tmp_path, monkeypatch):
    # The controller can read another user's descriptors with the service's capabilities (Supervisor-reported E1).
    _owner(monkeypatch, tmp_path, {b"XDG_DATA_HOME": str(tmp_path / "agent-xdg").encode(),
                                   b"MUSE_DATA_DIR": str(tmp_path / "unused").encode()})
    folder = _owner_session(tmp_path / "agent-xdg" / "muse")
    _fds(tmp_path, monkeypatch, folder / ".session.lock")
    assert discover_muse_session(4100, CWD) == SID


def test_cross_user_discovery_refuses_another_workspace(tmp_path, monkeypatch):
    _cli, owner_home = _owner(monkeypatch, tmp_path)
    folder = _owner_session(owner_home / ".local" / "share" / "muse", cwd="/tmp/another-workspace")
    _fds(tmp_path, monkeypatch, folder / ".session.lock")
    assert discover_muse_session(4100, CWD) is None


def test_cross_user_discovery_refuses_two_held_sessions(tmp_path, monkeypatch):
    _cli, owner_home = _owner(monkeypatch, tmp_path)
    root = owner_home / ".local" / "share" / "muse"
    first = _owner_session(root)
    second = _owner_session(root, sid="01a13be9-ffff-7fff-bfff-ffffffffffff")
    _fds(tmp_path, monkeypatch, first / ".session.lock", second / ".session.lock")
    assert discover_muse_session(4100, CWD) is None


def test_a_discovery_from_a_process_that_changed_is_not_saved(monkeypatch):
    # The pane process is re-checked by start time after discovery; a reused PID cannot supply an identity.
    from app.services import agent_activity_service as activity
    starts = iter(["27", "28"])
    monkeypatch.setattr(activity, "_process", lambda _: ("S", next(starts, "28")))
    monkeypatch.setattr(activity, "discover_muse_session", lambda pid, cwd: SID)
    binding = activity.ActivityBinding(41000, "27", CWD, tmux_target="%8", binding_id=1)
    metadata = {}
    result = activity._observe(1, "muse-cli", None, [binding], False, datetime.now(timezone.utc),
                               provenance=metadata)
    assert result.reason == "binding_changed" and "discovered_session_id" not in metadata


def test_discovery_reads_the_process_owners_sessions_not_the_controllers(tmp_path, monkeypatch):
    import app.services.muse_activity_service as m
    _cli, owner_home = _owner(monkeypatch, tmp_path)
    root = owner_home / ".local" / "share" / "muse"
    folder = root / "sessions" / "2026" / "10" / "14" / SID
    folder.mkdir(parents=True)
    (folder / ".session.lock").write_text("")
    (folder / "session.jsonl").write_text((FIX / "turn_completed.jsonl").read_text())
    fds = tmp_path / "fd"
    fds.mkdir()
    (fds / "7").symlink_to(folder / ".session.lock")
    monkeypatch.setattr(m, "_process_fd_dir", lambda pid: fds)
    monkeypatch.setattr(m, "_process_uid", lambda pid: __import__("os").getuid())  # never a host PID
    assert discover_muse_session(4100, CWD) == SID


def test_a_lock_under_the_controllers_root_is_not_this_processs_session(tmp_path, monkeypatch):
    import app.services.muse_activity_service as m
    _owner(monkeypatch, tmp_path)
    folder = tmp_path / "controller-muse" / "sessions" / "2026" / "10" / "14" / SID
    folder.mkdir(parents=True)
    (folder / ".session.lock").write_text("")
    (folder / "session.jsonl").write_text((FIX / "turn_completed.jsonl").read_text())
    fds = tmp_path / "fd"
    fds.mkdir()
    (fds / "7").symlink_to(folder / ".session.lock")
    monkeypatch.setattr(m, "_process_fd_dir", lambda pid: fds)
    monkeypatch.setattr(m, "_process_uid", lambda pid: __import__("os").getuid())  # never a host PID
    assert discover_muse_session(4100, CWD) is None


def _observable(tmp_path, monkeypatch):
    import app.services.muse_activity_service as m
    _cli, owner_home = _owner(monkeypatch, tmp_path)
    sessions = owner_home / ".local" / "share" / "muse" / "sessions"
    folder = sessions / "2026" / "10" / "14" / SID
    folder.mkdir(parents=True)
    (folder / "session.jsonl").write_text((FIX / "turn_completed.jsonl").read_text())
    monkeypatch.setattr(m, "_identity", lambda pid, session_id, cwd: True)
    return m, sessions, folder


def test_observation_reads_the_owners_log(tmp_path, monkeypatch):
    import os
    m, _sessions, _folder = _observable(tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_process_uid", lambda pid: os.getuid())
    assert m.observe_muse(4100, SID, CWD, NOW, START)[:2] == ("idle", "native_turn_completed")


@pytest.mark.skipif(__import__("os").geteuid() == 0, reason="root reads every mode")
@pytest.mark.parametrize("blocked", ["sessions", "day", "file"])
def test_an_unreadable_log_is_reported_as_unreadable(tmp_path, monkeypatch, blocked):
    import os
    m, sessions, folder = _observable(tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_process_uid", lambda pid: os.getuid())
    target = {"sessions": sessions, "day": folder.parent, "file": folder / "session.jsonl"}[blocked]
    target.chmod(0)
    try:
        assert m.observe_muse(4100, SID, CWD, NOW, START)[:2] == ("unknown", "native_log_unreadable")
    finally:
        target.chmod(0o700)


def test_a_missing_log_is_still_unavailable(tmp_path, monkeypatch):
    import os
    m, _sessions, folder = _observable(tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_process_uid", lambda pid: os.getuid())
    (folder / "session.jsonl").unlink()
    folder.rmdir()
    assert m.observe_muse(4100, SID, CWD, NOW, START)[:2] == ("unknown", "native_log_unavailable")


def test_the_guard_keeps_its_explicit_root(tmp_path, monkeypatch):
    # The supervisor guard is configured with its own muse_sessions_root; the per-process root does not reach it.
    supervisor = (Path(__file__).resolve().parents[3] / "scripts" / "product-factory" / "team-supervisor").read_text()
    assert "muse_sessions_root" in supervisor and "muse_data_dir_for_process" not in supervisor


def test_an_unreadable_log_is_an_observation_gap_like_an_unavailable_one():
    from app.services.owner_observation_pause import _GAPS
    assert {"native_log_unavailable", "native_log_unreadable"} <= _GAPS


# V1 review 5914876734: discovery returns an id only on affirmative binding proof
# (opened record with this id, metadata with this workspace, complete lines only),
# read as the observed process's owner. Each case runs through the real saver.
def _fixture_lines(sid=SID, cwd=CWD):
    return (FIX / "turn_completed.jsonl").read_text().replace(SID, sid).replace(CWD, cwd).splitlines(keepends=True)


def _discoverable(tmp_path, monkeypatch, lines, *, uid=None):
    import os
    import app.services.muse_activity_service as m
    _cli, owner_home = _owner(monkeypatch, tmp_path)
    folder = owner_home / ".local" / "share" / "muse" / "sessions" / "2026" / "10" / "14" / SID
    folder.mkdir(parents=True)
    (folder / ".session.lock").write_text("")
    (folder / "session.jsonl").write_text("".join(lines))
    _fds(tmp_path, monkeypatch, folder / ".session.lock")
    monkeypatch.setattr(m, "_process_uid", lambda pid: os.getuid() if uid is None else uid)
    return m


async def _saved_through_the_real_saver(db, monkeypatch):
    from app.models.database import AgentTeamPreset, AgentTeamSlot
    from app.services import agent_activity_service as activity
    preset = AgentTeamPreset(name="muse saver")
    db.add(preset)
    await db.flush()
    slot = AgentTeamSlot(preset_id=preset.id, position=1, display_name="M1", provider="muse-cli", repo_id="r1",
                         repo_path="/w1", repo_name="w1", launch_mode="plain", launch_options={"model": "m"})
    db.add(slot)
    await db.commit()
    monkeypatch.setattr(activity, "_process", lambda _: ("S", "27"))
    binding = activity.ActivityBinding(41000, "27", CWD, tmux_target="%8", binding_id=1)
    metadata = {}
    activity._observe(slot.id, "muse-cli", None, [binding], False, datetime.now(timezone.utc), provenance=metadata)
    entry = (slot.id, "muse-cli", None)
    await activity._save_codex_identities(db, [entry], [metadata], {slot.id: entry})
    await db.refresh(slot)
    return slot


@pytest.mark.asyncio
async def test_a_wrong_workspace_log_with_a_partial_last_record_is_not_saved(db, tmp_path, monkeypatch):
    _discoverable(tmp_path, monkeypatch, _fixture_lines(cwd="/tmp/another-workspace") + ['{"partial'])
    slot = await _saved_through_the_real_saver(db, monkeypatch)
    assert slot.launch_mode == "plain" and "session_id" not in slot.launch_options


@pytest.mark.asyncio
async def test_a_log_owned_by_another_user_than_the_process_is_not_saved(db, tmp_path, monkeypatch):
    import os
    _discoverable(tmp_path, monkeypatch, _fixture_lines(), uid=os.getuid() + 1)
    slot = await _saved_through_the_real_saver(db, monkeypatch)
    assert slot.launch_mode == "plain" and "session_id" not in slot.launch_options


@pytest.mark.asyncio
async def test_a_same_workspace_log_with_a_partial_last_record_is_still_saved(db, tmp_path, monkeypatch):
    _discoverable(tmp_path, monkeypatch, _fixture_lines() + ['{"partial'])
    slot = await _saved_through_the_real_saver(db, monkeypatch)
    assert slot.launch_mode == "resume" and slot.launch_options["session_id"] == SID


def _without(kind):
    return [line for line in _fixture_lines() if f'"payload_type": "{kind}"' not in line]


def _with_bad_first_event(replacement):
    lines = _fixture_lines()
    index = next(i for i, line in enumerate(lines) if '"payload_type": "runtime.session"' in line)
    lines[index] = replacement
    return lines


def _with_other_stream():
    lines = _fixture_lines()
    index = next(i for i, line in enumerate(lines) if '"payload_type": "runtime.session.metadata"' in line)
    lines[index] = lines[index].replace(f'"id": "{SID}"', '"id": "01a13be9-0000-7000-8000-000000000000"', 1)
    return lines


@pytest.mark.parametrize("case", ["no_opened", "no_metadata", "non_object_row", "invalid_json", "other_stream"])
def test_discovery_needs_every_binding_record_intact(tmp_path, monkeypatch, case):
    lines = {"no_opened": lambda: _without("session.opened.observed"),
             "no_metadata": lambda: _without("runtime.session.metadata"),
             "non_object_row": lambda: _with_bad_first_event("[1, 2]\n"),
             "invalid_json": lambda: _with_bad_first_event("{not json}\n"),
             "other_stream": _with_other_stream}[case]()
    _discoverable(tmp_path, monkeypatch, lines)
    assert discover_muse_session(4100, CWD) is None
