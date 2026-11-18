"""One actor across the progress, retry, and operator attention reads (#1131)."""
from datetime import timedelta

import pytest

from app.config import settings
from app.models.database import GithubApprovalRequest, GithubAttemptScopeRevision
from app.services.factory_projection_service import utc_now, work_projections
from app.services.github_coordination_service import hold_code
from app.services.github_operator_attention_service import GithubOperatorAttentionService
from app.services.github_work_progress_service import GithubWorkProgressService
from tests.agent_teams.test_github_work_progress import Client, Observer, db, seed  # noqa: F401


async def read_surfaces(db, item, scope, preset):
    progress = await GithubWorkProgressService(Observer(), Client()).summary(db, item.id)
    (projection,) = await work_projections(db, [(item, scope, preset)], utc_now())
    retry = next(action for action in projection.actions if action.name == "retry")
    attention = await GithubOperatorAttentionService().dispatch_snapshot(db, [scope])
    return (progress.phase, progress.next_actor, projection.waiting.actor, retry.required_actor,
            retry.state, [action["kind"] for action in attention["actions"]])


@pytest.mark.asyncio
@pytest.mark.parametrize("team_enabled,scope_enabled", [(True, True), (False, True), (True, False), (False, False)])
@pytest.mark.parametrize("pr_number,actor", [(None, "leader"), (42, "operator")])
async def test_escalation_precedes_ordinary_automation_pause(db, team_enabled, scope_enabled, pr_number, actor):
    item, scope, preset, _ = await seed(db)
    preset.autonomy_enabled = team_enabled
    scope.enabled = scope_enabled
    item.dispatch_status = "escalated"
    item.escalation_reason = "leader_ack_timeout"
    item.pr_number = pr_number
    await db.commit()

    expected = ("intervention", actor, actor, actor, "eligible", []) if actor == "leader" else (
        "intervention", actor, actor, actor, "blocked", ["inspect_attempt"])
    assert await read_surfaces(db, item, scope, preset) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [
    "hold", "abandoned_by_operator", "owner_observation_pause", "owner_observation_unavailable", "failed",
])
async def test_operator_owned_stops_remain_visible(db, monkeypatch, tmp_path, stop):
    item, scope, preset, _ = await seed(db)
    item.dispatch_status = "failed" if stop == "failed" else "escalated"
    item.escalation_reason = "leader_ack_timeout" if stop in {"failed", "hold"} else stop
    if stop == "hold":
        marker = tmp_path / "hold"
        marker.touch()
        monkeypatch.setattr(settings, "github_coordination_hold_paths", [str(marker)])
        monkeypatch.setattr("app.services.github_work_progress_service.hold_code", hold_code)
    await db.commit()

    phase = "paused" if stop == "hold" else "intervention"
    retry_state = "blocked" if stop == "failed" else "eligible"
    assert await read_surfaces(db, item, scope, preset) == (
        phase, "operator", "operator", "operator", retry_state, ["inspect_attempt"])


@pytest.mark.asyncio
@pytest.mark.parametrize("request_kind,stage,actor,operator_rows", [
    ("initial_plan", None, "operator", ["inspect_attempt"]),
    ("continuation", None, "leader", []),
    ("continuation", "decision_hold", "operator", ["inspect_checkpoint"]),
    ("continuation", "ack_hold", "operator", ["inspect_checkpoint"]),
])
async def test_escalated_approval_and_checkpoint_actor(db, request_kind, stage, actor, operator_rows):
    item, scope, preset, workspace = await seed(db)
    item.dispatch_status = "escalated"
    item.escalation_reason = "plan_blocked"
    item.pr_number = 42
    item.approval_round_count = 1
    approval_status = "approved" if stage == "ack_hold" else "pending"
    revision_id = None
    if request_kind == "continuation":
        revision = GithubAttemptScopeRevision(work_item_id=item.id, dispatch_nonce=item.dispatch_nonce, revision=1,
            owner_slot_id=item.owner_slot_id, owner_member_id=1, phase="diagnostic", execution_target="workspace",
            summary="fixture", allowed_paths=[], allowed_actions=[], allowed_commands=[], prohibited_actions=[],
            tool_fallbacks={}, baseline_head_sha="a" * 40, baseline_tree_sha="b" * 40,
            originating_escalation_reason="plan_blocked", expected_workspace_id=workspace.id,
            expected_lease_token_hash="fixture", max_failed_heads=1,
            status="approved" if approval_status == "approved" else "proposed", recovery_checkpoint_stage=stage)
        db.add(revision)
        await db.flush()
        revision_id = revision.id
    db.add(GithubApprovalRequest(work_item_id=item.id, request_kind=request_kind, dispatch_nonce=item.dispatch_nonce,
                                approval_round=1, owner_member_id=1, leader_member_id=2,
                                scope_revision_id=revision_id, request_fingerprint="fixture", status=approval_status))
    await db.commit()

    expected_phase = "plan_review" if actor == "leader" else "intervention"
    assert await read_surfaces(db, item, scope, preset) == (
        expected_phase, actor, actor, actor, "blocked", operator_rows)


