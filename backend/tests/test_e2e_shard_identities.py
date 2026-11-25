"""Exercise the journey manifest through its CLI and the Playwright reporter boundary."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "e2e_shard_identities.py"
COMMAND = ["--no-install", "playwright", "test", "--list", "--grep", "@active", "--reporter=json"]


def report(*entries: tuple[str, tuple[str, ...], str, tuple[str, ...]]) -> dict:
    suites = []
    for file, groups, title, projects in entries:
        spec = {"file": file, "title": title, "tests": [{"projectId": project} for project in projects]}
        suite = {"specs": [spec]}
        for group in reversed(groups):
            suite = {"specs": [], "suites": [{"title": group, **suite}]}
        suites.append({"title": file, **suite})
    return {"suites": suites, "errors": []}


A = ("e2e/a.spec.ts", ("Outer", "Inner"), "opens @active", ("browser-a",))
B = ("e2e/b.spec.ts", ("Outer", "Inner"), "opens @active", ("browser-a",))
C = ("e2e/a.spec.ts", ("Outer", "Inner"), "opens @active", ("browser-b",))
D = ("e2e/a.spec.ts", ("Outer › Inner",), "opens @active", ("browser-a",))


@pytest.fixture
def collect(tmp_path, monkeypatch):
    fake = tmp_path / "npx"
    fake.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

fixtures = Path(os.environ["JOURNEY_TEST_FIXTURES"])
with (fixtures / "calls").open("a") as calls:
    calls.write(json.dumps([str(Path.cwd()), *sys.argv[1:]]) + "\\n")
key = sys.argv[-1].removeprefix("--shard=") if sys.argv[-1].startswith("--shard=") else "full"
response = json.loads((fixtures / "reports.json").read_text())[key]
if response is False:
    sys.exit(2)
if response is not None:
    output = response if isinstance(response, str) else json.dumps(response)
    Path(os.environ["PLAYWRIGHT_JSON_OUTPUT_FILE"]).write_text(output)
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("JOURNEY_TEST_FIXTURES", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    output = tmp_path / "manifest.json"

    def run(full, first, second):
        (tmp_path / "reports.json").write_text(
            json.dumps({"full": full, "1/2": first, "2/2": second}), encoding="utf-8",
        )
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--shards", "2", "--output", str(output)],
            cwd=tmp_path, capture_output=True, text=True, check=False,
        )

    return run, output, tmp_path / "calls"


def test_cli_proves_and_writes_a_deterministic_partition(collect):
    run, output, calls = collect
    both_projects = (*A[:3], (C[3][0], A[3][0]))
    result = run(report(D, B, both_projects), report(C, B), report(D, A))
    assert result.returncode == 0, result.stderr
    first_output = output.read_text(encoding="utf-8")
    assert json.loads(first_output) == {
        "version": 1,
        "journeys": [
            ["browser-a", A[0], "Outer", "Inner", A[2]],
            ["browser-a", A[0], "Outer › Inner", A[2]],
            ["browser-a", B[0], "Outer", "Inner", B[2]],
            ["browser-b", C[0], "Outer", "Inner", C[2]],
        ],
        "shards": {
            "1/2": [["browser-a", B[0], "Outer", "Inner", B[2]], ["browser-b", C[0], "Outer", "Inner", C[2]]],
            "2/2": [["browser-a", A[0], "Outer", "Inner", A[2]], ["browser-a", A[0], "Outer › Inner", A[2]]],
        },
    }
    frontend = str(ROOT / "frontend")
    assert [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()] == [
        [frontend, *COMMAND],
        [frontend, *COMMAND, "--shard=1/2"],
        [frontend, *COMMAND, "--shard=2/2"],
    ]
    assert run(report(both_projects, B, D), report(B, C), report(A, D)).returncode == 0
    assert output.read_text(encoding="utf-8") == first_output


@pytest.mark.parametrize(
    ("full", "first", "second", "messages"),
    [
        (report(A, B), report(A), report(A), ("missing", "duplicate")),
        (report(A, B), report(A), report(C), ("missing", "unexpected")),
        (report(A, A), report(A), report(A), ("collision",)),
        (report(), report(A), report(B), ("full collection is empty",)),
        (report(A, B), report(A), report(), ("shard 2/2 collection is empty",)),
        (report(A, B), report(A), {"suites": [], "errors": [{}]}, ("reported collection errors",)),
        (report(A, B), report(A), {"suites": [{"specs": [None]}], "errors": []}, ("invalid Playwright report",)),
        (report(A, B), report(A), "{broken JSON", ("invalid Playwright report",)),
        (report(A, B), report(A), None, ("could not read the Playwright report",)),
        (report(A, B), report(A), False, ("exited with status 2",)),
    ],
)
def test_cli_rejects_unproved_partitions(collect, full, first, second, messages):
    run, output, _ = collect
    output.write_text("previous manifest\n", encoding="utf-8")
    result = run(full, first, second)
    assert result.returncode != 0
    assert all(message in result.stderr for message in messages), result.stderr
    assert output.read_text(encoding="utf-8") == "previous manifest\n"
