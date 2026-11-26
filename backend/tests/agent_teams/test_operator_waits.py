"""#1338 (part 2 of #1300): waits for checkpoint holds and owner observation pauses.

The owner waits for the operator at a recovery checkpoint hold or a recorded observation pause. The end records the
actor the release or resume supplied: the operator only from the operator-authenticated route, the Supervisor only from
a supplied external-actor identity, and nobody when no actor was supplied.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, text, update

from app.config import settings
from app.models.database import (
    FactoryAuditEvent, GithubAttemptScopeRevision, GithubOwnerObservationPause, GithubWorkItem, MailAgentSession,
)
from app.services import decision_wait_service as waits
from app.services import factory_audit_service as audit
from app.services.github_approval_service import github_approval_service
from app.utils import peer_process
from tests.agent_mail.conftest import db  # noqa: F401  (the shared in-memory database)
from tests.agent_mail.test_api import (  # noqa: F401  (fixtures of the Mail API tests)
    _continuation_approval_fixture, _continuation_request_body, _stub_continuation_github, client,
)

pytestmark = pytest.mark.asyncio
HOLD = (waits.HOLD_STARTED, waits.HOLD_ENDED)
PAUSE = (waits.PAUSE_STARTED, waits.PAUSE_ENDED)
OPERATOR = "hold-operator-token"
SUPERVISOR = audit.derive_actor(actor_kind="external_actor", external_actor_id=6)


async def _events(db, kinds):
    return (await db.scalars(select(FactoryAuditEvent).where(FactoryAuditEvent.event_kind.in_(kinds))
                             .order_by(FactoryAuditEvent.id))).all()


# Checkpoint holds
async def _held(client, db, monkeypatch):
    """A recovery-only continuation request: the owner waits at decision_hold for the operator."""
    monkeypatch.setattr(settings, "mail_capability_tokens_required", True)
    monkeypatch.setattr(settings, "operator_token", OPERATOR)
    monkeypatch.setattr(peer_process, "pane_is_alive", lambda _pid, _start: True)
    item, scope, members, tokens, _workspace = await _continuation_approval_fixture(db)
    monkeypatch.setattr(settings, "github_recovery_only_attempt", (
        f"{scope.id}:{item.id}:{item.pr_number}:{item.dispatch_nonce}:{item.dispatch_head_ref}"))
    _stub_continuation_github(monkeypatch)
    proposal = _continuation_request_body(item)
    proposal.update(phase="diagnostic", allowed_actions=["edit_tests", "revert_diagnostic_changes"],
                    allowed_paths=["tests/playback_smoke.py.in"], allowed_commands=["git diff --check"],
                    execution_target="hosted_ci")
    proposed = await client.post(f"/api/v1/agent-teams/github-work-items/{item.id}/continuation-requests",
                                 headers={"X-Synth-City-Session-Token": tokens[1]}, json=proposal)
    assert proposed.status_code == 200
    body = proposed.json()
    return item, members, tokens, body["approval"]["id"], body["revision"]["revision"]


async def _release(client, item, approval_id, revision_number, stage):
    return await client.post(
        f"/api/v1/agent-teams/github-work-items/{item.id}/scope-revisions/{revision_number}/checkpoint-release",
        headers={"X-Synth-City-Operator-Token": OPERATOR},
        json={"release": True, "dispatch_nonce": item.dispatch_nonce, "approval_request_id": approval_id,
              "stage": stage})


async def test_each_hold_waits_on_the_operator_until_the_operator_route_releases_it(client, db, monkeypatch):
    item, members, tokens, approval_id, number = await _held(client, db, monkeypatch)
    (start,) = await _events(db, HOLD)
    assert start.operation_id == f"hold_wait_started:request:{approval_id}:decision_hold"
    assert start.after_values == {"wait_request_id": approval_id, "request_kind": "checkpoint_hold",
                                  "recovery_checkpoint_stage": "decision_hold", "owner_member_id": members[1].id,
                                  "awaited_role": "operator"}
    assert (await _release(client, item, approval_id, number, "decision")).status_code == 200
    decided = await client.post("/api/v1/agent-mail/continuation-decisions",
                                headers={"X-Synth-City-Session-Token": tokens[0]},
                                json={"approval_request_id": approval_id, "work_item_id": item.id,
                                      "dispatch_nonce": item.dispatch_nonce, "decision": "approved",
                                      "reason": "Hosted diagnostic only"})
    assert decided.status_code == 200  # the Leader's approval moves the revision into ack_hold
    assert (await _release(client, item, approval_id, number, "ack")).status_code == 200
    assert (await _release(client, item, approval_id, number, "ack")).status_code != 500  # a replay adds nothing
    first, first_end, second, second_end = await _events(db, HOLD)
    assert [e.event_kind for e in (first, first_end, second, second_end)] == list(HOLD) * 2
    assert second.after_values["recovery_checkpoint_stage"] == "ack_hold"
    for end in (first_end, second_end):
        assert (end.actor_kind, end.actor_member_id, end.actor_reference) == (
            "operator", None, "shared-operator-credential")
        assert end.after_values["wait_outcome"] == "released" and end.after_values["answered_by_member_id"] is None
        assert end.fact_time >= first.fact_time


@pytest.mark.parametrize("actor,kind", [(SUPERVISOR, "external_actor"), (None, "system")])
async def test_a_release_records_only_the_actor_it_was_given(client, db, monkeypatch, actor, kind):
    item, _members, _tokens, approval_id, number = await _held(client, db, monkeypatch)
    await github_approval_service.release_recovery_checkpoint(
        db, item, revision_number=number, dispatch_nonce=item.dispatch_nonce, approval_request_id=approval_id,
        stage="decision", actor=actor)
    _start, end = await _events(db, HOLD)
    assert end.actor_kind == kind and end.actor_member_id is None
    assert end.actor_reference == ("external_actor:6" if actor else None)


async def test_an_unreleased_hold_stays_open_with_its_age(client, db, monkeypatch):
    _item, _members, _tokens, approval_id, _number = await _held(client, db, monkeypatch)
    (start,) = await _events(db, HOLD)
    (wait,) = [w for w in await waits.unended_decision_waits(db, now=start.fact_time + timedelta(minutes=7))
               if w.wait_kind == "hold"]
    assert (wait.request_id, wait.state, wait.age_seconds, wait.request_kind) == (
        approval_id, "open", 420, "checkpoint_hold")


async def test_a_hold_cleared_without_a_release_ends_unattributed(client, db, monkeypatch):
    item, *_ = await _held(client, db, monkeypatch)
    await db.execute(update(GithubAttemptScopeRevision).where(GithubAttemptScopeRevision.work_item_id == item.id)
                     .values(status="cancelled", cancelled_at=datetime.utcnow()))
    await db.commit()
    for _ in range(2):  # once
        await waits.reconcile_operator_waits(db, scope_id=item.scope_id, now=datetime.utcnow())
        await db.commit()
    _start, end = await _events(db, HOLD)
    assert (end.actor_kind, end.after_values["wait_outcome"]) == ("system", "hold_cleared")


async def test_a_hold_open_when_the_item_ends_closes_with_item_terminal(client, db, monkeypatch):
    item, _members, _tokens, _approval_id, _number = await _held(client, db, monkeypatch)
    await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == item.id).values(dispatch_status="merged"))
    await db.commit()
    await waits.reconcile_operator_waits(db, scope_id=item.scope_id, now=datetime.utcnow())
    await db.commit()
    _start, end = await _events(db, HOLD)
    assert (end.actor_kind, end.after_values["wait_outcome"]) == ("system", "item_terminal")


async def test_a_deleted_items_hold_is_unknown_not_open(client, db, monkeypatch):
    item, *_ = await _held(client, db, monkeypatch)
    await db.execute(text("DELETE FROM github_work_items WHERE id = :id"), {"id": item.id})
    await db.commit()
    (wait,) = [w for w in await waits.unended_decision_waits(db, now=datetime.utcnow()) if w.wait_kind == "hold"]
    assert (wait.state, wait.age_seconds, wait.reason) == ("unknown", None, "work item deleted")


async def test_a_hold_is_no_mail_wait_and_its_decision_wait_stays_its_own(client, db, monkeypatch):
    _item, _members, _tokens, approval_id, _number = await _held(client, db, monkeypatch)
    assert await _events(db, (waits.MAIL_STARTED,)) == []  # #1240: no Mail request is counted for the hold
    decision = await _events(db, (waits.STARTED,))
    assert [e.request_id for e in decision] == [approval_id]  # the Leader's own wait (#1239), keyed by request id
    (hold,) = await _events(db, HOLD)
    assert hold.operation_id != decision[0].operation_id


async def _cancelled(db, item, at, reason="Operator cancelled the held continuation."):
    await db.execute(update(GithubAttemptScopeRevision).where(GithubAttemptScopeRevision.work_item_id == item.id)
                     .values(status="cancelled", cancelled_at=at, cancellation_reason=reason))
    await db.commit()


async def test_a_late_reconcile_keeps_the_cancellation_time_and_reason(client, db, monkeypatch):
    """V1 review 6070971861: the first resolving transition and its own time, however late the monitor runs."""
    item, *_ = await _held(client, db, monkeypatch)
    (start,) = await _events(db, HOLD)
    cancelled = start.fact_time + timedelta(minutes=2)
    await _cancelled(db, item, cancelled)
    # The item closes 15 minutes after the cancellation, before the monitor's next pass an hour later.
    await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == item.id).values(
        dispatch_status="closed", updated_at=cancelled + timedelta(minutes=15)))
    await db.commit()
    await waits.reconcile_operator_waits(db, scope_id=item.scope_id, now=cancelled + timedelta(hours=1))
    await db.commit()
    _start, end = await _events(db, HOLD)
    assert (end.after_values["wait_outcome"], end.fact_time) == ("hold_cleared", cancelled)


@pytest.mark.parametrize("path", ["cancel", "supersede_stale_continuation"])
async def test_a_service_cancellation_ends_the_hold_at_its_recorded_time(client, db, monkeypatch, path):
    """V1 review 6071348579: these paths record the request's superseded_at but no revision end time."""
    from app.models.database import GithubApprovalRequest

    item, members, _tokens, approval_id, _number = await _held(client, db, monkeypatch)
    request = await db.get(GithubApprovalRequest, approval_id)
    if path == "cancel":
        await github_approval_service.cancel(db, request, requester_member_id=members[1].id)
    else:
        revision = await db.scalar(select(GithubAttemptScopeRevision).where(
            GithubAttemptScopeRevision.approval_request_id == approval_id))
        await github_approval_service.supersede_stale_continuation(db, request, revision)
    await db.commit()
    await db.refresh(request)
    revision = await db.scalar(select(GithubAttemptScopeRevision).where(
        GithubAttemptScopeRevision.approval_request_id == approval_id))
    assert request.superseded_at is not None and revision.cancelled_at is None and revision.completed_at is None
    await waits.reconcile_operator_waits(db, scope_id=item.scope_id, now=request.superseded_at + timedelta(hours=1))
    await db.commit()
    _start, end = await _events(db, HOLD)
    assert (end.after_values["wait_outcome"], end.fact_time) == ("hold_cleared", request.superseded_at)


