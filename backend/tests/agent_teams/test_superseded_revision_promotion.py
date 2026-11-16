"""#972: an open PR on a superseded revision is promoted and auto-merged on an exact-head acceptance.

A Leader settlement of a negative review ends the submitted revision as superseded and leaves the item 'dispatched'
with no owner acknowledgement. The owner can then push a head the reviewer accepts, but #957 only promotes a
completed revision on a 'verifying' item, so an accepted, green, ready and unheld PR was never promoted and a person
merged it by hand. The superseded revision alone grants nothing: the exact-head acceptance and every merge gate do.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from app.config import settings
from app.models.database import GithubAttemptScopeRevision, GithubHeadReview, GithubWorkItem, TeamGithubScope
from app.services.github_approval_service import github_approval_service
from app.services.github_verification_service import github_verification_service as service
from tests.agent_teams.test_github_verification_service import (  # noqa: F401  (fixtures and helpers shared)
    _HoldClient, _implementation_revision, _item, _owner, _scope, db,
)
from tests.agent_teams.test_head_acceptance import slot_member, twin  # noqa: F401  (fixtures and helpers shared)

OLD = "c" * 40  # the head the superseded revision submitted
HEAD = "a" * 40  # the head pushed afterwards and accepted
OTHER = "b" * 40
GREEN = [{"name": "ci", "status": "completed", "conclusion": "success"}]
ENDED_AT = datetime(2026, 11, 16, 0, 22, 0)
EVIDENCE = "https://github.com/o/r/pull/5#pullrequestreview-77"


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


async def superseded_case(db, *, shape="stopped", policy="auto", pull_head=HEAD, accepted_head=HEAD, checks=GREEN,
                          draft=False, labels=None, status="dispatched", handoff_state=None):
    """An item whose last revision a Leader settlement ended, with the owner's later head accepted and the PR open.

    'stopped' is the live shape: no owner acknowledgement and an amendment-origin revision. 'plain' keeps the
    acknowledgement and an ordinary revision.
    """
    scope = await _scope(db, merge_policy=policy, review_hold_label="review-hold")
    slot, member = await _owner(db, scope)
    stopped = shape == "stopped"
    item = await _item(db, scope, dispatch_status=status, pr_number=5, owner_slot_id=slot.id,
                       dispatch_nonce="attempt-1", active_scope_revision=4, attempt_phase="implementation",
                       ack_received_at=None if stopped else datetime.utcnow(), last_verified_sha=OLD,
                       handoff_state=handoff_state)
    revision, _workspace = await _implementation_revision(db, scope, item, slot, member)
    await db.execute(update(GithubAttemptScopeRevision).where(GithubAttemptScopeRevision.id == revision.id).values(
        status="superseded", completed_at=ENDED_AT, submitted_head_sha=OLD,
        origin_kind="scope_amendment" if stopped else "continuation"))
    if accepted_head is not None:
        _slot, verifier = await slot_member(db, scope, role="verifier", name="V1")
        db.add(GithubHeadReview(work_item_id=item.id, dispatch_nonce="attempt-1", pr_number=5,
                                head_sha=accepted_head, scope_revision=4, reviewer_member_id=verifier.id,
                                verdict="accepted", summary="Reviewed the exact head", evidence_url=EVIDENCE))
    await db.commit()
    await db.refresh(revision)
    client = _HoldClient(check_runs=checks)
    client.pull["head"] = dict(client.pull["head"], sha=pull_head)
    client.pull["draft"] = draft
    client.pull["labels"] = labels or []
    return Case(db, scope, item, revision, client)


def _untouched(case):
    assert (case.revision.status, case.revision.completed_at) == ("superseded", ENDED_AT)
    assert case.client.merge_calls == 0 and case.item.dispatch_status != "merged"


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["stopped", "plain"])
async def test_an_accepted_green_ready_unheld_item_on_a_superseded_revision_is_merged(db, shape):
    case = await superseded_case(db, shape=shape)
    assert case.item.dispatch_status == "dispatched" and case.item.last_verified_sha == OLD
    await case.tick()
    assert case.item.dispatch_status == "merged"
    assert case.client.merge_calls == 1 and case.client.merge_head_shas == [HEAD]
    assert case.item.last_verified_sha == HEAD
    assert (case.revision.status, case.revision.completed_at) == ("superseded", ENDED_AT)


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["stopped", "plain"])
async def test_without_the_promotion_step_the_item_stays_dispatched(db, monkeypatch, shape):
    """The removal control: this is the gap #972 reports, kept as a test of what the new step is for."""
    monkeypatch.setattr(service, "_promote_settled_revision", AsyncMock())
    case = await superseded_case(db, shape=shape)
    await case.tick()
    assert case.item.dispatch_status == "dispatched" and case.client.merge_calls == 0
    assert case.item.last_verified_sha == OLD


