"""#1453 (#1246 part 2): throughput, change markers and before-and-after in the campaign snapshot (design §3, §8).

Merges per week count each member once, in the UTC ISO week of its first merge in the campaign; a PR merge GitHub gave
no time is unknown, never placed in a week. Change markers are applied ``policy_change`` events of the campaign's team
lifetime from an allowlist of configuration sources: the Supervisor's clock and registration transitions and the
dispatch pause are not settings (the Supervisor's answers on #1246, 2026-11-25 and 2026-11-27). A marker carries the
changed field names, never their values. Before and after each marker: the median item time and n of the items wholly
on each side (dispatched and merged before; dispatched at or after), with the straddling and unknown items counted,
a small-sample label under five, and no verdict.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.models.database import (
    CampaignSnapshot,
    CampaignUpgradeCoverage,
    GithubWorkItem,
)
from app.services import campaign_record_service as campaigns
from app.services import campaign_snapshot_service as snapshots
from app.services import factory_audit_service as audit
from tests.agent_teams.test_campaign_item_time import (
    CREATED,
    FACTS_FROM,
    _dispatch,
    _m,
    _marker,
    _merge,
)
from tests.agent_teams.test_campaign_record import (  # noqa: F401  (fixtures)
    FakeGithub,
    _issue,
    db,
    world,
)
from tests.agent_teams.test_campaign_snapshot import AS_OF, OPERATOR, T0

pytestmark = pytest.mark.asyncio
DAY, MIN = 24 * 60, 60
SETTINGS = "agent_team_service.update_work_settings"


@pytest.fixture(autouse=True)
def fresh_cache():
    snapshots.clear_cache()
    yield
    snapshots.clear_cache()


async def _policy(db, world, minutes, *, source=SETTINGS, outcome="applied", team=None, before=None, after=None,
                  changed=None):
    """A ``policy_change`` event; a scope source carries the scope and its changed field names, as the route writes."""
    scope = source.startswith("agent_teams.")
    await audit.record_event(
        db, event_kind="policy_change", source=source, occurred_at=_m(minutes),
        actor=audit.derive_actor(actor_kind="operator"),
        team_preset_id=None if scope else (world.team if team is None else team),
        scope_id=world.scopes["a1"] if scope else None,
        before_values={"github_nudge_grace_seconds": 60} if before is None else before,
        after_values={"github_nudge_grace_seconds": 1234} if after is None else after,
        context_snapshot={"scope_changed_fields": changed} if changed is not None else None,
        action_outcome=outcome, operation_id=f"policy:{source}:{minutes}:{outcome}")


async def _flow(db, world, items, *, cached=False, as_of=AS_OF, marker=True):
    """``items``: (issue number, dispatch minutes or None, merge minutes (one or a tuple) or None, timed).
    ``marker``: the forward-coverage marker before the campaign (True), at a given time, or not installed (None)."""
    if marker is True:
        await _marker(db)
    elif marker is not None:
        await _marker(db, marker)
    db.add(CampaignUpgradeCoverage(id=1, started_at=FACTS_FROM))
    labelled = []
    for number, dispatched, merged, timed in items:
        labelled.append(_issue("a1", number, state="closed"))
        item = GithubWorkItem(scope_id=world.scopes["a1"], issue_number=number, issue_title="I", issue_url=f"u/{number}",
                              github_updated_at=T0, dispatch_status="merged", created_at=CREATED)
        db.add(item)
        await db.flush()
        if dispatched is not None:
            await _dispatch(db, item.id, dispatched)
        for n, at in enumerate(() if merged is None else (merged if isinstance(merged, tuple) else (merged,))):
            await _merge(db, world, item.id, number, at, 40 if n == 0 else None, timed=timed)  # one PR facts record
    await db.commit()
    campaign = await campaigns.current(db, world.team) or await campaigns.start(
        db, world.team, "label:v4.4", started_at=T0, actor=OPERATOR)
    return await snapshots.build_snapshot(db, campaign, FakeGithub(labelled={"a1": labelled}), as_of=as_of,
                                          cached=cached)


def _weeks(snapshot):
    return [(row.week_start, row.merges, row.partial) for row in snapshot.flow.weeks]


# Merges per week
async def test_merges_are_counted_per_utc_iso_week_with_the_edge_weeks_partial(db, world):
    """T0 is Friday 2026-11-20 09:00 and the cutoff Monday 2026-11-30 09:00: three weeks, the first and last cut."""
    snapshot = await _flow(db, world, [(10, 0, 60, True), (11, 0, 4 * DAY, True), (12, 0, 5 * DAY, True),
                                       (13, 0, 10 * DAY - 60, True)])
    assert _weeks(snapshot) == [(date(2026, 11, 16), 1, True), (date(2026, 11, 23), 2, False),
                                (date(2026, 11, 30), 1, True)]
    assert (snapshot.flow.merges, snapshot.flow.unknown_merges, snapshot.flow.coverage) == (4, 0, "full")
    assert (snapshot.flow.marker_coverage, snapshot.flow.unknown_reasons) == ("full", [])


async def test_a_member_merged_twice_counts_once_in_its_first_merge_week(db, world):
    snapshot = await _flow(db, world, [(10, 0, (60, 4 * DAY), True)])
    assert _weeks(snapshot) == [(date(2026, 11, 16), 1, True), (date(2026, 11, 23), 0, False),
                                (date(2026, 11, 30), 0, True)]


async def test_a_merge_github_gave_no_time_is_unknown_not_placed_in_a_week(db, world):
    snapshot = await _flow(db, world, [(10, 0, 60, True), (11, 0, 120, False)])
    assert (snapshot.flow.merges, snapshot.flow.unknown_merges) == (1, 1)
    assert sum(row.merges for row in snapshot.flow.weeks) == 1
    assert (snapshot.flow.coverage, snapshot.flow.unknown_reasons) == ("partial", ["merge_time_not_recorded"])


async def test_a_merge_outside_the_campaign_is_not_throughput(db, world):
    snapshot = await _flow(db, world, [(10, None, -60, True), (11, 0, 60, True)])
    assert snapshot.flow.merges == 1


@pytest.mark.parametrize(("broken", "coverage"), [({"a2"}, "partial"), ({"a1", "a2"}, "unavailable")])
async def test_a_membership_read_gap_makes_throughput_partial_or_unavailable(db, world, broken, coverage):
    await _flow(db, world, [(10, 0, 60, True)])
    snapshot = await snapshots.build_snapshot(db, await campaigns.current(db, world.team),
                                              FakeGithub(labelled={"a1": [_issue("a1", 10, state="closed")]},
                                                         broken=broken), as_of=AS_OF, cached=False)
    assert snapshot.flow.coverage == coverage
    assert any("a2" in reason for reason in snapshot.flow.unknown_reasons)


# V2 review 473: unknown inputs are never complete
async def test_an_item_time_unknown_start_makes_the_flow_partial_with_its_reason(db, world):
    await _policy(db, world, 1000)
    flow = (await _flow(db, world, [(10, 0, 60, True), (11, None, 1300, True)])).flow
    assert (flow.coverage, flow.unknown_reasons, flow.markers[0].unknown) == ("partial", ["dispatch_not_recorded"], 1)


async def test_without_the_coverage_marker_the_markers_are_partial(db, world):
    """Changes before audit recording began are unknown, so an empty or short marker list is not complete."""
    await _policy(db, world, 1000)
    flow = (await _flow(db, world, [(10, 0, 60, True)], marker=None)).flow
    assert (flow.marker_coverage, flow.coverage) == ("partial", "partial")
    assert "no_forward_coverage_marker" in flow.unknown_reasons


async def test_a_coverage_marker_after_the_campaign_start_leaves_the_markers_partial(db, world):
    flow = (await _flow(db, world, [(10, 0, 60, True)], marker=_m(10))).flow
    assert (flow.marker_coverage, flow.coverage) == ("partial", "partial")
    assert "change_markers_before_audit_recording" in flow.unknown_reasons


# Change markers
@pytest.mark.parametrize("source", ["agent_teams.update_github_scope",
                                    "agent_teams.update_github_scope_continuation_policy",
                                    "agent_team_service.update_preset", SETTINGS])
async def test_each_configuration_source_is_a_marker_with_its_changed_field_names(db, world, source):
    changed = ["max_scope_amendments"] if source == "agent_teams.update_github_scope" else None
    await _policy(db, world, 600, source=source, changed=changed)
    (marker,) = (await _flow(db, world, [(10, 0, 60, True)])).flow.markers
    assert (marker.at, marker.source) == (_m(600), source)
    assert marker.fields == (changed or ["github_nudge_grace_seconds"])


@pytest.mark.parametrize("source", ["agent_team_supervisor.clock", "agent_team_supervisor.registration",
                                    "agent_team_service.set_dispatch_pause"])
async def test_supervisor_transitions_and_the_dispatch_pause_are_not_markers(db, world, source):
    await _policy(db, world, 600, source=source)
    assert (await _flow(db, world, [(10, 0, 60, True)])).flow.markers == []


@pytest.mark.parametrize("case", ["rejected", "another_team", "after_the_cutoff", "before_the_campaign", "no_change",
                                  "scope_no_change"])
async def test_only_an_applied_change_of_this_team_in_the_campaign_is_a_marker(db, world, case):
    if case == "rejected":
        await _policy(db, world, 600, outcome="rejected")
    elif case == "another_team":
        await _policy(db, world, 600, team=world.other)
    elif case == "after_the_cutoff":
        await _policy(db, world, 11 * DAY)
    elif case == "before_the_campaign":
        await _policy(db, world, -60)
    elif case == "no_change":  # a save that changed nothing is not a change
        await _policy(db, world, 600, after={"github_nudge_grace_seconds": 60})
    else:  # the scope route names the fields it changed; an empty list is no change, whatever the full rows say
        await _policy(db, world, 600, source="agent_teams.update_github_scope", changed=[])
    assert (await _flow(db, world, [(10, 0, 60, True)])).flow.markers == []


async def test_a_marker_stores_field_names_never_their_values(db, world):
    await _policy(db, world, 600, before={"github_nudge_grace_seconds": 60, "wake_followup_enabled": True},
                  after={"github_nudge_grace_seconds": 1234, "wake_followup_enabled": True})
    flow = (await _flow(db, world, [(10, 0, 60, True)])).flow
    assert flow.markers[0].fields == ["github_nudge_grace_seconds"]
    assert "1234" not in flow.model_dump_json()


# Before and after
async def test_only_items_wholly_on_one_side_are_compared(db, world):
    """Marker at 1000: two before, two straddling (one merged exactly at it), two after (one dispatched exactly at
    it), one start unknown."""
    await _policy(db, world, 1000)
    flow = (await _flow(db, world, [(10, 0, 100, True), (11, 0, 120, True), (12, 900, 1100, True),
                                    (13, 1000, 1060, True), (14, 1200, 1500, True), (15, None, 1300, True),
                                    (16, 0, 1000, True)])).flow
    (marker,) = flow.markers
    assert (marker.before.items, marker.before.median_seconds) == (2, 110 * MIN)
    assert (marker.after.items, marker.after.median_seconds) == (2, 180 * MIN)
    assert (marker.straddling, marker.unknown) == (2, 1)


@pytest.mark.parametrize(("n", "small"), [(4, True), (5, False)])
async def test_a_side_under_five_items_is_labelled_a_small_sample_not_hidden(db, world, n, small):
    await _policy(db, world, 1000)
    flow = (await _flow(db, world, [(10 + i, 0, 100 + i, True) for i in range(n)])).flow
    side = flow.markers[0].before
    assert (side.items, side.small_sample, side.median_seconds is not None) == (n, small, True)
    assert (flow.markers[0].after.items, flow.markers[0].after.median_seconds,
            flow.markers[0].after.small_sample) == (0, None, False)


async def test_a_marker_carries_no_verdict(db, world):
    await _policy(db, world, 1000)
    (marker,) = (await _flow(db, world, [(10, 0, 100, True)])).flow.markers
    assert set(marker.model_dump()) == {"at", "source", "fields", "before", "after", "straddling", "unknown"}
    assert set(marker.before.model_dump()) == {"items", "median_seconds", "small_sample"}


async def test_a_new_marker_misses_the_cache(db, world):
    first = await _flow(db, world, [(10, 0, 100, True)], cached=True)
    assert first.flow.markers == []
    await _policy(db, world, 1000)
    await db.commit()
    again = await snapshots.build_snapshot(db, await campaigns.current(db, world.team),
                                           FakeGithub(labelled={"a1": [_issue("a1", 10, state="closed")]}),
                                           as_of=AS_OF, cached=True)
    assert [m.at for m in again.flow.markers] == [_m(1000)]


# Frozen snapshots
async def test_a_stored_v4_snapshot_without_the_section_reads_back_unchanged(db, world):
    snapshot = await _flow(db, world, [(10, 0, 100, True)])
    old = snapshot.model_dump(mode="json")
    old.pop("flow")
    old["calculation_version"] = "campaign-snapshot-v4"
    campaign = await campaigns.current(db, world.team)
    db.add(CampaignSnapshot(campaign_id=campaign.id, version=1, calculation_version="campaign-snapshot-v4",
                            payload=old, created_by="operator"))
    await db.commit()
    frozen = await snapshots.freeze(db, campaign, FakeGithub(), actor=OPERATOR)
    assert (frozen.flow, frozen.calculation_version) == (None, "campaign-snapshot-v4")
    assert (await snapshots.latest(db, campaign.id)).payload == old


async def test_a_new_freeze_stores_v5_with_the_section(db, world):
    await _flow(db, world, [(10, 0, 100, True)])
    campaign = await campaigns.current(db, world.team)
    frozen = await snapshots.freeze(db, campaign, FakeGithub(labelled={"a1": [_issue("a1", 10, state="closed")]}),
                                    actor=OPERATOR, as_of=AS_OF)
    stored = await snapshots.latest(db, campaign.id)
    assert (stored.calculation_version, frozen.calculation_version) == ("campaign-snapshot-v5",) * 2
    assert stored.payload["flow"]["merges"] == 1
