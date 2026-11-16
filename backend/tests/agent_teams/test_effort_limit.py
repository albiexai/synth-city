"""#938 option C: record the estimate and effort limit, notify at the limit, allow one Leader extension.

The ledger is advisory elapsed working time. No expiry is added or changed, and the idle monitor stays the
fail-safe. These tests use a file-backed SQLite database so two connections can race.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base
from app.models.database import (
    GithubApprovalRequest, GithubOwnerObservationPause, GithubWorkItem, GithubWorkspace, MailMessage,
    TeamGithubScope,
)
from app.services.agent_mail_service import agent_mail_service
from app.services.github_approval_service import GithubApprovalError, github_approval_service
from app.services.github_effort_limit_service import (
    EffortLimitError, effort_limit_service as service,
)
from tests.agent_mail.test_api import _dispatch_approval_fixture

T0 = datetime(2026, 11, 16, 5, 0, 0)


@pytest_asyncio.fixture
async def fx(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'effort.db'}", connect_args={"timeout": 5})
    async with engine.begin() as conn:
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(settings, "mail_capability_tokens_required", True)

    async def no_wake(*_args, **_kwargs):
        pass

    monkeypatch.setattr(agent_mail_service, "auto_nudge_members", no_wake)
    async with maker() as db:
        item, members, _tokens = await _dispatch_approval_fixture(db)
        await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == item.id).values(
            ack_received_at=T0 - timedelta(minutes=1)))
        # The attempt's leased workspace shows fresh owner contact (an extension needs recent owner activity).
        db.add(GithubWorkspace(scope_id=item.scope_id, path="/tmp/effort-workspace", leased_item_id=item.id,
                               leased_at=T0 - timedelta(minutes=30), lease_token="fixture-lease",
                               lease_last_owner_contact_at=T0 - timedelta(minutes=1)))
        await db.commit()
        data = SimpleNamespace(maker=maker, item_id=item.id, nonce=item.dispatch_nonce, scope_id=item.scope_id,
                               leader=members[0].id, owner=members[1].id)
    yield data
    await engine.dispose()


async def _item(fx, db):
    return (await db.execute(select(GithubWorkItem).where(GithubWorkItem.id == fx.item_id)
                             .execution_options(populate_existing=True))).scalar_one()


async def _set(fx, **values):
    async with fx.maker() as db:
        await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == fx.item_id).values(**values))
        await db.commit()


async def _notices(fx, kind="github_effort_limit_reached"):
    async with fx.maker() as db:
        rows = (await db.execute(select(MailMessage).where(MailMessage.subject.like("%effort limit%")))).scalars().all()
        return [row for row in rows if (row.payload or {}).get("kind") == kind]


async def _tick(fx, at):
    async with fx.maker() as db:
        return await service.tick_item(db, fx.item_id, now=at)


async def _limited(fx, limit=60, **extra):
    await _set(fx, effort_estimate_minutes=limit, effort_limit_minutes=limit, **extra)


# --- estimate and ceiling -----------------------------------------------------------------------------------------


def _scope(ceiling=120):
    return SimpleNamespace(max_item_effort_minutes=ceiling)


def test_an_estimate_of_90_is_recorded_as_a_90_minute_limit_not_clamped():
    assert service.limit_for_plan({"estimate_active_minutes": 90}, _scope()) == 90


def test_an_estimate_at_the_ceiling_is_accepted():
    assert service.limit_for_plan({"estimate_active_minutes": 120}, _scope(120)) == 120


@pytest.mark.parametrize("estimate", [121, 240, {"low": 10, "high": 121}, 120.5])
def test_an_estimate_above_the_ceiling_is_refused(estimate):
    with pytest.raises(EffortLimitError) as caught:
        service.limit_for_plan({"estimate_active_minutes": estimate}, _scope(120))
    assert caught.value.code == "effort_estimate_exceeds_ceiling" and caught.value.status_code == 400
    assert "120" in str(caught.value)


def test_a_low_high_estimate_uses_its_high_bound():
    assert service.limit_for_plan({"estimate_active_minutes": {"low": 30, "high": 75}}, _scope()) == 75


@pytest.mark.parametrize("estimate", [0, -5, True, float("nan"), float("inf"), "90", {"high": 0}, {"low": 9, "high": 3},
                                      10 ** 400])
def test_an_unusable_estimate_is_refused_not_guessed(estimate):
    with pytest.raises(EffortLimitError) as caught:
        service.limit_for_plan({"estimate_active_minutes": estimate}, _scope())
    assert caught.value.code in {"effort_estimate_invalid", "effort_estimate_exceeds_ceiling"}


@pytest.mark.parametrize("metadata", [None, {}, {"estimate_active_minutes": None}, {"allowed_paths": ["a"]}])
def test_a_missing_estimate_records_no_limit(metadata):
    assert service.limit_for_plan(metadata, _scope()) is None


@pytest.mark.asyncio
async def test_an_estimate_above_the_ceiling_stores_nothing_and_creates_no_request(fx):
    async with fx.maker() as db:
        item = await _item(fx, db)
        with pytest.raises(GithubApprovalError) as caught:
            await github_approval_service.create_initial_request(
                db, item, authenticated_owner_member_id=fx.owner, summary="plan",
                plan_metadata={"estimate_active_minutes": 500})
        assert caught.value.detail == "effort_estimate_exceeds_ceiling" and caught.value.status_code == 400
    async with fx.maker() as db:
        item = await _item(fx, db)
        assert item.effort_limit_minutes is None and item.effort_estimate_minutes is None
        assert await db.scalar(select(func.count()).select_from(GithubApprovalRequest)) == 0


@pytest.mark.asyncio
async def test_a_valid_estimate_is_stored_with_the_request_and_a_later_plan_replaces_it(fx):
    async with fx.maker() as db:
        item = await _item(fx, db)
        await github_approval_service.create_initial_request(
            db, item, authenticated_owner_member_id=fx.owner, summary="plan",
            plan_metadata={"estimate_active_minutes": {"low": 30, "high": 90}})
    async with fx.maker() as db:
        item = await _item(fx, db)
        assert (item.effort_estimate_minutes, item.effort_limit_minutes) == (90, 90)


@pytest.mark.asyncio
async def test_a_missing_estimate_stores_no_limit(fx):
    async with fx.maker() as db:
        item = await _item(fx, db)
        await github_approval_service.create_initial_request(
            db, item, authenticated_owner_member_id=fx.owner, summary="plan", plan_metadata={})
    async with fx.maker() as db:
        item = await _item(fx, db)
        assert item.effort_limit_minutes is None


@pytest.mark.asyncio
async def test_the_scope_ceiling_defaults_to_120_and_is_validated(fx):
    async with fx.maker() as db:
        scope = await db.get(TeamGithubScope, fx.scope_id)
        assert scope.max_item_effort_minutes == 120
    from app.models.schemas import TeamGithubScopeUpdate

    for bad in (14, 1441):
        with pytest.raises(ValueError):
            TeamGithubScopeUpdate(max_item_effort_minutes=bad)
    assert TeamGithubScopeUpdate(max_item_effort_minutes=15).max_item_effort_minutes == 15
    assert TeamGithubScopeUpdate(max_item_effort_minutes=1440).max_item_effort_minutes == 1440


# --- the ledger ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_ledger_adds_working_time_between_two_working_ticks(fx):
    await _limited(fx)
    assert await _tick(fx, T0) == "started"
    assert await _tick(fx, T0 + timedelta(seconds=60)) == "counted"
    async with fx.maker() as db:
        assert (await _item(fx, db)).effort_elapsed_seconds == 60


@pytest.mark.asyncio
async def test_a_gap_above_300_seconds_adds_300(fx):
    await _limited(fx)
    await _tick(fx, T0)
    await _tick(fx, T0 + timedelta(hours=2))
    async with fx.maker() as db:
        assert (await _item(fx, db)).effort_elapsed_seconds == 300


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["verifying", "escalated", "blocked", "pr_opened", "merged", "released"])
async def test_a_tick_in_a_waiting_status_adds_nothing_and_resets_the_last_tick(fx, status):
    await _limited(fx)
    await _tick(fx, T0)
    await _set(fx, dispatch_status=status)
    assert await _tick(fx, T0 + timedelta(seconds=60)) == "reset"
    async with fx.maker() as db:
        item = await _item(fx, db)
        assert item.effort_elapsed_seconds == 0 and item.effort_last_tick_at is None


@pytest.mark.asyncio
async def test_the_first_working_tick_after_a_wait_adds_nothing(fx):
    await _limited(fx)
    await _tick(fx, T0)
    await _set(fx, dispatch_status="verifying")
    await _tick(fx, T0 + timedelta(seconds=60))
    await _set(fx, dispatch_status="dispatched")
    assert await _tick(fx, T0 + timedelta(minutes=30)) == "started"
    async with fx.maker() as db:
        assert (await _item(fx, db)).effort_elapsed_seconds == 0
    await _tick(fx, T0 + timedelta(minutes=30, seconds=60))
    async with fx.maker() as db:
        assert (await _item(fx, db)).effort_elapsed_seconds == 60


@pytest.mark.asyncio
async def test_an_unacknowledged_attempt_is_not_working(fx):
    await _limited(fx, ack_received_at=None)
    assert await _tick(fx, T0) == "reset"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["waiting", "paused"])
async def test_an_observation_pause_stops_counting(fx, status):
    await _limited(fx)
    await _tick(fx, T0)
    async with fx.maker() as db:
        db.add(GithubOwnerObservationPause(work_item_id=fx.item_id, wait_key="w", authority_sha256="x",
                                           status=status, deadline=T0 + timedelta(hours=1)))
        await db.commit()
    assert await _tick(fx, T0 + timedelta(seconds=60)) == "reset"
    async with fx.maker() as db:
        assert (await _item(fx, db)).effort_elapsed_seconds == 0


@pytest.mark.asyncio
async def test_an_item_without_a_limit_is_not_counted(fx):
    assert await _tick(fx, T0) == "skipped"
    async with fx.maker() as db:
        item = await _item(fx, db)
        assert item.effort_elapsed_seconds == 0 and item.effort_last_tick_at is None


@pytest.mark.asyncio
async def test_two_concurrent_ticks_count_once(fx):
    await _limited(fx)
    await _tick(fx, T0)
    results = await asyncio.gather(_tick(fx, T0 + timedelta(seconds=60)), _tick(fx, T0 + timedelta(seconds=60)))
    assert sorted(results) in (["counted", "lost"], ["counted", "counted"])
    async with fx.maker() as db:
        assert (await _item(fx, db)).effort_elapsed_seconds == 60


@pytest.mark.asyncio
async def test_a_stale_tick_loses_the_compare_and_swap(fx):
    await _limited(fx)
    await _tick(fx, T0)
    async with fx.maker() as first, fx.maker() as second:
        item = await _item(fx, first)
        seen = item.effort_last_tick_at
        assert await service.tick_item(second, fx.item_id, now=T0 + timedelta(seconds=60)) == "counted"
        assert await service.tick_item(first, fx.item_id, now=T0 + timedelta(seconds=60), seen_last_tick=seen) == "lost"
    async with fx.maker() as db:
        assert (await _item(fx, db)).effort_elapsed_seconds == 60


# --- notices ------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_notice_goes_to_the_leader_and_the_owner_once_at_the_limit(fx):
    await _limited(fx, limit=15, effort_elapsed_seconds=899, effort_last_tick_at=T0)
    await _tick(fx, T0 + timedelta(seconds=2))
    notices = await _notices(fx)
    assert len(notices) == 2
    body = notices[0].body_markdown
    assert "advisory elapsed working time" in body and "not the owner's reported active effort" in body
    assert "approximation" in body and "300 seconds" in body
    await _tick(fx, T0 + timedelta(seconds=62))
    assert len(await _notices(fx)) == 2


@pytest.mark.asyncio
async def test_no_notice_before_the_limit(fx):
    await _limited(fx, limit=15, effort_elapsed_seconds=100, effort_last_tick_at=T0)
    await _tick(fx, T0 + timedelta(seconds=60))
    assert await _notices(fx) == []


@pytest.mark.asyncio
async def test_a_rerun_after_a_crash_between_the_ledger_and_the_mail_sends_each_notice_once(fx, monkeypatch):
    await _limited(fx, limit=15, effort_elapsed_seconds=899, effort_last_tick_at=T0)
    real = agent_mail_service.send_direct_message
    calls = {"n": 0}

    async def crashing(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("crash before the mail is committed")
        return await real(*args, **kwargs)

    monkeypatch.setattr(agent_mail_service, "send_direct_message", crashing)
    with pytest.raises(RuntimeError):
        await _tick(fx, T0 + timedelta(seconds=5))
    assert await _notices(fx) == []
    await _tick(fx, T0 + timedelta(seconds=65))
    await _tick(fx, T0 + timedelta(seconds=125))
    assert len(await _notices(fx)) == 2


@pytest.mark.asyncio
async def test_the_notice_changes_no_status_and_no_expiry(fx):
    await _limited(fx, limit=15, effort_elapsed_seconds=899, effort_last_tick_at=T0)
    async with fx.maker() as db:
        before = await _item(fx, db)
        snapshot = (before.dispatch_status, before.escalation_reason, before.active_scope_revision,
                    before.continuation_nudged_at)
    await _tick(fx, T0 + timedelta(seconds=2))
    async with fx.maker() as db:
        after = await _item(fx, db)
        assert (after.dispatch_status, after.escalation_reason, after.active_scope_revision,
                after.continuation_nudged_at) == snapshot


# --- the one Leader extension -------------------------------------------------------------------------------------


@pytest.mark.parametrize("limit,cap", [(60, 15), (100, 25), (120, 30), (240, 30), (15, 3)])
def test_the_extension_cap_is_the_smaller_of_30_minutes_and_25_percent(limit, cap):
    assert service.extension_cap(limit) == cap


async def _extend(fx, minutes=15, *, leader=None, nonce=None, reason="healthy"):
    async with fx.maker() as db:
        return await service.extend(db, work_item_id=fx.item_id, dispatch_nonce=nonce or fx.nonce,
                                    leader_member_id=leader or fx.leader, minutes=minutes, reason=reason,
                                    now=T0)


async def _code(coro):
    with pytest.raises(EffortLimitError) as caught:
        await coro
    return caught.value.code


@pytest.mark.asyncio
async def test_the_leader_extends_once_within_the_cap(fx):
    await _limited(fx, limit=60)
    result = await _extend(fx, 15)
    assert result["extension_minutes"] == 15 and result["effective_limit_minutes"] == 75
    async with fx.maker() as db:
        item = await _item(fx, db)
        assert item.effort_extension_minutes == 15 and item.effort_extension_reason == "healthy"
        assert item.effort_extension_decided_at == T0 and item.effort_limit_minutes == 60


@pytest.mark.asyncio
async def test_an_amount_above_the_cap_is_refused(fx):
    await _limited(fx, limit=60)
    assert await _code(_extend(fx, 16)) == "extension_exceeds_cap"
    assert await _code(_extend(fx, 0)) == "extension_exceeds_cap"


@pytest.mark.asyncio
async def test_only_the_items_leader_may_extend(fx):
    await _limited(fx)
    assert await _code(_extend(fx, 10, leader=fx.owner)) == "not_item_leader"
    assert await _code(_extend(fx, 10, leader=999999)) == "not_item_leader"


@pytest.mark.asyncio
async def test_a_stale_nonce_is_refused(fx):
    await _limited(fx)
    assert await _code(_extend(fx, 10, nonce="an-older-attempt")) == "effort_extension_stale"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["verifying", "escalated", "merged", "released", "blocked"])
async def test_an_item_that_is_not_working_is_refused(fx, status):
    await _limited(fx, dispatch_status=status)
    assert await _code(_extend(fx, 10)) == "effort_extension_stale"


@pytest.mark.asyncio
async def test_an_item_without_a_limit_is_refused(fx):
    assert await _code(_extend(fx, 10)) == "effort_limit_not_set"


@pytest.mark.asyncio
async def test_a_second_extension_is_refused_and_the_same_call_repeats_its_result(fx):
    await _limited(fx)
    first = await _extend(fx, 10)
    again = await _extend(fx, 10)
    assert again["extension_minutes"] == 10 and again["repeated"] is True and first["repeated"] is False
    assert await _code(_extend(fx, 5)) == "effort_extension_already_used"


@pytest.mark.asyncio
async def test_two_concurrent_extensions_one_wins(fx):
    await _limited(fx)
    outcomes = await asyncio.gather(_extend(fx, 10, reason="a"), _extend(fx, 12, reason="b"), return_exceptions=True)
    wins = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    losses = [outcome for outcome in outcomes if isinstance(outcome, EffortLimitError)]
    assert len(wins) == 1 and len(losses) == 1 and losses[0].code == "effort_extension_already_used"
    async with fx.maker() as db:
        assert (await _item(fx, db)).effort_extension_minutes == wins[0]["extension_minutes"]


@pytest.mark.asyncio
async def test_an_extension_racing_a_status_change_is_refused_or_applied_whole(fx):
    await _limited(fx)

    async def change():
        await _set(fx, dispatch_status="verifying")

    outcomes = await asyncio.gather(_extend(fx, 10), change(), return_exceptions=True)
    async with fx.maker() as db:
        item = await _item(fx, db)
    if isinstance(outcomes[0], dict):
        assert item.effort_extension_minutes == 10
    else:
        assert outcomes[0].code == "effort_extension_stale" and item.effort_extension_minutes == 0


@pytest.mark.asyncio
async def test_a_second_notice_goes_out_once_after_an_extension(fx):
    await _limited(fx, limit=15, effort_elapsed_seconds=899, effort_last_tick_at=T0)
    await _tick(fx, T0 + timedelta(seconds=2))
    assert len(await _notices(fx)) == 2
    await _extend(fx, 3)
    await _set(fx, effort_elapsed_seconds=18 * 60 - 1, effort_last_tick_at=T0)
    await _tick(fx, T0 + timedelta(seconds=2))
    await _tick(fx, T0 + timedelta(seconds=62))
    assert len(await _notices(fx)) == 4


@pytest.mark.asyncio
async def test_the_extension_changes_no_expiry_and_no_status(fx):
    await _limited(fx)
    async with fx.maker() as db:
        before = await _item(fx, db)
        snapshot = (before.dispatch_status, before.escalation_reason, before.continuation_activated_at,
                    before.continuation_nudged_at, before.ack_received_at)
    await _extend(fx, 10)
    async with fx.maker() as db:
        after = await _item(fx, db)
        assert (after.dispatch_status, after.escalation_reason, after.continuation_activated_at,
                after.continuation_nudged_at, after.ack_received_at) == snapshot


# --- unchanged behaviour and the migration -----------------------------------------------------------------------


def test_the_idle_monitor_and_the_proposal_expiry_are_unchanged():
    """The window setting is still the proposal expiry only; the idle timeout default is untouched."""
    assert settings.github_continuation_proposal_expiry_seconds == 3600
    assert settings.github_owner_idle_timeout_seconds == 900
    assert settings.github_nudge_grace_seconds == 180


@pytest.mark.asyncio
async def test_an_old_database_without_the_columns_is_migrated(tmp_path):
    from sqlalchemy import text

    from app.database import _run_sqlite_compat_migrations, _sqlite_columns

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE team_github_scopes DROP COLUMN max_item_effort_minutes"))
        for column in ("effort_estimate_minutes", "effort_limit_minutes", "effort_elapsed_seconds",
                       "effort_last_tick_at", "effort_extension_minutes", "effort_extension_decided_at",
                       "effort_extension_reason"):
            await conn.execute(text(f"ALTER TABLE github_work_items DROP COLUMN {column}"))
    async with engine.begin() as conn:
        await _run_sqlite_compat_migrations(conn)
    async with engine.begin() as conn:
        scope_columns = await _sqlite_columns(conn, "team_github_scopes")
        item_columns = await _sqlite_columns(conn, "github_work_items")
    await engine.dispose()
    assert "max_item_effort_minutes" in scope_columns
    assert {"effort_estimate_minutes", "effort_limit_minutes", "effort_elapsed_seconds", "effort_last_tick_at",
            "effort_extension_minutes", "effort_extension_decided_at", "effort_extension_reason"} <= set(item_columns)


@pytest.mark.asyncio
async def test_the_route_grants_the_leaders_extension_and_maps_refusals(fx):
    from fastapi import HTTPException

    from app.api.v1 import agent_mail
    from app.api.v1.agent_mail import MailEffortExtensionRequest

    await _limited(fx, limit=60)
    await _workspace_set(fx, lease_last_owner_contact_at=datetime.utcnow())  # the route reads the real clock
    leader, owner = SimpleNamespace(member_id=fx.leader), SimpleNamespace(member_id=fx.owner)
    request = MailEffortExtensionRequest(work_item_id=fx.item_id, dispatch_nonce=fx.nonce, minutes=15, reason="ok")
    async with fx.maker() as db:
        with pytest.raises(HTTPException) as caught:
            await agent_mail.extend_effort_limit(request, session=owner, db=db)
        assert (caught.value.status_code, caught.value.detail) == (403, "not_item_leader")
    async with fx.maker() as db:
        granted = await agent_mail.extend_effort_limit(request, session=leader, db=db)
        assert granted["effective_limit_minutes"] == 75 and granted["repeated"] is False
    async with fx.maker() as db:
        with pytest.raises(HTTPException) as caught:
            await agent_mail.extend_effort_limit(request.model_copy(update={"minutes": 14}), session=leader, db=db)
        assert (caught.value.status_code, caught.value.detail) == (409, "effort_extension_already_used")


@pytest.mark.asyncio
async def test_the_scope_monitor_advances_the_ledger_and_keeps_the_callers_rows_loaded(fx):
    from app.services.github_dispatch_service import github_dispatch_service

    await _limited(fx, effort_last_tick_at=datetime.utcnow() - timedelta(seconds=90))
    async with fx.maker(expire_on_commit=True) as db:  # the default session: a commit would expire what is loaded
        scope = await db.get(TeamGithubScope, fx.scope_id)
        await github_dispatch_service.monitor_dispatched(db, scope, [])
        assert scope.id == fx.scope_id  # still loaded: the tick expired nothing the monitor holds
    async with fx.maker() as db:
        assert 85 <= (await _item(fx, db)).effort_elapsed_seconds <= 95


@pytest.mark.asyncio
async def test_a_reactivation_between_two_working_ticks_books_nothing(fx):
    """Work, then escalated and re-acknowledged, then work again between two ticks: not one working span."""
    await _limited(fx)
    await _tick(fx, T0)
    await _set(fx, dispatch_status="escalated")
    await _set(fx, dispatch_status="dispatched", ack_received_at=T0 + timedelta(seconds=30))
    assert await _tick(fx, T0 + timedelta(seconds=60)) == "started"
    async with fx.maker() as db:
        item = await _item(fx, db)
        assert item.effort_elapsed_seconds == 0 and item.effort_last_tick_at == T0 + timedelta(seconds=60)
    await _tick(fx, T0 + timedelta(seconds=120))
    async with fx.maker() as db:
        assert (await _item(fx, db)).effort_elapsed_seconds == 60


@pytest.mark.asyncio
async def test_an_unrecorded_round_trip_inside_one_tick_gap_is_bounded_by_the_cap(fx):
    """verifying and back to dispatched between two ticks leaves no trace on the row: the error is at most 300 s."""
    await _limited(fx)
    await _tick(fx, T0)
    await _set(fx, dispatch_status="verifying")
    await _set(fx, dispatch_status="dispatched")
    assert await _tick(fx, T0 + timedelta(hours=3)) == "counted"
    async with fx.maker() as db:
        assert (await _item(fx, db)).effort_elapsed_seconds == 300


@pytest.mark.asyncio
async def test_a_reactivation_does_not_reset_the_ledger_total(fx):
    await _limited(fx, effort_elapsed_seconds=500, effort_last_tick_at=T0)
    await _set(fx, ack_received_at=T0 + timedelta(seconds=10))
    await _tick(fx, T0 + timedelta(seconds=60))
    async with fx.maker() as db:
        assert (await _item(fx, db)).effort_elapsed_seconds == 500


# --- V1 review 5915659215, finding 1: an extension needs recent owner activity ---------------------------------------


async def _workspace_set(fx, **values):
    async with fx.maker() as db:
        await db.execute(update(GithubWorkspace).where(GithubWorkspace.leased_item_id == fx.item_id).values(**values))
        await db.commit()


async def _refused_with(fx, code, minutes=10):
    assert await _code(_extend(fx, minutes)) == code
    async with fx.maker() as db:
        item = await _item(fx, db)
        assert item.effort_extension_minutes == 0 and item.effort_extension_decided_at is None


@pytest.mark.asyncio
async def test_an_extension_with_fresh_owner_contact_is_granted(fx):
    await _limited(fx)
    assert (await _extend(fx, 10))["extension_minutes"] == 10


@pytest.mark.asyncio
async def test_an_extension_is_refused_when_the_owner_contact_is_two_days_old(fx):
    await _limited(fx)
    await _workspace_set(fx, lease_last_owner_contact_at=T0 - timedelta(days=2))
    await _refused_with(fx, "effort_extension_no_recent_activity")


@pytest.mark.asyncio
async def test_an_extension_is_refused_at_the_edge_of_the_idle_window(fx):
    """The window is the delivery policy's owner idle time, else the setting (900 s): one second more is refused."""
    await _limited(fx)
    await _workspace_set(fx, lease_last_owner_contact_at=T0 - timedelta(seconds=settings.github_owner_idle_timeout_seconds))
    assert (await _extend(fx, 10))["extension_minutes"] == 10


