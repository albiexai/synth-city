"""Read bounded Antigravity CLI (agy) turn evidence for an explicitly identified process (#737).

Observed on agy 1.5.9 in an isolated runtime with a stub model (#737 P1), under the data root
``<passwd home>/.gemini/antigravity-cli``:

- ``presence/<uuid>.lock`` is held open by the live ``agy`` process for its conversation.
- ``conversation_summaries.db`` (SQLite, WAL): ``status`` is RUNNING with ``not_fully_idle`` 1
  during a turn and IDLE with 0 after it; ``workspace_uris`` names the trusted workspace.
- ``conversations/<uuid>.db`` (SQLite, WAL): ``steps(idx, step_type, status)``. Live statuses seen:
  2 while ``run_command`` ran (with skip-permissions), 3 when done, 9 while ``ask_question``
  waited. 6 was seen only after the process exited.
- ``brain/<uuid>/.system_generated/logs/transcript.jsonl``: one record per step; record statuses
  are not updated later, so only structure and tool names are taken from it.

The live database files are only read as bytes and copied; SQLite runs on a private copy.
Anything not matching an observed shape is ``unknown``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import sqlite3
import stat
import tempfile
import time

from app.services.providers.antigravity_cli import is_agy_executable

_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_TAIL_BYTES = 1_048_576
_SCAN_STEP = 1_048_576
_SCAN_BYTES = 8 * 1_048_576
_FILE_CAP = 16 * 1_048_576
_DEADLINE_SECONDS = 1.0
_VM_STEPS = 200_000
_STEP_ROWS = 64
_FRESH_SECONDS = 180

_RUNNING = "CASCADE_RUN_STATUS_RUNNING"
_IDLE = "CASCADE_RUN_STATUS_IDLE"
# Observed step types and statuses (numeric values only; agy's enum names are not mapped).
_INPUT, _PLANNER, _TOOL, _SYSTEM = 14, 15, 132, 101
_DONE, _TOOL_RUNNING, _USER_WAIT = 3, 2, 9


class _Unknown(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --- Process facts (replaced in tests; never host processes) --------------------------------

def _process_uid(pid: int) -> int:
    return Path(f"/proc/{pid}").stat().st_uid


def _passwd_home(uid: int) -> str:
    return pwd.getpwuid(uid).pw_dir


def _exe_identity(pid: int) -> tuple[str, int, int]:
    target = os.readlink(f"/proc/{pid}/exe")
    info = os.stat(f"/proc/{pid}/exe")
    return os.path.basename(target), info.st_dev, info.st_ino


def _fd_targets(pid: int) -> list[str]:
    root = Path(f"/proc/{pid}/fd")
    targets = []
    for entry in os.scandir(root):
        try:
            targets.append(os.readlink(entry.path))
        except OSError:
            continue
    return targets


def _between_rounds(attempt: int) -> None:
    """A test hook between the two copy rounds of one snapshot attempt."""


# --- Trusted root and identity --------------------------------------------------------------

def _owned_dir(path: Path, uid: int) -> None:
    info = os.lstat(path)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & stat.S_IWOTH):
        raise _Unknown("session_mismatch")


def _trusted_root(uid: int) -> Path:
    """``<passwd home>/.gemini/antigravity-cli``, with every component from the home down owned
    by the process uid, not a symlink and not world-writable. The process environment is not used."""
    home = Path(_passwd_home(uid))
    if not home.is_absolute() or os.path.realpath(home) != str(home):
        raise _Unknown("session_mismatch")
    root = home
    try:
        _owned_dir(root, uid)
        for part in (".gemini", "antigravity-cli"):
            root = root / part
            _owned_dir(root, uid)
    except FileNotFoundError:
        raise _Unknown("native_log_unavailable") from None
    return root


def _presence_ids(pid: int, root: Path) -> list[str]:
    presence = str(root / "presence") + os.sep
    found = []
    for target in _fd_targets(pid):
        if target.startswith(presence) and target.endswith(".lock"):
            name = target[len(presence):-len(".lock")]
            if _UUID.fullmatch(name):
                found.append(name)
    return sorted(set(found))


def _identity(pid: int) -> tuple:
    uid = _process_uid(pid)
    exe = _exe_identity(pid)
    return uid, exe


def _bound_root(pid: int) -> tuple[int, Path, tuple]:
    identity = _identity(pid)
    if not is_agy_executable(identity[1][0]):
        raise _Unknown("session_mismatch")
    return identity[0], _trusted_root(identity[0]), identity


# --- Bounded, coherent snapshots of live SQLite files ---------------------------------------

def _read_file(path: Path, uid: int, deadline: float) -> tuple[bytes, tuple] | None:
    """The bytes of one regular file owned by ``uid``; None when it does not exist."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise _Unknown("observation_unavailable") from None
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != uid or before.st_mode & stat.S_IWOTH):
            raise _Unknown("observation_unavailable")
        if before.st_size > _FILE_CAP:
            raise _Unknown("observation_incomplete")
        chunks, remaining = [], before.st_size + 1
        while remaining > 0:
            if time.monotonic() > deadline:
                raise _Unknown("observation_incomplete")
            chunk = os.read(fd, min(remaining, 1_048_576))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    key = (after.st_ino, after.st_size, after.st_mtime_ns)
    if (before.st_ino, before.st_size, before.st_mtime_ns) != key or len(data) != after.st_size:
        raise _Unknown("binding_changed")
    try:
        current = os.lstat(path)
    except FileNotFoundError:
        raise _Unknown("binding_changed") from None
    if (current.st_ino, current.st_size, current.st_mtime_ns) != key:
        raise _Unknown("binding_changed")
    return data, key


