"""#1244: the campaign snapshot service (design §3 and §9).

A snapshot is built from the campaign, its live membership and the recorded facts, with a calculation version. The
same inputs give the same snapshot. A partial history is unknown with a reason, never zero. The cache serves the
Overview panel's polls for a short time and misses on any new record a measure reads. Frozen snapshots are append-only:
a freeze or the Director's recompute adds a version and leaves the earlier ones unchanged.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from app.database import Base
from app.models.database import (
    CampaignSnapshot,
    GithubRateLimitHistoryCoverage,
    GithubRateLimitWindow,
    GithubWorkItem,
)
from app.services import campaign_record_service as campaigns
from app.services import campaign_snapshot_service as snapshots
from app.services import factory_audit_service as audit
from app.services.factory_metrics_service import build_metrics_window
from tests.agent_teams.test_campaign_record import (  # noqa: F401  (fixtures)
    FakeGithub,
    _issue,
    _stopped,
    db,
    world,
)

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


async def _campaign(db, world, *, recording=T0 - timedelta(days=1)):
    if recording is not None:
        db.add(GithubRateLimitHistoryCoverage(id=1, started_at=recording))
    item = await db.get(GithubWorkItem, world.items[("a1", 2)])
    item.dispatch_status = "merged"  # a1#2 was delivered; a2#3 was closed without delivery
    await db.commit()
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
    db.add(GithubWorkItem(scope_id=scope.id, issue_number=3, issue_title="Issue 3", issue_url="https://x/a2/3",
                          github_updated_at=T0, dispatch_status="merged"))
    await db.commit()
    by_name = _measures(await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False))
    assert (by_name["merged"].value, by_name["closed_without_delivery"].value) == (1, 1)