@pytest.mark.asyncio
async def test_an_extension_one_second_past_the_idle_window_is_refused(fx):
    await _limited(fx)
    await _workspace_set(
        fx, lease_last_owner_contact_at=T0 - timedelta(seconds=settings.github_owner_idle_timeout_seconds + 1))
    await _refused_with(fx, "effort_extension_no_recent_activity")


@pytest.mark.asyncio
async def test_an_extension_uses_the_policy_idle_window_when_the_policy_sets_one(fx):
    await _limited(fx, delivery_policy={"owner_idle_seconds": 120})
    await _workspace_set(fx, lease_last_owner_contact_at=T0 - timedelta(seconds=300))
    await _refused_with(fx, "effort_extension_no_recent_activity")


@pytest.mark.asyncio
async def test_an_extension_is_refused_without_any_owner_contact(fx):
    await _limited(fx)
    await _workspace_set(fx, lease_last_owner_contact_at=None)
    await _refused_with(fx, "effort_extension_no_recent_activity")


@pytest.mark.asyncio
async def test_an_extension_is_refused_without_a_leased_workspace(fx):
    await _limited(fx)
    await _workspace_set(fx, leased_item_id=None)
    await _refused_with(fx, "effort_extension_no_recent_activity")


@pytest.mark.asyncio
async def test_an_extension_is_refused_without_an_acknowledged_attempt(fx):
    await _limited(fx, ack_received_at=None)
    await _refused_with(fx, "effort_extension_not_acknowledged")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["waiting", "paused"])
