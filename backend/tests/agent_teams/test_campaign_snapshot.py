"""#1244: the campaign snapshot service (design §3 and §9).

A snapshot is built from the campaign, its live membership and the recorded facts, with a calculation version. The
same inputs give the same snapshot. A partial history is unknown with a reason, never zero. The cache serves the
Overview panel's polls for a short time and misses on any new record a measure reads. Frozen snapshots are append-only:
a freeze or the Director's recompute adds a version and leaves the earlier ones unchanged.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from app.database import Base
from app.models.database import (
    AgentTeamLaunch,
    CampaignSnapshot,
    CampaignUpgradeCoverage,
    GithubCheckRunHistory,
    GithubCheckRunHistoryCoverage,
    GithubMergeAttemptCoverage,
    GithubRequiredCheckWatch,
    GithubRateLimitHistoryCoverage,
    GithubRateLimitWindow,
    GithubWorkItem,
    MailTeamMember,
    TeamGithubScope,
)
from app.services import campaign_record_service as campaigns
from app.services import campaign_snapshot_service as snapshots
from app.services import factory_audit_service as audit
from app.services.factory_metrics_service import build_metrics_window
from app.services.github_check_observation import ACTIONS_APP_ID
from app.services.github_rate_limit_history import _active_writers
from tests.agent_teams.test_campaign_record import (  # noqa: F401  (fixtures)
    FakeGithub,
    _issue,
    _stopped,
    db as db,
    world as world
)

from tests.agent_teams.test_retro_attempt_collector import _dispatch, _request, _wait

pytestmark = pytest.mark.asyncio
T0 = datetime(2026, 11, 20, 9, 0, 0)
AS_OF = T0 + timedelta(days=10)
OPERATOR = audit.derive_actor(actor_kind="operator")
SYSTEM = audit.derive_actor(actor_kind="system")


def _client(**changes):
    """Team A's campaign issues: a1#1 open, a1#2 closed, a2#3 closed."""
    labelled = {"a1": [_issue("a1", 1), _issue("a1", 2, state="closed")], "a2": [_issue("a2", 3, state="closed")]}
    return FakeGithub(labelled=labelled, **changes)


@pytest.fixture(autouse=True)
def fresh_cache():
    snapshots.clear_cache()
    yield
    snapshots.clear_cache()


async def _deliver(db, item_id, at):
    """The merge fact the delivery recorder writes, at ``at``."""
    item = await db.get(GithubWorkItem, item_id)
    item.dispatch_status = "merged"
    await audit.record_delivery_fact(
        db, item_id=item_id, delivery_outcome="delivered", completion_kind="merged_code",
        fact_source="github_pull_request_merged", fact_time=at, artifact=f"pr:{item_id}",
        attempt=f"item:{item_id}:launch:None:revision:None", snapshot={"artifact_version": "b" * 40})
    await db.commit()


async def _close_undelivered(db, item_id, at):
    """The recorded non-delivery the watcher writes for an issue closed as not planned."""
    await audit.record_delivery_fact(
        db, item_id=item_id, delivery_outcome="closed_without_delivery", completion_kind="closed",
        fact_source="github_issue_closed", fact_time=at, artifact=f"issue:{item_id}",
        attempt=f"item:{item_id}:launch:None:revision:None")
    await db.commit()


async def _campaign(db, world, *, recording=T0 - timedelta(days=1), undelivered=True):
    if recording is not None:
        db.add(GithubRateLimitHistoryCoverage(id=1, started_at=recording))
        await db.commit()
    await _deliver(db, world.items[("a1", 2)], T0 + timedelta(days=1))
    if undelivered:  # a2#3 was closed without delivery, and that is recorded
        await _close_undelivered(db, world.items[("a2", 3)], T0 + timedelta(days=1))
    return await campaigns.start(db, world.team, "label:v4.4", started_at=T0, actor=OPERATOR)


def _measures(snapshot):
    return {sample.name: sample for sample in snapshot.metrics}


# The acceptance
async def test_the_same_inputs_give_the_same_snapshot(db, world):
    campaign = await _campaign(db, world)
    first = await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False)
    second = await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False)
    assert first == second and first is not second
    assert first.model_dump_json() == second.model_dump_json()
    by_name = _measures(first)
    assert [s.name for s in first.metrics] == sorted(by_name)
    assert {name: by_name[name].value for name in (
        "issues_in_scope", "merged", "closed_without_delivery", "deferred", "rate_limited_seconds")} == {
        "issues_in_scope": 3, "merged": 1, "closed_without_delivery": 1, "deferred": 0, "rate_limited_seconds": 0.0}
    assert all(s.calculation_version == snapshots.CALCULATION_VERSION for s in first.metrics)
    assert first.calculation_version == snapshots.CALCULATION_VERSION


async def test_a_partial_membership_is_unknown_with_a_reason(db, world):
    campaign = await _campaign(db, world)
    snapshot = await snapshots.build_snapshot(db, campaign, _client(broken={"a2"}), as_of=AS_OF, cached=False)
    by_name = _measures(snapshot)
    for name in ("issues_in_scope", "merged", "closed_without_delivery"):
        assert by_name[name].value is None and by_name[name].coverage == "partial"
        assert any("github_unreadable: o/a2" in reason for reason in by_name[name].unknown_reasons)
    assert by_name["deferred"].value == 0  # departures are recorded facts, not GitHub reads


async def test_a_campaign_from_before_the_recording_is_unknown_with_a_reason(db, world):
    campaign = await _campaign(db, world, recording=T0 + timedelta(days=2))
    snapshot = await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False)
    rate = _measures(snapshot)["rate_limited_seconds"]
    assert rate.value is None and rate.coverage == "partial"  # the recording began during the campaign
    assert any("before_recording" in reason for reason in rate.unknown_reasons)
    coverage = {entry.measure: entry for entry in snapshot.recorded_coverage}
    assert not coverage["rate_limit_windows"].covered


async def test_a_campaign_without_a_recording_marker_is_unknown_not_zero(db, world):
    campaign = await _campaign(db, world, recording=None)
    rate = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False))[
        "rate_limited_seconds"]
    assert rate.value is None and rate.coverage == "unavailable"
    assert "recording_not_installed" in rate.unknown_reasons


