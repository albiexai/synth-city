"""#968: the recorded estimate and effort limit are visible, and limit plus extension never passes the ceiling.

Continues #938. The effort fields were stored on the work item and used by the ledger, but no view showed them. The
Supervisor also moved one rule here: the Leader's extension is clamped so that the limit and the extension together
stay within the scope maximum ``max_item_effort_minutes``.
"""
from __future__ import annotations

import pytest
from sqlalchemy import update

from app.models.database import TeamGithubScope
from app.services.github_effort_limit_service import EffortLimitError, effort_limit_service as service
from tests.agent_teams.test_effort_limit import (  # noqa: F401  (fixtures and helpers shared)
    T0, _code, _extend, _item, _limited, _set, fx,
)


async def _ceiling(fx, minutes):
    async with fx.maker() as db:
        await db.execute(update(TeamGithubScope).where(TeamGithubScope.id == fx.scope_id)
                         .values(max_item_effort_minutes=minutes))
        await db.commit()


# --- the ceiling clamp ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("limit,ceiling,cap", [
    (60, 120, 15),    # the 25 percent rule binds: the ceiling leaves room
    (100, 120, 20),   # the ceiling binds: 120 - 100 is below both the 30 minutes and the 25 percent
    (100, 110, 10),
    (120, 120, 0),    # a limit at the ceiling leaves nothing
    (60, 60, 0),
    (130, 120, 0),    # a limit above a lowered ceiling never goes negative
    (200, 400, 30),   # the 30 minute cap binds
])
def test_the_clamped_cap_is_the_least_of_the_rule_and_the_room_under_the_ceiling(limit, ceiling, cap):
    assert service.extension_cap_for(limit, ceiling) == cap


def test_the_plain_cap_still_ignores_the_ceiling():
    assert service.extension_cap(100) == 25


@pytest.mark.asyncio
async def test_an_extension_that_would_pass_the_ceiling_is_refused_naming_the_ceiling(fx):
    await _limited(fx, limit=100)
    await _ceiling(fx, 110)
    with pytest.raises(EffortLimitError) as caught:
        await _extend(fx, 11)
    assert caught.value.code == "extension_exceeds_cap" and caught.value.status_code == 400
    assert "110" in str(caught.value) and "max_item_effort_minutes" in str(caught.value)
    async with fx.maker() as db:
        item = await _item(fx, db)
        assert item.effort_extension_minutes == 0 and item.effort_extension_decided_at is None


@pytest.mark.asyncio
async def test_an_extension_up_to_the_room_under_the_ceiling_is_granted(fx):
    await _limited(fx, limit=100)
    await _ceiling(fx, 110)
    result = await _extend(fx, 10)
    assert result["extension_minutes"] == 10 and result["effective_limit_minutes"] == 110


@pytest.mark.asyncio
async def test_a_limit_at_the_ceiling_cannot_be_extended(fx):
    await _limited(fx, limit=120)
    assert await _code(_extend(fx, 1)) == "extension_exceeds_cap"


@pytest.mark.asyncio
async def test_a_limit_far_below_the_ceiling_keeps_the_ordinary_cap(fx):
    await _limited(fx, limit=60)
    await _ceiling(fx, 480)
    assert (await _extend(fx, 15))["effective_limit_minutes"] == 75
    await _set(fx, effort_extension_minutes=0, effort_extension_decided_at=None, effort_extension_reason=None)
    assert await _code(_extend(fx, 16)) == "extension_exceeds_cap"


# --- the view and its three exposures ------------------------------------------------------------------------------

from datetime import datetime  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from sqlalchemy import func, select  # noqa: E402

from app.api.v1 import agent_mail, agent_teams  # noqa: E402
from app.models.database import (  # noqa: E402
    GithubAttemptScopeRevision, GithubWorkItem, GithubWorkspace, MailMessage,
)

VIEW_KEYS = {"estimate_minutes", "limit_minutes", "extension_minutes", "effective_limit_minutes",
             "ceiling_minutes", "extension_cap_minutes"}


def _row(**values):
    return SimpleNamespace(effort_estimate_minutes=values.get("estimate"), effort_limit_minutes=values.get("limit"),
                           effort_extension_minutes=values.get("extension", 0))