async def test_an_extension_is_refused_during_an_observation_pause(fx, status):
    await _limited(fx)
    async with fx.maker() as db:
        db.add(GithubOwnerObservationPause(work_item_id=fx.item_id, wait_key="w", authority_sha256="x", status=status,
                                           deadline=T0 + timedelta(hours=1)))
        await db.commit()
    await _refused_with(fx, "effort_extension_paused")


@pytest.mark.asyncio
async def test_a_resolved_observation_pause_does_not_refuse(fx):
    await _limited(fx)
    async with fx.maker() as db:
        db.add(GithubOwnerObservationPause(work_item_id=fx.item_id, wait_key="w", authority_sha256="x",
                                           status="resumed", deadline=T0 + timedelta(hours=1)))
        await db.commit()
    assert (await _extend(fx, 10))["extension_minutes"] == 10


@pytest.mark.asyncio
async def test_a_repeat_of_a_granted_extension_still_returns_its_record_without_fresh_contact(fx):
    await _limited(fx)
    await _extend(fx, 10)
    await _workspace_set(fx, lease_last_owner_contact_at=T0 - timedelta(days=2))
    assert (await _extend(fx, 10))["repeated"] is True


# The writer is held from the first statement, so another connection cannot change contact or a pause between the
# read and the write. The guarded update repeats the checks; each of these changes the row between the two inside
# the same transaction, as the only way to prove the repeat is real.