@pytest.mark.asyncio
async def test_without_the_observer_the_item_stays_dispatched(db, monkeypatch):
    monkeypatch.setattr(service, "_observe_superseded_revision", AsyncMock())
    case = await superseded_case(db)
    await case.tick()
    assert case.item.dispatch_status == "dispatched" and case.client.merge_calls == 0


@pytest.mark.asyncio
async def test_the_human_policy_promotes_to_ready_for_review_and_never_merges(db, monkeypatch):
    notify = AsyncMock()
    monkeypatch.setattr(service, "_notify_code_pr_ready_for_review", notify)
    case = await superseded_case(db, policy="human")
    for _ in range(3):
        await case.tick()
    assert case.item.dispatch_status == "ready_for_review" and case.item.last_verified_sha == HEAD
    assert case.client.merge_calls == 0 and notify.await_count == 1
    _untouched(case)


BLOCKERS = [
    ("no_acceptance", dict(accepted_head=None), "dispatched", "acceptance"),
    ("acceptance_for_the_old_head", dict(accepted_head=OLD), "dispatched", "acceptance"),
    ("acceptance_for_another_head", dict(accepted_head=OTHER), "dispatched", "acceptance"),
    ("checks_pending", dict(checks=[{"name": "ci", "status": "in_progress", "conclusion": None}]),
     "dispatched", "checks"),
    ("a_check_failed", dict(checks=[{"name": "ci", "status": "completed", "conclusion": "failure"}]),
     "dispatched", "checks"),
    ("no_checks", dict(checks=[]), "dispatched", "checks"),
    ("draft", dict(draft=True), "dispatched", "draft"),
    ("hold_label", dict(labels=[{"name": "review-hold"}]), "ready_for_review", "held"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["stopped", "plain"])
@pytest.mark.parametrize("name,overrides,status,word", BLOCKERS, ids=[b[0] for b in BLOCKERS])
async def test_each_blocker_records_its_reason_and_merges_nothing(db, shape, name, overrides, status, word):
    case = await superseded_case(db, shape=shape, **overrides)
    retries_before = case.item.retry_count
    await case.tick()
    assert case.item.dispatch_status == status
    assert word in (case.item.status_note or "").lower(), case.item.status_note
    assert case.item.retry_count == retries_before
    _untouched(case)


@pytest.mark.asyncio
async def test_a_reason_is_written_once_not_on_every_poll(db):
    case = await superseded_case(db, checks=[{"name": "ci", "status": "in_progress", "conclusion": None}])
    await case.tick()
    note, stamp = case.item.status_note, case.item.updated_at
    for _ in range(3):
        await case.tick()
    assert (case.item.status_note, case.item.updated_at) == (note, stamp)


@pytest.mark.asyncio
async def test_a_blocker_that_clears_lets_the_next_pass_promote(db):
    case = await superseded_case(db, checks=[{"name": "ci", "status": "in_progress", "conclusion": None}])
    await case.tick()
    assert case.item.dispatch_status == "dispatched"
    case.client.check_runs = list(GREEN)
    await case.tick()
    assert case.item.dispatch_status == "merged" and case.client.merge_calls == 1


@pytest.mark.asyncio
async def test_a_head_that_moves_during_the_check_read_stops_the_promotion(db):
    case = await superseded_case(db)
    original = case.client.list_check_runs_for_ref

    async def moving(owner, repo, ref):
        case.client.pull["head"] = dict(case.client.pull["head"], sha=OTHER)
        return await original(owner, repo, ref)

    case.client.list_check_runs_for_ref = moving
    await case.tick()
    assert case.client.merge_calls == 0 and case.item.dispatch_status == "dispatched"
    assert case.item.last_verified_sha == OLD


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["stopped", "plain"])
async def test_a_promoted_item_is_carried_to_the_merge_by_a_later_poll(db, shape):
    """Neither shape may strand a promoted item: a hold on the first poll is read again on the next."""
    case = await superseded_case(db, shape=shape, labels=[{"name": "review-hold"}])
    await case.tick()
    assert case.item.dispatch_status == "ready_for_review" and case.client.merge_calls == 0
    case.client.pull["labels"] = []
    await case.tick()
    assert case.item.dispatch_status == "merged" and case.client.merge_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["escalated", "merged"])
