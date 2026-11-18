"""#1132: no supervisor check-in attention for a paused team with no remaining work.

Live: deck-website-plan, paused with all 13 items done, showed "Supervisor check-in overdue" in Overview › Needs you.
The status stays truthful (late or absent); only the attention is suppressed, and only while the team is paused and
has no unfinished item in any of its scopes, whatever filter the summary was read with.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from app.models.database import AgentTeamPreset, AgentTeamSlot, GithubWorkItem, MailExternalActor, TeamGithubScope
from app.services.agent_team_summary_service import AgentTeamSummaryService
from app.services.agent_team_supervisor_service import observe_supervisor
from tests.agent_teams.test_team_summary_needs_you_filters import _Attention, _Progress, _no_hold  # noqa: F401

OVERDUE = "Supervisor check-in overdue"


async def _team(db, *, paused, statuses, overdue=True):
    """A team with a bound supervisor whose promised check-in is in the past (or future), two repositories and an
    item per status, alternating between them."""
    actor = MailExternalActor(actor_key=f"supervisor-1132-{len(statuses)}-{paused}", display_name="Supervisor",
                              kind="external_tool", token_hash=hashlib.sha256(b"synthetic-1132").hexdigest())
    db.add(actor)
    await db.flush()
    checked = datetime.utcnow() - timedelta(days=2 if overdue else 0)
    team = AgentTeamPreset(name=f"Team 1132 {paused} {statuses}", autonomy_enabled=not paused,
                           supervisor_actor_id=actor.id, supervisor_checked_at=checked,
                           supervisor_next_check_at=checked + timedelta(hours=1 if overdue else 24))
    db.add(team)
    await db.flush()
    scopes = [TeamGithubScope(preset_id=team.id, repo_owner="example", repo_name=name, repo_path=f"/private/{name}",
                              enabled=True) for name in ("r1", "r2")]
    slot = AgentTeamSlot(preset_id=team.id, display_name="codex", provider="codex-cli", repo_path="/private/repo",
                         repo_id="private", repo_name="repo", bootstrap_prompt="private", launch_options={})
    db.add_all([*scopes, slot])
    await db.flush()
    for number, status in enumerate(statuses, start=1):
        db.add(GithubWorkItem(scope_id=scopes[(number - 1) % 2].id, issue_number=number, issue_title="Item",
                              issue_url=f"https://x/{number}", github_updated_at=datetime.utcnow(),
                              dispatch_status=status, owner_slot_id=slot.id, dispatch_nonce=f"n-{team.id}-{number}"))
    await db.commit()
    return team, scopes


async def _summary(db, team, **filters):
    service = AgentTeamSummaryService(progress=_Progress(), attention=_Attention())
    return (await service.summary(db, team.id, **filters)).supervisor_observation


@pytest.mark.asyncio
async def test_a_paused_team_with_all_work_done_shows_no_overdue_attention(db):
    team, _ = await _team(db, paused=True, statuses=["merged", "completed", "merged"])
    observation = await _summary(db, team)
    assert observation.status in {"late", "absent"}  # the status stays truthful
    assert observation.attention is None
    direct = await observe_supervisor(db, team.id)  # the operator read and the check-in path use the same rule
    assert direct["status"] in {"late", "absent"} and direct["attention"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("paused,statuses", [
    (True, ["merged", "dispatched"]),   # paused, but work remains
    (False, []),                         # running, nothing to do
    (False, ["merged"]),                 # running, all done
])
async def test_the_overdue_attention_stays_unless_paused_with_nothing_left(db, paused, statuses):
    team, _ = await _team(db, paused=paused, statuses=statuses)
    assert (await _summary(db, team)).attention == OVERDUE
    assert (await observe_supervisor(db, team.id))["attention"] == OVERDUE


@pytest.mark.asyncio
async def test_a_filter_cannot_hide_the_teams_remaining_work(db):
    # Item 1 (merged) is in r1 and item 2 (dispatched) in r2. Filtering to r1 must not silence the attention.
    team, (r1, _r2) = await _team(db, paused=True, statuses=["merged", "dispatched"])
    assert (await _summary(db, team, scope_id=r1.id)).attention == OVERDUE


@pytest.mark.asyncio
async def test_a_healthy_check_in_has_no_attention_either_way(db):
    team, _ = await _team(db, paused=True, statuses=["merged"], overdue=False)
    observation = await _summary(db, team)
    assert observation.status == "healthy" and observation.attention is None
