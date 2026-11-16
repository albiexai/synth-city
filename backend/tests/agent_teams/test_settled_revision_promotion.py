"""#957: an item on a positively settled revision, with an open PR, is promoted and auto-merged.

Positive settlement completes the revision and leaves the item 'verifying'. The #931 observer then only
recorded a merge or escalated a closed PR; for an OPEN PR it did nothing, so an accepted, green, ready and
unheld PR was never promoted to ready_for_review and never auto-merged.
"""
from __future__ import annotations

import logging
from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import update

from app.config import settings
from app.models.database import GithubAttemptScopeRevision, GithubHeadReview
from app.services.github_approval_service import github_approval_service
from app.services.github_verification_service import github_verification_service as service
from tests.agent_teams.test_github_verification_service import (  # noqa: F401  (fixtures and helpers shared)
    _HoldClient, _implementation_revision, _item, _owner, _scope, db,
)
from tests.agent_teams.test_head_acceptance import slot_member

HEAD = "a" * 40
OTHER = "b" * 40
GREEN = [{"name": "ci", "status": "completed", "conclusion": "success"}]
SETTLED_AT = datetime(2026, 11, 16, 0, 22, 0)
EVIDENCE = "https://github.com/o/r/pull/5#pullrequestreview-77"
LOGGER = "app.services.github_verification_service"


@pytest.fixture(autouse=True)
def enforcement(monkeypatch):
    monkeypatch.setattr(settings, "mail_capability_tokens_required", True)
    monkeypatch.setattr(github_approval_service, "github_read_token", AsyncMock(return_value=None))


class Case:
    def __init__(self, db, scope, item, revision, client):
        self.db, self.scope, self.item, self.revision, self.client = db, scope, item, revision, client

    async def tick(self):
        await service.process_scope(self.db, self.scope, client=self.client)
        await self.db.refresh(self.item)
        await self.db.refresh(self.revision)


async def settled_case(db, *, policy="auto", pull_head=HEAD, settled_head=HEAD, accepted_head=HEAD, checks=GREEN,
                       draft=False, labels=None):
    """A verifying item on a completed (positively settled) revision whose PR is still open."""
    scope = await _scope(db, merge_policy=policy, review_hold_label="review-hold")
    slot, member = await _owner(db, scope)
    item = await _item(db, scope, dispatch_status="verifying", pr_number=5, owner_slot_id=slot.id,
                       dispatch_nonce="attempt-1", active_scope_revision=1, attempt_phase="implementation",
                       ack_received_at=datetime.utcnow())
    revision, _workspace = await _implementation_revision(db, scope, item, slot, member)
    await db.execute(update(GithubAttemptScopeRevision).where(GithubAttemptScopeRevision.id == revision.id)
                     .values(status="completed", completed_at=SETTLED_AT, submitted_head_sha=settled_head))
    if accepted_head is not None:
        _slot, verifier = await slot_member(db, scope, role="verifier", name="V1")
        db.add(GithubHeadReview(work_item_id=item.id, dispatch_nonce="attempt-1", pr_number=5,
                                head_sha=accepted_head, scope_revision=1, reviewer_member_id=verifier.id,
                                verdict="accepted", summary="Reviewed the exact head", evidence_url=EVIDENCE))
    await db.commit()
    await db.refresh(revision)
    client = _HoldClient(check_runs=checks)
    client.pull["head"] = dict(client.pull["head"], sha=pull_head)
    client.pull["draft"] = draft
    client.pull["labels"] = labels or []
    return Case(db, scope, item, revision, client)


@pytest.mark.asyncio
async def test_an_accepted_green_ready_unheld_item_is_promoted_and_auto_merged(db):
    case = await settled_case(db)
    assert case.item.dispatch_status == "verifying" and case.item.last_verified_sha is None
    await case.tick()
    assert case.item.dispatch_status == "merged"
    assert case.client.merge_calls == 1 and case.client.merge_head_shas == [HEAD]
    assert case.item.last_verified_sha == HEAD
    assert (case.revision.status, case.revision.completed_at) == ("completed", SETTLED_AT)


@pytest.mark.asyncio
async def test_without_the_promotion_step_the_item_stays_verifying(db, monkeypatch):
    """The removal control: this is the gap #957 reports, kept as a test of what the new step is for."""
    monkeypatch.setattr(service, "_promote_settled_revision", AsyncMock(), raising=False)
    case = await settled_case(db)
    await case.tick()
    assert case.item.dispatch_status == "verifying" and case.client.merge_calls == 0
    assert case.item.last_verified_sha is None and case.item.merge_wait_started_at is None


@pytest.mark.asyncio
async def test_the_human_policy_promotes_to_ready_for_review_and_never_merges(db, monkeypatch):
    notify = AsyncMock()
    monkeypatch.setattr(service, "_notify_code_pr_ready_for_review", notify)
    case = await settled_case(db, policy="human")
    for _ in range(3):
        await case.tick()
    assert case.item.dispatch_status == "ready_for_review" and case.item.last_verified_sha == HEAD
    assert case.client.merge_calls == 0 and case.item.dispatch_status != "merged"
    assert notify.await_count == 1


@pytest.mark.asyncio
async def test_the_same_fixture_differs_between_policies_only_by_the_policy(db, monkeypatch):
    monkeypatch.setattr(service, "_notify_code_pr_ready_for_review", AsyncMock())
    auto = await settled_case(db, policy="auto")
    await auto.tick()
    human = await settled_case(db, policy="human")
    await human.tick()
    assert (auto.item.dispatch_status, auto.client.merge_calls) == ("merged", 1)
    assert (human.item.dispatch_status, human.client.merge_calls) == ("ready_for_review", 0)


