"""#1246 split 1: item time from first dispatch to merge, by size band, in the campaign snapshot (design §3).

Bands are by lines changed (additions plus deletions) in R2, the PR facts recorded at merge (#1238): 0-100, 101-400
and over 400 (the Supervisor's answers on #1246, 2026-11-27). Item time runs from the first dispatch in the campaign
to a sourced merge in the campaign (V1 review 448). That dispatch is read from the delivering item lifetime, between
the later of campaign start and item creation and the merge, only where the C12 forward-coverage marker covers that
whole window. Anything else is unknown with its reason, never zero, and a band under five timed items is labelled a
small sample with its value and n.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from app.models.database import (
    CampaignSnapshot,
    CampaignUpgradeCoverage,
    GithubWorkItem,
    TeamGithubScope,
)
from app.services import campaign_record_service as campaigns
from app.services import campaign_snapshot_service as snapshots
from app.services import factory_audit_service as audit
from tests.agent_teams.test_campaign_record import (  # noqa: F401  (fixtures)
    FakeGithub,
    _issue,
    db,
    world,
)
from tests.agent_teams.test_campaign_snapshot import AS_OF, OPERATOR, T0

pytestmark = pytest.mark.asyncio
CREATED = T0 - timedelta(days=1)
MARKER = T0 - timedelta(days=2)
FACTS_FROM = T0 - timedelta(days=3)


@pytest.fixture(autouse=True)
def fresh_cache():
    snapshots.clear_cache()
    yield
    snapshots.clear_cache()


async def _marker(db, at=MARKER):
    await db.execute(text("CREATE TABLE IF NOT EXISTS deck_compat_migrations (name TEXT PRIMARY KEY, applied_at TEXT)"))
    await db.execute(text("INSERT INTO deck_compat_migrations (name, applied_at) VALUES (:n, :a)"),
                     {"n": audit.COVERAGE_MARKER, "a": at.isoformat()})
    await db.commit()


def _m(minutes):
    return T0 + timedelta(minutes=minutes)


async def _dispatch(db, item_id, minutes, launch=1):
    await audit.record_event(
        db, event_kind="work_lifecycle", source="test", occurred_at=_m(minutes),
        actor=audit.derive_actor(actor_kind="system"), item_id=item_id, after_values={"dispatch_status": "dispatched"},
        context_snapshot={"launch_attempt": audit.launch_attempt_key(item_id, launch)}, action_outcome="applied")


async def _merge(db, world, item_id, number, minutes, lines, *, artifact=None, source="github_pull_request_merged",
                 timed=True):
    """The merge fact and, when ``lines`` is given, the PR facts the watcher records for the same pull request."""
    artifact = f"o/a1/pull/{number}" if artifact is None else artifact
    await audit.record_delivery_fact(
        db, item_id=item_id, delivery_outcome="delivered", completion_kind="merged_code", fact_source=source,
        fact_time=_m(minutes) if timed else None, artifact=artifact or None,
        attempt=f"item:{item_id}:launch:1:revision:{minutes}:{artifact}")
    if lines is not None:
        additions, deletions = (None, None) if lines == "null" else (lines - lines // 3, lines // 3)
        await audit.record_event(
            db, event_kind=audit.PULL_FACTS_KIND, source="test", occurred_at=_m(minutes), fact_time=_m(minutes),
            actor=audit.derive_actor(actor_kind="system"), scope_id=world.scopes["a1"], team_preset_id=world.team,
            context_snapshot={"artifact": artifact.upper()}, after_values={"additions": additions, "deletions": deletions},
            action_outcome="applied", operation_id=f"pull_facts:{artifact}")


async def _snapshot(db, world, items, *, marker=MARKER, facts_from=FACTS_FROM, as_of=AS_OF, closed=(), cached=False):
    """``items``: (issue number, dispatch minutes or None, merge minutes or None, lines or None or "null").
    ``closed``: further closed members with no recorded outcome."""
    if marker is not None:
        await _marker(db, marker)
    if facts_from is not None:
        db.add(CampaignUpgradeCoverage(id=1, started_at=facts_from))
    labelled = [_issue("a1", number, state="closed") for number in closed]
    for number, dispatched, merged, lines in items:
        labelled.append(_issue("a1", number, state="closed"))
        item = GithubWorkItem(scope_id=world.scopes["a1"], issue_number=number, issue_title="I", issue_url=f"u/{number}",
                              github_updated_at=T0, dispatch_status="merged", created_at=CREATED)
        db.add(item)
        await db.flush()
        if dispatched is not None:
            for at in (dispatched if isinstance(dispatched, tuple) else (dispatched,)):
                await _dispatch(db, item.id, at)
        if merged is not None:
            await _merge(db, world, item.id, number, merged, lines)
    await db.commit()
    campaign = await campaigns.start(db, world.team, "label:v4.4", started_at=T0, actor=OPERATOR)
    return await snapshots.build_snapshot(db, campaign, FakeGithub(labelled={"a1": labelled}), as_of=as_of,
                                          cached=cached)


async def _again(db, world, numbers, *, cached=False):
    labelled = [_issue("a1", number, state="closed") for number in numbers]
    return await snapshots.build_snapshot(db, await campaigns.current(db, world.team),
                                          FakeGithub(labelled={"a1": labelled}), as_of=AS_OF, cached=cached)


async def _item(db, number):
    return (await db.scalars(text(f"SELECT id FROM github_work_items WHERE issue_number = {number}"))).first()


def _band(snapshot, band):
    (row,) = [row for row in snapshot.item_time.bands if row.band == band]
    return row


# Bands, the median and p85
async def test_item_time_by_band_has_its_median_and_p85_with_n(db, world):
    small = [(n, 0, 10 * n, 40) for n in range(10, 16)]  # 6 items: 100, 110, ..., 150 minutes
    big = [(20, 0, 30, 401), (21, 0, 60, 2000)]
    snapshot = await _snapshot(db, world, small + big + [(30, 0, 45, 101), (31, 0, 50, 400)])
    row = _band(snapshot, "0-100")
    assert (row.items, row.timed, row.small_sample, row.coverage) == (6, 6, False, "full")
    assert (row.median_seconds, row.p85_seconds) == (125 * 60, 150 * 60)  # nearest rank: ceil(0.85 * 6) = 6th
    assert (_band(snapshot, "101-400").items, _band(snapshot, "101-400").median_seconds) == (2, 47.5 * 60)
    over = _band(snapshot, "over-400")
    assert (over.items, over.p85_seconds, over.small_sample) == (2, 60 * 60, True)
    assert (snapshot.item_time.items, snapshot.item_time.timed, snapshot.item_time.coverage) == (10, 10, "full")


@pytest.mark.parametrize(("lines", "band"), [(0, "0-100"), (100, "0-100"), (101, "101-400"), (400, "101-400"),
                                             (401, "over-400")])
async def test_band_boundaries(db, world, lines, band):
    snapshot = await _snapshot(db, world, [(10, 0, 30, lines)])
    assert _band(snapshot, band).items == 1


async def test_a_small_sample_keeps_its_value_and_n(db, world):
    snapshot = await _snapshot(db, world, [(n, 0, 20, 10) for n in range(10, 14)])
    row = _band(snapshot, "0-100")
    assert (row.items, row.timed, row.median_seconds, row.small_sample) == (4, 4, 20 * 60, True)


async def test_an_empty_band_is_unknown_not_zero(db, world):
    snapshot = await _snapshot(db, world, [(10, 0, 20, 10)])
    row = _band(snapshot, "over-400")
    assert (row.items, row.median_seconds, row.p85_seconds, row.coverage) == (0, None, None, "unavailable")
    assert row.small_sample is False  # unknown, not a small sample


# The band's evidence
@pytest.mark.parametrize(("lines", "facts_from", "reason"), [
    (None, FACTS_FROM, "pull_request_facts_missing"),
    ("null", FACTS_FROM, "pull_request_facts_incomplete"),
    (40, T0 + timedelta(days=1), "merged_before_pull_facts_recording"),
    (40, None, "pull_facts_recording_not_installed"),
])
async def test_an_item_without_usable_pr_facts_is_in_the_unknown_band(db, world, lines, facts_from, reason):
    snapshot = await _snapshot(db, world, [(10, 0, 30, lines)], facts_from=facts_from)
    row = _band(snapshot, "unknown")
    assert (row.items, row.timed, row.median_seconds) == (1, 1, 30 * 60)
    assert reason in row.unknown_reasons
    assert snapshot.item_time.coverage == "partial"


# The first dispatch, evidence-only
async def test_the_first_dispatch_across_several_attempts_is_used(db, world):
    snapshot = await _snapshot(db, world, [(10, (20, 5, 40), 60, 10)])
    assert _band(snapshot, "0-100").median_seconds == 55 * 60


async def test_a_marker_after_the_search_windows_start_leaves_the_start_unknown(db, world):
    """An earlier dispatch in the campaign may predate the recording, so the recorded one is not taken as the first."""
    snapshot = await _snapshot(db, world, [(10, 30, 60, 10)], marker=_m(10))
    row = _band(snapshot, "0-100")
    assert (row.items, row.timed, row.median_seconds) == (1, 0, None)
    assert "dispatch_before_instrumentation" in row.unknown_reasons
    assert snapshot.item_time.coverage == "unavailable"


async def test_without_the_coverage_marker_the_section_is_unavailable(db, world):
    snapshot = await _snapshot(db, world, [(10, 0, 30, 10), (11, 0, 40, 500)], marker=None)
    assert (snapshot.item_time.timed, snapshot.item_time.coverage) == (0, "unavailable")
    assert "no_forward_coverage_marker" in snapshot.item_time.unknown_reasons


async def test_a_mix_of_evidenced_and_unknown_starts_is_partial(db, world):
    snapshot = await _snapshot(db, world, [(10, 0, 30, 10), (11, None, 40, 10)])
    row = _band(snapshot, "0-100")
    assert (row.items, row.timed, row.median_seconds, row.coverage) == (2, 1, 30 * 60, "partial")
    assert "dispatch_not_recorded" in row.unknown_reasons


async def test_a_deleted_items_start_is_not_recorded(db, world):
    snapshot = await _snapshot(db, world, [(10, 0, 30, 10)])
    item = (await db.scalars(text("SELECT id FROM github_work_items WHERE issue_number = 10"))).one()
    await db.execute(text("DELETE FROM github_work_items WHERE id = :i"), {"i": item})
    await db.commit()
    campaign = await campaigns.current(db, world.team)
    snapshot = await snapshots.build_snapshot(db, campaign, FakeGithub(labelled={"a1": [_issue("a1", 10, "closed")]}),
                                              as_of=AS_OF, cached=False)
    row = _band(snapshot, "0-100")
    assert (row.items, row.timed) == (1, 0)
    assert "item_lifetime_unknown" in row.unknown_reasons


async def test_a_failed_launch_is_not_a_dispatch(db, world):
    snapshot = await _snapshot(db, world, [(10, 20, 60, 10)])
    item = (await db.scalars(text("SELECT id FROM github_work_items WHERE issue_number = 10"))).one()
    await audit.record_event(
        db, event_kind="work_lifecycle", source="test", occurred_at=_m(5), actor=audit.derive_actor(actor_kind="system"),
        item_id=item, after_values={"dispatch_status": "failed"}, action_outcome="applied")
    await db.commit()
    snapshot = await snapshots.build_snapshot(db, await campaigns.current(db, world.team),
                                              FakeGithub(labelled={"a1": [_issue("a1", 10, "closed")]}), as_of=AS_OF,
                                              cached=False)
    assert _band(snapshot, "0-100").median_seconds == 40 * 60


async def test_another_teams_pr_facts_for_the_same_pull_request_are_not_read(db, world):
    await audit.record_event(
        db, event_kind=audit.PULL_FACTS_KIND, source="test", occurred_at=_m(1), fact_time=_m(1),
        actor=audit.derive_actor(actor_kind="system"), scope_id=world.scopes["b1"], team_preset_id=world.other,
        context_snapshot={"artifact": "o/a1/pull/10"}, after_values={"additions": 3000, "deletions": 0},
        action_outcome="applied", operation_id="pull_facts:other")
    snapshot = await _snapshot(db, world, [(10, 0, 30, 10)])
    assert (_band(snapshot, "0-100").items, _band(snapshot, "over-400").items) == (1, 0)


# V1 review 448: the campaign interval
async def test_the_first_dispatch_in_the_campaign_is_used(db, world):
    snapshot = await _snapshot(db, world, [(10, (-20, 10), 50, 10)])
    assert _band(snapshot, "0-100").median_seconds == 40 * 60


@pytest.mark.parametrize("dispatched", [-20, 60], ids=["only_before_the_campaign", "after_the_merge"])
async def test_a_dispatch_outside_the_search_window_is_not_used(db, world, dispatched):
    snapshot = await _snapshot(db, world, [(10, dispatched, 50, 10)])
    row = _band(snapshot, "0-100")
    assert (row.items, row.timed) == (1, 0)
    assert "dispatch_not_recorded" in row.unknown_reasons


async def test_a_merge_before_the_campaign_is_not_counted(db, world):
    snapshot = await _snapshot(db, world, [(10, -30, -5, 10)])
    assert snapshot.item_time.items == 0


async def test_a_pr_merged_before_the_campaign_does_not_end_the_campaigns_interval(db, world):
    await _snapshot(db, world, [(10, 10, 30, 10)])
    await _merge(db, world, await _item(db, 10), 10, -5, 10, artifact="o/a1/pull/7")
    await db.commit()
    assert _band(await _again(db, world, [10]), "0-100").median_seconds == 20 * 60


async def test_a_marker_before_the_campaign_start_covers_an_item_created_earlier(db, world):
    snapshot = await _snapshot(db, world, [(10, 10, 50, 10)], marker=CREATED + timedelta(hours=1))
    assert _band(snapshot, "0-100").median_seconds == 40 * 60


# V1 review 448: the delivering item lifetime
async def test_the_dispatch_comes_from_the_tracking_row_that_delivered(db, world):
    """Two scopes spell one repository differently; only the delivering row's dispatch counts."""
    twin = TeamGithubScope(preset_id=world.team, repo_owner="O", repo_name="A1", repo_path="/twin", enabled=True)
    db.add(twin)
    await db.flush()
    other = GithubWorkItem(scope_id=twin.id, issue_number=10, issue_title="I", issue_url="u/twin",
                           github_updated_at=T0, dispatch_status="dispatched", created_at=CREATED)
    db.add(other)
    await db.flush()
    await _dispatch(db, other.id, 5)  # earlier than the delivering row's own dispatch
    snapshot = await _snapshot(db, world, [(10, 10, 60, 10)])
    assert _band(snapshot, "0-100").median_seconds == 50 * 60