@pytest.mark.parametrize("ended_by", ["merge", "end_stopped"])
async def test_an_item_that_ends_first_gives_one_item_terminal_end_at_its_transition(client, db, monkeypatch,
                                                                                   ended_by):
    item, *_ = await _held(client, db, monkeypatch)
    (start,) = await _events(db, HOLD)
    ended = start.fact_time + timedelta(minutes=5)
    if ended_by == "merge":  # complete_current_revision supersedes the held revision in the merge's transaction
        await _cancelled(db, item, ended, reason="The work item reached a terminal state.")
        status = "merged"
    else:  # end-stopped cancels the revisions and records its event with the same time
        await _cancelled(db, item, ended, reason="Deferred to v4.5. Tracking moved.")
        await audit.record_event(
            db, event_kind="stopped_work_item_ended", source="test", occurred_at=ended,
            actor=audit.derive_actor(actor_kind="operator"), item_id=item.id, action_outcome="applied",
            operation_id=f"end_stopped_item:test-{item.id}", sanitized_reason="ended")
        status = "cancelled"
    await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == item.id).values(
        dispatch_status=status, updated_at=ended + timedelta(minutes=30)))
    await db.commit()
    for _ in range(2):  # once
        await waits.reconcile_operator_waits(db, scope_id=item.scope_id, now=ended + timedelta(hours=1))
        await db.commit()
    _start, end = await _events(db, HOLD)
    assert (end.after_values["wait_outcome"], end.fact_time) == ("item_terminal", ended)


