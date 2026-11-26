"""Optional diagnostics and queue reporting are separate from the required jobs."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
SCRIPT = ROOT / "scripts" / "ci" / "required_job_queue_times.py"


def _workflow(name):
    return yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _report(workflow, pages):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), workflow],
        input=json.dumps(pages), text=True, capture_output=True, check=True, timeout=10,
    )
    return result.stdout


def test_diagnostic_lanes_are_opt_in_and_codeql_still_scans_pushes():
    assert set(_workflow("e2e.yml")["jobs"]) == {"e2e-scope", "e2e-shards", "e2e"}
    diagnostics = _workflow("e2e-diagnostics.yml")
    assert set(diagnostics["on"]) == {"workflow_dispatch"}
    assert diagnostics["on"]["workflow_dispatch"]["inputs"]["lane"]["options"] == [
        "pending", "flicker-loop",
    ]
    job = diagnostics["jobs"]["diagnostics"]
    assert "'pending' && 15 || 35" in job["timeout-minutes"]
    assert any(step.get("run") == "npm run e2e:pending" for step in job["steps"])
    assert {"e2e-pending-results", "e2e-flicker-loop-result", "e2e-flicker-loop-failure"} == {
        step["with"]["name"] for step in job["steps"] if "upload-artifact" in step.get("uses", "")
    }
    assert set(_workflow("codeql.yml")["on"]) == {"push", "schedule"}


def test_report_waits_for_completed_runs_and_reads_the_exact_attempt():
    report = _workflow("ci-queue-times.yml")
    assert report["on"] == {"workflow_run": {
        "workflows": ["Backend regression tests", "Product CI", "Factory source checks", "E2E"],
        "types": ["completed"],
    }}
    assert report["permissions"] == {"actions": "read", "contents": "read"}
    checkout, measure = report["jobs"]["report"]["steps"]
    assert checkout["with"]["ref"] == "${{ github.workflow_sha }}"
    assert measure["env"]["SOURCE_ATTEMPT"] == "${{ github.event.workflow_run.run_attempt }}"
    assert "/attempts/$SOURCE_ATTEMPT/jobs" in measure["run"]


def test_report_keeps_each_required_job_from_paginated_results():
    start = "2026-11-26T13:49:05Z"
    report = _report("Factory source checks", [
        {"jobs": [{"name": "optional", "started_at": start},
                  {"name": "Changed Python undefined names", "started_at": start,
                   "created_at": "2026-11-26T13:49:03Z"}]},
        {"jobs": [{"name": "Rename old-name guard", "started_at": start, "created_at": start}]},
    ])
    assert "| Changed Python undefined names | 2 s |" in report
    assert "| Rename old-name guard | 0 s |" in report
    assert "optional" not in report


@pytest.mark.parametrize("created_at,started_at", [
    (None, "2026-11-26T13:49:05Z"),
    ("2026-11-26T13:49:03Z", None),
    ("2026-11-26T13:49:06Z", "2026-11-26T13:49:05Z"),
    ("2026-11-26T13:49:03", "2026-11-26T13:49:05Z"),
])
def test_report_marks_unusable_timestamps_unknown(created_at, started_at):
    report = _report("E2E", [{"jobs": [{
        "name": "e2e", "created_at": created_at, "started_at": started_at,
    }]}])
    assert "| e2e | Unknown |" in report


def test_report_marks_a_required_job_missing_from_this_attempt_unknown():
    assert "| Full backend regression tests | Unknown |" in _report("Backend regression tests", [])
