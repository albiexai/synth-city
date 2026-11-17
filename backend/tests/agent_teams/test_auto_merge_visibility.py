"""#939: say why an accepted PR was not merged automatically, show cap usage, and give a settled diagnostic
escalation back to the unchanged automatic merge path.

Every gate of that path stays in force; these tests cover only what #939 adds: the recorded reason, the notices,
the usage figure and the fenced return from 'escalated'.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.api.v1.agent_teams import _scope_response, list_github_scopes
from app.models.database import (
    AgentTeamPreset, AgentTeamSlot, GithubApprovalRequest, GithubAttemptScopeRevision, GithubWorkItem, MailMessage,
    MailReceipt, MailTeamMember,
)
from app.services import github_head_acceptance_service as acceptance
from app.services.agent_mail_service import agent_mail_service
from app.services.agent_team_supervisor_service import is_supervisor_message, supervisor_inbox
from app.services.github_verification_service import github_verification_service as service
from tests.agent_teams.test_github_verification_service import (  # noqa: F401  (fixtures and helpers)
    _HoldClient, _diagnostic_revision, _http_error, _item, _scope, capability_enforcement, db,
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
    """Every not-fired notice, whichever side it is for: the Leader's carries payload kind, the Supervisor's notice."""
    rows = (await db.execute(select(MailMessage))).scalars().all()
    return [m for m in rows if (m.payload or {}).get("kind") == "github_auto_merge_not_fired"
            or (m.payload or {}).get("notice") == "auto_merge_not_fired"]


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


# ---- review of PR #953: real Supervisor delivery, receipts, and one transaction ----

async def _leader_member(db, scope):
    preset = await db.get(AgentTeamPreset, scope.preset_id)
    return await db.scalar(select(MailTeamMember).where(MailTeamMember.team_slot_id == preset.leader_slot_id))


async def _leader_receipts(db, scope, *, with_payload_key):
    leader = await _leader_member(db, scope)
    rows = (await db.execute(select(MailMessage).join(MailReceipt, MailReceipt.message_id == MailMessage.id)
        .where(MailReceipt.member_id == leader.id))).scalars().all()
    return [m for m in rows if with_payload_key in (m.payload or {}) or (m.payload or {}).get("kind") == with_payload_key]


async def _not_fired_state(db, state, **kwargs):
    """The two places a notice arises: an escalated item that is not eligible, and a ready item at the cap."""
    scope, item, _revision, client = await _escalated(db, settled=False, cap=0, **kwargs)
    if state == "ready":
        item.dispatch_status = "ready_for_review"
        item.escalation_reason = None
        item.last_verified_sha = HEAD
        await db.commit()
    return scope, item, client


def _leader_side(notices, state):
    """The Leader-side notice: the new one for an escalated item, the existing ready notice for a ready one."""
    return [m for m in notices if (m.payload or {}).get("kind") == "github_auto_merge_not_fired"]


async def _ready_notices(db):
    rows = (await db.execute(select(MailMessage))).scalars().all()
    return [m for m in rows if m.subject == "Code PR ready for review" and "auto_merge_not_fired" in (m.payload or {})]


async def _note(db, item_id):
    return await db.scalar(select(GithubWorkItem.status_note).where(GithubWorkItem.id == item_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["escalated", "ready"])
async def test_the_bound_supervisor_sees_exactly_one_notice_and_nobody_else_does(db, state):
    scope, item, client = await _not_fired_state(db, state)
    await _poll(db, scope, client, times=3)

    seen = await supervisor_inbox(db, SimpleNamespace(id=6))
    assert len(seen) == 1
    assert seen[0].payload["notice"] == "auto_merge_not_fired" and seen[0].payload["work_item_id"] == item.id
    with pytest.raises(PermissionError):
        await supervisor_inbox(db, SimpleNamespace(id=9))  # an actor bound to no team
    other = AgentTeamPreset(name="Other", description="", created_by="t", supervisor_actor_id=7)
    db.add(other)
    await db.commit()
    assert await supervisor_inbox(db, SimpleNamespace(id=7)) == []  # another team's Supervisor sees nothing of it


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["escalated", "ready"])
async def test_the_leader_receives_the_notice_once_and_never_the_operator_row(db, state):
    scope, _item, client = await _not_fired_state(db, state)
    await _poll(db, scope, client, times=3)
    assert len(await _leader_receipts(db, scope, with_payload_key="auto_merge_not_fired" if state == "ready" else "github_auto_merge_not_fired")) == 1
    assert await _leader_receipts(db, scope, with_payload_key="notice") == []  # the operator row has no member receipts
    operator = _to_operator(await _notices(db))
    assert len(operator) == 1 and (await db.execute(select(MailReceipt).where(MailReceipt.message_id == operator[0].id))).first() is None


def _row(**overrides):
    base = dict(
        kind="message", audience_type="team_preset", audience_id="1", thread_root_id=None, sender_type="system",
        sender_member_id=None, sender_actor_id=None, recipient_member_id=None, subject="Auto-merge did not fire",
        payload={"notice": "auto_merge_not_fired", "work_item_id": 3, "pr_number": 5, "reason": "auto_merge_cap_reached",
                 "detail": "1 of 1 automatic merges in the last 24 hours", "head_sha": HEAD, "supervisor_actor_id": 6})
    base.update(overrides)
    return SimpleNamespace(**base)


def test_the_supervisor_recognizer_accepts_only_the_exact_notice_shape():
    assert is_supervisor_message(_row()) is True
    good = _row().payload
    assert is_supervisor_message(_row(payload={**good, "head_sha": None})) is True
    for bad in (
        _row(payload={**good, "extra": 1}),
        _row(payload={k: v for k, v in good.items() if k != "detail"}),
        _row(payload={**good, "reason": "anything_else"}),
        _row(payload={**good, "work_item_id": "3"}),
        _row(payload={**good, "pr_number": 0}),
        _row(payload={**good, "supervisor_actor_id": True}),
        _row(payload={**good, "detail": ""}),
        _row(subject="Something else"),
        _row(sender_type="member"),
        _row(sender_actor_id=6),
        _row(recipient_member_id=4),
        _row(thread_root_id=2),
        _row(audience_type="work_item"),
        _row(kind="broadcast"),
    ):
        assert is_supervisor_message(bad) is False, bad.payload


class _Boom(RuntimeError):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["escalated", "ready"])
@pytest.mark.parametrize("boundary", ["operator_row", "before_leader_send", "after_leader_commit"])
async def test_a_failure_at_any_boundary_leaves_nothing_half_done_and_the_next_poll_completes_it(
        db, monkeypatch, state, boundary):
    scope, item, client = await _not_fired_state(db, state)
    item_id = item.id  # a rollback expires the objects; keep plain values
    original_send = agent_mail_service.send_broadcast
    original_add = service._add_operator_not_fired
    armed = {"add": boundary == "operator_row", "send": boundary != "operator_row"}

    async def send(*args, **kwargs):
        if kwargs.get("audience_type") == "work_item" and armed["send"]:
            armed["send"] = False
            if boundary == "before_leader_send":
                raise _Boom("leader notice not sent")
            result = await original_send(*args, **kwargs)
            raise _Boom("failed after the commit")
        return await original_send(*args, **kwargs)

    async def add(*args, **kwargs):
        if armed["add"]:
            armed["add"] = False
            raise _Boom("operator row not added")
        return await original_add(*args, **kwargs)

    monkeypatch.setattr(agent_mail_service, "send_broadcast", send)
    monkeypatch.setattr(service, "_add_operator_not_fired", add)

    with pytest.raises(_Boom) as raised:
        await _poll(db, scope, client)
    assert raised.traceback[-1].name in {"add", "send"}  # it failed at the boundary under test, not earlier
    await db.rollback()
    await db.refresh(scope)

    note = await _note(db, item_id)
    code = "item_not_eligible" if state == "escalated" else "auto_merge_cap_reached"
    if boundary == "after_leader_commit":
        assert code in note  # the single commit already took the note and both notices
    else:
        assert "auto_merge" not in (note or "")  # no note without its notices: a sticky note cannot hide a lost one
        assert await _notices(db) == [] and await _ready_notices(db) == []

    await _poll(db, scope, client, times=3)  # the recovery polls, and repeats

    assert code in await _note(db, item_id)
    notices = await _notices(db)
    assert len(_to_operator(notices)) == 1  # exactly one for the Supervisor, however many attempts
    if state == "escalated":
        assert len(_leader_side(notices, state)) == 1
    else:
        assert len(await _ready_notices(db)) == 1


# ---- #1037: an accepted, green, unheld PR is not left for a human by a cause that has cleared ----

PENDING = {"name": "optional", "status": "in_progress", "conclusion": None}
DONE = {"name": "optional", "status": "completed", "conclusion": "success"}


@pytest.fixture
def clock(monkeypatch):
    import app.services.github_verification_service as verification_module

    class Clock(datetime):
        now = datetime(2026, 11, 17, 12, 0, 0)

        @classmethod
        def utcnow(cls):
            return cls.now

    monkeypatch.setattr(verification_module, "datetime", Clock)
    return Clock


async def _ready_messages(db):
    """Every "Code PR ready for review" message, whether or not it carries a not-fired reason."""
    rows = (await db.execute(select(MailMessage))).scalars().all()
    return [m for m in rows if m.subject == "Code PR ready for review"]


async def _ready(db, *, cap=5):
    scope = await _scope(db, merge_policy="auto", max_auto_merges_per_day=cap, review_hold_label="review-hold")
    item = await _item(db, scope, dispatch_status="ready_for_review", pr_number=5, last_verified_sha=HEAD)
    return scope, item, _Client(head=HEAD)


async def _stalled(db, clock):
    """An item whose merge wait ran out: a check stayed pending on the verified head for 60 minutes."""
    scope, item, client = await _ready(db)
    client.check_runs = SUCCESS + [PENDING]
    started = clock.now
    for elapsed in (0, 60):
        clock.now = started + timedelta(minutes=elapsed)
        await _poll(db, scope, client)
    await db.refresh(item)
    assert item.status_note.startswith("Auto-merge blocked: GitHub checks stalled")
    assert len(await _ready_messages(db)) == 1
    return scope, item, client


@pytest.mark.asyncio
async def test_a_check_rerun_long_after_an_earlier_wait_gets_a_fresh_wait_not_a_false_stall(db, clock):
    scope, item, client = await _ready(db)
    started = clock.now
    client.check_runs = SUCCESS + [PENDING]
    await _poll(db, scope, client)  # the first wait on this head
    clock.now = started + timedelta(minutes=20)
    client.check_runs = SUCCESS + [DONE]
    client.merge_error = _http_error(409)
    await _poll(db, scope, client)  # the checks finished; GitHub refused the merge for now
    await db.refresh(item)
    assert item.status_note.startswith("Transient merge failure")
    clock.now = started + timedelta(minutes=90)
    client.check_runs = SUCCESS + [PENDING]  # a check on the same head is run again
    await _poll(db, scope, client)
    await db.refresh(item)
    assert "stalled" not in item.status_note
    assert "waiting for checks" in item.status_note
    assert item.dispatch_status == "ready_for_review"
    assert await _ready_messages(db) == []


@pytest.mark.asyncio
async def test_a_capped_item_merges_once_capacity_returns_and_is_not_notified_again(db):
    scope, item, client = await _ready(db, cap=1)
    preset = await db.get(AgentTeamPreset, scope.preset_id)
    preset.supervisor_actor_id = 6
    earlier = await _item(db, scope, issue_number=2, dispatch_status="merged", pr_number=4,
                          auto_merged_at=datetime.utcnow() - timedelta(minutes=5))
    await _poll(db, scope, client, times=2)
    await db.refresh(item)
    assert "[auto_merge_cap_reached: 1 of 1" in item.status_note and client.merge_calls == 0

    earlier.auto_merged_at = datetime.utcnow() - timedelta(hours=25)  # the 24-hour window has moved on
    await db.commit()
    await _poll(db, scope, client)
    await db.refresh(item)
    assert item.dispatch_status == "merged" and client.merge_head_shas == [HEAD]
    assert len(await _ready_messages(db)) == 1
    assert len(_to_operator(await _notices(db))) == 1


@pytest.mark.asyncio
async def test_a_capped_item_still_passes_every_gate_when_offered_again(db):
    scope, item, client = await _ready(db, cap=1)
    earlier = await _item(db, scope, issue_number=2, dispatch_status="merged", pr_number=4,
                          auto_merged_at=datetime.utcnow() - timedelta(minutes=5))
    await _poll(db, scope, client)
    earlier.auto_merged_at = datetime.utcnow() - timedelta(hours=25)
    await db.commit()
    client.issue["labels"] = [{"name": "review-hold"}]
    await _poll(db, scope, client)
    await db.refresh(item)
    assert client.merge_calls == 0
    assert "held for independent review" in item.status_note  # the note names the cause that applies now


@pytest.mark.asyncio
async def test_a_stalled_item_merges_once_the_same_heads_checks_are_green(db, clock):
    scope, item, client = await _stalled(db, clock)
    note = item.status_note
    clock.now += timedelta(minutes=5)
    await _poll(db, scope, client)  # still pending: stays with a human, nothing new is sent
    await db.refresh(item)
    assert item.status_note == note and client.merge_calls == 0
    client.check_runs = SUCCESS + [DONE]
    clock.now += timedelta(minutes=5)
    await _poll(db, scope, client)
    await db.refresh(item)
    assert item.dispatch_status == "merged" and client.merge_head_shas == [HEAD]
    assert len(await _ready_messages(db)) == 1


@pytest.mark.asyncio
async def test_a_stalled_item_with_a_pending_commit_status_stays_with_a_human(db, clock):
    scope, item, client = await _stalled(db, clock)
    note = item.status_note
    client.check_runs = SUCCESS + [DONE]
    client.combined_status = {"state": "pending", "statuses": [{"context": "external", "state": "pending"}]}
    clock.now += timedelta(minutes=5)
    await _poll(db, scope, client, times=2)
    await db.refresh(item)
    assert item.status_note == note and client.merge_calls == 0
    assert len(await _ready_messages(db)) == 1


@pytest.mark.parametrize("moved", ["pull", "verified"])
@pytest.mark.asyncio
async def test_a_stalled_item_whose_head_moved_is_not_offered_again(db, clock, moved):
    scope, item, client = await _stalled(db, clock)
    note = item.status_note
    client.pull["head"]["sha"] = OTHER
    if moved == "verified":  # a later head was verified, but the stall was recorded for the earlier one
        item.last_verified_sha = OTHER
        await db.commit()
    client.check_runs = list(SUCCESS)
    await _poll(db, scope, client)
    await db.refresh(item)
    assert item.status_note == note and client.merge_calls == 0


@pytest.mark.parametrize("note", [
    "Auto-merge blocked: distinct current-round leader approval is required (approval is missing).",
    "Auto-merge blocked by repository policy; requires human merge.",
    "Auto-merge failed with GitHub status 500; requires human merge.",
    "Auto-merge retry budget exhausted after transient merge failure: mergeable_state=blocked",
])
@pytest.mark.asyncio
async def test_other_human_merge_fallbacks_stay_with_a_human(db, note):
    scope, item, client = await _ready(db)
    item.status_note = note
    await db.commit()
    await _poll(db, scope, client, times=2)
    await db.refresh(item)
    assert item.status_note == note and client.merge_calls == 0
    assert await _ready_messages(db) == []