def _round(paths: dict[str, Path], uid: int, deadline: float) -> dict[str, tuple | None]:
    files = {}
    for name, path in paths.items():
        read = _read_file(path, uid, deadline)
        files[name] = None if read is None else (hashlib.sha256(read[0]).hexdigest(), read[1], read[0])
    return files


def _snapshot(paths: dict[str, Path], uid: int, deadline: float) -> dict[str, bytes | None]:
    """Two identical copy rounds of every file, once retried; otherwise ``binding_changed``."""
    for attempt in range(2):
        first = _round(paths, uid, deadline)
        _between_rounds(attempt)
        second = _round(paths, uid, deadline)
        if all((first[n] is None) == (second[n] is None) and (first[n] is None or first[n][:2] == second[n][:2])
               for n in paths):
            return {name: (None if value is None else value[2]) for name, value in second.items()}
    raise _Unknown("binding_changed")


def _query(database: bytes, wal: bytes | None, deadline: float, statements: list[tuple[str, tuple]]) -> list:
    """Run fixed, limited queries on a private copy, under a VM-step budget and the deadline."""
    with tempfile.TemporaryDirectory(prefix="agy-observe-") as private:
        path = os.path.join(private, "copy.db")
        with open(path, "wb") as out:
            out.write(database)
        if wal is not None:
            with open(path + "-wal", "wb") as out:
                out.write(wal)
        connection = sqlite3.connect(path)
        steps = {"n": 0}

        def budget():
            steps["n"] += 1
            return 1 if steps["n"] > _VM_STEPS or time.monotonic() > deadline else 0
        connection.set_progress_handler(budget, 100)
        try:
            connection.execute("pragma query_only=on")
            if connection.execute("pragma quick_check").fetchone() != ("ok",):
                raise _Unknown("observation_invalid")
            return [connection.execute(sql, args).fetchall() for sql, args in statements]
        except sqlite3.DatabaseError:
            if time.monotonic() > deadline or steps["n"] > _VM_STEPS:
                raise _Unknown("observation_incomplete") from None
            raise _Unknown("observation_invalid") from None
        finally:
            connection.close()


def _summary(rows: list, cwd: str) -> dict | None:
    if len(rows) != 1:
        return None
    status, not_fully_idle, step_count, workspace_uris = rows[0]
    try:
        uris = json.loads(workspace_uris or "[]")
    except ValueError:
        raise _Unknown("observation_invalid") from None
    if not isinstance(uris, list) or Path(cwd).resolve().as_uri() not in uris:
        raise _Unknown("session_mismatch")
    return {"status": status, "not_fully_idle": not_fully_idle, "step_count": step_count}


# --- Transcript: structure and tool names only ----------------------------------------------

_KEEP = ("step_index", "source", "type", "status", "created_at")


