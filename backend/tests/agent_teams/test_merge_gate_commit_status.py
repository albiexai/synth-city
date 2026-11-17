"""#1076: no automatic merge path merges a head whose commit statuses are not all green.

Every merge goes through the review item's merge gate, and the settled-revision (#962) and superseded-revision (#972)
promotions lead there. Each case below has green check runs and the reviewer's acceptance, so only the commit status
decides; a success status on each path merges, so a refusal is never vacuous.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services import github_head_acceptance_service as acceptance
from app.services.github_approval_service import github_approval_service
from app.services.github_verification_service import github_verification_service as service
from tests.agent_teams.test_github_verification_service import (  # noqa: F401  (fixtures and helpers shared)
    _HoldClient, _item, _scope, db,
)
from tests.agent_teams.test_settled_revision_promotion import settled_case
from tests.agent_teams.test_superseded_revision_promotion import superseded_case

CONTEXT = "external/security"
GREEN = [{"name": "ci", "status": "completed", "conclusion": "success"}]


@pytest.fixture(autouse=True)
def enforcement(monkeypatch):
    monkeypatch.setattr(settings, "mail_capability_tokens_required", True)
    monkeypatch.setattr(github_approval_service, "github_read_token", AsyncMock(return_value=None))


def _status(state):
    return {"state": state, "total_count": 1, "statuses": [{"context": CONTEXT, "state": state}]}


class _Ordinary:
    """A ready_for_review item on the ordinary merge path, accepted by the reviewer."""

    def __init__(self, db, scope, item, client):
        self.db, self.scope, self.item, self.client = db, scope, item, client

    async def tick(self):
        await service.process_scope(self.db, self.scope, client=self.client)
        await self.db.refresh(self.item)


async def _case(db, monkeypatch, path):
    if path == "ordinary":
        monkeypatch.setattr(acceptance, "gate_note", AsyncMock(return_value=None))  # the reviewer accepted the head
        scope = await _scope(db, merge_policy="auto", review_hold_label="review-hold")
        item = await _item(db, scope, dispatch_status="ready_for_review", pr_number=5, last_verified_sha="sha")
        return _Ordinary(db, scope, item, _HoldClient(check_runs=list(GREEN)))
    return await (settled_case(db) if path == "settled" else superseded_case(db))


PATHS = ["ordinary", "settled", "superseded"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["failure", "error"])
@pytest.mark.parametrize("path", PATHS)
async def test_a_failed_commit_status_is_refused_on_every_merge_path_and_named(db, monkeypatch, path, state):
    case = await _case(db, monkeypatch, path)
    case.client.combined_status = _status(state)
    for _ in range(2):
        await case.tick()
    assert case.client.merge_calls == 0 and case.item.dispatch_status != "merged"
    assert "commit status failed" in case.item.status_note and CONTEXT in case.item.status_note


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_a_pending_commit_status_is_waited_for_on_every_merge_path(db, monkeypatch, path):
    case = await _case(db, monkeypatch, path)
    case.client.combined_status = _status("pending")
    for _ in range(2):
        await case.tick()
    assert case.client.merge_calls == 0 and case.item.dispatch_status != "merged"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_a_successful_commit_status_merges_on_every_merge_path(db, monkeypatch, path):
    case = await _case(db, monkeypatch, path)
    case.client.combined_status = _status("success")
    for _ in range(2):
        await case.tick()
    assert case.item.dispatch_status == "merged" and case.client.merge_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "named"), [
    ({"state": "failure", "statuses": []}, "failed commit status contexts"),  # failed, with no context listed
    ({"state": "failure", "total_count": 120, "statuses": []}, "unobserved commit status contexts"),  # paged out
])
async def test_a_failed_aggregate_whose_contexts_are_not_listed_is_still_refused(db, monkeypatch, status, named):
    case = await _case(db, monkeypatch, "ordinary")
    case.client.combined_status = status
    await case.tick()
    assert case.client.merge_calls == 0 and case.item.dispatch_status != "merged"
    assert named in case.item.status_note
