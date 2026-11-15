"""#939: say why an accepted PR was not merged automatically, show cap usage, and give a settled diagnostic
escalation back to the unchanged automatic merge path.

Every gate of that path stays in force; these tests cover only what #939 adds: the recorded reason, the notices,
the usage figure and the fenced return from 'escalated'.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.api.v1.agent_teams import _scope_response, list_github_scopes
from app.models.database import (
    AgentTeamPreset, AgentTeamSlot, GithubApprovalRequest, GithubAttemptScopeRevision, GithubWorkItem, MailMessage, MailTeamMember,
)
from app.services import github_head_acceptance_service as acceptance
from app.services.github_verification_service import github_verification_service as service
from tests.agent_teams.test_github_verification_service import (  # noqa: F401  (fixtures and helpers)
    _HoldClient, _diagnostic_revision, _item, _scope, capability_enforcement, db,
)

HEAD = "a" * 40
OTHER = "b" * 40
SUCCESS = [{"name": "ci", "status": "completed", "conclusion": "success"}]


@pytest.fixture(autouse=True)
def gate(monkeypatch):
    """The reviewer-acceptance gate is covered in test_head_acceptance.py; here it answers 'accepted' unless a test says no."""
    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(acceptance, "gate_note", mock)
    return mock


class _Client(_HoldClient):
    def __init__(self, head=HEAD, **kwargs):
        super().__init__(check_runs=SUCCESS, **kwargs)
        self.pull["head"]["sha"] = head
        self.before_issue_read = None  # a hook that runs while GitHub is awaited

    async def get_issue(self, owner, repo, number):
        if self.before_issue_read is not None:
            await self.before_issue_read()
        return await super().get_issue(owner, repo, number)


async def _escalated(db, *, settled=True, cap=10, reason="retry_count_exhausted", head=HEAD, **item_values):
    """An escalated item with an open PR whose head the reviewer accepted, after a completed diagnostic revision."""
    scope = await _scope(db, merge_policy="auto", max_auto_merges_per_day=cap, review_hold_label="review-hold")
    preset = await db.get(AgentTeamPreset, scope.preset_id)
    preset.autonomy_enabled = True
    preset.supervisor_actor_id = 6
    item = await _item(
        db, scope, dispatch_status="escalated", issue_type="code", pr_number=5, dispatch_nonce="nonce-1",
        escalation_reason=reason, attempt_phase="implementation", active_scope_revision=0,
        last_verified_sha=head, **item_values,
    )
    revision = None
    if settled:
        member = await db.scalar(select(MailTeamMember).where(MailTeamMember.team_slot_id == item.owner_slot_id))
        slot = await db.get(AgentTeamSlot, item.owner_slot_id)
        revision, _workspace = await _diagnostic_revision(db, scope, item, slot, member)
        revision.revision = 1
        revision.status = "completed"
        revision.completed_at = datetime.utcnow()
        revision.originating_escalation_reason = reason
        revision.evidence = {"version": 1, "diagnostic_completion": {"restored": True}}
    await db.commit()
    return scope, item, revision, _Client(head=head)


async def _notices(db):
    rows = (await db.execute(select(MailMessage))).scalars().all()
    return [m for m in rows if (m.payload or {}).get("kind") == "github_auto_merge_not_fired"]


def _to_operator(messages):
    return [m for m in messages if m.audience_type == "team_preset"]


def _to_leader_side(messages):
    return [m for m in messages if m.audience_type == "work_item"]


async def _poll(db, scope, client, times=1):
    for _ in range(times):
        await service.process_scope(db, scope, client=client)


# ---- the daily cap on a ready item ----

@pytest.mark.asyncio
async def test_cap_reached_records_the_reason_and_sends_one_notice_to_each_side(db):
    scope = await _scope(db, merge_policy="auto", max_auto_merges_per_day=1)
    preset = await db.get(AgentTeamPreset, scope.preset_id)
    preset.supervisor_actor_id = 6
    await _item(db, scope, issue_number=2, dispatch_status="merged", pr_number=4,
                auto_merged_at=datetime.utcnow() - timedelta(minutes=5))
    item = await _item(db, scope, dispatch_status="ready_for_review", pr_number=5, last_verified_sha="sha")
    client = _Client(head="sha")

    await _poll(db, scope, client, times=3)  # repeated polls

    await db.refresh(item)
    assert item.dispatch_status == "ready_for_review" and client.merge_calls == 0
    assert "Auto-merge budget exhausted" in item.status_note
    assert "[auto_merge_cap_reached: 1 of 1 automatic merges in the last 24 hours]" in item.status_note
    ready = [m for m in (await db.execute(select(MailMessage))).scalars() if m.subject == "Code PR ready for review"]
    assert len(ready) == 1  # the Leader-side notice, sent once
    assert ready[0].payload["auto_merge_not_fired"] == {
        "code": "auto_merge_cap_reached", "detail": "1 of 1 automatic merges in the last 24 hours", "head_sha": "sha"}
    operator = _to_operator(await _notices(db))
    assert len(operator) == 1 and operator[0].payload["supervisor_actor_id"] == 6
    assert operator[0].payload["reason"] == "auto_merge_cap_reached"


@pytest.mark.asyncio
async def test_no_operator_notice_without_a_bound_supervisor(db):
    scope = await _scope(db, merge_policy="auto", max_auto_merges_per_day=0)
    await _item(db, scope, dispatch_status="ready_for_review", pr_number=5, last_verified_sha="sha")
    await _poll(db, scope, _Client(head="sha"))
    assert await _notices(db) == []


@pytest.mark.asyncio
async def test_a_merge_below_the_cap_still_happens_and_sends_no_not_fired_notice(db):
    scope = await _scope(db, merge_policy="auto", max_auto_merges_per_day=2)
    item = await _item(db, scope, dispatch_status="ready_for_review", pr_number=5, last_verified_sha="sha")
    client = _Client(head="sha")
    await _poll(db, scope, client)
    await db.refresh(item)
    assert item.dispatch_status == "merged" and client.merge_calls == 1
    assert await _notices(db) == []


# ---- usage ----

@pytest.mark.asyncio
async def test_usage_counts_only_this_scopes_merges_of_the_last_24_hours(db):
    scope = await _scope(db, merge_policy="auto", max_auto_merges_per_day=5)
    other = await _scope(db, merge_policy="auto", repo_name="other", repo_path="/tmp/other")
    now = datetime.utcnow()
    for number, (target, age) in enumerate([(scope, 1), (scope, 23), (scope, 25), (other, 1)], start=10):
        await _item(db, target, issue_number=number, dispatch_status="merged", pr_number=number,
                    auto_merged_at=now - timedelta(hours=age))
    assert await service.auto_merges_last_24h(db, scope.id) == 2
    assert await service.auto_merges_last_24h(db, other.id) == 1
    assert await service._auto_merge_usage(db, scope) == (2, 5)


@pytest.mark.asyncio
async def test_the_scope_list_carries_usage_and_the_cap(db):
    scope = await _scope(db, merge_policy="auto", max_auto_merges_per_day=4)
    await _item(db, scope, issue_number=2, dispatch_status="merged", pr_number=4,
                auto_merged_at=datetime.utcnow() - timedelta(hours=2))
    listed = await list_github_scopes(scope.preset_id, db=db)
    assert [(s.max_auto_merges_per_day, s.auto_merges_last_24h) for s in listed.scopes] == [(4, 1)]
    assert _scope_response(scope).auto_merges_last_24h is None  # a response built without the count says so


# ---- an escalated item ----

@pytest.mark.asyncio
async def test_an_eligible_settled_diagnostic_escalation_merges_the_accepted_head(db, gate):
    scope, item, revision, client = await _escalated(db)

    await _poll(db, scope, client)  # given back to the automatic path
    await db.refresh(item)
    assert item.dispatch_status == "ready_for_review" and item.escalation_reason is None
    assert client.merge_calls == 0
    await db.refresh(revision)
    assert revision.evidence["auto_merge_reopened"] == {"head_sha": HEAD}
    assert revision.evidence["diagnostic_completion"] == {"restored": True}  # the diagnostic record is kept

    await _poll(db, scope, client)  # the unchanged path merges it
    await db.refresh(item)
    assert item.dispatch_status == "merged" and item.auto_merged_at is not None
    assert client.merge_head_shas == [HEAD]
    assert any(call.args[3] == HEAD for call in gate.await_args_list)  # the exact head was put to the gate
    assert await _notices(db) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "head_changed", "not_accepted", "hold_label", "draft", "no_leader_approval", "human_policy", "already_given_back",
    "pending_approval", "design_issue", "foreign_pr", "other_pr_number",
])
async def test_every_other_case_stays_escalated_and_silent(db, gate, case):
    scope, item, revision, client = await _escalated(db)
    if case == "head_changed":
        client.pull["head"]["sha"] = OTHER
    elif case == "not_accepted":
        gate.return_value = "Waiting for reviewer acceptance."
    elif case == "hold_label":
        client.pull["labels"] = [{"name": "review-hold"}]
    elif case == "draft":
        client.pull["draft"] = True
    elif case == "no_leader_approval":
        item.ack_approval_round = None
    elif case == "human_policy":
        scope.merge_policy = "human"
    elif case == "already_given_back":
        revision.evidence = {**revision.evidence, "auto_merge_reopened": {"head_sha": HEAD}}
    elif case == "pending_approval":
        db.add(GithubApprovalRequest(
            work_item_id=item.id, request_kind="continuation", dispatch_nonce="nonce-1", approval_round=1,
            owner_member_id=item.ack_approver_member_id, leader_member_id=item.ack_approver_member_id,
            request_fingerprint="f", status="pending"))
    elif case == "design_issue":
        item.issue_type = "design"
    elif case == "foreign_pr":
        client.pull["head"]["ref"] = "someone/else"
    elif case == "other_pr_number":
        client.pull["number"] = 6
    await db.commit()

    await _poll(db, scope, client, times=2)

    await db.refresh(item)
    assert item.dispatch_status == "escalated" and item.escalation_reason == "retry_count_exhausted"
    assert client.merge_calls == 0
    assert await _notices(db) == []


@pytest.mark.asyncio
async def test_an_escalation_that_is_not_a_settled_diagnostic_is_not_eligible_and_says_so_once(db):
    scope, item, _revision, client = await _escalated(db, settled=False)

    await _poll(db, scope, client, times=4)

    await db.refresh(item)
    assert item.dispatch_status == "escalated" and client.merge_calls == 0
    assert "[item_not_eligible: escalated: retry_count_exhausted]" in item.status_note
    notices = await _notices(db)
    assert len(_to_leader_side(notices)) == 1 and len(_to_operator(notices)) == 1
    assert {m.payload["reason"] for m in notices} == {"item_not_eligible"}
    assert notices[0].payload["detail"] == "escalated: retry_count_exhausted"


@pytest.mark.asyncio
async def test_a_diagnostic_for_another_escalation_is_not_a_settled_one(db):
    scope, item, revision, client = await _escalated(db)
    revision.originating_escalation_reason = "plan_blocked"
    await db.commit()
    await _poll(db, scope, client)
    await db.refresh(item)
    assert item.dispatch_status == "escalated" and "item_not_eligible" in item.status_note


@pytest.mark.asyncio
async def test_a_later_revision_means_the_diagnostic_is_no_longer_the_newest(db):
    scope, item, revision, client = await _escalated(db)
    later = GithubAttemptScopeRevision(**{
        column.name: getattr(revision, column.name) for column in revision.__table__.columns
        if column.name not in ("id", "created_at", "updated_at")})
    later.revision, later.phase, later.status = 2, "implementation", "approved"
    db.add(later)
    await db.commit()
    await _poll(db, scope, client)
    await db.refresh(item)
    assert item.dispatch_status == "escalated" and client.merge_calls == 0


@pytest.mark.asyncio
async def test_cap_reached_on_an_eligible_escalated_item_records_it_once_and_stays_escalated(db):
    scope, item, _revision, client = await _escalated(db, cap=1)
    await _item(db, scope, issue_number=2, dispatch_status="merged", pr_number=4,
                auto_merged_at=datetime.utcnow() - timedelta(minutes=5))

    await _poll(db, scope, client, times=4)

    await db.refresh(item)
    assert item.dispatch_status == "escalated" and item.escalation_reason == "retry_count_exhausted"
    assert "[auto_merge_cap_reached: 1 of 1 automatic merges in the last 24 hours]" in item.status_note
    notices = await _notices(db)
    assert len(_to_leader_side(notices)) == 1 and len(_to_operator(notices)) == 1
    assert client.merge_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["retry_count", "last_verified_sha", "pending_approval", "nonce"])
async def test_a_change_while_github_is_read_stops_the_transition(db, change):
    """The conditional update is the fence: whatever moved after the first read leaves the item escalated."""
    scope, item, revision, client = await _escalated(db)

    async def move():
        if change == "retry_count":
            await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == item.id).values(retry_count=7))
        elif change == "last_verified_sha":
            await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == item.id).values(last_verified_sha=OTHER))
        elif change == "nonce":
            await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == item.id).values(dispatch_nonce="nonce-2"))
        else:
            db.add(GithubApprovalRequest(
                work_item_id=item.id, request_kind="continuation", dispatch_nonce="nonce-1", approval_round=1,
                owner_member_id=item.ack_approver_member_id, leader_member_id=item.ack_approver_member_id,
                request_fingerprint="f", status="pending"))
        await db.commit()

    client.before_issue_read = move
    await _poll(db, scope, client)

    row = (await db.execute(select(GithubWorkItem.dispatch_status, GithubWorkItem.escalation_reason)
                            .where(GithubWorkItem.id == item.id))).one()
    assert tuple(row) == ("escalated", "retry_count_exhausted")
    await db.refresh(revision)
    assert "auto_merge_reopened" not in revision.evidence
    assert client.merge_calls == 0


@pytest.mark.asyncio
async def test_an_item_merged_outside_is_still_recorded_as_before(db):
    scope, item, _revision, client = await _escalated(db)
    client.pull.update(merged=True, state="closed", merged_at="2026-11-15T22:00:00Z")
    await _poll(db, scope, client)
    await db.refresh(item)
    assert item.dispatch_status == "merged"
    assert await _notices(db) == []
