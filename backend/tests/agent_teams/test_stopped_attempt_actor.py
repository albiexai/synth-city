"""One actor across the progress, retry, and operator attention reads (#1131)."""
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