async def test_two_lifetimes_delivering_at_the_same_instant_are_ambiguous(db, world):
    twin = TeamGithubScope(preset_id=world.team, repo_owner="O", repo_name="A1", repo_path="/twin", enabled=True)
    db.add(twin)
    await db.flush()
    other = GithubWorkItem(scope_id=twin.id, issue_number=10, issue_title="I", issue_url="u/twin",
                           github_updated_at=T0, dispatch_status="merged", created_at=CREATED)
    db.add(other)
    await db.flush()
    await _dispatch(db, other.id, 5)
    await _merge(db, world, other.id, 10, 60, None)
    snapshot = await _snapshot(db, world, [(10, 10, 60, 10)])
    row = _band(snapshot, "0-100")
    assert (row.items, row.timed) == (1, 0)
    assert "ambiguous_tracking" in row.unknown_reasons


async def test_a_replacement_tracking_row_does_not_supply_the_old_merges_dispatch(db, world):
    await _snapshot(db, world, [(10, None, 50, 10)])
    await db.execute(text("DELETE FROM github_work_items WHERE id = :i"), {"i": await _item(db, 10)})
    replacement = GithubWorkItem(scope_id=world.scopes["a1"], issue_number=10, issue_title="I", issue_url="u/r",
                                 github_updated_at=T0, dispatch_status="dispatched", created_at=CREATED)
    db.add(replacement)
    await db.flush()
    await _dispatch(db, replacement.id, 20)
    await db.commit()
    row = _band(await _again(db, world, [10]), "0-100")
    assert (row.items, row.timed) == (1, 0)
    assert "item_lifetime_unknown" in row.unknown_reasons