async def test_an_item_ending_while_still_held_uses_its_last_write_not_the_poll(client, db, monkeypatch):
    item, *_ = await _held(client, db, monkeypatch)
    (start,) = await _events(db, HOLD)
    written = start.fact_time + timedelta(minutes=10)
    await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == item.id).values(
        dispatch_status="closed", updated_at=written))
    await db.commit()
    await waits.reconcile_operator_waits(db, scope_id=item.scope_id, now=written + timedelta(hours=1))
    await db.commit()
    _start, end = await _events(db, HOLD)
    assert (end.after_values["wait_outcome"], end.fact_time) == ("item_terminal", written)


# Observation pauses
async def _paused(db, monkeypatch, tmp_path):
    """A recorded observation pause after its bounded wait: the owner waits for the operator to resume it."""
    from app.services import owner_observation_pause as pause
    from tests.agent_teams.test_factory_delivery_policy import observation_fixture

    scope, item, *_ = await observation_fixture(db, monkeypatch, tmp_path)
    assert await pause.handle_observation_gap(db, scope, item)
    record = (await db.scalars(select(GithubOwnerObservationPause))).one()
    assert record.status == "waiting" and await _events(db, PAUSE) == []  # the bounded wait is not yet a pause
    record.deadline = datetime.utcnow() - timedelta(seconds=1)
    await db.commit()
    assert await pause.handle_observation_gap(db, scope, item)
    assert record.status == "paused"
    return scope, item, record