def _record(line: bytes) -> dict:
    try:
        value = json.loads(line)
    except ValueError:
        raise _Unknown("observation_invalid") from None
    if not isinstance(value, dict):
        raise _Unknown("observation_invalid")
    record = {key: value.get(key) for key in _KEEP}
    calls = value.get("tool_calls")
    if isinstance(calls, list):
        record["tools"] = [call.get("name") if isinstance(call, dict) else None for call in calls]
    return record


def read_transcript_records(path: Path, uid: int) -> tuple[list[dict] | None, str | None]:
    """The latest records, back to the latest USER_INPUT where the bounded scan reaches it."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, "native_log_unavailable"
    except OSError:
        return None, "native_log_unreadable"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & stat.S_IWOTH:
            return None, "observation_unavailable"
        size = info.st_size
        offset = max(0, size - _TAIL_BYTES)
        data = os.pread(fd, size - offset, offset)
        if data and not data.endswith(b"\n"):
            return None, "observation_incomplete"
        while True:
            lines = data.split(b"\n")
            complete = lines[1:] if offset > 0 else lines
            records = [_record(line) for line in complete if line.strip()]
            if offset == 0 or any(r["type"] == "USER_INPUT" for r in records) or size - offset >= _SCAN_BYTES:
                return records, None
            step = min(_SCAN_STEP, offset)
            offset -= step
            data = os.pread(fd, step, offset) + data
    except _Unknown as failure:
        return None, failure.reason
    finally:
        os.close(fd)


# --- State from the observed shapes ---------------------------------------------------------

def _second(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def antigravity_state(summary: dict | None, step_count: int, steps: list[tuple[int, int, int]],
                      records: list[dict], started_at: datetime, now: datetime):
    """(state, reason, observed_at) from one coherent snapshot. Anything unproven is unknown."""
    inputs = [r for r in records if r["type"] == "USER_INPUT" and r["source"] == "USER_EXPLICIT"]
    if not inputs:
        return "unknown", "no_native_event", None
    current = inputs[-1]
    created = _second(current["created_at"])
    start_second = started_at.replace(microsecond=0)
    # One-second resolution: the input must be from a later second than the process start.
    if created is None or not isinstance(current["step_index"], int) or created <= start_second:
        return "unknown", "no_native_event", None
    live = {idx: (step_type, status) for idx, step_type, status in steps}
    if live.get(current["step_index"]) != (_INPUT, _DONE):
        return "unknown", "completion_unconfirmed", None
    if summary is None:
        return "unknown", "session_mismatch", None
    last_idx = max(live)
    last = live[last_idx]
    after = [r for r in records if isinstance(r["step_index"], int) and r["step_index"] > current["step_index"]]
    planners = [r for r in after if r["type"] == "PLANNER_RESPONSE" and r["source"] == "MODEL"]
    status, not_fully_idle = summary.get("status"), summary.get("not_fully_idle")

    if status == _IDLE and not_fully_idle == 0:
        if (last == (_PLANNER, _DONE) and last_idx > current["step_index"] and summary.get("step_count") == step_count
                and any(r["step_index"] == last_idx for r in planners)):
            return "idle", "native_turn_completed", created
        return "unknown", "completion_unconfirmed", None
    if status != _RUNNING or not_fully_idle != 1:
        return "unknown", "completion_unconfirmed", None

    planner = planners[-1] if planners else None
    tools = _tools(planner) if planner else None
    single = tools[0] if isinstance(tools, list) and len(tools) == 1 else None
    follows = planner is not None and last_idx == planner["step_index"] + 1 and live.get(planner["step_index"]) == (_PLANNER, _DONE)
    planner_at = _second(planner["created_at"]) if planner else None
    fresh_planner = planner_at is not None and planner_at > start_second

    if last[0] == _TOOL and last[1] == _USER_WAIT:
        if follows and fresh_planner and single == "ask_question":
            return "waiting_input", "native_question_open", planner_at
        return "unknown", "native_wait_unconfirmed", None
    if last[0] == _TOOL and last[1] == _TOOL_RUNNING:
        if follows and fresh_planner and single == "run_command":
            return _working(planner_at, now)
        return "unknown", "completion_unconfirmed", None
    if last_idx == current["step_index"]:
        return _working(created, now)
    if last == (_SYSTEM, _DONE) and last_idx > current["step_index"]:
        latest = max([created] + [t for t in (_second(r["created_at"]) for r in after) if t is not None])
        return _working(latest, now)
    if last == (_TOOL, _DONE) and follows and fresh_planner and single is not None:
        return _working(planner_at, now)
    return "unknown", "completion_unconfirmed", None


def _tools(record: dict) -> list | None:
    """Tool names of a PLANNER_RESPONSE, from a parsed (``tools``) or raw (``tool_calls``) record."""
    if "tools" in record:
        return record["tools"]
    calls = record.get("tool_calls")
    if not isinstance(calls, list):
        return None
    return [call.get("name") if isinstance(call, dict) else None for call in calls]


def _working(at: datetime, now: datetime):
    if now - at > timedelta(seconds=_FRESH_SECONDS):
        return "unknown", "native_event_stale", None
    return "working", "native_progress", at


# --- Observation and discovery --------------------------------------------------------------

def _databases(root: Path, conversation_id: str) -> dict[str, Path]:
    return {"summaries": root / "conversation_summaries.db",
            "summaries_wal": root / "conversation_summaries.db-wal",
            "conversation": root / "conversations" / f"{conversation_id}.db",
            "conversation_wal": root / "conversations" / f"{conversation_id}.db-wal"}


def _owned_tree(root: Path, relative: str, uid: int) -> Path:
    path = root
    for part in Path(relative).parts:
        path = path / part
        _owned_dir(path, uid)
    return path


def discover_antigravity_conversation(pid: int, cwd: str) -> str | None:
    """The conversation id of the one presence lock this process holds, bound to ``cwd``."""
    try:
        uid, root, identity = _bound_root(pid)
        ids = _presence_ids(pid, root)
        if len(ids) != 1:
            return None
        deadline = time.monotonic() + _DEADLINE_SECONDS
        files = _snapshot({"summaries": root / "conversation_summaries.db",
                           "summaries_wal": root / "conversation_summaries.db-wal"}, uid, deadline)
        if files["summaries"] is None:
            return None
        rows = _query(files["summaries"], files["summaries_wal"], deadline, [(
            "select status, not_fully_idle, step_count, workspace_uris from conversation_summaries"
            " where conversation_id=? limit 2", (ids[0],))])[0]
        if _summary(rows, cwd) is None or _identity(pid) != identity or _presence_ids(pid, root) != ids:
            return None
        return ids[0]
    except (_Unknown, OSError, KeyError):
        return None


def observe_antigravity(pid: int, conversation_id: str, cwd: str, now: datetime, started_at: datetime):
    try:
        if not _UUID.fullmatch(conversation_id):
            return "unknown", "session_mismatch", None
        uid, root, identity = _bound_root(pid)
        if _presence_ids(pid, root) != [conversation_id]:
            return "unknown", "session_mismatch", None
        deadline = time.monotonic() + _DEADLINE_SECONDS
        files = _snapshot(_databases(root, conversation_id), uid, deadline)
        if files["summaries"] is None or files["conversation"] is None:
            return "unknown", "native_log_unavailable", None
        summary_rows = _query(files["summaries"], files["summaries_wal"], deadline, [(
            "select status, not_fully_idle, step_count, workspace_uris from conversation_summaries"
            " where conversation_id=? limit 2", (conversation_id,))])[0]
        count_rows, step_rows = _query(files["conversation"], files["conversation_wal"], deadline, [
            ("select count(*) from steps", ()),
            ("select idx, step_type, status from steps order by idx desc limit ?", (_STEP_ROWS,))])
        summary = _summary(summary_rows, cwd)
        try:
            logs = _owned_tree(root, f"brain/{conversation_id}/.system_generated/logs", uid)
        except FileNotFoundError:
            return "unknown", "native_log_unavailable", None
        records, failure = read_transcript_records(logs / "transcript.jsonl", uid)
        if failure:
            return "unknown", failure, None
        if time.monotonic() > deadline:
            return "unknown", "observation_incomplete", None
        if _identity(pid) != identity or _presence_ids(pid, root) != [conversation_id]:
            return "unknown", "binding_changed", None
        return antigravity_state(summary, count_rows[0][0], step_rows, records, started_at, now)
    except _Unknown as failure:
        return "unknown", failure.reason, None
    except PermissionError:
        # The controller cannot read this member's files: see the cross-user install notes.
        return "unknown", "native_log_unreadable", None
    except OSError:
        return "unknown", "observation_unavailable", None