BLOCKERS = [
    ("head_differs", dict(pull_head=OTHER), "verifying", "is not the settled head", None),
    ("no_acceptance", dict(accepted_head=None), "verifying", "acceptance", None),
    ("acceptance_for_another_head", dict(accepted_head=OTHER), "verifying", "acceptance", None),
    ("checks_pending", dict(checks=[{"name": "ci", "status": "in_progress", "conclusion": None}]),
     "verifying", "checks", None),
    ("a_check_failed", dict(checks=[{"name": "ci", "status": "completed", "conclusion": "failure"}]),
     "verifying", "checks", None),
    ("no_checks", dict(checks=[]), "verifying", "checks", None),
    ("draft", dict(draft=True), "verifying", "draft", None),
    ("hold_label", dict(labels=[{"name": "review-hold"}]), "ready_for_review", "held", None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,overrides,status,word,_unused", BLOCKERS, ids=[b[0] for b in BLOCKERS])
async def test_each_blocker_records_its_reason_and_merges_nothing(db, name, overrides, status, word, _unused):
    case = await settled_case(db, **overrides)
    retries_before = case.item.retry_count
    await case.tick()
    assert case.client.merge_calls == 0 and case.item.dispatch_status == status
    assert word in (case.item.status_note or "").lower(), case.item.status_note
    assert case.item.retry_count == retries_before
    assert (case.revision.status, case.revision.completed_at) == ("completed", SETTLED_AT)
    assert case.item.dispatch_status != "merged"


@pytest.mark.asyncio
async def test_a_reason_is_written_once_not_on_every_poll(db):
    case = await settled_case(db, checks=[{"name": "ci", "status": "in_progress", "conclusion": None}])
    await case.tick()
    note, stamp = case.item.status_note, case.item.updated_at
    for _ in range(3):
        await case.tick()
    assert (case.item.status_note, case.item.updated_at) == (note, stamp)


@pytest.mark.asyncio
async def test_a_blocker_that_clears_lets_the_next_pass_promote(db):
    case = await settled_case(db, checks=[{"name": "ci", "status": "in_progress", "conclusion": None}])
    await case.tick()
    assert case.item.dispatch_status == "verifying"
    case.client.check_runs = list(GREEN)
    await case.tick()
    assert case.item.dispatch_status == "merged" and case.client.merge_calls == 1


@pytest.mark.asyncio
async def test_a_head_that_moves_during_the_check_read_stops_the_promotion(db):
    case = await settled_case(db)
    original = case.client.list_check_runs_for_ref

    async def moving(owner, repo, ref):
        case.client.pull["head"] = dict(case.client.pull["head"], sha=OTHER)
        return await original(owner, repo, ref)

    case.client.list_check_runs_for_ref = moving
    await case.tick()
    assert case.client.merge_calls == 0 and case.item.dispatch_status != "merged"
    assert case.item.last_verified_sha in (None, HEAD) and case.item.dispatch_status in ("verifying",)


@pytest.mark.asyncio
async def test_a_failed_github_read_changes_nothing_and_logs_nothing(db, caplog):
    case = await settled_case(db)

    async def failing(owner, repo, number):
        raise httpx.ConnectError("down")

    case.client.get_pull = failing
    before = (case.item.dispatch_status, case.item.status_note, case.item.last_verified_sha)
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await case.tick()
    assert (case.item.dispatch_status, case.item.status_note, case.item.last_verified_sha) == before
    assert [r for r in caplog.records if r.name == LOGGER and r.levelno >= logging.INFO] == []


@pytest.mark.asyncio
async def test_the_merged_and_closed_outcomes_are_unchanged(db):
    merged = await settled_case(db)
    merged.client.pull.update(state="closed", merged=True, merged_at="2026-11-16T00:30:00Z")
    await merged.tick()
    assert merged.item.dispatch_status == "merged" and merged.client.merge_calls == 0
    closed = await settled_case(db)
    closed.client.pull.update(state="closed", merged=False, merged_at=None)
    await closed.tick()
    assert closed.item.dispatch_status == "escalated" and closed.item.escalation_reason == "pr_closed_unmerged"


@pytest.mark.asyncio
async def test_a_foreign_open_response_promotes_nothing_and_is_logged_once(db, caplog, monkeypatch):
    monkeypatch.setattr(service, "_logged_inconsistent", set())
    case = await settled_case(db)
    case.client.pull["base"] = dict(case.client.pull["base"], repo={"full_name": "foreign/repo"})
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for _ in range(3):
            await case.tick()
    assert case.client.merge_calls == 0 and case.item.dispatch_status == "verifying"
    assert case.item.last_verified_sha is None
    assert len([r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]) == 1


@pytest.mark.asyncio
async def test_an_acceptance_from_a_reviewer_who_is_no_longer_a_verifier_does_not_promote(db):
    case = await settled_case(db)
    from sqlalchemy import select

    from app.models.database import AgentTeamSlot
    verifier = (await db.execute(select(AgentTeamSlot).where(AgentTeamSlot.role == "verifier"))).scalars().one()
    verifier.role = "Builder"
    await db.commit()
    await case.tick()
    assert case.client.merge_calls == 0 and case.item.dispatch_status == "verifying"
    assert "no longer from a current verifier" in case.item.status_note
