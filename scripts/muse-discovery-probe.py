#!/usr/bin/env python3
"""Read-only probe of Muse first-session discovery (#965).

Run it with the backend virtualenv's Python, as the same user and with the same capabilities as the controller
service, against one process. It calls the production ``discover_muse_session`` and prints one JSON object of
facts only: capability names, whether the target belongs to another user, whether its descriptor directory can be
listed, how many ``.session.lock`` descriptors it holds (counts), whether discovery returned an id, and, when an
expected id is given, whether it matched. It never prints a path, an id, an environment value, a log line or a
command line. It opens nothing for writing, sends no signal and makes no network call.

    setpriv ... python scripts/muse-discovery-probe.py --pid 12345 --cwd /path/to/the/workspace

Exit status 0 for every observation; a usage error exits nonzero.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import app.services.muse_activity_service as muse  # noqa: E402
from app.services.muse_activity_service import discover_muse_session  # noqa: E402,F401  (patched by tests)

_CAPABILITIES = {2: "cap_dac_read_search", 19: "cap_sys_ptrace"}


def _scandir(path):
    return os.scandir(path)


def capabilities(status_text: str) -> dict:
    """The names of the two capabilities this read needs, from a /proc status text (effective and ambient)."""
    found = {"effective": [], "ambient": []}
    for line in status_text.splitlines():
        name, _sep, value = line.partition(":")
        key = {"CapEff": "effective", "CapAmb": "ambient"}.get(name)
        if key is None:
            continue
        try:
            mask = int(value.strip(), 16)
        except ValueError:
            continue
        found[key] = sorted(label for bit, label in _CAPABILITIES.items() if mask >> bit & 1)
    return found


def _own_capabilities() -> dict:
    try:
        return capabilities(Path("/proc/self/status").read_text())
    except OSError:
        return {"effective": [], "ambient": []}


def _count_locks(entries, root) -> tuple[int, int]:
    locks = under_root = 0
    for index, entry in enumerate(entries):
        if index >= muse._MAX_DESCRIPTORS:
            break
        try:
            target = Path(os.readlink(entry.path))
        except OSError:
            continue
        if target.name != ".session.lock" or not target.is_absolute():
            continue
        locks += 1
        folder = target.parent
        if root is not None and muse._UUID.fullmatch(folder.name) and folder.resolve().is_relative_to(root):
            under_root += 1
    return locks, under_root


def observe(pid: int, cwd: str, expect_session_id: str | None = None) -> dict:
    facts = {
        "version": 1, "target_present": True, "capabilities": _own_capabilities(), "target_uid_differs": None,
        "fd_listable": False, "fd_failure": None, "lock_descriptors": 0, "locks_under_sessions_root": 0,
        "discovered": False, "matches_expected": None,
    }
    try:
        facts["target_uid_differs"] = muse._process_uid(pid) != os.geteuid()
    except FileNotFoundError:
        facts["target_present"] = False
    except OSError:
        pass
    try:
        root = muse._sessions_root(pid)
    except Exception:  # an unreadable environment: the root is unknown, not an error of the probe
        root = None
    try:
        with _scandir(muse._process_fd_dir(pid)) as entries:
            facts["fd_listable"] = True
            try:
                facts["lock_descriptors"], facts["locks_under_sessions_root"] = _count_locks(entries, root)
            except Exception as error:  # a symlink loop, say: report the kind only, with conservative counts
                facts["lock_count_error"] = type(error).__name__
    except PermissionError:
        facts["fd_failure"] = "permission_denied"
    except FileNotFoundError:
        facts["fd_failure"], facts["target_present"] = "not_found", False
    except OSError:
        facts["fd_failure"] = "unreadable"
    found = None
    try:
        found = discover_muse_session(pid, cwd)
    except Exception as error:  # only the kind of the failure is reported, never its message
        facts["discovery_error"] = type(error).__name__
    facts["discovered"] = found is not None
    if expect_session_id is not None:
        facts["matches_expected"] = found is not None and found == expect_session_id
    return facts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only probe of Muse first-session discovery.")
    parser.add_argument("--pid", type=int, required=True, help="the Muse pane process to observe")
    parser.add_argument("--cwd", required=True, help="the workspace directory of that pane")
    parser.add_argument("--expect-session-id", default=None, help="report only whether the discovered id matches it")
    args = parser.parse_args(argv)
    try:
        facts = observe(args.pid, args.cwd, args.expect_session_id)
    except Exception as error:  # nothing but a fixed-shape object may leave: a traceback would carry paths and names
        facts = {"version": 1, "probe_error": type(error).__name__}
    print(json.dumps(facts, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