async def test_a_departure_counts_as_deferred(db, world):
    campaign = await _campaign(db, world)
    await _stopped(db, world.items[("a1", 1)], "deferred", T0 + timedelta(days=1), target_milestone="v4.5")
    by_name = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False))
    assert (by_name["deferred"].value, by_name["issues_in_scope"].value) == (1, 2)


# The cache
async def test_a_poll_within_the_lifetime_is_served_from_the_cache(db, world):
    campaign = await _campaign(db, world)
    first = await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF)
    assert await snapshots.build_snapshot(db, campaign, _client(broken={"a1", "a2"}), as_of=AS_OF) is first


@pytest.mark.parametrize("record", ["departure", "rate_limit_window", "work_item", "campaign_start"])
async def test_a_new_record_invalidates_the_cache(db, world, record):
    campaign = await _campaign(db, world)
    first = await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF)
    if record == "departure":
        await _stopped(db, world.items[("a1", 1)], "deferred", T0 + timedelta(days=1), target_milestone="v4.5")
    elif record == "rate_limit_window":
        db.add(GithubRateLimitWindow(source="core", window_reset_at=T0 + timedelta(days=1, hours=1),
                                     reset_at=T0 + timedelta(days=1, hours=1), started_at=T0 + timedelta(days=1)))
        await db.commit()
    elif record == "work_item":
        item = await db.get(GithubWorkItem, world.items[("a2", 3)])
        item.dispatch_status = "merged"
        item.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await db.commit()
        await _deliver(db, item.id, T0 + timedelta(days=2))
    else:
        await campaigns.adjust_start(db, world.team, started_at=T0 - timedelta(days=1), expected_started_at=T0,
                                     actor=OPERATOR)
        campaign = await campaigns.current(db, world.team)
    second = await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF)
    assert second is not first and second != first


async def test_the_cache_expires_after_its_lifetime(db, world, monkeypatch):
    campaign = await _campaign(db, world)
    now = [1000.0]
    monkeypatch.setattr(snapshots, "_clock", lambda: now[0])
    first = await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF)
    now[0] += snapshots.CACHE_SECONDS - 1
    assert await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF) is first
    now[0] += 2
    assert await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF) is not first


# Frozen snapshots: append-only versions
async def _versions(db, campaign_id):
    return (await db.scalars(select(CampaignSnapshot).where(CampaignSnapshot.campaign_id == campaign_id)
                             .order_by(CampaignSnapshot.version))).all()


async def test_each_freeze_or_recompute_creates_a_new_version_and_leaves_earlier_ones_unchanged(db, world):
    campaign = await _campaign(db, world)
    first = await snapshots.freeze(db, campaign, _client(), actor=SYSTEM, as_of=AS_OF)
    stored = (await _versions(db, campaign.id))[0].payload
    await _stopped(db, world.items[("a1", 1)], "deferred", T0 + timedelta(days=1), target_milestone="v4.5")
    second = await snapshots.recompute(db, campaign, _client(), actor=OPERATOR, as_of=AS_OF)
    rows = await _versions(db, campaign.id)
    assert [(row.version, row.calculation_version) for row in rows] == [
        (1, snapshots.CALCULATION_VERSION), (2, snapshots.CALCULATION_VERSION)]
    assert rows[0].payload == stored == first.model_dump(mode="json")
    assert rows[1].payload == second.model_dump(mode="json") != stored
    assert (await snapshots.latest(db, campaign.id)).version == 2


@pytest.mark.parametrize("actor", [
    audit.derive_actor(actor_kind="member", member_id=61), SYSTEM,
    audit.derive_actor(actor_kind="external_actor", external_actor_id=6),
], ids=["member", "system", "supervisor"])
async def test_only_the_director_recomputes_a_frozen_snapshot(db, world, actor):
    campaign = await _campaign(db, world)
    await snapshots.freeze(db, campaign, _client(), actor=SYSTEM, as_of=AS_OF)
    with pytest.raises(campaigns.CampaignError) as refused:
        await snapshots.recompute(db, campaign, _client(), actor=actor, as_of=AS_OF)
    assert refused.value.code == "campaign_recompute_director_only"
    assert [row.version for row in await _versions(db, campaign.id)] == [1]


async def test_a_recompute_needs_a_frozen_snapshot(db, world):
    campaign = await _campaign(db, world)
    with pytest.raises(campaigns.CampaignError) as refused:
        await snapshots.recompute(db, campaign, _client(), actor=OPERATOR, as_of=AS_OF)
    assert refused.value.code == "campaign_snapshot_not_frozen"
    assert await _versions(db, campaign.id) == []


async def test_a_frozen_snapshot_records_who_made_it(db, world):
    campaign = await _campaign(db, world)
    await snapshots.freeze(db, campaign, _client(), actor=SYSTEM, as_of=AS_OF)
    await snapshots.recompute(db, campaign, _client(), actor=OPERATOR, as_of=AS_OF)
    assert [row.created_by for row in await _versions(db, campaign.id)] == ["system", "operator"]


async def test_the_snapshot_table_is_created_on_upgrade_and_keeps_existing_rows(db, world):
    """A database from before #1244 gains the table at start; the campaign and its records stay."""
    campaign = await _campaign(db, world)
    await db.execute(text("DROP TABLE campaign_snapshots"))
    await db.commit()
    await db.run_sync(lambda session: Base.metadata.create_all(session.connection()))
    await db.commit()
    await snapshots.freeze(db, campaign, _client(), actor=SYSTEM, as_of=AS_OF)
    assert [row.version for row in await _versions(db, campaign.id)] == [1]
    assert (await campaigns.current(db, world.team)).id == campaign.id


async def test_the_metrics_window_samples_keep_no_calculation_version(db, world):
    window = await build_metrics_window(db, window_start=T0, window_end=AS_OF)
    assert window.metrics and all(sample.calculation_version is None for sample in window.metrics)


async def test_a_changed_campaign_start_misses_the_cache_without_any_event(db, world):
    campaign = await _campaign(db, world)
    first = await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF)
    await db.execute(text("UPDATE campaigns SET started_at = :start WHERE id = :id"),
                     {"start": T0 - timedelta(days=3), "id": campaign.id})
    await db.commit()
    await db.refresh(campaign)
    second = await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF)
    assert second is not first and second.started_at == T0 - timedelta(days=3)


