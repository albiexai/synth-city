"""Read bounded Muse Code turn evidence for an explicitly identified process.

Muse Code writes one structured event log per session:
``<data dir>/sessions/YYYY/MM/DD/<session uuid>/session.jsonl``. Every record
names its session in ``stream.id`` and its time in ``recorded_at`` (epoch
microseconds). The live ``muse-bin`` process holds the session's
``.session.lock`` open, which binds a session to exactly one process.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from app.services.providers.muse_cli import is_muse_command, muse_data_dir_for_process

_TAIL_BYTES = 1_048_576
_HEAD_BYTES = 262_144
_SCAN_STEP = 1_048_576
_SCAN_BYTES = 8 * 1_048_576  # backward scan cap for a turn that started before the tail
_BINDING_RECORDS = {"session.opened.observed", "runtime.session.metadata"}
_MAX_DESCRIPTORS = 4096
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_PROGRESS = {"model_completed", "assistant_tool_calls_committed", "tool_result_batch_committed"}


def _process_fd_dir(pid: int) -> Path:
    return Path(f"/proc/{pid}/fd")


def _cmdline(pid: int) -> list[str]:
    return [os.fsdecode(part) for part in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if part]


def _sessions_root(pid: int) -> Path:
    return (muse_data_dir_for_process(pid) / "sessions").resolve()


def _process_uid(pid: int) -> int:
    return Path(f"/proc/{pid}").stat().st_uid


def _timestamp(value: object) -> datetime:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("Muse recorded_at must be integer microseconds")
    try:
        return datetime.fromtimestamp(value / 1_000_000, timezone.utc)
    except (OverflowError, OSError) as exc:
        raise ValueError("Muse recorded_at is out of range") from exc


def muse_state_from_lines(lines: list[bytes], session_id: str, cwd: str, now: datetime,
                          started_at: datetime) -> tuple[str, str, datetime | None, dict]:
    """Derive the turn state from complete event-log lines, oldest first."""
    details: dict = {}
    if lines and not lines[-1].endswith(b"\n"):
        return "unknown", "observation_incomplete", None, details
    opened = workspace = False
    state, reason, observed_at = "unknown", "no_native_event", None
    pending: set[str] = set()
    in_turn = False
    cursor = None
    for line in lines:
        row = json.loads(line)
        if not isinstance(row, dict):
            return "unknown", "observation_invalid", None, details
        if "payload_type" not in row:
            continue  # the retained-frame header carries no event
        stream = row.get("stream")
        if not isinstance(stream, dict) or stream.get("id") != session_id:
            return "unknown", "session_mismatch", None, details
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        kind = row["payload_type"]
        if kind == "session.opened.observed":
            record = payload.get("record") or {}
            if record.get("session_id") != session_id:
                return "unknown", "session_mismatch", None, details
            opened = True
            continue
        if kind == "runtime.session.metadata":
            record = payload.get("record") or {}
            root = record.get("workspace_root")
            if (not isinstance(root, str) or not Path(root).is_absolute()
                    or Path(root).resolve() != Path(cwd).resolve()):
                return "unknown", "session_mismatch", None, details
            workspace = True
            build = record.get("build")
            if isinstance(build, dict) and isinstance(build.get("semver"), str):
                details["build"] = build["semver"]
            if isinstance(record.get("model_id"), str):
                details["model"] = record["model_id"]
            continue
        if kind != "runtime.session":
            continue
        event = payload.get("event")
        if not isinstance(event, dict):
            continue
        timestamp = _timestamp(row.get("recorded_at"))
        if timestamp > now + timedelta(seconds=5):
            return "unknown", "observation_invalid", None, details
        if timestamp < started_at:
            continue
        if observed_at and timestamp < observed_at:
            return "unknown", "observation_invalid", None, details
        event_kind = event.get("kind")
        if event_kind == "started" and "prompt" in event:
            in_turn, pending = True, set()
            state, reason = "working", "native_turn_started"
        elif event_kind == "user_input_prompt_requested":
            if not in_turn or not isinstance(event.get("prompt_id"), str):
                state, reason = "unknown", "native_progress_without_start"
            else:
                pending.add(event["prompt_id"])
                state, reason = "working", "native_question_open"
        elif event_kind == "user_input_prompt_settled":
            pending.discard(event.get("prompt_id"))
            state, reason = ("working", "native_progress") if in_turn else ("unknown", "native_progress_without_start")
        elif event_kind == "terminal":
            if not in_turn:
                state, reason = "unknown", "completion_unconfirmed"
            elif event.get("terminal") == "completed":
                state, reason = "idle", "native_turn_completed"
            else:
                state, reason = "unknown", "native_turn_interrupted"
            in_turn, pending = False, set()
        elif event_kind in _PROGRESS:
            state, reason = ("working", "native_progress") if in_turn else ("unknown", "native_progress_without_start")
        else:
            continue
        observed_at = timestamp
        cursor = hashlib.sha256(f"{session_id}:{row.get('id')}:{event_kind}".encode()).hexdigest()
    if not (opened and workspace):
        return "unknown", "session_mismatch", None, details
    details["event_id"] = cursor
    if pending and state == "working":
        return "waiting_input", "native_question_open", observed_at, details
    if state == "working" and observed_at and (now - observed_at).total_seconds() > 180:
        return "unknown", "native_event_stale", observed_at, details
    return state, reason, observed_at, details


def _read_lines(path: Path, uid: int) -> tuple[list[bytes] | None, int, str | None]:
    """Read a bounded tail. Return (lines, offset, failure reason)."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != uid
                or before.st_mode & stat.S_IWOTH):
            raise ValueError("Unsafe native observation file")
        offset = max(0, before.st_size - _TAIL_BYTES)
        # The binding records are written once, near the head; a long session
        # pushes them out of the tail. Keep them from a bounded head read.
        head = _read_at(stream, 0, min(_HEAD_BYTES, offset)) if offset else b""
        data = _read_at(stream, offset, _TAIL_BYTES)
        lines = data.splitlines(keepends=True)
        carried: list[bytes] = []
        if offset:
            first, lines = (lines[0], lines[1:]) if lines else (b"", [])  # the first line of a tail may be partial
            if not any(_turn_record(line) in {"start", "end"} for line in lines):
                carried = _scan_for_turn(stream, offset, first)
        after = os.fstat(stream.fileno())
    current = path.stat(follow_symlinks=False)
    binding = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    if binding(before) != binding(after) or binding(after) != binding(current):
        return None, offset, "binding_changed"
    if offset:
        lines = [line for line in head.splitlines(keepends=True) if _is_binding_record(line)] + carried + lines
    return lines, offset, None


