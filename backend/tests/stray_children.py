"""Reap the child processes this test run starts, and signal only the ones it owns.

A child is signalled only when, at kill time, this run's token is in its environment, a fixture
adopted it while it was alive, and its start identity still matches what was recorded. Processes
found by walking parent PIDs, and processes without the token, are reported and never signalled.
"""
import os
import secrets
import signal
import subprocess
import time
from pathlib import Path

TOKEN_ENV = "SYNTH_TEST_STRAY_TOKEN"
TOKEN = secrets.token_hex(16)
os.environ[TOKEN_ENV] = TOKEN
PROC = Path("/proc")
_owners = []


def _stat_fields(pid, proc):
    try:
        text = (proc / str(pid) / "stat").read_text()
    except OSError:
        return None
    return text.rsplit(")", 1)[1].split()


def start_identity(pid, proc=PROC):
    fields = _stat_fields(pid, proc)
    if fields is None or fields[0] == "Z":
        return None
    return int(fields[19])


def parent_pid(pid, proc=PROC):
    fields = _stat_fields(pid, proc)
    return None if fields is None else int(fields[1])


def has_token(pid, token, proc=PROC):
    try:
        environ = (proc / str(pid) / "environ").read_bytes()
    except OSError:
        return False
    return f"{TOKEN_ENV}={token}".encode() in environ.split(b"\0")


def _pids(proc):
    for entry in proc.iterdir():
        if entry.name.isdigit():
            yield int(entry.name)


def command_name(pid, proc=PROC):
    try:
        argv0 = (proc / str(pid) / "cmdline").read_bytes().split(b"\0", 1)[0]
    except OSError:
        return "?"
    return argv0.decode(errors="replace").rsplit("/", 1)[-1] or "?"


def tokened(token=TOKEN, proc=PROC):
    return [pid for pid in _pids(proc) if has_token(pid, token, proc)]


def descendants(root, proc=PROC):
    children = {}
    for pid in _pids(proc):
        parent = parent_pid(pid, proc)
        if parent is not None:
            children.setdefault(parent, []).append(pid)
    found, pending = [], [root]
    while pending:
        for child in children.get(pending.pop(), ()):
            found.append(child)
            pending.append(child)
    return found


class Owned:
    def __init__(self, token=TOKEN, proc=PROC, kill=os.kill):
        self.token = token
        self.proc = proc
        self._kill = kill
        self._entries = []
        if proc is PROC:
            _owners.append(self)

    def adopt(self, child):
        popen = child if isinstance(child, subprocess.Popen) else None
        pid = child.pid if popen else child
        identity = start_identity(pid, self.proc)
        if identity is None or not has_token(pid, self.token, self.proc):
            raise RuntimeError(f"pid {pid} is not a live child started with this run's token")
        self._entries.append((pid, identity, popen))
        return pid

    def _is_ours(self, pid, identity):
        return start_identity(pid, self.proc) == identity and has_token(pid, self.token, self.proc)

    def _wait_gone(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(start_identity(pid, self.proc) != identity for pid, identity, _ in self._entries):
                return
            time.sleep(0.05)

    def reap(self, timeout=5.0):
        killed = []
        for pid, identity, _ in self._entries:
            if self._is_ours(pid, identity):
                self._kill(pid, signal.SIGTERM)
                killed.append(pid)
        self._wait_gone(timeout)
        for pid, identity, _ in self._entries:
            if self._is_ours(pid, identity):
                self._kill(pid, signal.SIGKILL)
        self._wait_gone(timeout)
        for _, _, popen in self._entries:
            if popen is not None:
                popen.wait(timeout=timeout)
                if popen.stderr:
                    popen.stderr.close()
        return killed


def close_run(owners=None, token=TOKEN, proc=PROC):
    """Return (kind, pid) for each leak. Registered survivors are reaped; the rest are report-only."""
    own_pid = os.getpid()
    leaks = []
    registered = set()
    for owner in (_owners if owners is None else owners):
        for pid, identity, _ in owner._entries:
            registered.add(pid)
            if owner._is_ours(pid, identity):
                leaks.append(("registered", pid, command_name(pid, proc)))
        owner.reap()
    token_pids = set(tokened(token, proc))
    for pid in sorted(token_pids - registered - {own_pid}):
        if start_identity(pid, proc) is not None:
            leaks.append(("unregistered", pid, command_name(pid, proc)))
    for pid in descendants(own_pid, proc):
        if pid not in token_pids | registered and start_identity(pid, proc) is not None:
            leaks.append(("descendant", pid, command_name(pid, proc)))
    return leaks
