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


def _cmdline(pid: int) -> list[str]:
    return [os.fsdecode(part) for part in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if part]


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


class _Tree:
    """Directory descriptors from the passwd home down, each opened with ``O_NOFOLLOW`` relative to
    its parent and checked (directory, owned by the uid, not world-writable). Files are opened
    relative to a held descriptor, so a component replaced during the read is never followed;
    ``unchanged`` then detects the replacement."""

    def __init__(self, root: Path, uid: int):
        self.uid, self.root, self.fds, self.ids = uid, root, {}, {}
        home = root.parent.parent
        try:
            self._hold("", os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
            self.dir(".gemini/antigravity-cli")
        except BaseException:
            self.close()
            raise

    def _hold(self, rel: str, fd: int) -> None:
        self.fds[rel] = fd
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != self.uid or info.st_mode & stat.S_IWOTH:
            raise _Unknown("session_mismatch")
        self.ids[rel] = (info.st_dev, info.st_ino)

    def dir(self, rel: str) -> int:
        parent = ""
        for part in rel.split("/"):
            if part in ("", ".", ".."):
                raise _Unknown("session_mismatch")
            current = f"{parent}/{part}" if parent else part
            if current not in self.fds:
                try:
                    fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fds[parent])
                except FileNotFoundError:
                    raise _Unknown("native_log_unavailable") from None
                except PermissionError:
                    raise
                except OSError:
                    # ELOOP or ENOTDIR: a symlink or a non-directory in place of a directory.
                    raise _Unknown("session_mismatch") from None
                self._hold(current, fd)
            parent = current
        return self.fds[parent]

    def data(self, rel: str = "") -> int:
        """A directory below ``antigravity-cli``."""
        return self.dir(".gemini/antigravity-cli" + (f"/{rel}" if rel else ""))

    def _trusted(self, info: os.stat_result, ident: tuple) -> bool:
        return (stat.S_ISDIR(info.st_mode) and info.st_uid == self.uid and not info.st_mode & stat.S_IWOTH
                and (info.st_dev, info.st_ino) == ident)

    def unchanged(self) -> bool:
        """Every held directory, by descriptor and by name, is still the same trusted directory."""
        home = self.root.parent.parent
        for rel, ident in self.ids.items():
            try:
                held = os.fstat(self.fds[rel])
                named = os.lstat(home / rel if rel else home)
            except OSError:
                return False
            if not self._trusted(held, ident) or not self._trusted(named, ident):
                return False
        return True

    def close(self) -> None:
        for fd in self.fds.values():
            os.close(fd)
        self.fds = {}


def _presence_ids(pid: int, tree: "_Tree") -> list[str]:
    presence = str(tree.root / "presence") + os.sep
    presence_fd = tree.data("presence")
    found = []
    for target in _fd_targets(pid):
        if target.startswith(presence) and target.endswith(".lock"):
            name = target[len(presence):]
            if not _UUID.fullmatch(name[:-len(".lock")]):
                continue
            try:
                info = os.stat(name, dir_fd=presence_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode) and info.st_uid == tree.uid:
                found.append(name[:-len(".lock")])
    return sorted(set(found))


_VALUED = ("--model", "--effort")


def permission_context(argv: list[str]) -> bool | None:
    """True for ``--dangerously-skip-permissions``, False without it, None for any argv that is not
    exactly the shape Albiex Synth City launches (``providers/antigravity_cli.py``)."""
    if not argv or not is_agy_executable(argv[0]):
        return None
    rest, skip = argv[1:], False
    for option in _VALUED:
        if rest[:1] == [option]:
            if len(rest) < 2 or not rest[1] or rest[1].startswith("-"):
                return None
            rest = rest[2:]
    if rest[:1] == ["--dangerously-skip-permissions"]:
        skip, rest = True, rest[1:]
    if rest[:1] == ["--conversation"] and len(rest) >= 2 and _UUID.fullmatch(rest[1]):
        rest = rest[2:]
    elif rest[:1] and rest[0].startswith("--conversation=") and _UUID.fullmatch(rest[0].split("=", 1)[1]):
        rest = rest[1:]
    if rest[:1] == ["-i"] and len(rest) == 2:
        rest = []
    return skip if not rest else None