def _read_at(stream, offset: int, size: int) -> bytes:
    stream.seek(offset)
    return stream.read(size)


def _turn_record(line: bytes) -> str | None:
    """Classify a complete record: a turn start, a turn end, a question event, or neither."""
    if not line.endswith(b"\n") or b'"runtime.session"' not in line:
        return None
    try:
        row = json.loads(line)
    except ValueError:
        return None
    if not isinstance(row, dict) or row.get("payload_type") != "runtime.session":
        return None
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    event = payload.get("event")
    if not isinstance(event, dict):
        return None
    kind = event.get("kind")
    if kind == "started" and "prompt" in event:
        return "start"
    if kind == "terminal":
        return "end"
    if kind in {"user_input_prompt_requested", "user_input_prompt_settled"}:
        return "question"
    return None


def _scan_for_turn(stream, offset: int, fragment: bytes) -> list[bytes]:
    """#769: a long turn can write more than the tail after its start.

    Read backwards from the tail in bounded steps for the nearest turn boundary.
    Keep that boundary and the question events after it, in file order. If the
    boundary ends a turn, also look for that turn's start, so a finished turn is
    confirmed. Give up at _SCAN_BYTES: the state then stays as conservative as before.
    """
    kept: list[bytes] = []
    ended = False  # a terminal was found; now only its turn's start is wanted
    carry = fragment  # the part of the line that straddles the tail offset
    end, floor = offset, max(0, offset - _SCAN_BYTES)
    while end > floor:
        start = max(floor, end - _SCAN_STEP)
        pieces = (_read_at(stream, start, end - start) + carry).splitlines(keepends=True)
        if start > 0:
            carry, pieces = (pieces[0], pieces[1:]) if pieces else (b"", [])
        else:
            carry = b""
        for line in reversed(pieces):
            kind = _turn_record(line)
            if kind == "question" and not ended:
                kept.append(line)
            elif kind == "start":
                kept.append(line)
                return kept[::-1]
            elif kind == "end":
                if ended:  # two ends without a start between: do not pair them, stay unconfirmed
                    return kept[::-1]
                kept.append(line)
                ended = True
        end = start
    return kept[::-1] if ended else []


def _is_binding_record(line: bytes) -> bool:
    if not line.endswith(b"\n"):
        return False
    try:
        row = json.loads(line)
    except ValueError:
        return False
    return isinstance(row, dict) and row.get("payload_type") in _BINDING_RECORDS