# V1 review 448: a sourced merge time
async def test_a_design_artifact_acceptance_does_not_displace_the_pr_merge(db, world):
    await _snapshot(db, world, [(10, 0, 60, 10)])
    await _merge(db, world, await _item(db, 10), 10, 30, None, artifact="design:doc", source="design_artifact_accepted")
    await db.commit()
    row = _band(await _again(db, world, [10]), "0-100")
    assert (row.items, row.median_seconds) == (1, 60 * 60)


async def test_a_merge_record_without_its_merge_time_is_unknown(db, world):
    await _snapshot(db, world, [(10, 0, None, None)])
    await _merge(db, world, await _item(db, 10), 10, 30, 10, timed=False)
    await db.commit()
    snapshot = await _again(db, world, [10])
    assert (snapshot.item_time.items, snapshot.item_time.timed) == (1, 0)
    assert "merge_time_not_recorded" in snapshot.item_time.unknown_reasons


async def test_a_legacy_automatic_merge_time_still_counts(db, world):
    await _snapshot(db, world, [(10, 10, None, None)])
    item = await db.get(GithubWorkItem, await _item(db, 10))
    item.auto_merged_at = _m(40)
    await db.commit()
    row = _band(await _again(db, world, [10]), "unknown")
    assert (row.items, row.median_seconds) == (1, 30 * 60)
    assert "artifact_not_recorded" in row.unknown_reasons


