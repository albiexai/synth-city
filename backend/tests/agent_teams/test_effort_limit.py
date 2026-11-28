"""#938 option C: record the estimate and effort limit, notify at the limit, allow one Leader extension.

The ledger is advisory elapsed working time. No expiry is added or changed, and the idle monitor stays the
fail-safe. These tests use a file-backed SQLite database so two connections can race.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1.agent_mail import _persist_initial_approval
from app.api.v1.agent_teams import report_dispatch_status
from app.config import settings
from app.database import Base
from app.models.database import (
    AgentTeamLaunch, AgentTeamPreset, FactoryAuditEvent, GithubApprovalRequest, GithubOwnerObservationPause,
    GithubWorkItem, GithubWorkspace, MailAgentSession, MailMessage, TeamGithubScope,
)
from app.models.schemas import DispatchStatusReport, MailApprovalRequestCreate
from app.services import agent_activity_service as activity
from app.services.agent_mail_service import agent_mail_service
from app.services.github_approval_service import GithubApprovalError, github_approval_service
from app.services.github_effort_limit_service import (
    EffortLimitError, effort_limit_service as service,
)
from app.services.owner_observation_pause import observation_wait_key
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
                               leader=members[0].id, leader_slot=members[0].team_slot_id,
                               owner=members[1].id, owner_slot=item.owner_slot_id)
    yield data
    await engine.dispose()


async def _item(fx, db):
    return (await db.execute(select(GithubWorkItem).where(GithubWorkItem.id == fx.item_id)
                             .execution_options(populate_existing=True))).scalar_one()


async def _set(fx, **values):
    async with fx.maker() as db:
        await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == fx.item_id).values(**values))
        await db.commit()


async def _scope_ceiling(fx, minutes):
    async with fx.maker() as db:
        await db.execute(update(TeamGithubScope).where(TeamGithubScope.id == fx.scope_id)
                         .values(max_item_effort_minutes=minutes))
        await db.commit()


async def _notices(fx, kind="github_effort_limit_reached"):
    async with fx.maker() as db:
        rows = (await db.execute(select(MailMessage).where(MailMessage.subject.like("%effort limit%")))).scalars().all()
        return [row for row in rows if (row.payload or {}).get("kind") == kind]


async def _tick(fx, at, *, observed=None):
    async with fx.maker() as db:
        observations = {fx.owner_slot: observed} if observed is not None else None
        return await service.tick_item(db, fx.item_id, now=at, provider_observations=observations)


async def _limited(fx, limit=60, **extra):
    await _set(fx, effort_estimate_minutes=limit, effort_limit_minutes=limit, **extra)


async def _bind_codex_lease(fx, leased_at):
    async with fx.maker() as db:
        workspace = await db.scalar(select(GithubWorkspace).where(GithubWorkspace.leased_item_id == fx.item_id))
        workspace.leased_at = leased_at
        workspace.leased_owner_pid, workspace.leased_owner_proc_start = 1001, "start-1"
        await db.commit()


def _codex_evidence(at, state="unknown", reason=activity.PROVIDER_USAGE_LIMIT, *, pid=1001):
    return activity.PrivateActivity(state, reason, at.replace(tzinfo=UTC), "native", None,
                                    binding_identity="bound", pane_lifetime=(pid, "start-1"))


async def _provider_events(db):
    return (await db.scalars(select(FactoryAuditEvent).where(
        FactoryAuditEvent.event_kind.in_(("provider_wait_started", "provider_wait_ended")),
    ).order_by(FactoryAuditEvent.id))).all()


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
@pytest.mark.parametrize(
    ("estimate", "code"),
    [(500, "effort_estimate_exceeds_ceiling"), (60.5, "plan_estimate_exceeds_limit")],
)
async def test_an_estimate_above_the_limit_stores_nothing_and_creates_no_request(fx, estimate, code):
    async with fx.maker() as db:
        item = await _item(fx, db)
        with pytest.raises(GithubApprovalError) as caught:
            await github_approval_service.create_initial_request(
                db, item, authenticated_owner_member_id=fx.owner, summary="plan",
                plan_metadata={"estimate_active_minutes": estimate})
        assert caught.value.status_code == 400
        if code == "plan_estimate_exceeds_limit":
            assert caught.value.detail["code"] == code
            assert "Split the issue" in caught.value.detail["message"]
        else:
            assert caught.value.detail == code
    async with fx.maker() as db:
        item = await _item(fx, db)
        assert item.effort_limit_minutes is None and item.effort_estimate_minutes is None
        assert await db.scalar(select(func.count()).select_from(GithubApprovalRequest)) == 0


@pytest.mark.asyncio
async def test_a_valid_estimate_is_stored_with_the_request_and_a_later_plan_replaces_it(fx):
    async with fx.maker() as db:
        item = await _item(fx, db)
        scope = await db.get(TeamGithubScope, item.scope_id)
        scope.max_plan_estimate_minutes = 90
        await db.commit()
        await github_approval_service.create_initial_request(
            db, item, authenticated_owner_member_id=fx.owner, summary="plan",
            plan_metadata={"estimate_active_minutes": {"low": 30, "high": 90}})
    async with fx.maker() as db:
        item = await _item(fx, db)
        assert (item.effort_estimate_minutes, item.effort_limit_minutes) == (90, 90)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "approved"])
async def test_a_recorded_plan_replays_after_the_scope_lowers_its_limit(fx, status):
    async with fx.maker() as db:
        item = await _item(fx, db)
        request = dict(authenticated_owner_member_id=fx.owner, summary="plan",
                       plan_metadata={"estimate_active_minutes": {"low": 30, "high": 60}})
        approval, created = await github_approval_service.create_initial_request(db, item, **request)
        assert created
        approval.status = status
        scope = await db.get(TeamGithubScope, item.scope_id)
        scope.max_plan_estimate_minutes = 45
        await db.commit()

        replay, created = await github_approval_service.create_initial_request(db, item, **request)
        assert (replay.id, replay.status, created) == (approval.id, status, False)
        await db.refresh(item)
        assert item.effort_limit_minutes == 60


@pytest.mark.asyncio
async def test_a_pending_plan_without_an_estimate_stores_no_limit(fx):
    async with fx.maker() as db:
        item = await _item(fx, db)
        await github_approval_service.create_initial_request(
            db, item, authenticated_owner_member_id=fx.owner, summary="plan", plan_metadata={})
    async with fx.maker() as db:
        item = await _item(fx, db)
        assert item.effort_limit_minutes is None


async def _linked_initial_plan(fx, db, metadata):
    session = await db.scalar(select(MailAgentSession).where(MailAgentSession.member_id == fx.owner))
    return await _persist_initial_approval(
        MailApprovalRequestCreate(work_item_id=fx.item_id, dispatch_nonce=fx.nonce,
                                  summary="plan", plan_metadata=metadata),
        session, db,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("listed", [False, True])
async def test_initial_paths_policy_is_checked_at_approval(fx, listed):
    async with fx.maker() as db:
        metadata = {"allowed_paths": ["test.py"]} if listed else {}
        approval = await _linked_initial_plan(fx, db, metadata)
        item = await _item(fx, db)
        scope = await db.get(TeamGithubScope, fx.scope_id)
        assert scope.initial_paths_required is False
        scope.initial_paths_required = True
        await db.commit()
        decision = dict(authenticated_leader_member_id=fx.leader, request_id=approval.id,
                        decision="approved", reason="proceed")
        if listed:
            assert (await github_approval_service.decide(db, item, **decision))[1]
        else:
            with pytest.raises(GithubApprovalError) as error:
                await github_approval_service.decide(db, item, **decision)
            assert error.value.detail == "plan_allowed_paths_missing"
            assert (await db.get(GithubApprovalRequest, approval.id)).status == "pending"
            item = await _item(fx, db)
            assert item.effort_limit_minutes is None
            scope = await db.get(TeamGithubScope, fx.scope_id)
            scope.initial_paths_required = False
            await db.commit()
            assert (await github_approval_service.decide(db, item, **decision))[1]
            scope.initial_paths_required = True
            await db.commit()
            assert not (await github_approval_service.decide(db, item, **decision))[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(("invalid_path", "expected"), [
    (False, "initial_plan_unverifiable"),
    (True, "allowed_paths_invalid"),
])
async def test_initial_paths_policy_refuses_unverified_link_or_invalid_paths(fx, invalid_path, expected):
    async with fx.maker() as db:
        path = "../outside.py" if invalid_path else "approved.py"
        approval = await _linked_initial_plan(fx, db, {"allowed_paths": [path]})
        scope = await db.get(TeamGithubScope, fx.scope_id)
        scope.initial_paths_required = True
        if not invalid_path:
            root = await db.get(MailMessage, approval.request_message_id)
            root.payload = {**root.payload, "plan_metadata": {"allowed_paths": ["different.py"]}}
        await db.commit()

        item = await _item(fx, db)
        with pytest.raises(GithubApprovalError) as error:
            await github_approval_service.decide(
                db, item, authenticated_leader_member_id=fx.leader, request_id=approval.id,
                decision="approved", reason="proceed",
            )
        assert error.value.detail == expected
    async with fx.maker() as db:
        assert (await db.get(GithubApprovalRequest, approval.id)).status == "pending"


@pytest.mark.asyncio
async def test_an_approved_plan_without_an_estimate_uses_the_ceiling_at_decision_time(fx):
    async with fx.maker() as db:
        approval = await _linked_initial_plan(fx, db, {})
        item = await _item(fx, db)
        scope = await db.get(TeamGithubScope, fx.scope_id)
        assert scope.max_item_effort_minutes == 120
        await db.commit()
        await _scope_ceiling(fx, 15)

        decision_args = dict(authenticated_leader_member_id=fx.leader, request_id=approval.id,
                             decision="approved", reason="proceed")
        decided, applied = await github_approval_service.decide(db, item, **decision_args)
        assert applied
        root = await db.get(MailMessage, decided.request_message_id)
        assert root.payload["plan_metadata"].get("estimate_active_minutes") is None
        assert item.effort_limit_minutes == 15

    async with fx.maker() as db:
        item = await _item(fx, db)
        assert item.effort_estimate_minutes is None
        assert service.effort_view(item, await db.get(TeamGithubScope, fx.scope_id, populate_existing=True)) == {
            "estimate_minutes": None, "limit_minutes": 15, "extension_minutes": 0,
            "effective_limit_minutes": 15, "ceiling_minutes": 15, "extension_cap_minutes": 0,
        }

    assert await _code(_extend(fx, 1)) == "extension_exceeds_ceiling"
    await _set(fx, effort_elapsed_seconds=899, effort_last_tick_at=T0)
    await _tick(fx, T0 + timedelta(seconds=2))
    assert len(await _notices(fx)) == 2

    await _scope_ceiling(fx, 20)
    async with fx.maker() as db:
        item = await _item(fx, db)
        _, applied = await github_approval_service.decide(db, item, **decision_args)
        assert not applied and item.effort_limit_minutes == 15
    assert (await _extend(fx, 1))["effective_limit_minutes"] == 16


@pytest.mark.asyncio
@pytest.mark.parametrize(("decision", "metadata", "legacy", "expected"), [
    ("approved", {"estimate_active_minutes": 60}, False, (60, 60)),
    ("rejected", {}, False, (None, None)),
    ("approved", {}, True, (None, None)),
])
async def test_initial_decision_preserves_explicit_estimates_rejections_and_legacy_replays(
    fx, decision, metadata, legacy, expected,
):
    async with fx.maker() as db:
        approval = await _linked_initial_plan(fx, db, metadata)
        if legacy:
            stored = await db.get(GithubApprovalRequest, approval.id)
            stored.status = "approved"
            stored.reason = "decision"
            await db.commit()
        item = await _item(fx, db)
        _, applied = await github_approval_service.decide(
            db, item, authenticated_leader_member_id=fx.leader, request_id=approval.id,
            decision=decision, reason="decision",
        )
        assert applied is not legacy
        assert (item.effort_estimate_minutes, item.effort_limit_minutes) == expected
    if legacy:
        assert await _code(_extend(fx, 1)) == "effort_limit_not_set"


@pytest.mark.asyncio
async def test_a_lost_approval_update_does_not_default_a_legacy_limit(fx, monkeypatch):
    resolve = github_approval_service.resolve_for_decision

    async def resolve_before_legacy_approval(db, item, **kwargs):
        pending = await resolve(db, item, **kwargs)
        assert pending.status == "pending"
        # A concurrent decision from the old path commits after the replay reads the request.
        async with fx.maker() as other:
            await other.execute(update(GithubApprovalRequest).where(GithubApprovalRequest.id == pending.id)
                                .values(status="approved", reason="decision", decided_at=T0))
            await other.commit()
        return pending

    monkeypatch.setattr(github_approval_service, "resolve_for_decision", resolve_before_legacy_approval)
    async with fx.maker() as db:
        approval = await _linked_initial_plan(fx, db, {})
        item = await _item(fx, db)
        decided, applied = await github_approval_service.decide(
            db, item, authenticated_leader_member_id=fx.leader, request_id=approval.id,
            decision="approved", reason="decision",
        )
        assert not applied and decided.status == "approved"

    async with fx.maker() as db:
        assert (await _item(fx, db)).effort_limit_minutes is None


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
    await _limited(fx, last_nudge_at=T0)
    await _tick(fx, T0)
    async with fx.maker() as db:
        item = await _item(fx, db)
        db.add(GithubOwnerObservationPause(work_item_id=item.id, wait_key=observation_wait_key(item), authority_sha256="x",
                                           status=status, deadline=T0 + timedelta(hours=1)))
        await db.commit()
    assert await _tick(fx, T0 + timedelta(seconds=60)) == "reset"
    async with fx.maker() as db:
        assert (await _item(fx, db)).effort_elapsed_seconds == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("recovery_state", "recovery_reason"), [
    ("working", "native_turn_started"), ("idle", "native_turn_completed"),
])
async def test_codex_usage_limit_suspends_effort_until_the_bound_owner_recovers(
    fx, monkeypatch, recovery_state, recovery_reason,
):
    await _limited(fx, dispatched_at=T0 - timedelta(minutes=10))
    await _bind_codex_lease(fx, T0 - timedelta(minutes=30))

    seen = None
    async def observe(*_args, **_kwargs):
        return {fx.owner_slot: seen} if seen is not None else None

    monkeypatch.setattr(activity, "observe_poll_team", observe)

    async def poll(at):
        async with fx.maker() as db:
            await service.tick_scope(db, await db.get(TeamGithubScope, fx.scope_id), now=at)

    await poll(T0)
    await poll(T0 + timedelta(seconds=60))
    stalled = T0 + timedelta(seconds=70)
    seen = _codex_evidence(stalled)
    await poll(stalled + timedelta(seconds=20))
    await poll(stalled + timedelta(hours=1))
    seen = _codex_evidence(stalled - timedelta(seconds=1), recovery_state, recovery_reason)
    await poll(stalled + timedelta(hours=1, seconds=10))
    seen = None
    await poll(stalled + timedelta(hours=2))
    async with fx.maker() as db:
        item = await _item(fx, db)
        assert (item.effort_elapsed_seconds, item.effort_last_tick_at) == (60, None)

    recovered = stalled + timedelta(hours=3)
    seen = _codex_evidence(recovered, recovery_state, recovery_reason)
    await poll(recovered + timedelta(seconds=10))
    await poll(recovered + timedelta(seconds=40))
    async with fx.maker() as db:
        assert (await _item(fx, db)).effort_elapsed_seconds == 90
        assert [(row.action_outcome, row.fact_time) for row in await _provider_events(db)] == [
            ("provider_usage_limit", stalled), ("provider_recovered", recovered),
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["before_lease", "before_dispatch", "other_binding", "unacknowledged"])
async def test_provider_usage_limit_requires_evidence_from_the_acked_attempt(fx, invalid):
    lease_at = T0 - timedelta(minutes=3)
    dispatched_at = T0 - timedelta(minutes=2)
    evidence_at = T0 + timedelta(seconds=10)
    if invalid == "before_lease":
        dispatched_at = lease_at - timedelta(seconds=2)
        evidence_at = lease_at - timedelta(seconds=1)
    elif invalid == "before_dispatch":
        evidence_at = dispatched_at - timedelta(seconds=1)
    ack_at = None if invalid == "unacknowledged" else T0 - timedelta(minutes=1)
    await _limited(fx, dispatched_at=dispatched_at, ack_received_at=ack_at)
    await _bind_codex_lease(fx, lease_at)

    await _tick(fx, T0)
    observed = _codex_evidence(evidence_at, pid=9000 if invalid == "other_binding" else 1001)
    result = await _tick(fx, T0 + timedelta(seconds=30), observed=observed)
    assert result == ("reset" if invalid == "unacknowledged" else "counted")
    async with fx.maker() as db:
        assert not await _provider_events(db)
        assert (await _item(fx, db)).effort_elapsed_seconds == (0 if invalid == "unacknowledged" else 30)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["launch", "owner", "lease"])
async def test_provider_interval_ends_when_the_owner_attempt_changes(fx, change):
    await _limited(fx, dispatched_at=T0 - timedelta(minutes=3))
    await _bind_codex_lease(fx, T0 - timedelta(minutes=3))
    assert await _tick(fx, T0, observed=_codex_evidence(T0)) == "reset"
    ended_at = T0 + timedelta(minutes=2)
    async with fx.maker() as db:
        item = await _item(fx, db)
        if change == "launch":
            scope = await db.get(TeamGithubScope, item.scope_id)
            launch = AgentTeamLaunch(preset_id=scope.preset_id, plan_hash="fixture-plan")
            db.add(launch)
            await db.flush()
            item.launch_id = launch.id
        elif change == "owner":
            item.owner_slot_id = fx.leader_slot
        else:
            workspace = await db.scalar(select(GithubWorkspace).where(GithubWorkspace.leased_item_id == item.id))
            workspace.leased_at = ended_at - timedelta(seconds=1)
            workspace.lease_token = "replacement-lease"
        await db.commit()

    assert await _tick(fx, ended_at) == "started"
    async with fx.maker() as db:
        started, ended = await _provider_events(db)
        assert (ended.action_outcome, ended.fact_time) == ("owner_attempt_ended", ended_at)
        assert ended.context_snapshot["launch_attempt"] == started.context_snapshot["launch_attempt"]


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


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["last_nudge_at", "owner_slot_id", "ack_received_at"])
async def test_effort_tick_rejects_owner_context_changes_before_the_write(fx, monkeypatch, field):
    await _limited(fx)
    await _tick(fx, T0)
    tick_at = T0 + timedelta(seconds=60)
    changed = fx.leader_slot if field == "owner_slot_id" else tick_at
    async with fx.maker() as db:
        execute = db.execute
        interleaved = False

        async def change_before_compare(statement, *args, **kwargs):
            nonlocal interleaved
            if statement.is_update and not interleaved:
                interleaved = True
                # Inject the new database state immediately before the compare-and-swap,
                # while the tick still holds its original ORM snapshot.
                await execute(update(GithubWorkItem).where(GithubWorkItem.id == fx.item_id)
                              .values({getattr(GithubWorkItem, field): changed})
                              .execution_options(synchronize_session=False))
            return await execute(statement, *args, **kwargs)

        monkeypatch.setattr(db, "execute", change_before_compare)
        assert await service.tick_item(db, fx.item_id, now=tick_at) == "lost"
        item = await _item(fx, db)
        assert interleaved and getattr(item, field) == changed
        assert (item.effort_elapsed_seconds, item.effort_last_tick_at) == (0, T0)


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
    assert "extend again up to the scope ceiling" in body and "limit once" not in body  # #1101
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


async def _extend(fx, minutes=15, *, leader=None, nonce=None, reason="healthy", expected=0,
                  evidence="tests pass; two files left", now=T0):
    async with fx.maker() as db:
        return await service.extend(db, work_item_id=fx.item_id, dispatch_nonce=nonce or fx.nonce,
                                    leader_member_id=leader or fx.leader, minutes=minutes, reason=reason,
                                    expected_extension_minutes=expected, evidence=evidence, now=now)


async def _code(coro):
    with pytest.raises(EffortLimitError) as caught:
        await coro
    return caught.value.code


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value", "cap"), [
    ("effort_extension_cap_minutes", 7, 7),
    ("effort_extension_cap_percent", 10, 6),
    ("effort_extension_cap_percent", 0, 0),
])
async def test_configured_extension_bound_applies_to_view_and_decision(fx, field, value, cap):
    await _limited(fx, limit=60)
    async with fx.maker() as db:
        scope = await db.get(TeamGithubScope, fx.scope_id)
        setattr(scope, field, value)
        await db.commit()
        assert service.effort_view(await _item(fx, db), scope)["extension_cap_minutes"] == cap
    assert await _code(_extend(fx, cap + 1)) == "extension_exceeds_cap"
    if cap:
        assert (await _extend(fx, cap))["extension_minutes"] == cap


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
@pytest.mark.parametrize("status", ["escalated", "merged", "released", "blocked"])
async def test_an_item_that_is_not_working_is_refused(fx, status):
    await _limited(fx, dispatch_status=status)
    assert await _code(_extend(fx, 10)) == "effort_extension_stale"


@pytest.mark.asyncio
async def test_an_item_without_a_limit_is_refused(fx):
    assert await _code(_extend(fx, 10)) == "effort_limit_not_set"


@pytest.mark.asyncio
async def test_a_replayed_extension_is_refused_by_the_expected_total_and_grants_nothing(fx):
    await _limited(fx)
    first = await _extend(fx, 10)
    assert first["extension_minutes"] == 10 and first["repeated"] is False
    # The replay still says it saw a total of 0; the recorded total is now 10 (#1100).
    assert await _code(_extend(fx, 10)) == "effort_extension_total_changed"
    # A caller that saw the new total extends again (#1101).
    assert (await _extend(fx, 5, expected=10))["extension_minutes"] == 15
    async with fx.maker() as db:
        assert (await _item(fx, db)).effort_extension_minutes == 15


@pytest.mark.asyncio
async def test_an_item_under_review_may_be_extended(fx):
    # #1059: review corrections run while the item is verifying; the extension was refused as stale.
    await _limited(fx, dispatch_status="verifying")
    result = await _extend(fx, 10)
    assert result["extension_minutes"] == 10 and result["effective_limit_minutes"] == 70


@pytest.mark.asyncio
async def test_the_leader_extends_repeatedly_up_to_the_scope_ceiling(fx):
    await _limited(fx, limit=60)
    await _scope_ceiling(fx, 90)
    assert (await _extend(fx, 15, reason="first", evidence="review finding 1"))["effective_limit_minutes"] == 75
    assert (await _extend(fx, 15, expected=15, reason="second", evidence="review finding 2"))[
        "effective_limit_minutes"] == 90
    assert await _code(_extend(fx, 1, expected=30)) == "extension_exceeds_ceiling"
    async with fx.maker() as db:
        item = await _item(fx, db)
        assert (item.effort_limit_minutes, item.effort_extension_minutes, item.effort_extension_reason) == (
            60, 30, "second")
        events = (await db.scalars(select(FactoryAuditEvent).where(
            FactoryAuditEvent.event_kind == "effort_extension").order_by(FactoryAuditEvent.id))).all()
    assert [("review finding 1" in e.sanitized_reason, "review finding 2" in e.sanitized_reason)
            for e in events] == [(True, False), (False, True)]


@pytest.mark.asyncio
async def test_a_step_beyond_the_scope_ceiling_is_refused_with_its_own_code_and_grants_nothing(fx):
    await _limited(fx, limit=60)
    await _scope_ceiling(fx, 70)
    with pytest.raises(EffortLimitError) as caught:
        await _extend(fx, 15)  # within the per-step cap of 15, but 10 minutes of room are left
    assert (caught.value.code, caught.value.status_code) == ("extension_exceeds_ceiling", 400)
    assert "10 minutes" in str(caught.value) and "70" in str(caught.value)
    assert await _code(_extend(fx, 16)) == "extension_exceeds_cap"  # the per-step cap still applies first
    async with fx.maker() as db:
        assert (await _item(fx, db)).effort_extension_minutes == 0
    assert (await _extend(fx, 10))["effective_limit_minutes"] == 70


@pytest.mark.asyncio
async def test_every_step_keeps_the_per_step_cap_of_the_original_limit(fx):
    await _limited(fx, limit=60)
    await _extend(fx, 15)
    # The cap is 25 percent of the original 60 minutes, not of the 75-minute effective limit.
    assert await _code(_extend(fx, 16, expected=15)) == "extension_exceeds_cap"


@pytest.mark.asyncio
async def test_an_expected_total_that_differs_from_the_recorded_one_is_refused(fx):
    await _limited(fx)
    with pytest.raises(EffortLimitError) as caught:
        await _extend(fx, 10, expected=5)
    assert caught.value.code == "effort_extension_total_changed" and caught.value.status_code == 409
    assert "0 minutes" in str(caught.value)  # names the recorded total the caller should have seen
    async with fx.maker() as db:
        item = await _item(fx, db)
        assert item.effort_extension_minutes == 0 and item.effort_extension_decided_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("expected", [None, -1, True, "0", 1.0])
async def test_the_expected_total_is_required(fx, expected):
    await _limited(fx)
    with pytest.raises(EffortLimitError) as caught:
        await _extend(fx, 10, expected=expected)
    assert (caught.value.code, caught.value.status_code) == ("expected_extension_minutes_required", 400)


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", [None, "", "   ", 7])
async def test_evidence_is_required(fx, evidence):
    await _limited(fx)
    with pytest.raises(EffortLimitError) as caught:
        await _extend(fx, 10, evidence=evidence)
    assert (caught.value.code, caught.value.status_code) == ("extension_evidence_required", 400)
    async with fx.maker() as db:
        assert (await _item(fx, db)).effort_extension_minutes == 0


@pytest.mark.asyncio
async def test_a_granted_extension_records_one_audit_event_with_its_reason_and_evidence(fx):
    await _limited(fx)
    assert await _code(_extend(fx, 10, expected=3)) == "effort_extension_total_changed"
    await _extend(fx, 10, reason="close to done", evidence="4 of 5 tests green")
    async with fx.maker() as db:
        events = (await db.scalars(select(FactoryAuditEvent).where(
            FactoryAuditEvent.event_kind == "effort_extension"))).all()
    assert len(events) == 1  # the refusal recorded nothing
    event = events[0]
    assert (event.item_id, event.actor_kind, event.actor_member_id, event.action_outcome) == (
        fx.item_id, "member", fx.leader, "granted")
    assert "close to done" in event.sanitized_reason and "4 of 5 tests green" in event.sanitized_reason


@pytest.mark.asyncio
async def test_two_concurrent_extensions_one_wins(fx):
    await _limited(fx)
    outcomes = await asyncio.gather(_extend(fx, 10, reason="a"), _extend(fx, 12, reason="b"), return_exceptions=True)
    wins = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    losses = [outcome for outcome in outcomes if isinstance(outcome, EffortLimitError)]
    assert len(wins) == 1 and len(losses) == 1 and losses[0].code == "effort_extension_total_changed"
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
async def test_an_old_database_without_the_effort_and_plan_columns_is_migrated(tmp_path):
    from sqlalchemy import text

    from app.database import _run_sqlite_compat_migrations, _sqlite_columns

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE team_github_scopes DROP COLUMN max_item_effort_minutes"))
        await conn.execute(text("ALTER TABLE team_github_scopes DROP COLUMN max_plan_estimate_minutes"))
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
    assert {"max_item_effort_minutes", "max_plan_estimate_minutes"} <= scope_columns
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
    request = MailEffortExtensionRequest(work_item_id=fx.item_id, dispatch_nonce=fx.nonce, minutes=15, reason="ok",
                                         expected_extension_minutes=0, evidence="three tests left")
    async with fx.maker() as db:
        with pytest.raises(HTTPException) as caught:
            await agent_mail.extend_effort_limit(request, session=owner, db=db)
        assert (caught.value.status_code, caught.value.detail) == (403, "not_item_leader")
    async with fx.maker() as db:
        granted = await agent_mail.extend_effort_limit(request, session=leader, db=db)
        assert granted["effective_limit_minutes"] == 75 and granted["repeated"] is False
    async with fx.maker() as db:
        with pytest.raises(HTTPException) as caught:
            await agent_mail.extend_effort_limit(request, session=leader, db=db)
        assert (caught.value.status_code, caught.value.detail) == (409, "effort_extension_total_changed")
    async with fx.maker() as db:
        with pytest.raises(HTTPException) as caught:
            await agent_mail.extend_effort_limit(
                request.model_copy(update={"minutes": 46, "expected_extension_minutes": 15}), session=leader, db=db)
        assert (caught.value.status_code, caught.value.detail) == (400, "extension_exceeds_cap")
    async with fx.maker() as db:
        again = await agent_mail.extend_effort_limit(
            request.model_copy(update={"minutes": 14, "expected_extension_minutes": 15}), session=leader, db=db)
        assert again["effective_limit_minutes"] == 89
    async with fx.maker() as db:
        with pytest.raises(HTTPException) as caught:
            await agent_mail.extend_effort_limit(MailEffortExtensionRequest(
                work_item_id=fx.item_id, dispatch_nonce=fx.nonce, minutes=5, reason="ok", evidence="x"),
                session=leader, db=db)
        assert (caught.value.status_code, caught.value.detail) == (400, "expected_extension_minutes_required")


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
    await _limited(fx, last_nudge_at=T0)
    async with fx.maker() as db:
        item = await _item(fx, db)
        db.add(GithubOwnerObservationPause(work_item_id=item.id, wait_key=observation_wait_key(item),
                                           authority_sha256="x", status=status,
                                           deadline=T0 + timedelta(hours=1)))
        await db.commit()
    await _refused_with(fx, "effort_extension_paused")


@pytest.mark.asyncio
async def test_progress_after_a_new_nudge_ends_prior_waits_and_allows_extension(fx):
    now = datetime.utcnow()
    await _limited(fx, last_nudge_at=now - timedelta(seconds=10))
    async with fx.maker() as db:
        item = await _item(fx, db)
        old = GithubOwnerObservationPause(work_item_id=item.id, wait_key=observation_wait_key(item),
                                          authority_sha256="old", deadline=now + timedelta(seconds=50))
        db.add(old)
        item.last_nudge_at = now
        current = GithubOwnerObservationPause(work_item_id=item.id, wait_key=observation_wait_key(item),
                                              authority_sha256="current", deadline=now + timedelta(seconds=60))
        db.add(current)
        await db.commit()
        session = await db.scalar(select(MailAgentSession).where(MailAgentSession.member_id == fx.owner))
        await report_dispatch_status(DispatchStatusReport(work_item_id=item.id, status="in_progress",
                                                           lease_token="fixture-lease"), session=session, db=db)
        for row in (old, current):
            await db.refresh(row)
            assert row.status == "ended"
        events = (await db.scalars(select(FactoryAuditEvent).where(
            FactoryAuditEvent.event_kind == "observation_wait_ended",
        ).order_by(FactoryAuditEvent.id))).all()
        assert {row.after_values["wait_pause_id"]: row.action_outcome for row in events} == {
            old.id: "contact_renewed", current.id: "contact_renewed",
        }
    assert (await _extend(fx, now=datetime.utcnow()))["extension_minutes"] == 15


@pytest.mark.asyncio
@pytest.mark.parametrize("expiration", ["superseded_nudge", "deadline"])
async def test_stale_wait_does_not_block_an_extension(fx, expiration):
    await _limited(fx, last_nudge_at=T0)
    async with fx.maker() as db:
        item = await _item(fx, db)
        changed_nudge = expiration == "superseded_nudge"
        db.add(GithubOwnerObservationPause(work_item_id=item.id, wait_key=observation_wait_key(item),
                                           authority_sha256="fixture", deadline=(
                                               T0 + timedelta(minutes=1) if changed_nudge else T0)))
        if changed_nudge:
            item.last_nudge_at = T0 + timedelta(seconds=1)
        await db.commit()
    assert (await _extend(fx, now=T0 + timedelta(seconds=2)))["extension_minutes"] == 15


@pytest.mark.asyncio
async def test_a_resolved_observation_pause_does_not_refuse(fx):
    await _limited(fx)
    async with fx.maker() as db:
        db.add(GithubOwnerObservationPause(work_item_id=fx.item_id, wait_key="w", authority_sha256="x",
                                           status="resumed", deadline=T0 + timedelta(hours=1)))
        await db.commit()
    assert (await _extend(fx, 10))["extension_minutes"] == 10


@pytest.mark.asyncio
async def test_a_repeat_of_a_granted_extension_is_refused_by_the_expected_total_without_fresh_contact(fx):
    await _limited(fx)
    await _extend(fx, 10)
    await _workspace_set(fx, lease_last_owner_contact_at=T0 - timedelta(days=2))
    assert await _code(_extend(fx, 10)) == "effort_extension_total_changed"


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
@pytest.mark.parametrize("change", ["contact", "pause", "ack", "total"])
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
        elif change == "total":  # the guarded update keys on the expected total (#1100)
            await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == fx.item_id)
                             .values(effort_extension_minutes=7))
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


# --- the Supervisor notice beyond the ceiling (#1102) ---------------------------------------------------------------


async def _bind_supervisor(fx, actor_id=6):
    async with fx.maker() as db:
        preset_id = (await db.get(TeamGithubScope, fx.scope_id)).preset_id
        await db.execute(update(AgentTeamPreset).where(AgentTeamPreset.id == preset_id)
                         .values(supervisor_actor_id=actor_id))
        await db.commit()


async def _ceiling_notices(fx):
    async with fx.maker() as db:
        return (await db.scalars(select(MailMessage).where(MailMessage.subject == "Effort ceiling reached"))).all()


@pytest.mark.asyncio
async def test_a_request_beyond_the_ceiling_notifies_the_bound_supervisor_once_per_attempt(fx, caplog):
    from app.services.agent_team_supervisor_service import supervisor_inbox

    await _limited(fx, limit=60)
    await _scope_ceiling(fx, 70)
    await _bind_supervisor(fx)
    assert await _code(_extend(fx, 15)) == "extension_exceeds_ceiling"  # the refusal itself is unchanged
    caplog.clear()
    assert await _code(_extend(fx, 12)) == "extension_exceeds_ceiling"  # the same attempt: no second notice
    assert not [r for r in caplog.records if "effort ceiling notice" in r.getMessage()]  # found, not refused
    notices = await _ceiling_notices(fx)
    assert len(notices) == 1
    notice = notices[0]
    assert notice.payload == {
        "notice": "effort_ceiling_reached", "work_item_id": fx.item_id, "issue_number": notice.payload["issue_number"],
        "limit_minutes": 60, "extension_minutes": 0, "ceiling_minutes": 70, "requested_minutes": 15,
        "supervisor_actor_id": 6}
    assert fx.nonce not in str(notice.payload) and fx.nonce not in notice.body_markdown
    assert "beyond it, the Supervisor decides" in notice.body_markdown and "10 minutes" in notice.body_markdown
    async with fx.maker() as db:
        seen = await supervisor_inbox(db, SimpleNamespace(id=6))
        assert [row.id for row in seen] == [notice.id]
        db.add(AgentTeamPreset(name="Other", description="", created_by="t", supervisor_actor_id=7))
        await db.commit()
        assert await supervisor_inbox(db, SimpleNamespace(id=7)) == []  # another team's Supervisor sees nothing
    # A new attempt is a new decision for the Supervisor.
    await _set(fx, dispatch_nonce="the-next-attempt")
    assert await _code(_extend(fx, 15, nonce="the-next-attempt")) == "extension_exceeds_ceiling"
    assert len(await _ceiling_notices(fx)) == 2


@pytest.mark.asyncio
async def test_no_ceiling_notice_without_a_bound_supervisor(fx):
    await _limited(fx, limit=60)
    await _scope_ceiling(fx, 70)
    assert await _code(_extend(fx, 15)) == "extension_exceeds_ceiling"
    assert await _ceiling_notices(fx) == []


@pytest.mark.asyncio
async def test_other_refusals_send_no_ceiling_notice(fx):
    await _limited(fx, limit=60)
    await _scope_ceiling(fx, 70)
    await _bind_supervisor(fx)
    assert await _code(_extend(fx, 16)) == "extension_exceeds_cap"  # the per-step cap is the Leader's own bound
    assert await _code(_extend(fx, 10, expected=5)) == "effort_extension_total_changed"
    await _set(fx, dispatch_status="escalated")  # an attempt that is not live
    assert await _code(_extend(fx, 15)) == "extension_exceeds_ceiling"
    assert await _ceiling_notices(fx) == []


@pytest.mark.asyncio
async def test_a_failed_notice_write_still_refuses_and_records_nothing(fx, monkeypatch):
    await _limited(fx, limit=60)
    await _scope_ceiling(fx, 70)
    await _bind_supervisor(fx)

    async def broken(*_args, **_kwargs):
        raise RuntimeError("mail store down")

    monkeypatch.setattr(service, "_add_ceiling_notice", broken)
    assert await _code(_extend(fx, 15)) == "extension_exceeds_ceiling"
    assert await _ceiling_notices(fx) == []


def _ceiling_root(**changes):
    payload = {"notice": "effort_ceiling_reached", "work_item_id": 3, "issue_number": 1102, "limit_minutes": 60,
               "extension_minutes": 0, "ceiling_minutes": 70, "requested_minutes": 15, "supervisor_actor_id": 6}
    payload.update(changes.pop("payload", {}))
    for key in changes.pop("drop", ()):
        payload.pop(key)
    root = dict(kind="message", audience_type="team_preset", thread_root_id=None, sender_type="system",
                sender_member_id=None, sender_actor_id=None, recipient_member_id=None,
                subject="Effort ceiling reached", payload=payload)
    root.update(changes)
    return SimpleNamespace(**root)


@pytest.mark.parametrize("changes,recognised", [
    ({}, True),
    ({"payload": {"extension_minutes": 15}}, True),
    ({"payload": {"extra": 1}}, False),
    ({"drop": ("requested_minutes",)}, False),
    ({"payload": {"work_item_id": "3"}}, False),
    ({"payload": {"extension_minutes": -1}}, False),
    ({"payload": {"ceiling_minutes": True}}, False),
    ({"payload": {"notice": "something_else"}}, False),
    ({"subject": "Effort limit reached"}, False),
    ({"sender_type": "member"}, False),
    ({"recipient_member_id": 4}, False),
])
def test_the_supervisor_recognises_exactly_the_ceiling_notice(changes, recognised):
    from app.services.agent_team_supervisor_service import _is_effort_ceiling_notice

    assert _is_effort_ceiling_notice(_ceiling_root(**changes)) is recognised


def test_the_charter_line_says_the_leader_extends_within_the_ceiling():
    from app.services.factory_delivery_policy import policy_guidance, scope_policy

    text = policy_guidance(scope_policy(SimpleNamespace(delivery_policy=None)), None)
    assert "the Leader extends within the scope ceiling; beyond it, the Supervisor decides" in text
    assert "Ask the operator at the limit" not in text