def _session_path(root: Path, session_id: str) -> Path | None:
    """Find the one session log. An unreadable level raises PermissionError.

    Path.glob silently skips directories it cannot read, which would report an
    unreadable log as a missing one, so each level is listed explicitly.
    """
    matches = []
    try:
        years = list(os.scandir(root))
    except FileNotFoundError:
        return None
    for year in years:
        if not year.is_dir(follow_symlinks=False):
            continue
        for month in os.scandir(year.path):
            if not month.is_dir(follow_symlinks=False):
                continue
            for day in os.scandir(month.path):
                if not day.is_dir(follow_symlinks=False):
                    continue
                path = Path(day.path) / session_id / "session.jsonl"
                try:
                    os.stat(path, follow_symlinks=False)
                except (FileNotFoundError, NotADirectoryError):
                    continue
                matches.append(path)
    return matches[0] if len(matches) == 1 else None


def discover_muse_session(pid: int, cwd: str) -> str | None:
    """Find the one session whose lock this exact process holds."""
    root = _sessions_root(pid)
    found = set()
    with os.scandir(_process_fd_dir(pid)) as entries:
        for index, entry in enumerate(entries):
            if index >= _MAX_DESCRIPTORS:
                raise ValueError("Process descriptor observation exceeds its bound")
            try:
                target = Path(os.readlink(entry.path))
            except OSError:
                continue
            if target.name != ".session.lock" or not target.is_absolute():
                continue
            folder = target.parent
            if not _UUID.fullmatch(folder.name) or not folder.resolve().is_relative_to(root):
                continue
            found.add(folder)
    if len(found) != 1:
        return None
    folder = found.pop()
    session_id = folder.name
    try:
        # Read as the observed process's owner: a log another user wrote is not this process's session.
        lines, _offset, failure = _read_lines(folder / "session.jsonl", _process_uid(pid))
        if failure or lines is None:
            return None
        return session_id if _session_binding(lines, session_id, cwd) else None
    except (OSError, ValueError):
        return None


def _session_binding(lines: list[bytes], session_id: str, cwd: str) -> bool:
    """Affirmative identity proof for a first launch, from complete records only.

    An incomplete activity tail is not identity proof: drop a trailing partial line,
    then require this session's opened record and its workspace record for this cwd,
    with every record on this session's stream.
    """
    if lines and not lines[-1].endswith(b"\n"):
        lines = lines[:-1]
    opened = workspace = False
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            return False
        if not isinstance(row, dict):
            return False
        if "payload_type" not in row:
            continue  # the retained-frame header carries no event
        stream = row.get("stream")
        if not isinstance(stream, dict) or stream.get("id") != session_id:
            return False
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        record = payload.get("record") if isinstance(payload.get("record"), dict) else {}
        if row["payload_type"] == "session.opened.observed":
            if record.get("session_id") != session_id:
                return False
            opened = True
        elif row["payload_type"] == "runtime.session.metadata":
            root = record.get("workspace_root")
            if (not isinstance(root, str) or not Path(root).is_absolute()
                    or Path(root).resolve() != Path(cwd).resolve()):
                return False
            workspace = True
    return opened and workspace


def _identity(pid: int, session_id: str, cwd: str) -> bool:
    argv = _cmdline(pid)
    if not argv or not is_muse_command(argv[0]):
        return False
    if "resume" in argv:
        index = argv.index("resume")
        return index + 1 < len(argv) and argv[index + 1] == session_id
    return discover_muse_session(pid, cwd) == session_id


def observe_muse(pid: int, session_id: str, cwd: str, now: datetime,
                 started_at: datetime, *, provenance: dict | None = None):
    if not _UUID.fullmatch(session_id) or not _identity(pid, session_id, cwd):
        return "unknown", "session_mismatch", None
    try:
        path = _session_path(_sessions_root(pid), session_id)
        if path is None:
            return "unknown", "native_log_unavailable", None
        lines, _offset, failure = _read_lines(path, _process_uid(pid))
    except PermissionError:
        # The controller cannot read this member's log: see the cross-user install notes.
        return "unknown", "native_log_unreadable", None
    if failure or lines is None:
        return "unknown", failure or "observation_unavailable", None
    state, reason, observed_at, details = muse_state_from_lines(lines, session_id, cwd, now, started_at)
    if provenance is not None:
        provenance.update(session_id=session_id, event_id=details.get("event_id"),
                          muse_build=details.get("build"), muse_model=details.get("model"))
    return state, reason, observed_at
