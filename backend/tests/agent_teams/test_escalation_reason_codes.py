"""#1139 (part a): an escalated item always has a specific reason code, never an empty one or "unknown".

Every path that returns an attempt to escalation used to copy its revision's originating_escalation_reason, and a
scope amendment stores that as "" (it starts from a healthy attempt), so a handoff that superseded an active amendment
left the item escalated with no reason. The escalated auto-merge note then derived "unknown". The projection's waiting
reason code is #1181.
"""
from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

import pytest

from app.services.github_dispatch_service import ESCALATION_REASONS, restored_escalation_reason
from tests.agent_teams.test_auto_merge_visibility import _escalated, _notices, _poll, db  # noqa: F401

APP = pathlib.Path(__file__).resolve().parents[2] / "app"


@pytest.mark.parametrize("originating,expected", [
    ("", "plan_blocked"),  # a scope amendment starts from a healthy attempt
    (None, "plan_blocked"),
    ("owner_idle_timeout", "owner_idle_timeout"),
])
def test_a_restored_escalation_always_has_a_specific_reason(originating, expected):
    reason = restored_escalation_reason(SimpleNamespace(originating_escalation_reason=originating))
    assert reason == expected and reason in ESCALATION_REASONS


@pytest.mark.asyncio
async def test_the_auto_merge_note_names_a_missing_reason_specifically(db, monkeypatch):
    from unittest.mock import AsyncMock

    from app.services import github_head_acceptance_service as acceptance

    monkeypatch.setattr(acceptance, "gate_note", AsyncMock(return_value=None))  # the head is accepted
    scope, item, _revision, client = await _escalated(db, settled=False)
    item.escalation_reason = ""  # a legacy row written before this fix
    await db.commit()
    await _poll(db, scope, client, times=2)
    await db.refresh(item)
    assert "[item_not_eligible: escalated: escalation_reason_missing]" in item.status_note
    assert "unknown" not in item.status_note
    assert {m.payload["detail"] for m in await _notices(db)} == {"escalated: escalation_reason_missing"}


def _writes():
    """Every value written to escalation_reason in the application, as source text."""
    found = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr == "escalation_reason":
                        found.append((path.relative_to(APP), node.lineno, node.value))
            elif isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in {
                    "values", "GithubWorkItem"}:  # an UPDATE ... SET or a new row; a response model only reads it
                for keyword in node.keywords:
                    if keyword.arg == "escalation_reason":
                        found.append((path.relative_to(APP), node.lineno, keyword.value))
    return found


# A name bound to a known code where it is written (a parameter checked against ESCALATION_REASONS, a module constant,
# or the Leader-cancel path's computed value that already falls back to "plan_blocked").
KNOWN_NAMES = {"reason", "REASON", "new_escalation_reason"}


def test_every_escalation_reason_write_is_a_specific_code():
    bad = []
    for path, line, value in _writes():
        if isinstance(value, ast.Constant) and (value.value is None or value.value in ESCALATION_REASONS):
            continue
        if isinstance(value, ast.Name) and value.id in KNOWN_NAMES:
            continue
        if isinstance(value, ast.IfExp) and all(
                isinstance(branch, ast.Constant) and (branch.value is None or branch.value in ESCALATION_REASONS)
                for branch in (value.body, value.orelse)):
            continue
        if isinstance(value, ast.Call) and getattr(value.func, "id", getattr(value.func, "attr", "")) == \
                "restored_escalation_reason":
            continue
        bad.append(f"{path}:{line} {ast.unparse(value)}")
    assert bad == []