async def test_a_superseded_issue_is_not_deferred(db, world):
    campaign = await _campaign(db, world)
    await _stopped(db, world.items[("a1", 1)], "superseded", T0 + timedelta(days=1), superseding_pr_number=77)
    by_name = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False))
    assert (by_name["deferred"].value, by_name["issues_in_scope"].value) == (0, 2)


async def test_another_teams_delivery_of_the_same_issue_is_not_merged_here(db, world):
    """Team B watches o/a2 too and delivered #3 there; for team A's campaign, a2#3 stays closed without delivery."""
    from app.models.database import TeamGithubScope

    campaign = await _campaign(db, world)
    scope = TeamGithubScope(preset_id=world.other, repo_owner="o", repo_name="a2", repo_path="/private/b-a2")
    db.add(scope)
    await db.flush()
    other = GithubWorkItem(scope_id=scope.id, issue_number=3, issue_title="Issue 3", issue_url="https://x/a2/3",
                           github_updated_at=T0, dispatch_status="pending")
    db.add(other)
    await db.commit()
    await _deliver(db, other.id, T0 + timedelta(days=1))
    by_name = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False))
    assert (by_name["merged"].value, by_name["closed_without_delivery"].value) == (1, 1)


# V1 review 6079984028
CUTOFF = T0 + timedelta(days=5)


@pytest.mark.parametrize("hours,deferred,in_scope", [(-1, 1, 2), (1, 0, None)], ids=["before", "after"])
async def test_a_departure_counts_only_up_to_the_cutoff(db, world, hours, deferred, in_scope):
    campaign = await _campaign(db, world)
    await _stopped(db, world.items[("a1", 1)], "deferred", CUTOFF + timedelta(hours=hours), target_milestone="v4.5")
    by_name = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=CUTOFF, cached=False))
    assert by_name["deferred"].value == deferred
    assert by_name["issues_in_scope"].value == in_scope  # membership after a later departure is indeterminate
    if in_scope is None:
        assert "departure_after_cutoff" in by_name["issues_in_scope"].unknown_reasons


@pytest.mark.parametrize("hours,merged", [(-1, 2), (1, None)], ids=["before", "after"])
async def test_a_delivery_counts_only_up_to_the_cutoff(db, world, hours, merged):
    campaign = await _campaign(db, world)
    await _deliver(db, world.items[("a2", 3)], CUTOFF + timedelta(hours=hours))
    by_name = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=CUTOFF, cached=False))
    assert by_name["merged"].value == merged
    if merged is None:
        assert "delivery_after_cutoff" in by_name["merged"].unknown_reasons
    else:
        assert by_name["closed_without_delivery"].value == 0


async def test_a_membership_read_after_the_cutoff_is_unknown(db, world):
    campaign = await _campaign(db, world)
    snapshot = await snapshots.build_snapshot(db, campaign, _client(), as_of=CUTOFF,
                                              observed_at=CUTOFF + timedelta(minutes=1), cached=False)
    for name in ("issues_in_scope", "merged", "closed_without_delivery"):
        sample = _measures(snapshot)[name]
        assert sample.value is None and "membership_read_after_cutoff" in sample.unknown_reasons
    assert _measures(snapshot)["deferred"].value == 0  # recorded facts stay known


async def test_a_newly_watched_repository_misses_the_cache(db, world):
    from app.models.database import TeamGithubScope

    campaign = await _campaign(db, world)
    first = await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF)
    db.add(TeamGithubScope(preset_id=world.team, repo_owner="o", repo_name="a3", repo_path="/private/a3"))
    await db.commit()
    client = _client()
    client.labelled["a3"] = [_issue("a3", 4)]
    second = await snapshots.build_snapshot(db, campaign, client, as_of=AS_OF)
    assert (_measures(first)["issues_in_scope"].value, _measures(second)["issues_in_scope"].value) == (3, 4)


async def test_a_status_change_at_an_unchanged_timestamp_misses_the_cache(db, world):
    campaign = await _campaign(db, world)
    first = await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF)
    latest = await db.scalar(select(func.max(GithubWorkItem.updated_at)))
    await db.execute(text("UPDATE github_work_items SET dispatch_status = 'cancelled', updated_at = :at"
                          " WHERE id = :id"), {"at": latest, "id": world.items[("a1", 1)]})
    await db.commit()
    second = await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF)
    assert second is not first


async def test_a_cache_hit_reports_rate_limit_coverage_that_became_incomplete(db, world, monkeypatch):
    from app.services.github_rate_limit_history import GithubRateLimitHistory

    campaign = await _campaign(db, world)
    first = await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF)
    assert (_measures(first)["rate_limited_seconds"].value, _measures(first)["rate_limited_seconds"].coverage) == (
        0.0, "full")
    writer = GithubRateLimitHistory(None)
    monkeypatch.setattr(writer, "_ensure_task", lambda: None)  # the window stays awaiting persistence
    writer.observe("core", T0 + timedelta(days=1), T0 + timedelta(days=1, hours=1))
    _active_writers[db.bind] = writer
    try:
        second = await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF)
    finally:
        _active_writers.pop(db.bind, None)
    rate = _measures(second)["rate_limited_seconds"]
    assert rate.value is None and rate.coverage == "partial"
    assert second.friction.rate_limited_time == rate


async def test_a_repeated_finish_returns_the_first_frozen_version(db, world):
    campaign = await _campaign(db, world)
    first = await snapshots.freeze(db, campaign, _client(), actor=SYSTEM, as_of=AS_OF)
    await _stopped(db, world.items[("a1", 1)], "deferred", T0 + timedelta(days=1), target_milestone="v4.5")
    again = await snapshots.freeze(db, campaign, _client(), actor=SYSTEM, as_of=AS_OF + timedelta(minutes=5))
    assert again == first
    assert [row.version for row in await _versions(db, campaign.id)] == [1]