def test_an_item_without_a_recorded_limit_has_no_view():
    assert service.effort_view(_row(), SimpleNamespace(max_item_effort_minutes=120)) is None


def test_the_view_shows_the_estimate_the_limit_and_the_room_under_the_ceiling():
    view = service.effort_view(_row(estimate=60, limit=60), SimpleNamespace(max_item_effort_minutes=120))
    assert view == {"estimate_minutes": 60, "limit_minutes": 60, "extension_minutes": 0,
                    "effective_limit_minutes": 60, "ceiling_minutes": 120, "extension_cap_minutes": 15}


def test_the_view_shows_an_extension_and_no_further_room():
    view = service.effort_view(_row(estimate=60, limit=60, extension=15), SimpleNamespace(max_item_effort_minutes=120))
    assert view["extension_minutes"] == 15 and view["effective_limit_minutes"] == 75
    assert view["extension_cap_minutes"] == 0 and set(view) == VIEW_KEYS


def test_the_view_applies_the_ceiling_clamp_to_the_room_it_shows():
    view = service.effort_view(_row(estimate=100, limit=100), SimpleNamespace(max_item_effort_minutes=110))
    assert view["extension_cap_minutes"] == 10


async def _owner_session(fx):
    async with fx.maker() as db:
        item = await _item(fx, db)
        return SimpleNamespace(member_id=fx.owner, team_slot_id=item.owner_slot_id, team_preset_id=None,
                               bound_pane_pid=4242, bound_pane_proc_start="12345")


async def _revision(fx, *, status="active"):
    async with fx.maker() as db:
        item = await _item(fx, db)
        workspace = (await db.execute(select(GithubWorkspace).where(
            GithubWorkspace.leased_item_id == fx.item_id))).scalar_one()
        db.add(GithubAttemptScopeRevision(
            work_item_id=fx.item_id, dispatch_nonce=fx.nonce, revision=1, owner_slot_id=item.owner_slot_id,
            owner_member_id=fx.owner, phase="implementation", execution_target="workspace",
            summary="A follow-up", allowed_paths=["src/a.py"], allowed_actions=["push_pr_head"],
            allowed_commands=["pytest -q"], prohibited_actions=[], tool_fallbacks={}, baseline_head_sha="b" * 40,
            baseline_tree_sha="c" * 40, originating_escalation_reason="retry_count_exhausted",
            expected_workspace_id=workspace.id, expected_lease_token_hash="hash", max_failed_heads=2,
            status=status, created_at=datetime(2026, 11, 16, 5, 0, 0)))
        await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == fx.item_id)
                         .values(active_scope_revision=1, attempt_phase="implementation"))
        await db.commit()


def _no_private_values(fx, payload):
    text = str(payload)
    assert fx.nonce not in text and "fixture-lease" not in text


@pytest.mark.asyncio
async def test_the_revision_view_shows_the_recorded_estimate_and_limit(fx):
    await _limited(fx, limit=60)
    await _revision(fx)
    async with fx.maker() as db:
        rows = await agent_teams.list_github_work_item_scope_revisions(fx.item_id, principal=None, db=db)
    assert len(rows) == 1 and rows[0].effort is not None
    shown = rows[0].effort.model_dump()
    assert set(shown) == VIEW_KEYS and shown["estimate_minutes"] == 60 and shown["limit_minutes"] == 60
    assert shown["extension_cap_minutes"] == 15
    _no_private_values(fx, shown)


@pytest.mark.asyncio
async def test_the_revision_view_without_a_recorded_limit_shows_none(fx):
    await _revision(fx)
    async with fx.maker() as db:
        rows = await agent_teams.list_github_work_item_scope_revisions(fx.item_id, principal=None, db=db)
    assert rows[0].effort is None


@pytest.mark.asyncio
async def test_the_work_item_context_shows_the_estimate_the_limit_and_the_extension(fx):
    await _limited(fx, limit=60, effort_extension_minutes=15)
    await _revision(fx)
    session = await _owner_session(fx)
    async with fx.maker() as db:
        context = await agent_teams.claim_github_work_item_continuation(
            fx.item_id, SimpleNamespace(headers={}), session=session, db=db)
    assert context.effort is not None
    shown = context.effort.model_dump()
    assert shown["limit_minutes"] == 60 and shown["extension_minutes"] == 15
    assert shown["effective_limit_minutes"] == 75 and shown["ceiling_minutes"] == 120
    assert context.active_revision is not None and context.active_revision.effort == context.effort
    _no_private_values(fx, shown)


