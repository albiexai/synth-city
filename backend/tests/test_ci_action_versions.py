"""The workflows use the v7 line of actions/upload-artifact and actions/setup-python (#1157).

A floor, not an exact pin: the next Dependabot bump must not break it. v6 of upload-artifact moved to Node 24 and v7
of setup-python removed the `pip-install` input, so a workflow that still sits below v7 or still passes that input is
behind what these jobs were checked against.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
FLOOR = 7
GUARDED = ("actions/upload-artifact", "actions/setup-python")


def _steps():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in (document.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                uses = str(step.get("uses", ""))
                if uses.startswith(GUARDED):
                    yield path.name, job_name, uses, step


def _major(reference: str) -> int | None:
    match = re.fullmatch(r"v(\d+)(?:\.\d+){0,2}", reference)
    return int(match.group(1)) if match else None


def test_the_guarded_actions_are_used_at_all():
    assert len(list(_steps())) >= 10, "the workflows lost their uploads or Python setups"


@pytest.mark.parametrize("action", GUARDED)
def test_every_use_is_at_the_v7_line_or_later(action):
    behind = []
    for name, job, uses, _ in _steps():
        if not uses.startswith(action + "@"):
            continue
        reference = uses.split("@", 1)[1]
        major = _major(reference)
        # A full commit SHA or a branch name cannot be compared; it is not behind a version it does not name.
        if major is not None and major < FLOOR:
            behind.append(f"{name}:{job} uses {uses}")
    assert not behind, "below v%d: %s" % (FLOOR, "; ".join(behind))


def test_no_setup_python_step_passes_the_input_that_v7_removed():
    offenders = [f"{name}:{job}" for name, job, uses, step in _steps()
                 if uses.startswith("actions/setup-python@") and "pip-install" in (step.get("with") or {})]
    assert not offenders, "pip-install was removed in setup-python v7: " + ", ".join(offenders)