async def _second_session(tmp_path):
    from sqlalchemy import event as sa_event
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'campaigns.db'}")

    def pragmas(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")

    sa_event.listen(engine.sync_engine, "connect", pragmas)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def test_two_simultaneous_first_finishes_both_return_version_one(db, world, tmp_path):
    import asyncio

    campaign = await _campaign(db, world)
    engine, maker = await _second_session(tmp_path)

    async def finish():
        async with maker() as session:
            own = await campaigns.current(session, world.team)
            return await snapshots.freeze(session, own, _client(), actor=SYSTEM, as_of=AS_OF)

    try:
        results = await asyncio.gather(finish(), finish())
    finally:
        await engine.dispose()
    assert results[0] == results[1]
    assert [row.version for row in await _versions(db, campaign.id)] == [1]


async def test_two_concurrent_director_recomputes_keep_every_version(db, world, tmp_path):
    import asyncio

    campaign = await _campaign(db, world)
    await snapshots.freeze(db, campaign, _client(), actor=SYSTEM, as_of=AS_OF)
    campaign_id = campaign.id
    engine, maker = await _second_session(tmp_path)

    async def recompute():
        async with maker() as session:
            own = await campaigns.current(session, world.team)
            return await snapshots.recompute(session, own, _client(), actor=OPERATOR, as_of=AS_OF)

    try:
        await asyncio.gather(recompute(), recompute())
    finally:
        await engine.dispose()
    db.expire_all()
    assert [row.version for row in await _versions(db, campaign_id)] == [1, 2, 3]


async def test_a_recorded_non_delivery_is_not_merged(db, world):
    campaign = await _campaign(db, world)
    await audit.record_delivery_fact(
        db, item_id=world.items[("a2", 3)], delivery_outcome="closed_without_delivery", completion_kind="closed",
        fact_source="github_issue_closed", fact_time=T0 + timedelta(days=2), artifact="issue:3",
        attempt=f"item:{world.items[('a2', 3)]}:launch:None:revision:None")
    await db.commit()
    by_name = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False))
    assert (by_name["merged"].value, by_name["closed_without_delivery"].value) == (1, 1)


# V1 review 6080664549 (findings 1, 2 and 4; finding 3 is #1395)
async def test_a_recompute_reads_membership_after_the_cutoff_as_unknown(db, world):
    """GitHub's membership today says nothing about an earlier cutoff: the normal recompute entry point is unknown."""
    campaign = await _campaign(db, world)
    first = await snapshots.freeze(db, campaign, _client(), actor=SYSTEM, as_of=CUTOFF)
    assert _measures(first)["issues_in_scope"].value == 3
    later = _client()
    later.labelled["a2"].append(_issue("a2", 6))  # an issue added after the finish
    for observed_at in (None, CUTOFF + timedelta(days=1)):  # the real read time, by default or supplied
        again = await snapshots.recompute(db, campaign, later, actor=OPERATOR, as_of=CUTOFF, observed_at=observed_at)
        in_scope = _measures(again)["issues_in_scope"]
        assert in_scope.value is None and "membership_read_after_cutoff" in in_scope.unknown_reasons


async def test_an_overlapping_refresh_never_replaces_a_newer_cutoffs_entry(db, world, monkeypatch):
    campaign = await _campaign(db, world)
    older, newer = T0 + timedelta(days=2), T0 + timedelta(days=4)
    await snapshots.build_snapshot(db, campaign, _client(), as_of=older)
    real, interleaved = snapshots._rate_limited, []

    async def rate_limited_while_the_newer_read_lands(db_, coverage, started, as_of):
        sample = await real(db_, coverage, started, as_of)
        if not interleaved and as_of == older:
            interleaved.append(as_of)
            await snapshots.build_snapshot(db, campaign, _client(), as_of=newer)  # populates the newer entry
            return sample.model_copy(update={"value": None, "unknown_reasons": ["changed"]})  # forces a replace
        return sample

    monkeypatch.setattr(snapshots, "_rate_limited", rate_limited_while_the_newer_read_lands)
    refreshed = await snapshots.build_snapshot(db, campaign, _client(), as_of=older)
    assert interleaved and refreshed.as_of == older  # the caller gets its own refreshed result
    assert snapshots._cache[campaign.id][2].as_of == newer  # the newer read's entry stays
    monkeypatch.setattr(snapshots, "_rate_limited", real)
    assert (await snapshots.build_snapshot(db, campaign, _client(), as_of=newer)).as_of == newer


async def test_a_retired_team_without_repositories_does_not_keep_a_cached_zero(db, world):
    from app.models.database import AgentTeamPreset

    team = AgentTeamPreset(name="No repositories")
    db.add(team)
    await db.commit()
    campaign = await campaigns.start(db, team.id, "label:v4.4", started_at=T0, actor=OPERATOR)
    first = await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF)
    assert _measures(first)["issues_in_scope"].value == 0
    await db.execute(text("DELETE FROM agent_team_presets WHERE id = :id"), {"id": team.id})
    await db.commit()
    second = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF))["issues_in_scope"]
    assert second.value is None and "team_retired" in second.unknown_reasons


# #1395: missing delivery history reads as unknown, never as a non-delivery (V1 review 6080664549, finding 3)
async def _delete_item(db, item_id):
    await db.delete(await db.get(GithubWorkItem, item_id))
    await db.commit()


@pytest.mark.parametrize("scope_link", [True, False], ids=["scope_kept", "scope_link_gone"])
async def test_a_deleted_items_recorded_merge_still_counts_as_merged(db, world, scope_link):
    campaign = await _campaign(db, world)
    await _delete_item(db, world.items[("a1", 2)])
    if not scope_link:  # the team key alone places it
        await db.execute(text("UPDATE factory_audit_events SET scope_id = NULL WHERE event_kind = 'delivery_evidence'"))
        await db.commit()
    by_name = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False))
    assert (by_name["merged"].value, by_name["closed_without_delivery"].value) == (1, 1)
    assert (by_name["merged"].unknown_count, by_name["merged"].coverage) == (0, "full")


@pytest.mark.parametrize("hours,merged", [(-1, 2), (1, None)], ids=["before", "after"])
async def test_a_legacy_auto_merge_without_a_delivery_row_counts_as_merged(db, world, hours, merged):
    campaign = await _campaign(db, world, undelivered=False)
    item = await db.get(GithubWorkItem, world.items[("a2", 3)])
    item.auto_merged_at = CUTOFF + timedelta(hours=hours)
    await db.commit()
    by_name = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=CUTOFF, cached=False))
    assert by_name["merged"].value == merged
    if merged is None:
        assert "delivery_after_cutoff" in by_name["merged"].unknown_reasons
    else:
        assert (by_name["closed_without_delivery"].value, by_name["merged"].unknown_count) == (0, 0)


