"""pytest plugin: record the final outcome of every test that ran, one line per node id (#1013).

The sharded backend job loads it with ``-p tests.ci_shard_identity --identity-out=<file>``. The gate compares
these executed identities with the collected ones, so a test that was collected but never ran, or ran twice,
is noticed. Each line is a JSON object: ``{"id": <node id>, "outcome": <outcome>, "runs": <int>}``. The outcome
is the worst of the setup, call and teardown reports: error, failed, xpassed, xfailed, skipped, passed.
"""
from __future__ import annotations

import json

_RANK = {"passed": 1, "skipped": 2, "xfailed": 3, "xpassed": 4, "failed": 5, "error": 6}
_records: dict[str, dict] = {}


def pytest_addoption(parser):
    parser.addoption("--identity-out", action="store", default=None,
                     help="write one JSON line per executed test (node id, final outcome, run count)")


def _phase_outcome(report) -> str:
    if report.failed:
        return "failed" if report.when == "call" else "error"
    if report.skipped:
        return "xfailed" if hasattr(report, "wasxfail") else "skipped"
    return "xpassed" if report.when == "call" and hasattr(report, "wasxfail") else "passed"


def pytest_runtest_logreport(report):
    record = _records.setdefault(report.nodeid, {"outcome": "passed", "runs": 0})
    if report.when == "setup":
        record["runs"] += 1
    outcome = _phase_outcome(report)
    if _RANK[outcome] > _RANK[record["outcome"]]:
        record["outcome"] = outcome


def pytest_sessionfinish(session):
    path = session.config.getoption("--identity-out")
    if not path:
        return
    with open(path, "w", encoding="utf-8") as handle:
        for node_id in sorted(_records):
            handle.write(json.dumps({"id": node_id, **_records[node_id]}) + "\n")