def _after_activity_read(monkeypatch, change):
    original = service._read_activity

    async def wrapped(db, item, scope, now):
        result = await original(db, item, scope, now)
        await change(db)
        return result

    monkeypatch.setattr(service, "_read_activity", wrapped)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["contact", "pause", "ack"])
async def test_the_guarded_update_refuses_a_change_made_after_the_activity_read(fx, monkeypatch, change):
    await _limited(fx)

    async def make_the_change(db):
        if change == "contact":
            await db.execute(update(GithubWorkspace).where(GithubWorkspace.leased_item_id == fx.item_id)
                             .values(lease_last_owner_contact_at=T0 - timedelta(seconds=5)))
        elif change == "pause":
            db.add(GithubOwnerObservationPause(work_item_id=fx.item_id, wait_key="w", authority_sha256="x",
                                               status="paused", deadline=T0 + timedelta(hours=1)))
            await db.flush()
        else:
            await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == fx.item_id).values(ack_received_at=None))

    _after_activity_read(monkeypatch, make_the_change)
    assert await _code(_extend(fx, 10)) == "effort_extension_stale"
    async with fx.maker() as db:
        assert (await _item(fx, db)).effort_extension_minutes == 0


@pytest.mark.asyncio
async def test_another_connection_cannot_write_between_the_read_and_the_update(fx, monkeypatch):
    """The extension holds the SQLite writer from its first statement: a second connection's write is refused."""
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    await _limited(fx)
    outcome = {"blocked": False}

    async def second_connection_writes(db):
        async with fx.maker() as other:
            await other.execute(text("PRAGMA busy_timeout=50"))
            try:
                await other.execute(update(GithubWorkspace).where(GithubWorkspace.leased_item_id == fx.item_id)
                                    .values(lease_last_owner_contact_at=None))
                await other.commit()
            except OperationalError:
                outcome["blocked"] = True
                await other.rollback()

    _after_activity_read(monkeypatch, second_connection_writes)
    assert (await _extend(fx, 10))["extension_minutes"] == 10
    assert outcome["blocked"] is True