async def test_a_closed_issue_without_any_evidence_is_unknown_not_undelivered(db, world):
    campaign = await _campaign(db, world, undelivered=False)
    by_name = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False))
    for name, known in (("merged", 1), ("closed_without_delivery", 0)):
        sample = by_name[name]
        assert (sample.value, sample.unknown_count, sample.coverage) == (known, 1, "partial")
        assert sample.unknown_reasons == ["delivery_history_missing"]
    assert by_name["issues_in_scope"].value == 3


async def test_nothing_known_about_any_closed_issue_is_unknown_not_zero(db, world):
    campaign = await _campaign(db, world, undelivered=False)
    await _delete_item(db, world.items[("a1", 2)])
    # Nothing left places the merge: no team key, and neither its item, its scope nor its team reference survives.
    await db.execute(text("UPDATE factory_audit_events SET team_context_key = NULL, scope_id = NULL, "
                          "team_preset_id = NULL WHERE event_kind = 'delivery_evidence'"))
    await db.commit()
    by_name = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False))
    for name in ("merged", "closed_without_delivery"):
        assert (by_name[name].value, by_name[name].unknown_count, by_name[name].coverage) == (None, 2, "unavailable")


@pytest.mark.parametrize("hours,undelivered", [(-1, 1), (1, None)], ids=["before", "after"])
async def test_a_recorded_non_delivery_counts_only_up_to_the_cutoff(db, world, hours, undelivered):
    campaign = await _campaign(db, world, undelivered=False)
    await _close_undelivered(db, world.items[("a2", 3)], CUTOFF + timedelta(hours=hours))
    by_name = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=CUTOFF, cached=False))
    assert by_name["closed_without_delivery"].value == (undelivered if undelivered else 0)
    assert by_name["closed_without_delivery"].unknown_count == (0 if undelivered else 1)


def _strip(*fields):
    """An upgraded database: the delivery rows recorded before an identity field was snapshotted lack it."""
    return text("UPDATE factory_audit_events SET context_snapshot = json_remove(context_snapshot, "
                + ", ".join(f"'$.{field}'" for field in fields) + ") WHERE event_kind = 'delivery_evidence'")


async def test_an_upgraded_delivery_row_without_its_identity_is_read_through_its_live_item(db, world):
    campaign = await _campaign(db, world)
    await db.execute(_strip("issue_number", "repo_owner", "repo_name"))
    await db.execute(text("UPDATE factory_audit_events SET team_context_key = NULL, scope_id = NULL, "
                          "team_preset_id = NULL WHERE event_kind = 'delivery_evidence'"))  # the live item alone
    await db.commit()
    by_name = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False))
    assert (by_name["merged"].value, by_name["closed_without_delivery"].value) == (1, 1)
    assert by_name["merged"].unknown_count == 0


async def test_an_upgraded_delivery_row_whose_item_is_gone_leaves_the_closed_issues_unknown(db, world):
    """Its merge can't be placed: it may be any closed issue's, so none of them is known to be undelivered."""
    campaign = await _campaign(db, world)
    await db.execute(_strip("issue_number", "repo_owner", "repo_name"))
    await db.commit()
    await _delete_item(db, world.items[("a1", 2)])
    by_name = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False))
    for name in ("merged", "closed_without_delivery"):
        assert (by_name[name].value, by_name[name].unknown_count) == (None, 2)
        assert by_name[name].unknown_reasons == ["delivery_identity_unknown"]


async def test_an_outcome_that_is_not_a_non_delivery_leaves_the_issue_unknown(db, world):
    campaign = await _campaign(db, world, undelivered=False)
    await audit.record_delivery_fact(
        db, item_id=world.items[("a2", 3)], delivery_outcome="unknown", completion_kind="unknown",
        fact_source="github_issue_closed", fact_time=T0 + timedelta(days=2), artifact="issue:3",
        attempt=f"item:{world.items[('a2', 3)]}:launch:None:revision:None")
    await db.commit()
    sample = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False))[
        "closed_without_delivery"]
    assert (sample.value, sample.unknown_count, sample.unknown_reasons) == (0, 1, ["delivery_history_missing"])


async def test_a_non_delivery_that_cannot_be_placed_does_not_hide_the_merges(db, world):
    """Only an unplaced merge could be any closed issue's; an unplaced non-delivery leaves just that issue unknown."""
    campaign = await _campaign(db, world)
    await db.execute(text("UPDATE factory_audit_events SET context_snapshot = json_remove(context_snapshot, "
                          "'$.issue_number') WHERE delivery_outcome = 'closed_without_delivery'"))
    await db.commit()
    await _delete_item(db, world.items[("a2", 3)])
    by_name = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False))
    assert (by_name["merged"].value, by_name["merged"].unknown_count) == (1, 1)
    assert by_name["merged"].unknown_reasons == ["delivery_history_missing"]



# V1 review 6085711218
async def test_an_upgraded_row_whose_item_is_gone_is_placed_by_its_live_scope(db, world):
    """No team key, the item deleted, but the row's scope is the team's own and its snapshot names the issue."""
    campaign = await _campaign(db, world)
    await _close_undelivered(db, world.items[("a1", 2)], T0 + timedelta(hours=12))  # before its merge
    await db.execute(text("UPDATE factory_audit_events SET team_context_key = NULL, team_preset_id = NULL "
                          "WHERE event_kind = 'delivery_evidence' AND delivery_outcome = 'delivered'"))  # scope alone
    await db.commit()
    await _delete_item(db, world.items[("a1", 2)])
    by_name = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False))
    assert (by_name["merged"].value, by_name["closed_without_delivery"].value) == (1, 1)
    assert by_name["merged"].unknown_count == 0


async def test_a_repository_owner_is_matched_whatever_its_capitalization(db, world):
    from app.models.database import TeamGithubScope

    campaign = await _campaign(db, world)  # a1#2's merge is recorded under owner "o"
    scope = await db.get(TeamGithubScope, world.scopes["a1"])
    scope.repo_owner = "O"
    await db.commit()
    await _close_undelivered(db, world.items[("a1", 2)], T0 + timedelta(hours=12))  # recorded under "O"
    by_name = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False))
    assert (by_name["merged"].value, by_name["closed_without_delivery"].value) == (1, 1)
    assert by_name["merged"].unknown_count == 0


