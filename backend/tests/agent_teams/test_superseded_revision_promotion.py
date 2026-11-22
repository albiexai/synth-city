"""#972: an open PR on a superseded revision is promoted and auto-merged on an exact-head acceptance.

A Leader settlement of a negative review ends the submitted revision as superseded and leaves the item 'dispatched'
with no owner acknowledgement. The owner can then push a head the reviewer accepts, but #957 only promotes a
completed revision on a 'verifying' item, so an accepted, green, ready and unheld PR was never promoted and a person
merged it by hand. The superseded revision alone grants nothing: the exact-head acceptance and every merge gate do.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.config import settings
from app.models.database import (
    AgentTeamPreset, AgentTeamSlot, GithubAttemptScopeRevision, GithubHeadReview, GithubOwnerObservationPause,
    GithubWorkItem, GithubWorkspace, MailTeamMember, TeamGithubScope,
)
from app.services import github_coordination_service as coordination
from app.services import github_head_acceptance_service as acceptance
from app.services.github_approval_service import github_approval_service
from app.services.github_ci_watch_service import github_ci_watch_service as ci_watch
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
    await db.execute(update(AgentTeamPreset).where(AgentTeamPreset.id == scope.preset_id).values(autonomy_enabled=True))
    await db.commit()
    slot, member = await _owner(db, scope)
    stopped = shape == "stopped"
    item = await _item(db, scope, dispatch_status=status, pr_number=5, owner_slot_id=slot.id,
                       dispatch_nonce="attempt-1", active_scope_revision=4, attempt_phase="implementation",
                       ack_received_at=None if stopped else datetime.utcnow(), last_verified_sha=OLD,
                       handoff_state=handoff_state)
    revision, _workspace = await _implementation_revision(db, scope, item, slot, member)
    await db.execute(update(GithubAttemptScopeRevision).where(GithubAttemptScopeRevision.id == revision.id).values(
        status="superseded", completed_at=ENDED_AT, submitted_head_sha=OLD,
        expected_lease_token_hash=github_approval_service.lease_token_hash(f"lease-{item.id}"),
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
    case = Case(db, scope, item, revision, client)
    # Plain ids, read now: a poll expires the ORM objects and a second connection must not touch them mid-poll.
    case.ids = {"item": item.id, "scope": scope.id, "team": scope.preset_id, "owner_slot": slot.id}
    return case


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


# --- V1 review 5923921938: the former-verifier case through this observer ------------------------------------------


async def _demote_the_accepting_reviewer(db):
    member_id = await db.scalar(select(GithubHeadReview.reviewer_member_id))
    slot_id = await db.scalar(select(MailTeamMember.team_slot_id).where(MailTeamMember.id == member_id))
    await db.execute(update(AgentTeamSlot).where(AgentTeamSlot.id == slot_id).values(role="implementer"))
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["stopped", "plain"])
async def test_an_acceptance_from_a_reviewer_who_is_no_longer_a_verifier_does_not_promote(db, shape):
    case = await superseded_case(db, shape=shape)
    await _demote_the_accepting_reviewer(db)
    await case.tick()
    assert case.item.dispatch_status == "dispatched" and case.client.merge_calls == 0
    assert case.item.last_verified_sha == OLD
    assert "no longer from a current verifier" in case.item.status_note
    _untouched(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["stopped", "plain"])
async def test_the_observer_consults_the_acceptance_gate(db, monkeypatch, shape):
    """The control: with the entry gate forced open the former-verifier fixture is promoted, so the gate is what
    stops it at entry; and the final fence, which reads the verifier role itself, still refuses the merge."""
    monkeypatch.setattr(acceptance, "gate_note", AsyncMock(return_value=None))
    case = await superseded_case(db, shape=shape)
    await _demote_the_accepting_reviewer(db)
    await case.tick()
    assert case.item.last_verified_sha == HEAD and case.item.dispatch_status == "ready_for_review"
    assert case.client.merge_calls == 0


# --- V1 review 5923921938: authority withdrawn after the last awaited read --------------------------------------------

REQUIRED = ["Full backend regression tests", "Product CI gate", "Changed Python undefined names", "e2e",
            "Rename old-name guard"]
LATE = ["handoff", "escalated", "revision_pointer", "phase", "scope_disabled", "human_policy", "autonomy_off",
        "paused", "hold", "reviewer_role", "acceptance_negative", "approval_round", "workspace_release",
        # V1 review 5924614352: current participants, a re-acquired lease and every column of the revision row
        "verifier_replaced", "leader_replaced", "owner_disabled", "workspace_reacquired", "budget_changed",
        "revision_summary_changed"]


async def _configured_case(db, monkeypatch):
    """The superseded fixture under a modern delivery policy: all five required job identities, autonomy on."""
    monkeypatch.setattr(ci_watch, "_wake", AsyncMock(return_value=True))
    case = await superseded_case(db)
    case.item.delivery_policy_revision = 2
    case.item.delivery_policy = {"required_checks": [{"name": n, "app_slug": "github-actions"} for n in REQUIRED],
                                 "broad_checks": "hosted"}
    case.client.check_runs = [{"name": n, "app": {"slug": "github-actions"}, "head_sha": HEAD, "status": "completed",
                               "conclusion": "success"} for n in REQUIRED]
    await db.execute(update(AgentTeamPreset).where(AgentTeamPreset.id == case.scope.preset_id)
                     .values(autonomy_enabled=True))
    await db.commit()
    return case


async def _commit_change(case, maker, kind, monkeypatch, fact_id, slot_id):
    """Commit one withdrawal of authority through a second connection."""
    item_id, scope_id, team_id = case.ids["item"], case.ids["scope"], case.ids["team"]
    async with maker() as other:
        item = update(GithubWorkItem).where(GithubWorkItem.id == item_id)
        changes = {
            "handoff": item.values(handoff_state="pending"),
            "escalated": item.values(dispatch_status="escalated"),
            "revision_pointer": item.values(active_scope_revision=5),
            "phase": item.values(attempt_phase="diagnostic"),
            "approval_round": item.values(approval_round_count=2),
            "scope_disabled": update(TeamGithubScope).where(TeamGithubScope.id == scope_id).values(enabled=False),
            "human_policy": update(TeamGithubScope).where(TeamGithubScope.id == scope_id).values(merge_policy="human"),
            "autonomy_off": update(AgentTeamPreset).where(AgentTeamPreset.id == team_id)
            .values(autonomy_enabled=False),
            "reviewer_role": update(AgentTeamSlot).where(AgentTeamSlot.id == slot_id).values(role="implementer"),
            "acceptance_negative": update(GithubHeadReview).where(GithubHeadReview.id == fact_id)
            .values(verdict="changes_required"),
            "workspace_release": update(GithubWorkspace).where(GithubWorkspace.leased_item_id == item_id)
            .values(leased_item_id=None, lease_token=None),
            "workspace_reacquired": update(GithubWorkspace).where(GithubWorkspace.leased_item_id == item_id)
            .values(lease_token="a-new-acquisition", leased_at=datetime.utcnow() + timedelta(seconds=5)),
            "owner_disabled": update(AgentTeamSlot).where(AgentTeamSlot.id == case.ids["owner_slot"])
            .values(enabled=False),
            "budget_changed": update(GithubAttemptScopeRevision).where(
                GithubAttemptScopeRevision.work_item_id == item_id).values(failed_head_count=1),
            "revision_summary_changed": update(GithubAttemptScopeRevision).where(
                GithubAttemptScopeRevision.work_item_id == item_id).values(summary="A changed summary"),
        }
        if kind in ("verifier_replaced", "leader_replaced"):
            replaced_slot = slot_id if kind == "verifier_replaced" else await other.scalar(
                select(AgentTeamPreset.leader_slot_id).where(AgentTeamPreset.id == team_id))
            other.add(MailTeamMember(
                identity_key=f"slot:{replaced_slot}:replacement", repo_id="r", repo_path="/tmp/r", repo_name="r",
                display_name="Replacement", participant_kind="team_slot", team_preset_id=team_id,
                team_slot_id=replaced_slot))
        elif kind == "paused":
            other.add(GithubOwnerObservationPause(
                work_item_id=item_id, wait_key="a-late-pause", authority_sha256="0" * 64, status="paused",
                deadline=datetime.utcnow() + timedelta(minutes=1)))
        elif kind == "hold":
            monkeypatch.setattr(coordination, "hold_code", lambda: "hold")
        else:
            await other.execute(changes[kind])
        await other.commit()


def _late_change(case, maker, kind, monkeypatch, fact_id, slot_id):
    """Commit the change while the last issue-label read is awaited."""
    original, state = case.client.get_issue, {"calls": 0}

    async def get_issue(*args, **kwargs):
        state["calls"] += 1
        result = await original(*args, **kwargs)
        if state["calls"] == 2 and kind != "never":
            await _commit_change(case, maker, kind, monkeypatch, fact_id, slot_id)
        return result

    case.client.get_issue = get_issue
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", LATE + ["never"])
async def test_authority_withdrawn_at_the_last_label_read_does_not_merge(twin, monkeypatch, kind):
    db, maker = twin
    case = await _configured_case(db, monkeypatch)
    fact = (await db.execute(select(GithubHeadReview))).scalar_one()
    slot_id = await db.scalar(select(MailTeamMember.team_slot_id).where(MailTeamMember.id == fact.reviewer_member_id))
    state = _late_change(case, maker, kind, monkeypatch, fact.id, slot_id)
    await case.tick()
    assert state["calls"] == 2, (kind, state, case.item.status_note)
    if kind == "never":  # the control: nothing changes, and the configured five-job fixture merges
        assert case.item.dispatch_status == "merged" and case.client.merge_calls == 1
    else:
        assert case.client.merge_calls == 0, (kind, case.item.dispatch_status, case.item.status_note)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["handoff", "paused", "workspace_release", "human_policy", "autonomy_off",
                                  "approval_round", "reviewer_role", "acceptance_negative", "hold",
                                  "verifier_replaced", "leader_replaced", "owner_disabled", "workspace_reacquired",
                                  "budget_changed", "revision_summary_changed"])
async def test_authority_withdrawn_at_the_final_local_boundary_does_not_merge(twin, monkeypatch, kind):
    """The change lands after the last awaited check of the merge path and before the merge call."""
    db, maker = twin
    case = await _configured_case(db, monkeypatch)
    fact = (await db.execute(select(GithubHeadReview))).scalar_one()
    slot_id = await db.scalar(select(MailTeamMember.team_slot_id).where(MailTeamMember.id == fact.reviewer_member_id))
    original = service._settled_context_note
    state = {"calls": 0}

    async def boundary(*args, **kwargs):
        note = await original(*args, **kwargs)
        state["calls"] += 1
        await _commit_change(case, maker, kind, monkeypatch, fact.id, slot_id)
        return note

    monkeypatch.setattr(service, "_settled_context_note", boundary)
    await case.tick()
    assert state["calls"] == 1, (kind, state)
    assert case.client.merge_calls == 0, (kind, case.item.dispatch_status, case.item.status_note)


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["stopped", "plain"])
async def test_a_missing_lease_at_entry_is_refused_precisely(db, shape):
    case = await superseded_case(db, shape=shape)
    await db.execute(update(GithubWorkspace).where(GithubWorkspace.leased_item_id == case.item.id)
                     .values(leased_item_id=None, lease_token=None))
    await db.commit()
    await case.tick()
    assert case.item.dispatch_status == "dispatched" and case.item.last_verified_sha == OLD
    assert "workspace lease is missing" in case.item.status_note
    _untouched(case)


@pytest.mark.asyncio
async def test_an_item_that_carries_an_escalation_reason_is_refused_at_entry(db):
    case = await superseded_case(db)
    await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == case.item.id)
                     .values(escalation_reason="retry_count_exhausted"))
    await db.commit()
    await case.tick()
    assert case.item.dispatch_status == "dispatched" and case.item.last_verified_sha == OLD
    assert "escalation" in case.item.status_note
    _untouched(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["stopped", "plain"])
async def test_a_replaced_acquisition_at_entry_is_refused_precisely(db, shape):
    """A re-acquired workspace has a new token while the revision keeps the hash it was approved with."""
    case = await superseded_case(db, shape=shape)
    await db.execute(update(GithubWorkspace).where(GithubWorkspace.leased_item_id == case.item.id)
                     .values(lease_token="a-new-acquisition", leased_at=datetime.utcnow() + timedelta(seconds=5)))
    await db.commit()
    await case.tick()
    assert case.item.dispatch_status == "dispatched" and case.item.last_verified_sha == OLD
    assert "acquired again" in case.item.status_note
    _untouched(case)


@pytest.mark.asyncio
async def test_a_refusal_note_never_overwrites_a_replaced_attempt(twin):
    """An already promoted item is replaced by a new dispatch while the capture is awaited."""
    db, maker = twin
    case = await superseded_case(db, status="ready_for_review")
    await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == case.item.id).values(last_verified_sha=HEAD))
    await db.execute(update(GithubWorkspace).where(GithubWorkspace.leased_item_id == case.item.id)
                     .values(leased_item_id=None, lease_token=None))
    await db.commit()
    item_id = case.ids["item"]
    original = service._capture_superseded_fence

    async def replaced_during_capture(*args, **kwargs):
        refusal = await original(*args, **kwargs)
        assert isinstance(refusal, str), refusal  # the old attempt's lease is gone, so the capture refuses
        async with maker() as other:
            await other.execute(update(GithubWorkItem).where(GithubWorkItem.id == item_id).values(
                dispatch_nonce="a-newer-attempt", status_note="The new owner's own note"))
            await other.commit()
        return refusal

    service._capture_superseded_fence = replaced_during_capture
    try:
        await case.tick()
    finally:
        del service._capture_superseded_fence  # back to the class method
    assert case.item.dispatch_nonce == "a-newer-attempt"
    assert case.item.status_note == "The new owner's own note"
    assert case.client.merge_calls == 0
