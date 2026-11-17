"""Self-test for the session-end stray-child check."""
import os
import signal
import subprocess
import sys
import time

import pytest

from tests import stray_children

FAKE_TOKEN = "f" * 32

ORPHAN_LAUNCHER = """
import os, sys
if os.fork() == 0:
    os.setsid()
    grandchild = os.fork()
    if grandchild == 0:
        os.execv("/bin/sleep", ["sleep", "60"])
    with open(sys.argv[1], "w") as handle:
        handle.write(str(grandchild))
    os._exit(0)
"""


def write_proc(root, pid, *, ppid, start, token=None):
    entry = root / str(pid)
    entry.mkdir(parents=True)
    (entry / "stat").write_text(f"{pid} (proc) S {ppid} " + " ".join(["0"] * 17) + f" {start}\n")
    environ = b"PATH=/bin\0"
    if token is not None:
        environ += f"{stray_children.TOKEN_ENV}={token}".encode() + b"\0"
    (entry / "environ").write_bytes(environ)


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, pid, sig):
        self.calls.append((pid, sig))


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached")


def test_a_reparented_process_with_the_token_is_found(tmp_path):
    write_proc(tmp_path, 100, ppid=1, start=5000, token=FAKE_TOKEN)
    write_proc(tmp_path, 101, ppid=1, start=5001, token="0" * 32)
    assert stray_children.tokened(FAKE_TOKEN, tmp_path) == [100]


def test_descendants_without_the_token_are_listed_for_report_only(tmp_path):
    write_proc(tmp_path, 300, ppid=1, start=6000)
    write_proc(tmp_path, 301, ppid=300, start=6001)
    write_proc(tmp_path, 302, ppid=301, start=6002, token=FAKE_TOKEN)
    assert stray_children.descendants(300, tmp_path) == [301, 302]


def test_reap_signals_only_an_adopted_process_that_still_matches(tmp_path):
    write_proc(tmp_path, 400, ppid=1, start=7000, token=FAKE_TOKEN)
    write_proc(tmp_path, 401, ppid=1, start=7001, token=FAKE_TOKEN)
    kill = Recorder()
    owned = stray_children.Owned(FAKE_TOKEN, proc=tmp_path, kill=kill)
    owned.adopt(400)
    assert owned.reap(timeout=0.1) == [400]
    assert kill.calls[0] == (400, signal.SIGTERM)
    assert {pid for pid, _ in kill.calls} == {400}


def test_reap_refuses_a_reused_pid_with_a_new_start_identity(tmp_path):
    write_proc(tmp_path, 410, ppid=1, start=8000, token=FAKE_TOKEN)
    kill = Recorder()
    owned = stray_children.Owned(FAKE_TOKEN, proc=tmp_path, kill=kill)
    owned.adopt(410)
    (tmp_path / "410" / "stat").write_text("410 (proc) S 1 " + " ".join(["0"] * 17) + " 8001\n")
    assert owned.reap(timeout=0.1) == []
    assert kill.calls == []


def test_reap_refuses_a_process_whose_token_is_gone(tmp_path):
    write_proc(tmp_path, 420, ppid=1, start=9000, token=FAKE_TOKEN)
    kill = Recorder()
    owned = stray_children.Owned(FAKE_TOKEN, proc=tmp_path, kill=kill)
    owned.adopt(420)
    (tmp_path / "420" / "environ").write_bytes(b"PATH=/bin\0")
    assert owned.reap(timeout=0.1) == []
    assert kill.calls == []


def test_adopt_refuses_a_child_started_without_the_token(tmp_path):
    write_proc(tmp_path, 430, ppid=1, start=9500)
    owned = stray_children.Owned(FAKE_TOKEN, proc=tmp_path, kill=Recorder())
    with pytest.raises(RuntimeError):
        owned.adopt(430)


def test_close_run_reports_a_fake_leak_without_signalling_it(tmp_path):
    write_proc(tmp_path, 500, ppid=1, start=10000, token=stray_children.TOKEN)
    leaks = stray_children.close_run(owners=[], token=stray_children.TOKEN, proc=tmp_path)
    assert leaks == [("unregistered", 500, "?")]


def test_a_real_reparented_orphan_is_found_and_reaped(tmp_path):
    pid_file = tmp_path / "orphan.pid"
    launcher = subprocess.Popen([sys.executable, "-c", ORPHAN_LAUNCHER, str(pid_file)])
    launcher.wait(timeout=10)
    deadline = time.monotonic() + 5
    while not pid_file.read_text().strip() and time.monotonic() < deadline:
        time.sleep(0.02)
    orphan = int(pid_file.read_text())
    owned = stray_children.Owned()
    owned.adopt(orphan)
    wait_until(lambda: stray_children.parent_pid(orphan) != os.getpid())
    assert orphan in stray_children.tokened()
    assert orphan in owned.reap()
    assert stray_children.start_identity(orphan) is None


def test_a_deliberate_leak_is_reported_and_left_for_its_owner():
    leaked = subprocess.Popen(["sleep", "60"])
    try:
        leaks = stray_children.close_run(owners=[])
        assert ("unregistered", leaked.pid, "sleep") in leaks
        assert stray_children.start_identity(leaked.pid) is not None
    finally:
        leaked.kill()
        leaked.wait(timeout=5)


def test_reap_leaves_unrelated_and_other_run_processes_alone():
    env_without_token = {k: v for k, v in os.environ.items() if k != stray_children.TOKEN_ENV}
    unrelated = subprocess.Popen(["sleep", "60"], env=env_without_token)
    other_run = subprocess.Popen(
        ["sleep", "60"], env=dict(env_without_token, **{stray_children.TOKEN_ENV: "1" * 32})
    )
    owned = stray_children.Owned()
    try:
        with pytest.raises(RuntimeError):
            owned.adopt(unrelated)
        with pytest.raises(RuntimeError):
            owned.adopt(other_run)
        owned.reap(timeout=0.1)
        assert stray_children.start_identity(unrelated.pid) is not None
        assert stray_children.start_identity(other_run.pid) is not None
    finally:
        for process in (unrelated, other_run):
            process.kill()
            process.wait(timeout=5)
