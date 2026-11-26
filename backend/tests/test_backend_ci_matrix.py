"""The backend workflow runs shards behind a gate that keeps the required check name (#1013)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "backend-tests.yml"
REQUIRED = "Full backend regression tests"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _jobs() -> dict:
    return _workflow()["jobs"]


def _gate() -> dict:
    gates = [job for job in _jobs().values() if job.get("name") == REQUIRED]
    assert len(gates) == 1, "exactly one job carries the required check name"
    return gates[0]


def _steps_text(job: dict) -> str:
    return "\n".join(str(step.get("run", "")) + str(step.get("with", "")) for step in job["steps"])


def test_the_shard_matrix_lists_every_shard_and_matches_the_count_the_commands_use():
    workflow = _workflow()
    count = int(workflow["env"]["BACKEND_SHARDS"])
    shard_job = _jobs()["backend"]
    assert shard_job["strategy"]["matrix"]["shard"] == list(range(1, count + 1))
    assert shard_job["strategy"]["fail-fast"] is False
    assert shard_job["name"] == f"Backend shard ${{{{ matrix.shard }}}} of {count}"
    shard_text = _steps_text(shard_job)
    assert "--of ${{ env.BACKEND_SHARDS }}" in shard_text and "--shard ${{ matrix.shard }}" in shard_text
    assert "--shards ${{ env.BACKEND_SHARDS }}" in _steps_text(_gate())


def test_only_the_gate_carries_the_required_name_and_the_shards_are_not_required():
    jobs = _jobs()
    names = [job.get("name") for job in jobs.values()]
    assert names.count(REQUIRED) == 1
    assert jobs["backend"]["name"] != REQUIRED
    assert _gate() == jobs["backend-gate"]


def test_the_gate_depends_on_the_shards_runs_always_and_fails_first_on_a_bad_shard_result():
    gate = _gate()
    assert gate["needs"] in ("backend", ["backend"])
    assert "always()" in gate["if"]
    first = gate["steps"][0]
    assert "needs.backend.result" in str(first["env"]) and "exit 1" in first["run"]


def _first_step_exit_code(result: str) -> int:
    run = _gate()["steps"][0]["run"]
    return subprocess.run(["bash", "-e", "-c", run], env={**os.environ, "SHARDS_RESULT": result}).returncode


def test_the_gate_cannot_pass_on_a_failed_cancelled_or_skipped_shard():
    assert _first_step_exit_code("success") == 0
    for result in ("failure", "cancelled", "skipped", ""):
        assert _first_step_exit_code(result) != 0, result


def test_the_gate_proves_the_partition_and_keeps_the_fixtures_check():
    text = _steps_text(_gate())
    assert "scripts/ci/backend_shard_identities.py" in text
    assert "--registry .workflow-ci/.github/known-flakes.json" in text
    assert "--collect-only" in text and "--ignore=tests/test_cc_bridge_real_sudo.py" in text
    downloads = [step for step in _gate()["steps"] if step.get("uses", "").startswith("actions/download-artifact@")]
    assert len(downloads) == 1 and downloads[0]["with"] == {
        "pattern": "backend-test-timings-*-attempt-*",
        "path": "shard-artifacts",
        "merge-multiple": False,
    }
    assert "scripts/factory-fixtures.py --check" in text
    names = [step.get("name", "") for step in _gate()["steps"]]
    assert names.index("Prove that the shards ran every test exactly once") < names.index("Check the active factory fixtures")


def test_each_shard_collects_then_runs_with_the_identity_plugin_and_keeps_its_artifact():
    shard = _jobs()["backend"]
    text = _steps_text(shard)
    assert text.index("--collect-only") < text.index("-p tests.ci_shard_identity")
    assert "--identity-out=backend-shard-${{ matrix.shard }}-executed.jsonl" in text
    assert "--registry ../.workflow-ci/.github/known-flakes.json" in text
    tooling = next(step for step in shard["steps"] if step.get("name") == "Check out the workflow's gate tooling")
    assert tooling["with"]["ref"] == "${{ github.workflow_sha }}"
    assert tooling["with"]["persist-credentials"] is False
    uploads = [step for step in shard["steps"] if str(step.get("uses", "")).startswith("actions/upload-artifact@")]
    assert len(uploads) == 1
    assert uploads[0]["with"]["name"] == "backend-test-timings-${{ matrix.shard }}-attempt-${{ github.run_attempt }}"
    assert uploads[0]["with"]["if-no-files-found"] == "error"
    for kind in ("collected.txt", "executed.jsonl", "pytest-exit.txt"):
        assert f"backend-shard-${{{{ matrix.shard }}}}-{kind}" in uploads[0]["with"]["path"]
    assert (ROOT / "backend" / "tests" / "ci_shard_identity.py").is_file()


def _shard_run():
    return next(step for step in _jobs()["backend"]["steps"] if step.get("name") == "Run this shard of the backend suite")


def test_shard_records_the_real_exit_and_stops_on_pytest_harness_failure(tmp_path):
    step = _shard_run()
    assert not step.get("continue-on-error", False)
    run = step["run"].replace("${{ matrix.shard }}", "1")
    (tmp_path / "backend-shard-files.txt").write_text("tests/test_example.py\n", encoding="utf-8")
    for code in (0, 1, 2, 3, 4, 5, 6, 139):
        stub = f'python() {{ if [ "$1" = "-m" ]; then return {code}; fi; return 0; }}\n'
        result = subprocess.run(["bash", "-e", "-c", stub + run], cwd=tmp_path)
        assert result.returncode == (0 if code <= 1 else code)
        assert (tmp_path / "backend-shard-1-pytest-exit.txt").read_text(encoding="utf-8") == f"{code}\n"


@pytest.fixture
def local_shard_workflow(tmp_path):
    backend = tmp_path / "backend"
    backend.mkdir()
    workflow = tmp_path / ".workflow-ci"
    tools = workflow / "scripts/ci"
    tools.mkdir(parents=True)
    for script in ("known_flakes.py", "backend_shard_identities.py"):
        shutil.copyfile(ROOT / "scripts/ci" / script, tools / script)
    registry = workflow / ".github/known-flakes.json"
    registry.parent.mkdir()
    node_id = "test_example.py::test_operation"
    registry.write_text(json.dumps([{
        "test_id": node_id,
        "failure_signature": "TimeoutError: operation did not respond",
        "issue": 1303,
        "expires_on": "9999-12-31",
    }]), encoding="utf-8")
    (backend / "test_example.py").write_text(
        "import os\n"
        "import pytest\n"
        "@pytest.fixture\n"
        "def scenario():\n"
        "    case = os.environ['SHARD_TEST_CASE']\n"
        "    if case == 'fixture':\n"
        "        raise TimeoutError('operation did not respond')\n"
        "    return case\n"
        "def test_operation(scenario):\n"
        "    if scenario == 'registered':\n"
        "        raise TimeoutError('operation did not respond')\n"
        "    if scenario == 'unmatched':\n"
        "        raise TimeoutError('operation did not respond (different)')\n",
        encoding="utf-8",
    )
    (backend / "backend-shard-files.txt").write_text("test_example.py\n", encoding="utf-8")
    (backend / "backend-shard-1-collected.txt").write_text(f"{node_id}\n", encoding="utf-8")
    full = tmp_path / "candidate/backend/backend-full-collected.txt"
    full.parent.mkdir(parents=True)
    full.write_text(f"{node_id}\n", encoding="utf-8")
    env = {
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
        "PYTHONPATH": str(ROOT / "backend"),
    }
    return backend, env


def _run_local_shard(root, fixture, case, attempt):
    backend, env = fixture
    run = _shard_run()["run"].replace("${{ matrix.shard }}", "1")
    result = subprocess.run(
        ["bash", "-e", "-c", run], cwd=backend, env=env | {"SHARD_TEST_CASE": case},
        capture_output=True, text=True, timeout=60,
    )
    upload = next(step for step in _jobs()["backend"]["steps"] if step.get("uses", "").startswith("actions/upload-artifact@"))
    assert "always()" in upload["if"]
    artifact = root / "shard-artifacts" / f"backend-test-timings-1-attempt-{attempt}"
    artifact.mkdir(parents=True)
    for entry in upload["with"]["path"].splitlines():
        file = root / entry.replace("${{ matrix.shard }}", "1")
        shutil.copyfile(file, artifact / file.name)
    return result


def _run_local_gate(root, fixture):
    _, env = fixture
    step = next(step for step in _gate()["steps"] if step.get("name") == "Prove that the shards ran every test exactly once")
    run = step["run"].replace("${{ env.BACKEND_SHARDS }}", "1")
    return subprocess.run(["bash", "-e", "-c", run], cwd=root, env=env, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize(("case", "expected"), [("registered", 0), ("unmatched", 1), ("fixture", 1)])
def test_producer_and_aggregate_agree_on_pytest_failures(tmp_path, local_shard_workflow, case, expected):
    producer = _run_local_shard(tmp_path, local_shard_workflow, case, attempt=1)
    assert producer.returncode == expected, producer.stdout + producer.stderr
    gate = _run_local_gate(tmp_path, local_shard_workflow)
    assert gate.returncode == expected, gate.stdout + gate.stderr


def test_a_failed_job_rerun_uses_the_newest_producer_result(tmp_path, local_shard_workflow):
    for attempt, (case, expected) in enumerate([("unmatched", 1), ("unmatched", 1), ("passing", 0)], start=1):
        producer = _run_local_shard(tmp_path, local_shard_workflow, case, attempt)
        assert producer.returncode == expected, producer.stdout + producer.stderr
        gate = _run_local_gate(tmp_path, local_shard_workflow)
        assert gate.returncode == expected, gate.stdout + gate.stderr
        assert f"shard 1: backend-test-timings-1-attempt-{attempt}" in gate.stdout


def test_no_job_adds_a_permission_and_the_real_sudo_job_is_unchanged():
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert all("permissions" not in job for job in workflow["jobs"].values())
    sudo = workflow["jobs"]["real-sudo"]
    assert sudo["name"] == "Hosted real-sudo attach" and sudo["timeout-minutes"] == 20