def _identity(pid: int) -> tuple:
    uid = _process_uid(pid)
    exe = _exe_identity(pid)
    try:
        permission = permission_context(_cmdline(pid))
    except OSError:
        permission = None
    return uid, exe, permission


def _bound_root(pid: int) -> tuple[int, "_Tree", tuple]:
    identity = _identity(pid)
    if not is_agy_executable(identity[1][0]):
        raise _Unknown("session_mismatch")
    return identity[0], _Tree(_trusted_root(identity[0]), identity[0]), identity


# --- Bounded snapshots of live SQLite files --------------------------------------------------

def _read_file(source: tuple[int, str], uid: int, deadline: float) -> tuple[bytes, tuple] | None:
    """The bytes of one regular file owned by ``uid`` in a held directory; None when it does not exist."""
    dir_fd, path = source
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
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
        current = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise _Unknown("binding_changed") from None
    if (current.st_ino, current.st_size, current.st_mtime_ns) != key:
        raise _Unknown("binding_changed")
    return data, key


def _round(paths: dict[str, tuple[int, str]], uid: int, deadline: float) -> dict[str, tuple | None]:
    files = {}
    for name, path in paths.items():
        read = _read_file(path, uid, deadline)
        files[name] = None if read is None else (hashlib.sha256(read[0]).hexdigest(), read[1], read[0])
    return files


def _snapshot(paths: dict[str, tuple[int, str]], uid: int, deadline: float) -> dict[str, bytes | None]:
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
# A ``tool_calls`` value that is present but not a list of objects with a string name.
MALFORMED = "malformed"


def _tool_names(calls: object) -> list[str] | str:
    if isinstance(calls, list) and all(isinstance(c, dict) and isinstance(c.get("name"), str) for c in calls):
        return [c["name"] for c in calls]
    return MALFORMED


def _record(line: bytes) -> dict:
    try:
        value = json.loads(line)
    except ValueError:
        raise _Unknown("observation_invalid") from None
    if not isinstance(value, dict):
        raise _Unknown("observation_invalid")
    record = {key: value.get(key) for key in _KEEP}
    # Observed text replies have no ``tool_calls`` key; any present value must be well formed.
    if "tool_calls" in value:
        record["tools"] = _tool_names(value["tool_calls"])
    return record


def read_transcript_records(path: Path | str, uid: int, *, dir_fd: int | None = None,
                            ) -> tuple[list[dict] | None, str | None]:
    """The latest records, back to the latest USER_INPUT where the bounded scan reaches it."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
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


def _step_time(record: dict, earliest: datetime, latest: datetime) -> datetime | None:
    """The record's whole-second time when its step index is an integer and the time lies in range."""
    index = record.get("step_index")
    if not isinstance(index, int) or isinstance(index, bool):
        return None
    at = _second(record.get("created_at"))
    return at if at is not None and earliest <= at <= latest else None