@pytest.mark.asyncio
async def test_the_work_item_context_without_a_limit_shows_none(fx):
    session = await _owner_session(fx)
    async with fx.maker() as db:
        context = await agent_teams.claim_github_work_item_continuation(
            fx.item_id, SimpleNamespace(headers={}), session=session, db=db)
    assert context.effort is None


def _preflight_request(fx, **extra):
    return agent_mail.MailPreflightRequest(
        kind="initial_plan", work_item_id=fx.item_id, dispatch_nonce=fx.nonce, summary="A plan",
        plan_metadata={"estimate_active_minutes": 45}, **extra)


@pytest.mark.asyncio
async def test_the_preflight_output_shows_the_recorded_estimate_and_limit(fx):
    await _limited(fx, limit=60)
    session = await _owner_session(fx)
    async with fx.maker() as db:
        result = await agent_mail.preflight_request(_preflight_request(fx), session=session, db=db)
    assert {"passed", "problems", "warnings", "effort"} <= set(result)
    assert result["effort"]["estimate_minutes"] == 60 and result["effort"]["limit_minutes"] == 60
    assert set(result["effort"]) == VIEW_KEYS
    _no_private_values(fx, result["effort"])


@pytest.mark.asyncio
async def test_the_preflight_output_without_a_recorded_limit_shows_none(fx):
    session = await _owner_session(fx)
    async with fx.maker() as db:
        result = await agent_mail.preflight_request(_preflight_request(fx), session=session, db=db)
    assert result["effort"] is None


@pytest.mark.asyncio
async def test_the_preflight_stays_read_only(fx):
    await _limited(fx, limit=60)
    session = await _owner_session(fx)

    async def counts():
        async with fx.maker() as db:
            return [await db.scalar(select(func.count()).select_from(table)) for table in
                    (MailMessage, GithubAttemptScopeRevision)] + [(await _item(fx, db)).updated_at]

    before = await counts()
    async with fx.maker() as db:
        await agent_mail.preflight_request(_preflight_request(fx), session=session, db=db)
    assert await counts() == before


# --- V1 review 5925693104: the extension must read the ceiling as it is now, not as the session saw it -------------


@pytest.mark.asyncio
async def test_a_ceiling_lowered_by_another_connection_after_the_scope_was_loaded_is_honoured(fx):
    """The session already holds the scope row (ceiling 120); a second connection lowers it to 110 and commits."""
    await _limited(fx, limit=100)
    async with fx.maker() as session:
        cached = await session.get(TeamGithubScope, fx.scope_id)
        assert cached.max_item_effort_minutes == 120
        await _ceiling(fx, 110)  # a second connection, committed before the extension starts
        with pytest.raises(EffortLimitError) as caught:
            await service.extend(session, work_item_id=fx.item_id, dispatch_nonce=fx.nonce, leader_member_id=fx.leader,
                                 minutes=15, reason="healthy", now=T0)
    assert caught.value.code == "extension_exceeds_cap" and "110" in str(caught.value)
    async with fx.maker() as db:
        item = await _item(fx, db)
        assert item.effort_extension_minutes == 0 and item.effort_extension_decided_at is None


@pytest.mark.asyncio
async def test_a_ceiling_raised_by_another_connection_after_the_scope_was_loaded_is_honoured(fx):
    await _limited(fx, limit=100)
    await _ceiling(fx, 110)
    async with fx.maker() as session:
        cached = await session.get(TeamGithubScope, fx.scope_id)
        assert cached.max_item_effort_minutes == 110
        await _ceiling(fx, 480)
        result = await service.extend(session, work_item_id=fx.item_id, dispatch_nonce=fx.nonce,
                                      leader_member_id=fx.leader, minutes=15, reason="healthy", now=T0)
    assert result["effective_limit_minutes"] == 115