async def test_a_pause_waits_on_the_operator_until_the_operator_route_resumes_it(db, monkeypatch, tmp_path):
    from tests.agent_teams.test_end_stopped_work_item import OPERATOR as HEADERS, _api

    scope, item, record = await _paused(db, monkeypatch, tmp_path)
    (start,) = await _events(db, PAUSE)
    assert start.operation_id == f"pause_wait_started:pause:{record.id}"
    owner = await db.scalar(select(MailAgentSession.member_id).where(
        MailAgentSession.member_id.is_not(None)).order_by(MailAgentSession.id.desc()).limit(1))
    assert start.after_values == {"wait_pause_id": record.id, "request_kind": "observation_pause",
                                  "owner_member_id": owner, "awaited_role": "operator"}
    assert owner is not None
    assert start.fact_time == record.paused_at
    async with _api(db, monkeypatch) as api:
        for _ in range(2):  # a replay adds nothing
            response = await api.post(f"/presets/{scope.preset_id}/work-items/{item.id}/resume-observation",
                                      json={"pause_id": record.id, "reason": "Inspect complete"}, headers=HEADERS)
            assert response.status_code == 200
    _start, end = await _events(db, PAUSE)
    assert (end.actor_kind, end.actor_member_id, end.actor_reference) == (
        "operator", None, "shared-operator-credential")
    assert end.after_values == {"wait_pause_id": record.id, "wait_outcome": "resumed",
                                "answered_by_member_id": None}
    assert end.fact_time == record.resumed_at


@pytest.mark.parametrize("actor,kind", [(SUPERVISOR, "external_actor"), (None, "system")])
async def test_a_resume_records_only_the_actor_it_was_given(db, monkeypatch, tmp_path, actor, kind):
    from app.services import owner_observation_pause as pause

    scope, item, record = await _paused(db, monkeypatch, tmp_path)
    if actor is None:
        await pause.resume_observation_pause(db, item, scope, record.id, "Inspect complete")
    else:
        await pause.resume_observation_pause(db, item, scope, record.id, "Inspect complete", actor=actor)
    _start, end = await _events(db, PAUSE)
    assert end.actor_kind == kind and end.actor_reference == ("external_actor:6" if actor else None)


async def test_a_pause_open_when_the_item_ends_closes_with_item_terminal(db, monkeypatch, tmp_path):
    scope, item, _record = await _paused(db, monkeypatch, tmp_path)
    (wait,) = [w for w in await waits.unended_decision_waits(db, now=datetime.utcnow()) if w.wait_kind == "pause"]
    assert wait.state == "open" and wait.owner_member_id is not None  # the verified owner, not the operator
    await waits.reconcile_operator_waits(db, scope_id=scope.id, now=datetime.utcnow())  # a live item: still open
    assert [e.event_kind for e in await _events(db, PAUSE)] == [waits.PAUSE_STARTED]
    written = datetime.utcnow()
    item.dispatch_status = "cancelled"
    item.updated_at = written
    await db.commit()
    for _ in range(2):  # once
        await waits.reconcile_operator_waits(db, scope_id=scope.id, now=written + timedelta(hours=1))
        await db.commit()
    _start, end = await _events(db, PAUSE)
    assert (end.actor_kind, end.after_values["wait_outcome"]) == ("system", "item_terminal")
    assert end.fact_time == written  # the item's last write bounds the end, not the late poll


async def test_the_pause_notice_is_no_mail_wait(db, monkeypatch, tmp_path):
    await _paused(db, monkeypatch, tmp_path)
    assert await _events(db, (waits.MAIL_STARTED,)) == []


async def test_the_monitor_reconciles_every_scope_item_not_only_limited_ones(db, monkeypatch, tmp_path):
    """The effort tick covers limited items only; the operator waits are reconciled for every item."""
    from app.services.github_dispatch_service import github_dispatch_service

    scope, item, _record = await _paused(db, monkeypatch, tmp_path)
    assert item.effort_limit_minutes is None
    item.dispatch_status = "cancelled"
    await db.commit()
    called = []

    async def reconcile(session, *, scope_id, now):
        called.append(scope_id)

    monkeypatch.setattr(waits, "reconcile_operator_waits", reconcile)
    await github_dispatch_service.monitor_dispatched(db, scope, [])
    assert called == [scope.id]


async def test_a_failed_reconcile_never_breaks_the_monitor(db, monkeypatch, tmp_path):
    from app.services.github_dispatch_service import github_dispatch_service

    scope, _item, _record = await _paused(db, monkeypatch, tmp_path)

    async def failing(session, *, scope_id, now):
        raise RuntimeError("reconcile failed")

    monkeypatch.setattr(waits, "reconcile_operator_waits", failing)
    await github_dispatch_service.monitor_dispatched(db, scope, [])  # logged and recovered, not raised
    assert scope.id is not None  # the monitor's loaded scope is still usable after recovery
