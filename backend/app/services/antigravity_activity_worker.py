"""Run Antigravity evidence reads in a child that can be stopped at the deadline."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time


_WORKER_COMMAND = [sys.executable, "-I", str(Path(__file__).resolve())]
_DEADLINE_SECONDS = 1.0
_REAP_SECONDS = 0.05


class _WorkerFailure(Exception):
    pass


def _run(operation: str, *arguments: object) -> object:
    deadline = time.monotonic() + _DEADLINE_SECONDS
    try:
        # The parent owns the copy directory so a killed child cannot leave database copies behind.
        with TemporaryDirectory(prefix="agy-observer-") as private:
            request = json.dumps([operation, deadline, *arguments])
            process = subprocess.Popen(
                _WORKER_COMMAND, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding="utf-8",
                env={"TMPDIR": private},
            )
            try:
                output, _ = process.communicate(
                    request, timeout=max(0.0, deadline - time.monotonic() - _REAP_SECONDS),
                )
                if process.returncode != 0:
                    raise _WorkerFailure("observation_unavailable")
                value = json.loads(output)
            finally:
                if process.poll() is None:
                    process.kill()
                    # Reap within the reserved window, even if the kernel has delayed the exit.
                    try:
                        process.wait(timeout=max(0.0, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        pass
                for pipe in (process.stdin, process.stdout):
                    if pipe is not None:
                        pipe.close()
    except subprocess.TimeoutExpired:
        raise _WorkerFailure("observation_incomplete") from None
    except (OSError, ValueError):
        raise _WorkerFailure("observation_unavailable") from None
    if time.monotonic() > deadline:
        raise _WorkerFailure("observation_incomplete")
    return value


def discover_antigravity_conversation(pid: int, cwd: str) -> str | None:
    try:
        value = _run("discover", pid, cwd)
    except _WorkerFailure:
        return None
    return value if isinstance(value, str) else None


def observe_antigravity(
    pid: int, conversation_id: str, cwd: str, now: datetime, started_at: datetime,
) -> tuple[str, str, datetime | None]:
    try:
        value = _run("observe", pid, conversation_id, cwd, now.isoformat(), started_at.isoformat())
        if not isinstance(value, list) or len(value) != 3:
            raise _WorkerFailure("observation_unavailable")
        state, reason, observed = value
        if not isinstance(state, str) or not isinstance(reason, str):
            raise _WorkerFailure("observation_unavailable")
        return state, reason, datetime.fromisoformat(observed) if observed is not None else None
    except _WorkerFailure as failure:
        return "unknown", str(failure), None
    except (TypeError, ValueError):
        return "unknown", "observation_unavailable", None


def _main() -> int:
    # Isolated Python ignores ambient import paths; use this checkout's own backend package.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from app.services import antigravity_activity_service as activity

    operation, deadline, *arguments = json.load(sys.stdin)
    if operation == "discover":
        result = activity.discover_antigravity_conversation(*arguments, deadline=deadline)
    elif operation == "observe":
        pid, conversation_id, cwd, now, started_at = arguments
        state, reason, observed_at = activity.observe_antigravity(
            pid, conversation_id, cwd, datetime.fromisoformat(now), datetime.fromisoformat(started_at),
            deadline=deadline,
        )
        result = [state, reason, observed_at.isoformat() if observed_at is not None else None]
    else:
        return 2
    json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
