"""The registry is validated by both required gates, including docs-only runs."""

import json
import runpy
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/ci/known_flakes.py"
REGISTRY = ROOT / ".github/known-flakes.json"
TODAY = date(2026, 11, 26)
RECORD = {
    "test_id": "tests/test_example.py::test_operation",
    "failure_signature": "TimeoutError: operation did not respond",
    "issue": 123,
    "expires_on": "2026-11-26",
}
validate = runpy.run_path(str(SCRIPT))["validate"]


def test_registry_has_unique_test_ids():
    errors = validate([RECORD, RECORD | {"issue": 456}], TODAY)
    assert len(errors) == 1
    assert "Duplicate test ID" in errors[0] and RECORD["test_id"] in errors[0]


@pytest.mark.parametrize(("entry", "expected"), [
    ({}, "expected exactly"),
    (RECORD | {"unknown": True}, "expected exactly"),
    (RECORD | {"test_id": " "}, "test_id"),
    (RECORD | {"failure_signature": "["}, "not a valid regex"),
    (RECORD | {"failure_signature": ""}, "nonempty regex"),
    (RECORD | {"issue": True}, "positive integer"),
    (RECORD | {"issue": 0}, "positive integer"),
    (RECORD | {"expires_on": "2026-02-29"}, "YYYY-MM-DD"),
    (RECORD | {"expires_on": "20261126"}, "YYYY-MM-DD"),
])
def test_rejects_malformed_records(entry, expected):
    assert expected in validate([entry], TODAY)[0]


def test_registry_must_be_an_array():
    assert "expected an array" in validate({"records": [RECORD]}, TODAY)[0]


def test_expiry_uses_the_calendar_day():
    assert validate([], TODAY) == []
    assert validate([RECORD | {"expires_on": None}], TODAY) == []
    assert validate([RECORD], TODAY) == []
    assert validate([RECORD | {"expires_on": "2026-11-27"}], TODAY) == []
    errors = validate([RECORD | {"expires_on": "2026-11-25"}], TODAY)
    assert len(errors) == 1
    assert "expired" in errors[0] and RECORD["test_id"] in errors[0] and "issue #123" in errors[0]


def test_cli_rejects_duplicate_json_fields_and_missing_registry(tmp_path):
    registry = tmp_path / "known-flakes.json"

    def run():
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(registry)], text=True, capture_output=True, check=False,
        )

    missing = run()
    assert missing.returncode == 1 and "Could not read" in missing.stderr
    registry.write_text('[{"test_id":"first","test_id":"second"}]', encoding="utf-8")
    duplicate = run()
    assert duplicate.returncode == 1 and "duplicate field" in duplicate.stderr
    registry.write_text(REGISTRY.read_text(encoding="utf-8"), encoding="utf-8")
    assert run().returncode == 0


@pytest.mark.parametrize(("workflow", "gate_id"), [
    ("backend-tests.yml", "backend-gate"),
    ("e2e.yml", "e2e"),
])
def test_required_gate_enforces_expiry_using_its_tooling_and_candidate_data(workflow, gate_id, tmp_path):
    jobs = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text(encoding="utf-8"))["jobs"]
    steps = jobs[gate_id]["steps"]
    step_index, validation = next(
        (index, step) for index, step in enumerate(steps)
        if step.get("name") == "Validate the known-flakes registry"
    )
    assert "if" not in validation and not validation.get("continue-on-error", False)
    checkouts = [step for step in steps[:step_index] if step.get("uses", "").startswith("actions/checkout@")]
    assert all("if" not in step for step in checkouts)
    assert [step["with"]["ref"] for step in checkouts] == [
        "${{ github.event.pull_request.head.sha || github.sha }}", "${{ github.workflow_sha }}",
    ]

    validator = tmp_path / ".workflow-ci/scripts/ci/known_flakes.py"
    validator.parent.mkdir(parents=True)
    shutil.copyfile(SCRIPT, validator)
    registry = tmp_path / "candidate/.github/known-flakes.json"
    registry.parent.mkdir(parents=True)

    def run(expiry):
        registry.write_text(json.dumps([RECORD | {"expires_on": expiry}]), encoding="utf-8")
        return subprocess.run(
            ["bash", "-e", "-c", validation["run"]], cwd=tmp_path, text=True, capture_output=True, check=False,
        )

    assert run(None).returncode == 0
    expired = run("2000-01-01")
    assert expired.returncode == 1
    assert RECORD["test_id"] in expired.stderr and "issue #123" in expired.stderr
