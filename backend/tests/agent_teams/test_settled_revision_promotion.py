"""#957: an item on a positively settled revision, with an open PR, is promoted and auto-merged.

Positive settlement completes the revision and leaves the item 'verifying'. The #931 observer then only
recorded a merge or escalated a closed PR; for an OPEN PR it did nothing, so an accepted, green, ready and
unheld PR was never promoted to ready_for_review and never auto-merged.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import delete as _delete
from sqlalchemy import select as _select
from sqlalchemy import update

from app.config import settings
from app.models.database import (
    GithubAttemptScopeRevision, GithubHeadReview, GithubRequiredCheckWatch, GithubRevisionReview, MailTeamMember,
)
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
@pytest.mark.parametrize("case_kind", ["accepted", "red", "withdrawn", "old_head", "former_verifier", "head_rejected"])
async def test_settled_revision_review_can_supply_exact_head_acceptance(db, case_kind):
    checks = GREEN
    if case_kind == "red":
        checks = [{"name": "ci", "status": "completed", "conclusion": "failure"}]
    case = await settled_case(db, accepted_head=None, checks=checks)
    case.revision.owner_slot_id = case.item.owner_slot_id
    case.revision.owner_member_id = await db.scalar(
        _select(MailTeamMember.id).where(MailTeamMember.team_slot_id == case.item.owner_slot_id)
    )
    slot, reviewer = await slot_member(db, case.scope, role="verifier", name="V2")
    if case_kind == "former_verifier":
        slot.role = "builder"
    review_head = OTHER if case_kind == "old_head" else HEAD
    watch = GithubRequiredCheckWatch(
        work_item_id=case.item.id, dispatch_nonce=case.item.dispatch_nonce, scope_revision=case.revision.revision,
        pr_number=case.item.pr_number, owner_slot_id=case.item.owner_slot_id, head_sha=review_head,
        policy_digest="fixture", reviewer_member_id=reviewer.id, checks=[],
        observed_at=SETTLED_AT, changed_at=SETTLED_AT,
    )
    db.add(watch)
    await db.flush()
    db.add(GithubRevisionReview(
        work_item_id=case.item.id, revision_id=case.revision.id, watch_id=watch.id,
        dispatch_nonce=case.item.dispatch_nonce, head_sha=review_head, reviewer_member_id=reviewer.id,
        expected_lease_token_hash=case.revision.expected_lease_token_hash, verdict="accepted",
        status="withdrawn" if case_kind == "withdrawn" else "consumed",
        settled_by_member_id=case.item.ack_approver_member_id, summary="Reviewed the submitted revision",
        evidence_url=EVIDENCE,
    ))
    if case_kind == "head_rejected":
        db.add(GithubHeadReview(
            work_item_id=case.item.id, dispatch_nonce=case.item.dispatch_nonce,
            pr_number=case.item.pr_number, scope_revision=case.revision.revision, head_sha=HEAD,
            reviewer_member_id=reviewer.id, verdict="changes_required",
            summary="New blocking finding", evidence_url=EVIDENCE,
        ))
    await db.commit()
    await case.tick()
    if case_kind == "accepted":
        assert case.item.dispatch_status == "merged", case.item.status_note
        assert case.client.merge_head_shas == [HEAD]
    else:
        assert case.item.dispatch_status == "verifying"
        assert case.client.merge_calls == 0
        if case_kind == "red":
            assert "required checks are not all green" in case.item.status_note
        if case_kind == "head_rejected":
            assert "Reviewer requires changes" in case.item.status_note
    assert case.revision.status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("state,changed_head", [("verifying", False), ("verifying", True), ("ready_for_review", True)])
async def test_settled_poll_clears_unreadable_ci_on_green_or_changed_head(db, state, changed_head):
    head = OTHER if changed_head else HEAD
    case = await settled_case(db, policy="human", pull_head=head, draft=True)
    case.item.checks_unavailable_since = datetime.utcnow() - timedelta(minutes=20)
    case.item.checks_unavailable_reason = "api_error"
    case.item.checks_unavailable_head_sha = HEAD
    case.item.dispatch_status = state
    case.item.verification_head_sha = HEAD
    await db.commit()
    await case.tick()
    assert case.item.checks_unavailable_since is None
    assert case.item.checks_unavailable_reason is None
    assert case.item.checks_unavailable_head_sha is None
    assert case.item.dispatch_status == "verifying"
    if changed_head:
        assert "scope amendment" in case.item.status_note
        assert case.revision.submitted_head_sha == HEAD


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


# --- V1 review 5914280179: the settled context is fenced across awaits and at the merge boundary ---------------

from sqlalchemy import update as _update  # noqa: E402

from app.models.database import GithubWorkItem, TeamGithubScope  # noqa: E402
from tests.agent_teams.test_head_acceptance import twin  # noqa: E402,F401  (fixture)

WITHDRAWALS = ["revision_superseded", "submitted_head_changed", "completed_at_cleared", "scope_disabled",
               "attempt_replaced"]


def _withdraw(case, kind):
    revision_id, scope_id, item_id = case.revision.id, case.scope.id, case.item.id
    if kind == "revision_superseded":
        return _update(GithubAttemptScopeRevision).where(GithubAttemptScopeRevision.id == revision_id).values(
            status="superseded")
    if kind == "submitted_head_changed":
        return _update(GithubAttemptScopeRevision).where(GithubAttemptScopeRevision.id == revision_id).values(
            submitted_head_sha=OTHER)
    if kind == "completed_at_cleared":
        return _update(GithubAttemptScopeRevision).where(GithubAttemptScopeRevision.id == revision_id).values(
            completed_at=None)
    if kind == "scope_disabled":
        return _update(TeamGithubScope).where(TeamGithubScope.id == scope_id).values(enabled=False)
    return _update(GithubWorkItem).where(GithubWorkItem.id == item_id).values(dispatch_nonce="a-newer-attempt")


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
async def test_a_withdrawal_during_the_first_check_read_promotes_and_merges_nothing(twin, kind):
    db, maker = twin
    case = await settled_case(db)
    state = _withdraw_during(case, maker, kind, 1)
    await case.tick()
    assert state["calls"] >= 1
    assert case.client.merge_calls == 0 and case.item.dispatch_status != "merged"
    assert case.item.dispatch_status == "verifying" and case.item.last_verified_sha is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", WITHDRAWALS)
async def test_a_withdrawal_during_a_later_read_before_the_merge_does_not_merge(twin, kind):
    db, maker = twin
    case = await settled_case(db)
    state = _withdraw_during(case, maker, kind, 2)
    await case.tick()
    assert state["calls"] >= 2, "the merge path must read the checks again"
    assert case.client.merge_calls == 0 and case.item.dispatch_status != "merged"


@pytest.mark.asyncio
async def test_with_no_withdrawal_the_two_connection_flow_still_merges(twin):
    db, maker = twin
    case = await settled_case(db)
    state = _withdraw_during(case, maker, "revision_superseded", 99)  # never fires
    await case.tick()
    assert state["calls"] >= 2
    assert case.item.dispatch_status == "merged" and case.client.merge_calls == 1


@pytest.mark.asyncio
async def test_a_concurrent_switch_to_the_human_policy_still_never_auto_merges(twin):
    db, maker = twin
    case = await settled_case(db)
    scope_id = case.scope.id
    original, done = case.client.list_check_runs_for_ref, {"fired": False}

    async def racing(owner, repo, ref):
        if not done["fired"]:
            done["fired"] = True
            async with maker() as other:
                await other.execute(_update(TeamGithubScope).where(TeamGithubScope.id == scope_id)
                                    .values(merge_policy="human"))
                await other.commit()
        return await original(owner, repo, ref)

    case.client.list_check_runs_for_ref = racing
    await case.tick()
    assert case.client.merge_calls == 0 and case.item.dispatch_status != "merged"


@pytest.mark.asyncio
async def test_the_terminal_writer_is_taken_once_before_the_promotion_is_written(db, monkeypatch):
    import app.services.github_verification_service as module

    calls = []
    real = module.lock_terminal_transition

    async def counting(db_, item_):
        calls.append(item_.dispatch_status)
        return await real(db_, item_)

    monkeypatch.setattr(module, "lock_terminal_transition", counting)
    case = await settled_case(db, policy="human")
    monkeypatch.setattr(service, "_notify_code_pr_ready_for_review", AsyncMock())
    await case.tick()
    assert calls == ["verifying"], calls  # the item was still verifying when the writer was taken
    assert case.item.dispatch_status == "ready_for_review"


# --- V1 review 5914361919: the boundary judges the revision by status for every phase, and notes are fenced ----


@pytest.mark.asyncio
async def test_a_revision_turned_diagnostic_and_superseded_before_the_merge_does_not_merge(twin):
    db, maker = twin
    case = await settled_case(db)
    revision_id = case.revision.id
    original, state = case.client.list_check_runs_for_ref, {"calls": 0}

    async def racing(owner, repo, ref):
        state["calls"] += 1
        if state["calls"] == 2:
            async with maker() as other:
                await other.execute(_update(GithubAttemptScopeRevision)
                                    .where(GithubAttemptScopeRevision.id == revision_id)
                                    .values(phase="diagnostic", status="superseded"))
                await other.commit()
        return await original(owner, repo, ref)

    case.client.list_check_runs_for_ref = racing
    await case.tick()
    assert state["calls"] >= 2
    assert case.client.merge_calls == 0 and case.item.dispatch_status != "merged"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,status,completed,head,blocked", [
    ("implementation", "completed", True, HEAD, False),
    ("implementation", "completed", True, OTHER, True),
    ("implementation", "completed", False, HEAD, True),
    ("implementation", "superseded", True, HEAD, True),
    ("implementation", "active", True, HEAD, True),
    ("diagnostic", "completed", True, OTHER, False),   # a settled diagnostic restores the baseline: no head match
    ("diagnostic", "completed", False, OTHER, True),
    ("diagnostic", "superseded", True, OTHER, True),
    ("diagnostic", "submitted", True, OTHER, True),
])
async def test_the_boundary_judges_a_revision_row_by_its_status_in_every_phase(db, phase, status, completed, head,
                                                                              blocked):
    case = await settled_case(db)
    await db.execute(_update(GithubAttemptScopeRevision).where(GithubAttemptScopeRevision.id == case.revision.id)
                     .values(phase=phase, status=status, completed_at=SETTLED_AT if completed else None))
    await db.commit()
    expected = await service._settled_snapshot(db, case.scope, case.item)
    note = await service._settled_context_note(db, case.scope, case.item, head, expected)
    assert (note is not None) == blocked, note


@pytest.mark.asyncio
async def test_an_item_with_no_revision_row_is_not_judged_by_the_boundary(db):
    case = await settled_case(db)
    await db.execute(_update(GithubWorkItem).where(GithubWorkItem.id == case.item.id).values(active_scope_revision=0))
    await db.commit()
    await db.refresh(case.item)
    expected = await service._settled_snapshot(db, case.scope, case.item)
    assert expected is None
    assert await service._settled_context_note(db, case.scope, case.item, HEAD, expected) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["checks", "acceptance", "head"])
async def test_a_note_never_overwrites_a_new_attempt_that_replaced_the_item_during_the_read(twin, blocker):
    db, maker = twin
    overrides = {"checks": dict(checks=[{"name": "ci", "status": "in_progress", "conclusion": None}]),
                 "acceptance": dict(accepted_head=None), "head": dict(pull_head=OTHER)}[blocker]
    case = await settled_case(db, **overrides)
    item_id = case.item.id
    original, state = case.client.list_check_runs_for_ref, {"fired": False}

    async def replace_the_attempt():
        async with maker() as other:
            await other.execute(_update(GithubWorkItem).where(GithubWorkItem.id == item_id).values(
                dispatch_nonce="a-newer-attempt", dispatch_status="dispatched",
                status_note="The new attempt is healthy."))
            await other.commit()
        state["fired"] = True

    async def racing_checks(owner, repo, ref):
        if not state["fired"]:
            await replace_the_attempt()
        return await original(owner, repo, ref)

    case.client.list_check_runs_for_ref = racing_checks
    original_pull = case.client.get_pull

    async def racing_pull(owner, repo, number):
        pull = await original_pull(owner, repo, number)
        if blocker != "checks" and not state["fired"]:
            await replace_the_attempt()
        return pull

    case.client.get_pull = racing_pull
    await case.tick()
    await db.refresh(case.item)
    assert state["fired"]
    assert case.item.status_note == "The new attempt is healthy."
    assert case.client.merge_calls == 0



@pytest.mark.asyncio
async def test_the_policy_claim_holds_the_writer_across_the_fresh_pr_read(twin):
    """After the policy claim the first connection holds the SQLite writer, so a second connection cannot commit
    a replacement attempt during the fresh PR read: the draft and head-changed notes need no further fence."""
    db, maker = twin
    case = await settled_case(db, draft=True)
    item_id = case.item.id
    original, outcome = case.client.get_pull, {"calls": 0, "blocked": False}

    async def probing(owner, repo, number):
        outcome["calls"] += 1
        if outcome["calls"] == 2:
            from sqlalchemy.exc import OperationalError
            async with maker() as other:
                try:
                    await other.execute(_update(GithubWorkItem).where(GithubWorkItem.id == item_id)
                                        .values(status_note="a second connection wrote"))
                    await other.commit()
                except OperationalError:
                    outcome["blocked"] = True
                    await other.rollback()
        return await original(owner, repo, number)

    case.client.get_pull = probing
    await case.tick()
    assert outcome["calls"] >= 2 and outcome["blocked"] is True


# --- V1 review 5914463958: an operation that began on a settled revision keeps the identity it began with ------


def _changed(value, column=None):
    """A different value of the same kind, for any revision column (a null takes its column type)."""
    if value is None and column is not None:
        kind = column.type.python_type
        return {datetime: SETTLED_AT, int: 7, str: "changed", dict: {"changed": True}, list: ["changed"]}[kind]
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 7
    if isinstance(value, str):
        return value + "x"
    if isinstance(value, datetime):
        return value.replace(year=value.year + 1)
    if isinstance(value, list):
        return [*value, "changed"]
    if isinstance(value, dict):
        return {**value, "changed": True}
    return "changed"


async def _change_during_check_read(case, maker, values, call_number=2):
    revision_id = case.revision.id
    original, state = case.client.list_check_runs_for_ref, {"calls": 0}

    async def racing(owner, repo, ref):
        state["calls"] += 1
        if state["calls"] == call_number:
            async with maker() as other:
                await other.execute(_update(GithubAttemptScopeRevision)
                                    .where(GithubAttemptScopeRevision.id == revision_id).values(**values))
                await other.commit()
        return await original(owner, repo, ref)

    case.client.list_check_runs_for_ref = racing
    return state


@pytest.mark.asyncio
async def test_a_revision_turned_diagnostic_with_another_head_during_the_read_does_not_merge(twin):
    """V1's retained case: phase and submitted head change, status and completion time stay as they were."""
    db, maker = twin
    case = await settled_case(db)
    state = await _change_during_check_read(case, maker, {"phase": "diagnostic", "submitted_head_sha": OTHER})
    await case.tick()
    assert state["calls"] >= 2
    assert case.client.merge_calls == 0 and case.item.dispatch_status != "merged"