# V1 review 448: tied deliveries
async def test_tied_deliveries_with_and_without_an_artifact_do_not_raise(db, world):
    await _snapshot(db, world, [(10, 0, 30, 10)])
    await _merge(db, world, await _item(db, 10), 10, 30, None, artifact="")
    await db.commit()
    row = _band(await _again(db, world, [10]), "0-100")
    assert (row.items, row.median_seconds) == (1, 30 * 60)


async def test_tied_deliveries_with_conflicting_artifacts_are_unknown(db, world):
    await _snapshot(db, world, [(10, 0, 30, 10)])
    await _merge(db, world, await _item(db, 10), 10, 30, 500, artifact="o/a1/pull/99")
    await db.commit()
    row = _band(await _again(db, world, [10]), "unknown")
    assert (row.items, row.median_seconds) == (1, 30 * 60)
    assert "conflicting_artifacts" in row.unknown_reasons


# V1 review 448: coverage and the cache
async def test_a_member_with_an_unknown_outcome_makes_the_section_partial(db, world):
    snapshot = await _snapshot(db, world, [(10, 0, 30, 10)], closed=(11,))
    assert (snapshot.item_time.coverage, snapshot.item_time.timed) == ("partial", 1)
    assert "delivery_history_missing" in snapshot.item_time.unknown_reasons