async def test_a_reopened_issues_known_merge_is_kept_beside_an_unknown_closed_issue(db, world):
    campaign = await _campaign(db, world, undelivered=False)
    reopened = FakeGithub(labelled={"a1": [_issue("a1", 1), _issue("a1", 2)], "a2": [_issue("a2", 3, state="closed")]})
    by_name = _measures(await snapshots.build_snapshot(db, campaign, reopened, as_of=AS_OF, cached=False))
    assert (by_name["merged"].value, by_name["merged"].unknown_count, by_name["merged"].coverage) == (1, 1, "partial")


async def test_a_legacy_merge_time_set_later_misses_the_cache(db, world):
    campaign = await _campaign(db, world, undelivered=False)
    first = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF))
    assert (first["merged"].value, first["merged"].unknown_count) == (1, 1)
    await db.execute(text("UPDATE github_work_items SET auto_merged_at = :at WHERE id = :id"),
                     {"at": T0 + timedelta(days=2), "id": world.items[("a2", 3)]})
    await db.commit()
    again = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF))
    assert (again["merged"].value, again["merged"].unknown_count) == (2, 0)


# V1 review 6086115646
async def test_a_deleted_scopes_merge_is_placed_by_its_surviving_team_reference(db, world):
    """No team key, the scope deleted with its items, and the team watching the repository again."""
    from app.models.database import TeamGithubScope

    campaign = await _campaign(db, world)
    await _close_undelivered(db, world.items[("a1", 2)], T0 + timedelta(hours=12))  # before its merge
    await db.execute(text("UPDATE factory_audit_events SET team_context_key = NULL "
                          "WHERE event_kind = 'delivery_evidence' AND delivery_outcome = 'delivered'"))
    await db.commit()
    await db.delete(await db.get(TeamGithubScope, world.scopes["a1"]))
    await db.commit()
    db.add(TeamGithubScope(preset_id=world.team, repo_owner="o", repo_name="a1", repo_path="/private/a1-again",
                           enabled=True))
    await db.commit()
    by_name = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False))
    assert (by_name["merged"].value, by_name["closed_without_delivery"].value) == (1, 1)
    assert by_name["merged"].unknown_count == 0


async def test_one_repository_watched_under_two_spellings_counts_each_issue_once(db, world):
    from app.models.database import TeamGithubScope

    campaign = await _campaign(db, world)
    db.add(TeamGithubScope(preset_id=world.team, repo_owner="O", repo_name="a1", repo_path="/private/a1-upper",
                           enabled=True))
    await db.commit()
    by_name = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False))
    assert (by_name["issues_in_scope"].value, by_name["merged"].value, by_name["closed_without_delivery"].value) == (
        3, 1, 1)


async def test_a_fact_that_names_another_team_never_counts_here_through_a_live_item(db, world):
    """The reconciliation writer keeps the original attempt's team key; the item and scope are this team's now."""
    campaign = await _campaign(db, world)
    other = await audit.context_key_for(db, "team", world.other)
    await db.commit()
    await _deliver(db, world.items[("a2", 3)], T0 + timedelta(days=2))
    await db.execute(text("UPDATE factory_audit_events SET team_context_key = :other WHERE event_kind = "
                          "'delivery_evidence' AND delivery_outcome = 'delivered' AND item_id = :item"),
                     {"other": other, "item": world.items[("a2", 3)]})
    await db.commit()
    by_name = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False))
    assert (by_name["merged"].value, by_name["closed_without_delivery"].value) == (1, 1)


# V1 review 6086713296
@pytest.mark.parametrize("evidence", ["legacy_auto_merge", "keyless_row"])
async def test_a_replacement_team_during_the_read_never_adds_to_the_old_campaign(db, world, monkeypatch, evidence):
    """The team is retired and replaced under the same preset id after its membership was read: the replacement's
    merge of a2#3 (closed without delivery for the old team) must not count for the old campaign."""
    from app.models.database import FactoryContextKey

    campaign = await _campaign(db, world)
    read = campaigns.members

    async def replaced_during_the_read(db, campaign, client):
        membership = await read(db, campaign, client)
        await db.execute(text("UPDATE factory_context_keys SET retired_at = :at WHERE context_key = :key"),
                         {"at": AS_OF, "key": campaign.team_context_key})
        db.add(FactoryContextKey(key_kind="team", numeric_id=world.team, context_key="team:replacement"))
        if evidence == "legacy_auto_merge":
            await db.execute(text("UPDATE github_work_items SET auto_merged_at = :at WHERE id = :id"),
                             {"at": T0 + timedelta(days=3), "id": world.items[("a2", 3)]})
        else:  # a row with no team key that names the reused preset id
            await _deliver(db, world.items[("a2", 3)], T0 + timedelta(days=3))
            await db.execute(text("UPDATE factory_audit_events SET team_context_key = NULL, scope_id = NULL, "
                                  "item_id = NULL WHERE id = (SELECT max(id) FROM factory_audit_events)"))
        await db.commit()
        return membership

    monkeypatch.setattr(campaigns, "members", replaced_during_the_read)
    by_name = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False))
    assert (by_name["merged"].value, by_name["closed_without_delivery"].value) == (1, 1)


# #1252: the issue count of each repository
async def test_a_snapshot_counts_the_issues_of_each_repository(db, world):
    campaign = await _campaign(db, world)
    snapshot = await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False)
    assert [(r.repo, r.issues, r.complete, r.reason) for r in snapshot.repositories] == [
        ("o/a1", 2, True, None), ("o/a2", 1, True, None)]
    assert sum(r.issues for r in snapshot.repositories) == _measures(snapshot)["issues_in_scope"].value


async def test_a_repository_that_could_not_be_read_has_an_unknown_count(db, world):
    campaign = await _campaign(db, world)
    snapshot = await snapshots.build_snapshot(db, campaign, _client(broken=("a2",)), as_of=AS_OF, cached=False)
    by_repo = {r.repo: r for r in snapshot.repositories}
    assert (by_repo["o/a1"].issues, by_repo["o/a1"].complete) == (2, True)
    assert (by_repo["o/a2"].issues, by_repo["o/a2"].complete) == (None, False) and by_repo["o/a2"].reason