async def test_an_item_that_is_not_dispatched_or_in_review_is_left_alone(db, status):
    case = await superseded_case(db, status=status)
    await case.tick()
    assert case.client.merge_calls == 0 and case.item.dispatch_status == status


@pytest.mark.asyncio
async def test_a_handoff_in_progress_is_not_promoted(db):
    case = await superseded_case(db, handoff_state="pending")
    await case.tick()
    assert case.client.merge_calls == 0 and case.item.dispatch_status == "dispatched"
    assert case.item.last_verified_sha == OLD


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["diagnostic"])
async def test_only_an_implementation_revision_is_promoted(db, phase):
    case = await superseded_case(db)
    await db.execute(update(GithubAttemptScopeRevision).where(GithubAttemptScopeRevision.id == case.revision.id)
                     .values(phase=phase))
    await db.commit()
    await case.tick()
    assert case.client.merge_calls == 0 and case.item.dispatch_status == "dispatched"


@pytest.mark.asyncio
async def test_the_merged_and_closed_outcomes_are_unchanged(db):
    merged = await superseded_case(db, shape="plain")
    merged.client.pull.update(state="closed", merged=True, merged_at="2026-11-16T00:30:00Z")
    await merged.tick()
    assert merged.client.merge_calls == 0 and merged.item.dispatch_status == "dispatched"
    closed = await superseded_case(db, shape="plain")
    closed.client.pull.update(state="closed", merged=False, merged_at=None)
    await closed.tick()
    assert closed.client.merge_calls == 0 and closed.item.dispatch_status == "dispatched"


@pytest.mark.asyncio
async def test_a_foreign_open_response_promotes_nothing(db, monkeypatch):
    monkeypatch.setattr(service, "_logged_inconsistent", set())
    case = await superseded_case(db)
    case.client.pull["base"] = dict(case.client.pull["base"], repo={"full_name": "foreign/repo"})
    for _ in range(2):
        await case.tick()
    assert case.client.merge_calls == 0 and case.item.dispatch_status == "dispatched"
    assert case.item.last_verified_sha == OLD


WITHDRAWALS = ["revision_changed", "revision_completed", "scope_disabled", "attempt_replaced"]


def _withdraw(case, kind):
    revision_id, scope_id, item_id = case.revision.id, case.scope.id, case.item.id
    if kind == "revision_changed":
        return update(GithubAttemptScopeRevision).where(GithubAttemptScopeRevision.id == revision_id).values(
            failed_head_count=1)
    if kind == "revision_completed":
        return update(GithubAttemptScopeRevision).where(GithubAttemptScopeRevision.id == revision_id).values(
            status="completed")
    if kind == "scope_disabled":
        return update(TeamGithubScope).where(TeamGithubScope.id == scope_id).values(enabled=False)
    return update(GithubWorkItem).where(GithubWorkItem.id == item_id).values(dispatch_nonce="a-newer-attempt")


def _withdraw_during(case, maker, kind, call_number):
    """Run the change through a second connection while the Nth check read is awaited."""
    original, state = case.client.list_check_runs_for_ref, {"calls": 0}
    statement = _withdraw(case, kind)

    async def racing(owner, repo, ref):
        state["calls"] += 1
        if state["calls"] == call_number:
            async with maker() as other:
                await other.execute(statement)
                await other.commit()
        return await original(owner, repo, ref)

    case.client.list_check_runs_for_ref = racing
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", WITHDRAWALS)
async def test_a_change_during_the_first_check_read_promotes_and_merges_nothing(twin, kind):
    db, maker = twin
    case = await superseded_case(db)
    state = _withdraw_during(case, maker, kind, 1)
    await case.tick()
    assert state["calls"] >= 1
    assert case.client.merge_calls == 0 and case.item.dispatch_status != "merged"
    assert case.item.last_verified_sha == OLD


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", WITHDRAWALS)
async def test_a_change_during_a_later_read_before_the_merge_does_not_merge(twin, kind):
    db, maker = twin
    case = await superseded_case(db)
    state = _withdraw_during(case, maker, kind, 2)
    await case.tick()
    assert state["calls"] >= 2, "the merge path must read the checks again"
    assert case.client.merge_calls == 0 and case.item.dispatch_status != "merged"


@pytest.mark.asyncio
async def test_with_no_change_the_two_connection_flow_still_merges(twin):
    db, maker = twin
    case = await superseded_case(db)
    state = _withdraw_during(case, maker, "revision_changed", 99)  # never fires
    await case.tick()
    assert state["calls"] >= 2
    assert case.item.dispatch_status == "merged" and case.client.merge_calls == 1