async def test_installing_the_coverage_marker_misses_the_cache(db, world):
    snapshot = await _snapshot(db, world, [(10, 0, 30, 10)], marker=None, cached=True)
    assert snapshot.item_time.timed == 0
    await _marker(db)
    assert (await _again(db, world, [10], cached=True)).item_time.timed == 1


# The cutoff
async def test_a_merge_after_the_cutoff_is_not_counted(db, world):
    snapshot = await _snapshot(db, world, [(10, 0, 30, 10), (11, 0, 90, 10)], as_of=_m(60))
    assert (_band(snapshot, "0-100").items, snapshot.item_time.items) == (1, 1)


# Frozen payloads
async def test_a_stored_v2_snapshot_without_the_section_still_reads_back_unchanged(db, world):
    snapshot = await _snapshot(db, world, [(10, 0, 30, 10)])
    old = snapshot.model_dump(mode="json")
    old.pop("item_time")
    old["calculation_version"] = "campaign-snapshot-v2"
    campaign = await campaigns.current(db, world.team)
    db.add(CampaignSnapshot(campaign_id=campaign.id, version=1, calculation_version="campaign-snapshot-v2",
                            payload=old, created_by="operator"))
    await db.commit()
    frozen = await snapshots.freeze(db, campaign, FakeGithub(), actor=OPERATOR)
    assert (frozen.item_time, frozen.calculation_version) == (None, "campaign-snapshot-v2")
    assert frozen.model_dump(mode="json", exclude={"item_time"}) == old
    assert (await snapshots.latest(db, campaign.id)).payload == old


async def test_a_new_freeze_stores_v3_with_the_section(db, world):
    await _snapshot(db, world, [(10, 0, 30, 10)])
    campaign = await campaigns.current(db, world.team)
    frozen = await snapshots.freeze(db, campaign, FakeGithub(labelled={"a1": [_issue("a1", 10, "closed")]}),
                                    actor=OPERATOR, as_of=AS_OF)
    stored = await snapshots.latest(db, campaign.id)
    assert (stored.calculation_version, frozen.calculation_version) == ("campaign-snapshot-v3", "campaign-snapshot-v3")
    assert stored.payload["item_time"]["bands"][0]["band"] == "0-100"
