"""Exercise the journey manifest through its CLI and the Playwright reporter boundary."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "e2e_shard_identities.py"
COMMAND = ["--no-install", "playwright", "test", "--list", "--grep", "@active", "--reporter=json"]


def report(
    *entries: tuple[str, tuple[str, ...], str, tuple[str, ...]], executions: tuple[str, ...] | None = None,
) -> dict:
    suites = []
    for file, groups, title, projects in entries:
        spec = {"file": file, "title": title, "tests": [{"projectId": project} for project in projects]}
        if executions is not None:
            for project in spec["tests"]:
                project.update({
                    "expectedStatus": "passed",
                    "status": "expected",
                    "results": [{"status": outcome, "retry": retry} for retry, outcome in enumerate(executions)],
                })
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

    def run(full, first, second, *, artifacts=None):
        (tmp_path / "reports.json").write_text(
            json.dumps({"full": full, "1/2": first, "2/2": second}), encoding="utf-8",
        )
        command = [sys.executable, str(SCRIPT), "--shards", "2", "--output", str(output)]
        if artifacts is not None:
            command += ["--artifacts", str(artifacts)]
        return subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=False)

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


def artifact(root: Path, shard: int, attempt: int, result) -> None:
    folder = root / f"e2e-journeys-{shard}-attempt-{attempt}"
    folder.mkdir(parents=True)
    if result is not None:
        payload = result if isinstance(result, str) else json.dumps(result)
        (folder / "results.json").write_text(payload, encoding="utf-8")


def test_runtime_proof_selects_latest_attempt_per_shard(collect, tmp_path):
    run, output, _ = collect
    artifacts = tmp_path / "artifacts"
    artifact(artifacts, 1, 9, report(A, executions=("failed",)))
    artifact(artifacts, 1, 10, report(A, executions=("passed",)))
    artifact(artifacts, 2, 1, report(B, executions=("passed",)))
    result = run(report(A, B), report(A), report(B), artifacts=artifacts)
    assert result.returncode == 0, result.stderr
    assert "2 active journeys" in result.stdout
    assert output.is_file()


@pytest.mark.parametrize(
    ("last_report", "message"),
    [
        (report(), "missing"),
        (report(A, B, executions=("passed",)), "duplicate"),
        (report(C, executions=("passed",)), "unexpected"),
        (report(B, executions=("skipped",)), "one passing execution"),
        (report(B, executions=("failed", "passed")), "one passing execution"),
        (report(B, executions=()), "one passing execution"),
        (report(B, executions=("failed",)), "one passing execution"),
        ({"suites": [], "errors": [{}]}, "reported errors"),
        ("{broken JSON", "invalid Playwright report"),
        (None, "could not read the Playwright report"),
    ],
)
def test_runtime_proof_rejects_latest_invalid_shard(collect, tmp_path, last_report, message):
    run, output, _ = collect
    artifacts = tmp_path / "artifacts"
    artifact(artifacts, 1, 1, report(A, executions=("passed",)))
    artifact(artifacts, 2, 1, report(B, executions=("passed",)))
    artifact(artifacts, 2, 2, last_report)
    output.write_text("previous manifest\n", encoding="utf-8")
    result = run(report(A, B), report(A), report(B), artifacts=artifacts)
    assert result.returncode != 0
    assert message in result.stderr, result.stderr
    assert output.read_text(encoding="utf-8") == "previous manifest\n"


def test_runtime_proof_rejects_incomplete_artifacts_and_misplaced_journeys(collect, tmp_path):
    run, _, _ = collect
    artifacts = tmp_path / "artifacts"
    artifact(artifacts, 1, 1, report(B, executions=("passed",)))
    missing = run(report(A, B), report(A), report(B), artifacts=artifacts)
    assert missing.returncode != 0
    assert "missing artifact for shard 2" in missing.stderr
    artifact(artifacts, 2, 1, report(A, executions=("passed",)))
    misplaced = run(report(A, B), report(A), report(B), artifacts=artifacts)
    assert misplaced.returncode != 0
    assert "does not match the planned shard" in misplaced.stderr


def test_workflow_uses_the_same_shard_count_and_attempt_artifacts():
    jobs = yaml.safe_load((ROOT / ".github/workflows/e2e.yml").read_text(encoding="utf-8"))["jobs"]
    shards = jobs["e2e-shards"]
    gate = jobs["e2e"]
    assert shards["strategy"] == {"fail-fast": False, "matrix": {"shard": [1, 2, 3, 4]}}
    assert shards["if"] == "needs.e2e-scope.outputs.run_suites == 'true'"
    run = next(step["run"] for step in shards["steps"] if step.get("name") == "Run the active journeys for this shard")
    assert "--shard=${{ matrix.shard }}/${{ strategy.job-total }}" in run
    upload = next(step for step in shards["steps"] if step.get("name") == "Upload the journey report")
    assert upload["with"]["name"] == "e2e-journeys-${{ matrix.shard }}-attempt-${{ github.run_attempt }}"
    assert upload["with"]["if-no-files-found"] == "error"
    download = next(step for step in gate["steps"] if step.get("uses", "").startswith("actions/download-artifact@"))
    assert download["with"]["pattern"] == "e2e-journeys-*-attempt-*"
    assert download["with"]["merge-multiple"] is False
    proof = gate["steps"][-1]
    for step in gate["steps"][1:]:
        assert step["if"] == "needs.e2e-scope.outputs.run_suites == 'true'"
    assert "--shards 4" in proof["run"] and '--artifacts "$RUNNER_TEMP/e2e-journey-artifacts"' in proof["run"]


@pytest.mark.parametrize(("scope_result", "run_suites", "shard_result", "success"), [
    ("success", "true", "success", True),
    ("success", "true", "failure", False),
    ("success", "true", "cancelled", False),
    ("success", "true", "skipped", False),
    ("success", "false", "skipped", True),
    ("success", "false", "failure", False),
    ("failure", "false", "skipped", False),
    ("success", "", "skipped", False),
])
def test_required_workflow_gate_rejects_failed_shards(scope_result, run_suites, shard_result, success):
    workflow = yaml.safe_load((ROOT / ".github/workflows/e2e.yml").read_text(encoding="utf-8"))
    gate = workflow["jobs"]["e2e"]
    assert gate["name"] == "e2e"
    assert gate["needs"] == ["e2e-scope", "e2e-shards"]
    assert gate["if"] == "always()"
    step = gate["steps"][0]
    assert step["env"] == {
        "SCOPE_RESULT": "${{ needs.e2e-scope.result }}",
        "RUN_SUITES": "${{ needs.e2e-scope.outputs.run_suites }}",
        "SHARDS_RESULT": "${{ needs.e2e-shards.result }}",
    }
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env={**os.environ, "SCOPE_RESULT": scope_result, "RUN_SUITES": run_suites, "SHARDS_RESULT": shard_result},
        capture_output=True, text=True, check=False,
    )
    assert (result.returncode == 0) is success, result.stderr


def test_workflow_gate_uses_its_checker_on_an_older_candidate_branch(collect, tmp_path, monkeypatch):
    _, _, calls = collect
    fixture = tmp_path / "fixture-repo"
    fixture.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(fixture), *args], text=True, capture_output=True, check=True,
        ).stdout

    git("init", "-b", "workflow")
    git("config", "user.name", "Workflow fixture")
    git("config", "user.email", "fixture@example.invalid")
    checker = fixture / "scripts/ci/e2e_shard_identities.py"
    checker.parent.mkdir(parents=True)
    checker.write_text(
        "import argparse\np=argparse.ArgumentParser()\np.add_argument('--shards')\n"
        "p.add_argument('--output')\np.parse_args()\n",
        encoding="utf-8",
    )
    (fixture / "frontend").mkdir()
    (fixture / "frontend/journeys.txt").write_text("before branch\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "Legacy checker")
    git("switch", "-c", "candidate")
    (fixture / "frontend/journeys.txt").write_text("candidate journeys\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "Older candidate journeys")
    git("switch", "workflow")
    checker.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    (fixture / "frontend/workflow-only.txt").write_text("workflow-only journey\n", encoding="utf-8")
    yml = fixture / ".github/workflows/e2e.yml"
    yml.parent.mkdir(parents=True)
    yml.write_text((ROOT / ".github/workflows/e2e.yml").read_text(encoding="utf-8"), encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "Workflow and checker update")
    git("merge", "--no-ff", "--no-edit", "candidate")

    jobs = yaml.safe_load(git("show", "workflow:.github/workflows/e2e.yml"))["jobs"]
    gate = jobs["e2e"]
    checkouts = [step["with"] for step in gate["steps"] if step.get("uses", "").startswith("actions/checkout@")]
    assert [x["ref"] for x in checkouts] == [
        "${{ github.event.pull_request.head.sha || github.sha }}", "${{ github.workflow_sha }}",
    ]
    candidate, tooling = (tmp_path / x["path"] for x in checkouts)
    git("clone", "--branch", "candidate", str(fixture), str(candidate))
    git("clone", "--branch", "workflow", "--no-checkout", str(fixture), str(tooling))
    git("-C", str(tooling), "sparse-checkout", "set", *checkouts[1]["sparse-checkout"].splitlines())
    git("-C", str(tooling), "checkout", "workflow")
    assert (candidate / "frontend/journeys.txt").read_text(encoding="utf-8") == "candidate journeys\n"
    assert not (candidate / "frontend/workflow-only.txt").exists()
    assert git("show", "workflow:frontend/workflow-only.txt") == "workflow-only journey\n"
    assert not (tooling / "frontend").exists()
    old = subprocess.run(
        [sys.executable, str(candidate / checker.relative_to(fixture)), "--artifacts", "reports"],
        text=True, capture_output=True, check=False,
    )
    assert old.returncode != 0 and "unrecognized arguments: --artifacts" in old.stderr

    expected = {"full": report(A, B, C, D)}
    runner_temp = tmp_path / "runner-temp"
    for shard, entry in enumerate((A, B, C, D), start=1):
        expected[f"{shard}/4"] = report(entry)
        artifact(runner_temp / "e2e-journey-artifacts", shard, 1, report(entry, executions=("passed",)))
    (tmp_path / "reports.json").write_text(json.dumps(expected), encoding="utf-8")
    monkeypatch.setenv("RUNNER_TEMP", str(runner_temp))
    result = subprocess.run(
        ["bash", "-eu", "-c", gate["steps"][-1]["run"]], cwd=tmp_path,
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Proved the execution of 4 active journeys" in result.stdout
    assert (runner_temp / "e2e-manifest.json").is_file()
    assert {json.loads(line)[0] for line in calls.read_text(encoding="utf-8").splitlines()} == {
        str(candidate / "frontend"),
    }


def test_required_gates_keep_their_names_and_source_contract():
    def job(workflow, key):
        return yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text(encoding="utf-8"))["jobs"][key]

    def refs(gate):
        return [s["with"]["ref"] for s in gate["steps"] if s.get("uses", "").startswith("actions/checkout@")]

    workflow_sha = "${{ github.workflow_sha }}"
    backend = job("backend-tests.yml", "backend-gate")
    assert backend["name"] == "Full backend regression tests"
    assert refs(backend) == ["${{ github.event.pull_request.head.sha || github.sha }}", workflow_sha]
    backend_commands = {s.get("name"): s for s in backend["steps"]}
    assert backend_commands["Detect a docs-only change"]["run"] == "python3 .workflow-ci/scripts/ci/docs_only.py"
    assert backend_commands["Collect the full backend suite"]["working-directory"] == "candidate/backend"
    assert ".workflow-ci/scripts/ci/backend_shard_identities.py" in backend_commands[
        "Prove that the shards ran every test exactly once"
    ]["run"]

    python = job("factory-checks.yml", "python")
    assert python["name"] == "Changed Python undefined names"
    assert refs(python) == ["${{ github.event.pull_request.head.sha }}", workflow_sha]
    assert python["steps"][-1]["working-directory"] == "candidate"
    assert job("factory-checks.yml", "old_names")["name"] == "Rename old-name guard"

    product = job("product-ci.yml", "gate")
    assert product["name"] == "Product CI gate"
    assert refs(product) == []
    assert refs(job("e2e.yml", "e2e-scope")) == [workflow_sha]
