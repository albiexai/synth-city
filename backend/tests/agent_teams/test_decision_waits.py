"""#1239 part 1: decision waits for plan approvals and continuation decisions."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, text

from app.api.v1 import agent_mail
from app.config import settings
from app.models.database import (
    FactoryAuditEvent, GithubApprovalRequest, GithubAttemptScopeRevision, GithubWorkItem, GithubWorkspace,
    MailAgentSession,
)
from app.models.schemas import MailApprovalRequestCreate
from app.services import decision_wait_service as waits
from app.services import factory_audit_service as audit
from app.services import factory_metrics_service as metrics
from app.services.github_approval_service import github_approval_service
from app.utils import peer_process
from tests.agent_mail.conftest import db  # noqa: F401  (the shared in-memory database)
from tests.agent_mail.test_api import (  # noqa: F401  (fixtures of the Mail API tests)
    _continuation_approval_fixture, _continuation_request_body, _dispatch_approval_fixture,
    _stub_continuation_github, client,
)

pytestmark = pytest.mark.asyncio


async def _events(db, kind=None):
    rows = (await db.scalars(select(FactoryAuditEvent).where(
        FactoryAuditEvent.event_kind.in_([kind] if kind else [waits.STARTED, waits.ENDED]))
        .order_by(FactoryAuditEvent.id))).all()
    return rows


async def _initial(db, *, delivered=False):
    item, members, _tokens = await _dispatch_approval_fixture(db)
    if delivered:  # the Mail route; a decision needs the delivered request
        session = await db.scalar(select(MailAgentSession).where(MailAgentSession.member_id == members[1].id))
        response = await agent_mail.request_work_item_approval(MailApprovalRequestCreate(
            work_item_id=item.id, dispatch_nonce=item.dispatch_nonce, summary="bounded plan"), session, db)
        return item, members, await db.get(GithubApprovalRequest, response.id)
    request, _created = await github_approval_service.create_initial_request(
        db, item, authenticated_owner_member_id=members[1].id, summary="bounded plan")
    return item, members, request


async def _continuation(db, client, monkeypatch):
    monkeypatch.setattr(settings, "mail_capability_tokens_required", True)
    monkeypatch.setattr(peer_process, "pane_is_alive", lambda _pid, _start: True)
    item, _scope, members, tokens, _workspace = await _continuation_approval_fixture(db)
    _stub_continuation_github(monkeypatch)
    proposed = await client.post(
        f"/api/v1/agent-teams/github-work-items/{item.id}/continuation-requests",
        headers={"X-Synth-City-Session-Token": tokens[1]}, json=_continuation_request_body(item))
    assert proposed.status_code == 200, proposed.text
    request = await db.get(GithubApprovalRequest, proposed.json()["approval"]["id"])
    return item, members, tokens, request


# Plan approvals
async def test_a_plan_approval_request_starts_one_wait_on_the_leader(db):
    item, members, request = await _initial(db)
    (start,) = await _events(db)
    assert start.event_kind == waits.STARTED and start.request_id == request.id
    assert start.operation_id == f"decision_wait_started:request:{request.id}"
    assert start.after_values == {
        "wait_request_id": request.id, "request_kind": "initial_plan", "owner_member_id": members[1].id,
        "awaited_member_id": members[0].id, "awaited_role": "leader"}
    assert start.fact_time == request.created_at and start.item_id == item.id
    assert start.actor_member_id == members[1].id


@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_a_plan_decision_ends_the_wait_answered_by_the_leader(db, monkeypatch, decision):
    monkeypatch.setattr(peer_process, "pane_is_alive", lambda _pid, _start: True)
    item, members, request = await _initial(db, delivered=True)
    await github_approval_service.decide(
        db, item, authenticated_leader_member_id=members[0].id, decision=decision, reason="checked",
        request_id=request.id)
    await github_approval_service.decide(  # a replay adds nothing
        db, item, authenticated_leader_member_id=members[0].id, decision=decision, reason="checked",
        request_id=request.id)
    start, end = await _events(db)
    await db.refresh(request)
    assert end.event_kind == waits.ENDED and end.request_id == request.id
    assert end.after_values == {"wait_request_id": request.id, "wait_outcome": decision,
                                "answered_by_member_id": members[0].id}
    assert end.fact_time == request.decided_at and end.actor_member_id == members[0].id
    assert end.item_context_key == start.item_context_key


async def test_a_new_round_supersedes_and_ends_the_earlier_wait_unattributed(db):
    item, members, first = await _initial(db)
    item.approval_round_count += 1
    await db.commit()
    second, _created = await github_approval_service.create_initial_request(
        db, item, authenticated_owner_member_id=members[1].id, summary="second plan")
    events = await _events(db)
    assert [(e.event_kind, e.request_id) for e in events] == [
        (waits.STARTED, first.id), (waits.ENDED, first.id), (waits.STARTED, second.id)]
    assert events[1].after_values["wait_outcome"] == "superseded"
    assert events[1].after_values["answered_by_member_id"] is None and events[1].actor_member_id is None


async def _timeout(db, item, members, request):
    assert await github_approval_service.supersede_initial_for_timeout(db, item, request)
    await db.commit()


async def _owner_cancel(db, item, members, request):
    await github_approval_service.cancel(db, request, requester_member_id=members[1].id)


async def _recovery_cancel(db, item, members, request):
    from app.services.github_initial_approval_recovery import cancel_stranded_initial_approval

    item.dispatch_status, item.escalation_reason = "escalated", "leader_ack_timeout"
    await db.commit()
    await cancel_stranded_initial_approval(db, work_item_id=item.id, request_id=request.id,
                                           dispatch_nonce=item.dispatch_nonce, reason="stranded")


async def _end_stopped(db, item, members, request):
    from app.services.github_end_stopped_item_service import end_pending_requests

    await end_pending_requests(db, item.id, now=datetime.utcnow())
    await db.commit()


@pytest.mark.parametrize(("end", "outcome"), [
    (_timeout, "superseded"), (_owner_cancel, "cancelled"), (_recovery_cancel, "cancelled"),
    (_end_stopped, "superseded")])
async def test_a_plan_request_ended_without_a_decision_ends_its_wait_unattributed(db, end, outcome):
    item, members, request = await _initial(db)
    await end(db, item, members, request)
    await end(db, item, members, request) if end is _end_stopped else None  # a repeat adds nothing
    (_start, ended) = await _events(db)
    await db.refresh(request)
    assert ended.after_values == {"wait_request_id": request.id, "wait_outcome": outcome,
                                  "answered_by_member_id": None}
    assert ended.actor_member_id is None and ended.fact_time == request.superseded_at


# Continuation decisions
@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_a_continuation_request_and_its_decision_are_one_wait(db, client, monkeypatch, decision):
    item, members, tokens, request = await _continuation(db, client, monkeypatch)
    body = {"approval_request_id": request.id, "work_item_id": item.id, "dispatch_nonce": item.dispatch_nonce,
            "decision": decision, "reason": "checked"}
    for _ in range(2):
        decided = await client.post("/api/v1/agent-mail/continuation-decisions",
                                    headers={"X-Synth-City-Session-Token": tokens[0]}, json=body)
        assert decided.status_code == 200, decided.text
    start, end = await _events(db)
    await db.refresh(request)
    assert start.after_values["request_kind"] == "continuation" and start.request_id == request.id
    assert end.after_values == {"wait_request_id": request.id, "wait_outcome": decision,
                                "answered_by_member_id": members[0].id}
    assert end.fact_time == request.decided_at


async def test_an_expired_continuation_ends_the_wait(db, client, monkeypatch):
    _item, _members, _tokens, request = await _continuation(db, client, monkeypatch)
    revision = await db.get(GithubAttemptScopeRevision, request.scope_revision_id)
    revision.expires_at = datetime.utcnow() - timedelta(seconds=1)
    await db.commit()
    assert await github_approval_service.expire_continuation_if_needed(db, request, revision) == "expired_pending"
    (_start, end) = await _events(db)
    assert end.after_values["wait_outcome"] == "expired"


@pytest.mark.parametrize("status", ["pending", "approved"])
async def test_a_stale_continuation_ends_its_wait_only_while_pending(db, client, monkeypatch, status):
    _item, members, _tokens, request = await _continuation(db, client, monkeypatch)
    revision = await db.get(GithubAttemptScopeRevision, request.scope_revision_id)
    if status == "approved":  # an already decided request is not a wait this supersession ends
        request.status = "approved"
        await db.commit()
    await github_approval_service.supersede_stale_continuation(db, request, revision)
    ends = await _events(db, waits.ENDED)
    assert [e.after_values["wait_outcome"] for e in ends] == (["superseded"] if status == "pending" else [])


# Open and unknown waits
async def test_a_never_answered_request_stays_open_with_its_age(db):
    _item, members, request = await _initial(db)
    now = request.created_at + timedelta(hours=2)
    (start,) = await _events(db)
    (wait,) = await waits.unended_decision_waits(db, now=now, team_context_key=start.team_context_key)
    assert await waits.unended_decision_waits(db, now=now, team_context_key="team:other") == []
    assert (wait.request_id, wait.state, wait.age_seconds) == (request.id, "open", 7200.0)
    assert (wait.owner_member_id, wait.awaited_member_id) == (members[1].id, members[0].id)


async def test_a_deleted_work_item_makes_its_wait_unknown_not_open(db):
    item, _members, request = await _initial(db)
    await db.execute(text("DELETE FROM github_work_items WHERE id = :id"), {"id": item.id})
    await db.commit()
    (wait,) = await waits.unended_decision_waits(db, now=datetime.utcnow())
    assert (wait.state, wait.age_seconds, wait.reason) == ("unknown", None, "work item deleted")


async def test_an_ended_wait_is_not_listed(db, monkeypatch):
    monkeypatch.setattr(peer_process, "pane_is_alive", lambda _pid, _start: True)
    item, members, request = await _initial(db, delivered=True)
    await github_approval_service.decide(db, item, authenticated_leader_member_id=members[0].id,
                                         decision="approved", reason="ok", request_id=request.id)
    assert await waits.unended_decision_waits(db, now=datetime.utcnow()) == []


async def test_a_request_reusing_a_deleted_items_id_does_not_end_the_old_wait(db):
    """V1 on 0329759d: a scope amendment (no start in part 1) reusing the id must not close the old wait."""
    item, members, request = await _initial(db)
    await db.execute(text("DELETE FROM github_approval_requests"))
    await db.execute(text("DELETE FROM github_work_items WHERE id = :id"), {"id": item.id})
    await db.commit()
    other = GithubWorkItem(scope_id=item.scope_id, issue_number=93, issue_title="other", issue_url="u",
                           github_updated_at=datetime.utcnow(), dispatch_status="dispatched",
                           owner_slot_id=item.owner_slot_id, routing_method="label", dispatch_nonce="a" * 16,
                           dispatch_head_ref="deck/other", approval_round_count=1)
    workspace = GithubWorkspace(scope_id=item.scope_id, path="/tmp/other", leased_item_id=None)
    db.add_all([other, workspace])
    await db.flush()
    revision = GithubAttemptScopeRevision(
        work_item_id=other.id, dispatch_nonce=other.dispatch_nonce, revision=1, owner_slot_id=other.owner_slot_id,
        owner_member_id=members[1].id, origin_kind="scope_amendment", phase="implementation",
        execution_target="workspace", summary="amend", allowed_paths=["a.py"], allowed_actions=["edit_tests"],
        allowed_commands=[], prohibited_actions=[], tool_fallbacks={}, baseline_head_sha="b" * 40,
        baseline_tree_sha="c" * 40, originating_escalation_reason="", expected_workspace_id=workspace.id,
        expected_lease_token_hash="h", max_failed_heads=1, expires_at=datetime.utcnow() - timedelta(seconds=1))
    db.add(revision)
    await db.flush()
    amendment = GithubApprovalRequest(
        id=request.id, work_item_id=other.id, request_kind="continuation", dispatch_nonce=other.dispatch_nonce,
        approval_round=2, owner_member_id=members[1].id, leader_member_id=members[0].id,
        scope_revision_id=revision.id, request_fingerprint="amendment")
    db.add(amendment)
    await audit.context_key_for(db, "item", other.id)  # the amendment's own request fact gives the item a lifetime key
    await db.commit()
    assert await github_approval_service.expire_continuation_if_needed(db, amendment, revision) == "expired_pending"
    assert await _events(db, waits.ENDED) == []
    (wait,) = await waits.unended_decision_waits(db, now=datetime.utcnow())
    assert (wait.request_id, wait.state, wait.reason) == (request.id, "unknown", "work item deleted")


async def test_a_wait_ends_once_whatever_ends_it_later(db):
    _item, members, request = await _initial(db)
    await waits.record_wait_ended(db, [request.id], outcome="approved", source="test", answered_by_member_id=members[0].id)
    await waits.record_wait_ended(db, [request.id, request.id], outcome="superseded", source="test")
    await db.commit()
    assert [e.after_values["wait_outcome"] for e in await _events(db, waits.ENDED)] == ["approved"]


async def test_a_request_without_a_start_gets_no_end(db):
    """A scope amendment, which part 1 does not start, or a request from before the upgrade."""
    item, members, _tokens = await _dispatch_approval_fixture(db)
    db.add(GithubApprovalRequest(work_item_id=item.id, request_kind="continuation", dispatch_nonce=item.dispatch_nonce,
                                 approval_round=1, owner_member_id=members[1].id, leader_member_id=members[0].id,
                                 request_fingerprint="f"))
    await db.commit()
    request = (await db.scalars(select(GithubApprovalRequest))).one()
    await waits.record_wait_ended(db, [request.id], outcome="superseded", source="test")
    await db.commit()
    assert await _events(db) == []


async def test_a_rolled_back_request_leaves_no_wait(db):
    item, members, _tokens = await _dispatch_approval_fixture(db)
    await github_approval_service.create_initial_request(
        db, item, authenticated_owner_member_id=members[1].id, summary="plan", commit=False)
    await db.rollback()
    assert await _events(db) == []


async def test_the_delivery_metrics_do_not_change(db):
    window = {"window_start": datetime.utcnow() - timedelta(hours=1), "window_end": datetime.utcnow() + timedelta(hours=1)}
    await _initial(db)

    async def values():
        return [(m.name, m.value, m.sample_count, m.unknown_count)
                for m in (await metrics.build_metrics_window(db, **window)).metrics]

    with_waits = await values()
    assert len(await _events(db, waits.STARTED)) == 1
    await db.execute(text("DELETE FROM factory_audit_events WHERE event_kind LIKE 'decision_wait_%'"))
    await db.commit()
    assert await values() == with_waits


async def test_a_request_id_reused_by_a_later_item_is_another_wait(db):
    item, members, request = await _initial(db)
    await db.execute(text("DELETE FROM github_approval_requests"))
    await db.execute(text("DELETE FROM github_work_items WHERE id = :id"), {"id": item.id})
    await db.commit()
    again = GithubWorkItem(scope_id=item.scope_id, issue_number=92, issue_title="again", issue_url="u",
                           github_updated_at=datetime.utcnow(), dispatch_status="dispatched",
                           owner_slot_id=item.owner_slot_id, routing_method="label", dispatch_nonce="fedcba9876543210",
                           dispatch_head_ref="deck/again", approval_round_count=1)
    db.add(again)
    await db.commit()
    reused, _created = await github_approval_service.create_initial_request(
        db, again, authenticated_owner_member_id=members[1].id, summary="plan")
    assert reused.id == request.id  # SQLite reuses the freed id
    starts = await _events(db, waits.STARTED)
    assert len(starts) == 2 and starts[0].item_context_key != starts[1].item_context_key
    (wait,) = [w for w in await waits.unended_decision_waits(db, now=datetime.utcnow()) if w.state == "open"]
    assert wait.request_id == reused.id