_REVISION_COLUMNS = [column.name for column in GithubAttemptScopeRevision.__table__.columns if column.name != "id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("column", _REVISION_COLUMNS)
async def test_a_change_to_any_revision_column_during_the_read_does_not_merge(twin, column):
    db, maker = twin
    case = await settled_case(db)
    row = (await db.execute(_select(GithubAttemptScopeRevision.__table__)
                            .where(GithubAttemptScopeRevision.id == case.revision.id))).mappings().one()
    state = await _change_during_check_read(case, maker, {column: _changed(row[column], GithubAttemptScopeRevision.__table__.c[column])})
    await case.tick()
    assert state["calls"] >= 2
    assert case.client.merge_calls == 0 and case.item.dispatch_status != "merged"


@pytest.mark.asyncio
async def test_an_operation_that_began_on_a_settled_diagnostic_is_not_blocked_by_its_phase(db):
    case = await settled_case(db)
    await db.execute(_update(GithubAttemptScopeRevision).where(GithubAttemptScopeRevision.id == case.revision.id)
                     .values(phase="diagnostic", submitted_head_sha=OTHER))
    await db.commit()
    expected = await service._settled_snapshot(db, case.scope, case.item)
    assert expected is not None
    assert await service._settled_context_note(db, case.scope, case.item, HEAD, expected) is None
    await db.execute(_update(GithubAttemptScopeRevision).where(GithubAttemptScopeRevision.id == case.revision.id)
                     .values(phase="implementation"))
    await db.commit()
    assert await service._settled_context_note(db, case.scope, case.item, HEAD, expected) is not None


@pytest.mark.asyncio
async def test_a_revision_row_deleted_during_the_operation_blocks_the_merge(db):
    case = await settled_case(db)
    expected = await service._settled_snapshot(db, case.scope, case.item)
    await db.execute(_delete(GithubAttemptScopeRevision).where(GithubAttemptScopeRevision.id == case.revision.id))
    await db.commit()
    assert await service._settled_context_note(db, case.scope, case.item, HEAD, expected) is not None