async def test_a_snapshot_stored_before_repository_counts_still_reads(db, world):
    campaign = await _campaign(db, world)
    payload = (await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False)).model_dump(mode="json")
    del payload["repositories"]
    assert snapshots.Snapshot.model_validate(payload).repositories == []


async def test_the_cached_snapshot_is_read_without_building_one(db, world):
    campaign = await _campaign(db, world)
    assert snapshots.cached_snapshot(campaign.id) is None
    built = await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF)
    assert snapshots.cached_snapshot(campaign.id) == built


async def test_a_cutoff_gap_leaves_every_repository_count_unknown_in_the_snapshot(db, world):
    campaign = await _campaign(db, world)
    snapshot = await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, observed_at=AS_OF + timedelta(days=1), cached=False)
    assert [(r.issues, r.complete, r.cutoff_gaps) for r in snapshot.repositories] == [
        (None, True, ["membership_read_after_cutoff"])] * 2


async def _ci_campaign(db, world):
    db.add_all([CampaignUpgradeCoverage(id=1, started_at=T0),
                GithubCheckRunHistoryCoverage(id=1, started_at=T0),
                GithubMergeAttemptCoverage(id=1, started_at=T0)])
    await audit.install_forward_coverage(await db.connection(), installed_at=T0)
    for scope_id in world.scopes.values():
        scope = await db.get(TeamGithubScope, scope_id)
        scope.created_at = T0
    for (_, issue), item_id in world.items.items():
        item = await db.get(GithubWorkItem, item_id)
        item.pr_number = 100 + issue
        item.created_at = T0
    await db.commit()
    return await _campaign(db, world)


def _ci_run(world, run_id, *, repo="a1", pr=101, head="a" * 40, name="Compile", app="github-actions",
            required=True, outcome="success", queued=0, started=10, completed=30, observed=None):
    def at(seconds):
        return T0 + timedelta(seconds=seconds) if seconds is not None else None

    app_id = ACTIONS_APP_ID if app == "github-actions" else 404
    return GithubCheckRunHistory(
        repository=f"o/{repo}", check_run_id=run_id, scope_id=world.scopes[repo], pr_number=pr, head_sha=head,
        required=required, check_name=name, app_id=app_id, app_slug=app, conclusion=outcome, job_created_at=at(queued),
        started_at=at(started), completed_at=at(completed), observed_at=observed or at(completed or 1))


async def test_ci_measures_work_for_two_apps_and_preserve_their_evidence(db, world):
    campaign = await _ci_campaign(db, world)
    runs = [
        _ci_run(world, 1, outcome="failure", completed=40),
        _ci_run(world, 2, queued=40, started=60, completed=130),
        _ci_run(world, 3, head="b" * 40, completed=20),
        _ci_run(world, 4, name="lint", required=False, completed=50),
        _ci_run(world, 8, app="buildbot", queued=None, completed=15),
        _ci_run(world, 5, repo="a2", pr=103, name="Nightly suite", app="buildbot", outcome="failure", queued=None),
        _ci_run(world, 6, repo="a2", pr=103, name="Nightly suite", app="buildbot", started=31, completed=76),
        _ci_run(world, 7, pr=999, completed=1000),
        _ci_run(world, 9, repo="b1", pr=109),
        _ci_run(world, 10, observed=AS_OF + timedelta(seconds=1)),
    ]
    db.add_all(runs)
    await db.commit()
    first = await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF)
    rows = {(row.repository, row.app_slug, row.check_name): row for row in first.ci.required_checks}
    actions = rows["o/a1", "github-actions", "Compile"]
    assert actions.median_seconds.value == 30 and actions.p85_seconds.value == 70
    assert actions.median_seconds.sample_count == 3 and actions.median_seconds.coverage == "full"
    assert actions.queue_seconds.value == 10 and actions.queue_seconds.sample_count == 3
    outcomes = {(row.repository, row.app_slug, row.check_name): row for row in first.ci.check_outcomes}
    actions_outcomes = outcomes["o/a1", "github-actions", "Compile"]
    assert (actions_outcomes.reruns.value, actions_outcomes.flaky_heads.value) == (1, 1)
    assert first.ci.slowest_required_check == rows["o/a2", "buildbot", "Nightly suite"]
    assert len(rows) == 3 and outcomes["o/a1", "buildbot", "Compile"].reruns.value == 0
    buildbot = rows["o/a2", "buildbot", "Nightly suite"]
    assert buildbot.median_seconds.value == 32.5
    assert outcomes["o/a2", "buildbot", "Nightly suite"].flaky_heads.value == 1
    assert buildbot.queue_seconds.value is None and buildbot.queue_seconds.coverage == "unavailable"
    assert "ci_does_not_report_queue_time" in buildbot.queue_seconds.unknown_reasons
    (optional,) = first.ci.non_required_checks
    assert optional.runner_seconds.value == 40 and optional.runner_seconds.sample_count == 1

    runs[0].conclusion = "cancelled"
    await db.commit()
    refreshed = await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF)
    assert refreshed is not first
    refreshed_actions = next(row for row in refreshed.ci.check_outcomes
                             if row.app_slug == "github-actions" and row.check_name == "Compile")
    assert refreshed_actions.flaky_heads.value == 0