async def seed_continuation(db, *, checkpoint_stage=None):
    item, scope, preset, workspace = await seed(db)
    item.dispatch_status = "escalated"
    item.escalation_reason = "plan_blocked"
    item.pr_number = 42
    item.dispatch_nonce = "a" * 16
    item.approval_round_count = 1
    revision = GithubAttemptScopeRevision(work_item_id=item.id, dispatch_nonce=item.dispatch_nonce, revision=1,
        owner_slot_id=item.owner_slot_id, owner_member_id=1, phase="diagnostic", execution_target="workspace",
        summary="fixture", allowed_paths=[], allowed_actions=[], allowed_commands=[], prohibited_actions=[],
        tool_fallbacks={}, baseline_head_sha="a" * 40, baseline_tree_sha="b" * 40,
        originating_escalation_reason="plan_blocked", expected_workspace_id=workspace.id,
        expected_lease_token_hash="fixture", max_failed_heads=1, status="proposed",
        recovery_checkpoint_stage=checkpoint_stage)
    db.add(revision)
    await db.flush()
    request = GithubApprovalRequest(work_item_id=item.id, request_kind="continuation",
        dispatch_nonce=item.dispatch_nonce, approval_round=1, owner_member_id=1, leader_member_id=2,
        scope_revision_id=revision.id, request_fingerprint="fixture", request_message_id=1)
    db.add(request)
    await db.flush()
    revision.approval_request_id = request.id
    await db.commit()
    return item, scope, preset, revision, request


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ordinary", "selected", "different_attempt", "hold"])
async def test_continuation_transitions_across_surfaces(db, monkeypatch, tmp_path, mode):
    recovery = mode in {"selected", "different_attempt"}
    item, scope, preset, revision, request = await seed_continuation(
        db, checkpoint_stage="decision_hold" if recovery else None)
    monkeypatch.setattr("app.services.github_work_progress_service.hold_code", hold_code)
    monkeypatch.setattr(settings, "github_recovery_only_attempt", "")
    monkeypatch.setattr(settings, "github_coordination_hold_paths", [])
    if recovery:
        pr_number = item.pr_number if mode == "selected" else item.pr_number + 1
        selector = f"{scope.id}:{item.id}:{pr_number}:{item.dispatch_nonce}:{item.dispatch_head_ref}"
        monkeypatch.setattr(settings, "github_recovery_only_attempt", selector)
    if mode == "hold":
        marker = tmp_path / "hold"
        marker.touch()
        monkeypatch.setattr(settings, "github_coordination_hold_paths", [str(marker)])

    checkpoint = ("paused", "operator", "operator", "operator", "blocked", ["inspect_checkpoint"])
    paused = ("paused", "operator", "operator", "operator", "blocked", ["inspect_attempt"])
    if recovery:
        assert await read_surfaces(db, item, scope, preset) == checkpoint
        revision.recovery_checkpoint_stage = "decision_open"
        await db.commit()
    can_continue = mode in {"ordinary", "selected"}
    assert await read_surfaces(db, item, scope, preset) == (
        ("plan_review", "leader", "leader", "leader", "blocked", []) if can_continue else paused)

    request.status = "approved"
    revision.status = "approved"
    revision.recovery_checkpoint_stage = "ack_hold" if recovery else None
    await db.commit()
    assert await read_surfaces(db, item, scope, preset) == (checkpoint if recovery else paused if mode == "hold" else
        ("intervention", "operator", "operator", "operator", "blocked", ["inspect_attempt"]))

    revision.recovery_checkpoint_stage = "ack_open" if recovery else None
    revision.delivery_message_id = 2
    revision.delivered_at = utc_now().replace(tzinfo=None)
    await db.commit()
    assert await read_surfaces(db, item, scope, preset) == (
        ("intervention", "owner", "owner", "owner", "blocked", []) if can_continue else paused)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,actor,phase", [
    ("decision_open", "leader", "plan_review"), ("ack_open", "owner", "intervention"),
])
@pytest.mark.parametrize("blocker", ["hold", "invalid_hold", "expiry", "ordinary_expiry"])
async def test_continuation_blockers_take_precedence(db, monkeypatch, tmp_path, stage, actor, phase, blocker):
    recovery = blocker != "ordinary_expiry"
    item, scope, preset, revision, request = await seed_continuation(
        db, checkpoint_stage=stage if recovery else None)
    monkeypatch.setattr("app.services.github_work_progress_service.hold_code", hold_code)
    selector = f"{scope.id}:{item.id}:{item.pr_number}:{item.dispatch_nonce}:{item.dispatch_head_ref}"
    monkeypatch.setattr(settings, "github_recovery_only_attempt", selector if recovery else "")
    monkeypatch.setattr(settings, "github_coordination_hold_paths", [])
    now = utc_now().replace(tzinfo=None)
    revision.expires_at = now + timedelta(minutes=5)
    if stage == "ack_open":
        request.status = "approved"
        revision.status = "approved"
        revision.delivery_message_id = 2
        revision.delivered_at = now
    await db.commit()
    assert await read_surfaces(db, item, scope, preset) == (phase, actor, actor, actor, "blocked", [])

    if blocker == "hold":
        marker = tmp_path / "hold"
        marker.touch()
        monkeypatch.setattr(settings, "github_coordination_hold_paths", [str(marker)])
    elif blocker == "invalid_hold":
        monkeypatch.setattr(settings, "github_coordination_hold_paths", ["relative-hold"])
    else:
        revision.expires_at = now - timedelta(seconds=1)
        await db.commit()

    assert await read_surfaces(db, item, scope, preset) == (
        "paused" if recovery else "intervention", "operator", "operator", "operator", "blocked", ["inspect_attempt"])