def antigravity_state(summary: dict | None, step_count: int, steps: list[tuple[int, int, int]],
                      records: list[dict], started_at: datetime, now: datetime, *,
                      skip_permissions: bool = False):
    """(state, reason, observed_at) from one snapshot. Anything unproven is unknown.

    ``skip_permissions`` is True only when the bound process was launched with exactly
    ``--dangerously-skip-permissions``, the only context in which a running ``run_command`` was observed.
    """
    if not all(isinstance(v, int) and not isinstance(v, bool) for row in steps for v in row) or any(
            len(row) != 3 for row in steps):
        return "unknown", "observation_invalid", None
    positions = [i for i, r in enumerate(records)
                 if r.get("type") == "USER_INPUT" and r.get("source") == "USER_EXPLICIT"]
    if not positions:
        return "unknown", "no_native_event", None
    current = records[positions[-1]]
    start_second = started_at.replace(microsecond=0)
    latest = now + timedelta(seconds=1)
    created = _step_time(current, start_second, latest)
    # One-second resolution: the input must be from a later second than the process start.
    if created is None or created <= start_second:
        return "unknown", "no_native_event", None
    live = {idx: (step_type, status) for idx, step_type, status in steps}
    if live.get(current["step_index"]) != (_INPUT, _DONE):
        return "unknown", "completion_unconfirmed", None
    if summary is None:
        return "unknown", "session_mismatch", None
    # The current turn is every record after U in transcript order. Each must be well formed,
    # follow U and be from U's second up to now; nothing malformed is skipped.
    after = records[positions[-1] + 1:]
    if any(_step_time(r, created, latest) is None or r["step_index"] <= current["step_index"]
           or not isinstance(r.get("type"), str) or not isinstance(r.get("source"), str) for r in after):
        return "unknown", "observation_invalid", None
    planners = [r for r in after if r["type"] == "PLANNER_RESPONSE" and r["source"] == "MODEL"]
    if any(_tools(r) == MALFORMED for r in planners):
        return "unknown", "observation_invalid", None
    # A reply carries no tool call or exactly one; any other shape was not observed.
    if any(tools is not None and len(tools) != 1 for tools in (_tools(r) for r in planners)):
        return "unknown", "completion_unconfirmed", None
    last_idx = max(live)
    last = live[last_idx]
    status, not_fully_idle = summary.get("status"), summary.get("not_fully_idle")

    if status == _IDLE and not_fully_idle == 0:
        reply = [r for r in planners if r["step_index"] == last_idx]
        if (last == (_PLANNER, _DONE) and last_idx > current["step_index"] and summary.get("step_count") == step_count
                and len(reply) == 1 and _tools(reply[0]) is None):
            return "idle", "native_turn_completed", _step_time(reply[0], created, latest)
        return "unknown", "completion_unconfirmed", None
    if status != _RUNNING or not_fully_idle != 1:
        return "unknown", "completion_unconfirmed", None

    planner = planners[-1] if planners else None
    tools = _tools(planner) if planner else None
    single = tools[0] if tools else None
    follows = (planner is not None and last_idx == planner["step_index"] + 1
               and live.get(planner["step_index"]) == (_PLANNER, _DONE))
    planner_at = _step_time(planner, created, latest) if planner else None

    if last[0] == _TOOL and last[1] == _USER_WAIT:
        if follows and planner_at is not None and single == "ask_question":
            return "waiting_input", "native_question_open", planner_at
        return "unknown", "native_wait_unconfirmed", None
    if last[0] == _TOOL and last[1] == _TOOL_RUNNING:
        if follows and planner_at is not None and single == "run_command" and skip_permissions is True:
            return _working(planner_at, now)
        return "unknown", "completion_unconfirmed", None
    if last_idx == current["step_index"]:
        return _working(created, now)
    if last == (_SYSTEM, _DONE) and last_idx > current["step_index"]:
        return _working(max([created] + [_step_time(r, created, latest) for r in after]), now)
    if last == (_TOOL, _DONE) and follows and planner_at is not None and single is not None:
        return _working(planner_at, now)
    return "unknown", "completion_unconfirmed", None


def _tools(record: dict) -> list[str] | str | None:
    """Tool names of a PLANNER_RESPONSE from a parsed (``tools``) or raw (``tool_calls``) record:
    None when it has no tool calls, ``MALFORMED`` when the value is not a list of named calls."""
    if "tools" in record:
        return record["tools"] if isinstance(record["tools"], list) else MALFORMED
    if "tool_calls" in record:
        return _tool_names(record["tool_calls"])
    return None


def _working(at: datetime, now: datetime):
    if now - at > timedelta(seconds=_FRESH_SECONDS):
        return "unknown", "native_event_stale", None
    return "working", "native_progress", at