async def test_ci_unknowns_and_queue_load_use_only_observed_overlapping_prs(db, world):
    campaign = await _ci_campaign(db, world)
    db.add(GithubWorkItem(scope_id=world.scopes["a1"], issue_number=4, issue_title="Issue 4",
                          issue_url="https://x/a1/4", github_updated_at=T0, pr_number=104, created_at=T0))
    client = _client()
    client.labelled["a1"].append(_issue("a1", 4))
    db.add_all([
        _ci_run(world, 1, queued=0, started=1, completed=100),
        _ci_run(world, 2, queued=0, started=1, completed=90, name="Other build"),
        _ci_run(world, 3, pr=102, queued=10, started=13, completed=100),
        _ci_run(world, 4, pr=104, queued=20, started=31, completed=100),
        _ci_run(world, 5, pr=104, queued=None, started=None, completed=110, name="Incomplete"),
    ])
    await db.commit()
    result = await snapshots.build_snapshot(db, campaign, client, as_of=AS_OF, cached=False)
    load = result.ci.queue_under_load
    assert load.quiet_queue_seconds.value == 1 and load.quiet_queue_seconds.sample_count == 2
    assert load.loaded_queue_seconds.value == 11 and load.loaded_queue_seconds.sample_count == 1
    incomplete = next(r for r in result.ci.required_checks if r.check_name == "Incomplete")
    assert incomplete.median_seconds.value is None and incomplete.median_seconds.unknown_count == 1

    partial = await snapshots.build_snapshot(db, campaign, _client(broken={"a2"}), as_of=AS_OF, cached=False)
    assert partial.ci.queue_under_load.quiet_queue_seconds.coverage == "partial"
    assert partial.ci.item_time_share.value is None


async def test_friction_uses_scoped_merge_path_waits_and_errors_through_the_cutoff(db, world):
    campaign = await _ci_campaign(db, world)
    item = await db.get(GithubWorkItem, world.items["a1", 1])
    launch = AgentTeamLaunch(preset_id=world.team, plan_hash="fixture-1247")
    db.add(launch)
    for member_id in (60, 61):
        db.add(MailTeamMember(id=member_id, identity_key=f"fixture-{member_id}", repo_id="a1",
                              repo_path="/r", repo_name="a1", display_name=f"Member {member_id}"))
    await db.flush()
    await _dispatch(db, item, launch.id, T0, current_nonce="fixture-1247")
    request = await _request(db, item, "fixture-1247", T0)
    await _wait(db, "decision", f"request:{request.id}", T0, T0 + timedelta(seconds=20), item=item,
                request_id=request.id, after={"awaited_role": "leader"})
    await _wait(db, "mail", "message:1247", T0 + timedelta(seconds=20), T0 + timedelta(seconds=30),
                team=world.team, after={"owner_member_id": request.owner_member_id, "awaited_member_id": 60})
    db.add(GithubRequiredCheckWatch(
        work_item_id=item.id, dispatch_nonce="fixture-1247", scope_revision=0, pr_number=101, head_sha="a" * 40,
        owner_slot_id=1, policy_digest="fixture-1247", observed_at=T0, changed_at=T0))
    db.add(_ci_run(world, 1, queued=30, started=40, completed=60))
    member = audit.derive_actor(actor_kind="member", member_id=61)
    for item_id, actor, when in ((item.id, member, T0), (world.items["b1", 9], member, T0),
                                 (item.id, SYSTEM, T0), (item.id, member, AS_OF + timedelta(seconds=1))):
        await audit.record_event(db, event_kind="owner_status_report", source="test", occurred_at=when,
                                 actor=actor, item_id=item_id, action_outcome="rejected")
    merged_at = T0 + timedelta(seconds=120)
    await audit.record_delivery_fact(
        db, item_id=item.id, delivery_outcome="delivered", completion_kind="merged_code",
        fact_source="github_pull_request_merged", fact_time=merged_at, artifact="o/a1/pull/101",
        attempt=audit.launch_attempt_key(item.id, launch.id), launch_attempt=audit.launch_attempt_key(item.id, launch.id))
    fact = await audit.record_event(
        db, event_kind=audit.PULL_FACTS_KIND, source="test", occurred_at=merged_at, fact_time=merged_at, actor=SYSTEM,
        scope_id=world.scopes["a1"], context_snapshot={"artifact": "o/a1/pull/101"},
        after_values={"merge_path": "unknown", "merged_by": "Integration Bot", "merged_by_type": "Bot"})
    await db.commit()

    before = await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF)
    assert before.friction.hand_merges.value is None
    assert before.friction.decision_waits.value == 2 and before.friction.decision_wait_seconds.value == 30
    assert before.friction.agent_errors.value == 1 and before.friction.agent_errors.sample_count == 1
    assert before.friction.rate_limited_time == _measures(before)["rate_limited_seconds"]

    single_member = FakeGithub(labelled={"a1": [_issue("a1", 1)]})
    share = await snapshots.build_snapshot(db, campaign, single_member, as_of=AS_OF, cached=False)
    assert share.ci.item_time_share.value == pytest.approx(30 / 120)
    db.add(_ci_run(world, 3, head="b" * 40, completed=70))
    await db.commit()
    unwatched = await snapshots.build_snapshot(db, campaign, single_member, as_of=AS_OF, cached=False)
    assert unwatched.ci.item_time_share.value is None
    assert "required_checks_not_reconciled_with_attempt_intervals" in unwatched.ci.item_time_share.unknown_reasons
    db.add(_ci_run(world, 2, observed=AS_OF + timedelta(seconds=1)))
    await db.commit()
    earlier = await snapshots.build_snapshot(db, campaign, single_member, as_of=AS_OF, cached=False)
    assert earlier.ci.item_time_share.value is None
    assert "required_check_evidence_after_cutoff" in earlier.ci.item_time_share.unknown_reasons

    await audit.record_event(
        db, event_kind=audit.MERGE_PATH_KIND, source="test", occurred_at=merged_at + timedelta(seconds=1), actor=SYSTEM,
        team_context_key=fact.team_context_key, scope_context_key=fact.scope_context_key,
        context_snapshot={"artifact": "o/a1/pull/101"}, after_values={"merge_path": "outside"})
    await db.commit()
    after = await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF)
    (outside,) = after.friction.outside_merges
    assert after.friction.hand_merges.value == 1 and after.friction.hand_merges.coverage == "partial"
    assert (outside.artifact, outside.merged_by, outside.merged_by_type) == ("o/a1/pull/101", "Integration Bot", "Bot")


async def test_ci_friction_are_additive_to_older_frozen_snapshots(db, world):
    campaign = await _campaign(db, world, recording=None)
    new = await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False)
    assert new.ci.item_time_share.value is None
    assert new.friction.decision_waits.value is None and new.friction.agent_errors.value is None
    old = new.model_dump(exclude={"ci", "friction"})
    old["calculation_version"] = "campaign-snapshot-v4"
    restored = snapshots.Snapshot.model_validate(old)
    assert restored.ci is None and restored.friction is None