# --- Observation and discovery --------------------------------------------------------------

def _databases(tree: _Tree, conversation_id: str) -> dict[str, tuple[int, str]]:
    root, conversations = tree.data(), tree.data("conversations")
    return {"summaries": (root, "conversation_summaries.db"),
            "summaries_wal": (root, "conversation_summaries.db-wal"),
            "conversation": (conversations, f"{conversation_id}.db"),
            "conversation_wal": (conversations, f"{conversation_id}.db-wal")}


_SUMMARY_QUERY = ("select status, not_fully_idle, step_count, workspace_uris from conversation_summaries"
                  " where conversation_id=? limit 2")


def _final(pid: int, tree: _Tree, identity: tuple, ids: list[str], deadline: float) -> bool:
    """The binding still holds and the observation finished inside its one deadline."""
    if _identity(pid) != identity or _presence_ids(pid, tree) != ids or not tree.unchanged():
        raise _Unknown("binding_changed")
    return time.monotonic() <= deadline


def discover_antigravity_conversation(pid: int, cwd: str) -> str | None:
    """The conversation id of the one presence lock this process holds, bound to ``cwd``."""
    deadline = time.monotonic() + _DEADLINE_SECONDS
    tree = None
    try:
        uid, tree, identity = _bound_root(pid)
        ids = _presence_ids(pid, tree)
        if len(ids) != 1:
            return None
        root = tree.data()
        files = _snapshot({"summaries": (root, "conversation_summaries.db"),
                           "summaries_wal": (root, "conversation_summaries.db-wal")}, uid, deadline)
        if files["summaries"] is None:
            return None
        rows = _query(files["summaries"], files["summaries_wal"], deadline, [(_SUMMARY_QUERY, (ids[0],))])[0]
        if _summary(rows, cwd) is None or not _final(pid, tree, identity, ids, deadline):
            return None
        return ids[0]
    except (_Unknown, OSError, KeyError):
        return None
    finally:
        if tree is not None:
            tree.close()


def observe_antigravity(pid: int, conversation_id: str, cwd: str, now: datetime, started_at: datetime):
    deadline = time.monotonic() + _DEADLINE_SECONDS
    tree = None
    try:
        if not _UUID.fullmatch(conversation_id):
            return "unknown", "session_mismatch", None
        uid, tree, identity = _bound_root(pid)
        if _presence_ids(pid, tree) != [conversation_id]:
            return "unknown", "session_mismatch", None
        files = _snapshot(_databases(tree, conversation_id), uid, deadline)
        if files["summaries"] is None or files["conversation"] is None:
            return "unknown", "native_log_unavailable", None
        summary_rows = _query(files["summaries"], files["summaries_wal"], deadline,
                              [(_SUMMARY_QUERY, (conversation_id,))])[0]
        count_rows, step_rows = _query(files["conversation"], files["conversation_wal"], deadline, [
            ("select count(*) from steps", ()),
            ("select idx, step_type, status from steps order by idx desc limit ?", (_STEP_ROWS,))])
        summary = _summary(summary_rows, cwd)
        logs = tree.data(f"brain/{conversation_id}/.system_generated/logs")
        records, failure = read_transcript_records("transcript.jsonl", uid, dir_fd=logs)
        if failure:
            return "unknown", failure, None
        state = antigravity_state(summary, count_rows[0][0], step_rows, records, started_at, now,
                                  skip_permissions=identity[2] is True)
        # Refuse any result that arrives after the one deadline, after every final check.
        if not _final(pid, tree, identity, [conversation_id], deadline):
            return "unknown", "observation_incomplete", None
        return state
    except _Unknown as failure:
        return "unknown", failure.reason, None
    except PermissionError:
        # The controller cannot read this member's files: see the cross-user install notes.
        return "unknown", "native_log_unreadable", None
    except OSError:
        return "unknown", "observation_unavailable", None
    finally:
        if tree is not None:
            tree.close()
