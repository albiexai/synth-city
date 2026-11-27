"""#1252: the campaigns list behind Insights > Retrospectives.

Read only: it reads the database and the snapshot cache, never GitHub. A repository count comes from the latest frozen
snapshot, else the last live one, else it is unknown with a reason, never zero. "Before recording" needs the recording
marker; a missing snapshot alone never says it.
"""
from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, update

from app.database import get_db
from app.main import app
from app.models.database import (
    Campaign,
    CampaignSnapshot,
    CampaignUpgradeCoverage,
    FactoryContextKey,
    TeamGithubScope,
)
from app.services import campaign_record_service as campaigns
from app.services import campaign_snapshot_service as snapshots
from app.services.factory_campaign_list_service import campaign_list
from app.services.factory_projection_service import FactoryReadError
from tests.agent_teams.test_campaign_record import FakeGithub, _issue, db, world  # noqa: F401  (fixtures)
from tests.agent_teams.test_campaign_snapshot import AS_OF, OPERATOR, T0, _campaign, _client

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def fresh_cache():
    snapshots.clear_cache()
    yield
    snapshots.clear_cache()


async def _list(db, **changes):
    arguments = {"team_id": None, "scope_id": None, "status": "all", "limit": 50, "cursor": None, **changes}
    return await campaign_list(db, **arguments)


async def _start(db, team, selector="label:v4.4", at=T0):
    return await campaigns.start(db, team, selector, started_at=at, actor=OPERATOR)


async def _finish_without_snapshot(db, campaign, at):
    await db.execute(update(Campaign).where(Campaign.id == campaign.id).values(status="finished", finished_at=at))
    await db.commit()


def _by_repo(row):
    return {item.repo: (item.issues, item.reason) for item in row.repositories}


async def test_the_list_reads_no_github(db, world, monkeypatch):
    async def refuse(*_args, **_kwargs):
        raise AssertionError("the list called the network")

    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    await _start(db, world.team)
    response = await _list(db)
    assert [row.selector for row in response.campaigns] == ["label:v4.4"]


async def test_a_live_campaign_with_no_snapshot_names_its_repositories_with_no_count(db, world):
    await _start(db, world.team)
    row = (await _list(db)).campaigns[0]
    assert (row.team.name, row.status, row.source, row.repositories_unknown_reason) == ("A", "active", None, None)
    assert _by_repo(row) == {"o/a1": (None, "not_read_yet"), "o/a2": (None, "not_read_yet")}
    assert (row.merged.value, row.merged.reason, row.in_scope.reason) == (None, "not_read_yet", "not_read_yet")


async def test_a_live_snapshot_gives_the_counts_with_its_source_and_age(db, world):
    campaign = await _start(db, world.team)
    await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF)
    row = (await _list(db)).campaigns[0]
    assert (row.source.kind, row.source.as_of) == ("live", AS_OF)
    assert _by_repo(row) == {"o/a1": (2, None), "o/a2": (1, None)}
    assert row.in_scope.value == 3


async def test_a_frozen_snapshot_beats_a_live_one(db, world):
    campaign = await _start(db, world.team)
    await snapshots.freeze(db, campaign, _client(), actor=OPERATOR, as_of=AS_OF)
    snapshots.clear_cache()
    changed = FakeGithub(labelled={"a1": [_issue("a1", 1)]})
    await snapshots.build_snapshot(db, campaign, changed, as_of=AS_OF + timedelta(days=1))  # a different live count
    row = (await _list(db)).campaigns[0]
    assert row.source.kind == "frozen"
    assert _by_repo(row) == {"o/a1": (2, None), "o/a2": (1, None)}


async def test_a_repository_that_could_not_be_read_has_no_count_and_its_reason(db, world):
    campaign = await _start(db, world.team)
    await snapshots.build_snapshot(db, campaign, _client(broken=("a2",)), as_of=AS_OF)
    row = (await _list(db)).campaigns[0]
    assert row.repositories[0].issues == 2 and row.repositories[0].reason is None
    assert row.repositories[1].issues is None and row.repositories[1].reason


async def test_a_snapshot_made_before_repository_counts_is_unknown_not_empty(db, world):
    campaign = await _start(db, world.team)
    snapshot = await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, cached=False)
    payload = snapshot.model_dump(mode="json")
    del payload["repositories"]
    db.add(CampaignSnapshot(campaign_id=campaign.id, version=1, calculation_version=snapshot.calculation_version,
                            payload=payload, created_by="operator"))
    await db.commit()
    row = (await _list(db)).campaigns[0]
    assert _by_repo(row) == {"o/a1": (None, "snapshot_without_repository_counts"), "o/a2": (None, "snapshot_without_repository_counts")}
    assert row.source.kind == "frozen" and row.in_scope.value == 3


@pytest.mark.parametrize("marker_offset, expected", [
    (None, "no_snapshot"),                # no recording marker: not evidence
    (timedelta(days=-1), "no_snapshot"),  # recording began before the campaign did
    (timedelta(days=1), "before_recording"),  # the campaign began before recording
])
async def test_a_finished_campaign_with_no_snapshot_is_before_recording_only_with_evidence(
        db, world, marker_offset, expected):
    campaign = await _start(db, world.team)
    await _finish_without_snapshot(db, campaign, T0 + timedelta(days=3))
    if marker_offset is not None:
        db.add(CampaignUpgradeCoverage(id=1, started_at=T0 + marker_offset))
        await db.commit()
    row = (await _list(db)).campaigns[0]
    assert (row.repositories, row.repositories_unknown_reason, row.source) == ([], expected, None)
    assert (row.merged.value, row.merged.reason) == (None, expected)


async def test_only_evidenced_finished_campaigns_are_counted_as_before_recording(db, world):
    first = await _start(db, world.team, "label:v4.3", T0)
    await _finish_without_snapshot(db, first, T0 + timedelta(days=2))
    second = await _start(db, world.team, "label:v4.4", T0 + timedelta(days=5))
    await _finish_without_snapshot(db, second, T0 + timedelta(days=8))
    third = await _start(db, world.other, "label:x", T0)
    await _finish_without_snapshot(db, third, T0 + timedelta(days=9))  # finished after the marker
    db.add(CampaignUpgradeCoverage(id=1, started_at=T0 + timedelta(days=4)))
    await db.commit()
    response = await _list(db)
    assert response.finished_before_recording == 1  # only the first ended before the marker
    assert (response.counts.live, response.counts.finished) == (0, 3)


async def test_a_retired_team_has_no_name(db, world):
    await _start(db, world.team)
    await db.execute(update(FactoryContextKey).where(
        FactoryContextKey.key_kind == "team", FactoryContextKey.numeric_id == world.team).values(
        retired_at=T0 + timedelta(days=1)))
    await db.commit()
    assert (await _list(db)).campaigns[0].team is None


async def test_filters_by_team_status_and_repository(db, world):
    live_a = await _start(db, world.team)
    done_b = await _start(db, world.other, "milestone:1")
    await _finish_without_snapshot(db, done_b, T0 + timedelta(days=2))
    both = await _list(db)
    assert (both.total, both.counts.live, both.counts.finished) == (2, 1, 1)
    assert [row.id for row in (await _list(db, team_id=world.team)).campaigns] == [live_a.id]
    live = await _list(db, status="live")
    assert [row.id for row in live.campaigns] == [live_a.id] and (live.counts.live, live.counts.finished) == (1, 1)
    assert [row.id for row in (await _list(db, status="finished")).campaigns] == [done_b.id]
    by_repository = await _list(db, scope_id=world.scopes["a1"])
    assert [row.id for row in by_repository.campaigns] == [live_a.id]
    # The finished campaign of team B has no known repositories, so the filter hides it and says so.
    assert by_repository.hidden_unknown_repositories == 1
    assert (await _list(db)).hidden_unknown_repositories == 0


async def test_a_repository_filter_keeps_every_campaign_that_includes_it_in_any_team(db, world):
    live_a = await _start(db, world.team)
    live_b = await _start(db, world.other, "label:b")
    db.add(TeamGithubScope(preset_id=world.other, repo_owner="O", repo_name="A1", repo_path="/private/a1b", enabled=True))
    await db.commit()
    kept = await _list(db, scope_id=world.scopes["a1"])
    assert {row.id for row in kept.campaigns} == {live_a.id, live_b.id}  # the spelling is not case-sensitive
    assert kept.hidden_unknown_repositories == 0


async def test_pages_follow_the_cursor(db, world):
    for number in range(3):
        campaign = await _start(db, world.team if number == 0 else world.other, f"label:{number}", T0 + timedelta(days=number))
        await _finish_without_snapshot(db, campaign, T0 + timedelta(days=number, hours=1))
    first = await _list(db, limit=2)
    assert (len(first.campaigns), first.has_more, first.next_cursor, first.total) == (2, True, "2", 3)
    last = await _list(db, limit=2, cursor=first.next_cursor)
    assert (len(last.campaigns), last.has_more, last.next_cursor) == (1, False, None)


async def test_bad_arguments_are_refused(db, world):
    for changes in ({"status": "other"}, {"cursor": "x"}, {"cursor": "-1"}):
        with pytest.raises(FactoryReadError) as refused:
            await _list(db, **changes)
        assert refused.value.status == 422
    with pytest.raises(FactoryReadError) as missing:
        await _list(db, scope_id=999999)
    assert missing.value.status == 404


# V1 review 114
async def test_a_cutoff_gap_leaves_every_repository_count_unknown(db, world):
    campaign = await _start(db, world.team)
    await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF, observed_at=AS_OF + timedelta(days=1))
    row = (await _list(db)).campaigns[0]
    assert _by_repo(row) == {"o/a1": (None, "membership_read_after_cutoff"), "o/a2": (None, "membership_read_after_cutoff")}
    assert row.in_scope.value is None and "membership_read_after_cutoff" in row.in_scope.reasons


async def test_a_partial_measure_keeps_its_value_coverage_unknown_count_and_reason(db, world):
    campaign = await _campaign(db, world, undelivered=False)  # one proven merge and one closed issue with no outcome
    await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF)
    row = (await _list(db)).campaigns[0]
    assert (row.merged.value, row.merged.coverage, row.merged.unknown_count) == (1, "partial", 1)
    assert row.merged.reasons == ["delivery_history_missing"] and row.in_scope.coverage == "full"


async def test_the_age_of_frozen_counts_is_the_snapshot_cutoff_and_the_freeze_time_is_separate(db, world):
    campaign = await _start(db, world.team)
    await snapshots.freeze(db, campaign, _client(), actor=OPERATOR, as_of=AS_OF)
    source = (await _list(db)).campaigns[0].source
    assert (source.kind, source.as_of) == ("frozen", AS_OF)
    assert source.frozen_at is not None and source.frozen_at != AS_OF


async def test_a_team_filter_excludes_a_retired_lifetime_with_the_same_id(db, world):
    current = await _start(db, world.team)
    db.add(Campaign(team_context_key="team:old-lifetime", preset_id=world.team, selector="label:old", status="finished",
                    started_at=T0 - timedelta(days=30), finished_at=T0 - timedelta(days=20)))
    await db.commit()
    assert len((await _list(db)).campaigns) == 2
    assert [row.id for row in (await _list(db, team_id=world.team)).campaigns] == [current.id]


async def test_a_live_campaign_stays_under_a_repository_added_after_its_snapshot_with_an_unknown_count(db, world):
    campaign = await _start(db, world.team)
    await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF)
    added = TeamGithubScope(preset_id=world.team, repo_owner="o", repo_name="a3", repo_path="/private/a3", enabled=True)
    db.add(added)
    await db.commit()
    kept = await _list(db, scope_id=added.id)
    assert [row.id for row in kept.campaigns] == [campaign.id]
    assert _by_repo(kept.campaigns[0])["o/a3"] == (None, "repository_added_after_snapshot")
    assert _by_repo(kept.campaigns[0])["o/a1"] == (2, None)


async def test_the_hidden_tally_counts_only_campaigns_that_the_status_filter_shows(db, world):
    await _start(db, world.team)
    done = await _start(db, world.other, "label:b")
    await _finish_without_snapshot(db, done, T0 + timedelta(days=2))
    assert (await _list(db, scope_id=world.scopes["a1"], status="live")).hidden_unknown_repositories == 0
    assert (await _list(db, scope_id=world.scopes["a1"], status="finished")).hidden_unknown_repositories == 1
    assert (await _list(db, scope_id=world.scopes["a1"])).hidden_unknown_repositories == 1


async def test_two_spellings_of_one_repository_are_listed_once(db, world):
    await _start(db, world.team)
    db.add(TeamGithubScope(preset_id=world.team, repo_owner="O", repo_name="A1", repo_path="/private/a1x", enabled=True))
    await db.commit()
    assert [item.repo.lower() for item in (await _list(db)).campaigns[0].repositories] == ["o/a1", "o/a2"]


# V1 review 116: the membership of a live campaign is the current one, also when it is empty
async def _snapshotted_campaign_whose_team_then_watches_nothing(db, world):
    campaign = await _start(db, world.team)
    await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF)
    await db.execute(delete(TeamGithubScope).where(TeamGithubScope.preset_id == world.team))
    await db.commit()
    return campaign


async def test_a_live_team_that_watches_nothing_lists_nothing_not_the_old_snapshot(db, world):
    await _snapshotted_campaign_whose_team_then_watches_nothing(db, world)
    row = (await _list(db)).campaigns[0]
    assert (row.repositories, row.repositories_unknown_reason, row.source.kind) == ([], "no_repositories_watched", "live")


async def test_an_empty_current_membership_is_neither_kept_nor_hidden_by_a_repository_filter(db, world):
    await _snapshotted_campaign_whose_team_then_watches_nothing(db, world)
    other = TeamGithubScope(preset_id=world.other, repo_owner="o", repo_name="a1", repo_path="/private/a1b", enabled=True)
    db.add(other)
    await db.commit()
    kept = await _list(db, scope_id=other.id)
    assert (kept.campaigns, kept.hidden_unknown_repositories) == ([], 0)
    assert len((await _list(db)).campaigns) == 1


async def test_a_finishing_campaign_follows_its_current_scopes_too(db, world):
    campaign = await _start(db, world.team)
    await snapshots.build_snapshot(db, campaign, _client(), as_of=AS_OF)
    await db.execute(update(Campaign).where(Campaign.id == campaign.id).values(status="finishing"))
    await db.execute(delete(TeamGithubScope).where(TeamGithubScope.repo_name == "a2"))
    await db.commit()
    assert _by_repo((await _list(db)).campaigns[0]) == {"o/a1": (2, None)}


async def test_a_live_campaign_of_a_retired_team_keeps_its_recorded_repositories(db, world):
    await _start(db, world.team)
    campaign = (await _list(db)).campaigns[0]
    await snapshots.build_snapshot(db, await db.get(Campaign, campaign.id), _client(), as_of=AS_OF)
    await db.execute(update(FactoryContextKey).where(FactoryContextKey.key_kind == "team", FactoryContextKey.numeric_id == world.team)
                     .values(retired_at=T0 + timedelta(days=1)))
    await db.execute(delete(TeamGithubScope).where(TeamGithubScope.preset_id == world.team))
    await db.commit()
    row = (await _list(db)).campaigns[0]
    assert (row.team, _by_repo(row)) == (None, {"o/a1": (2, None), "o/a2": (1, None)})


@pytest_asyncio.fixture
async def client(db):
    async def override():
        yield db

    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = override
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
            yield http
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


async def test_the_route_answers_and_validates_its_filters(db, world, client):
    await _start(db, world.team)
    answer = await client.get("/api/v1/factory/campaigns", params={"team_id": world.team})
    assert answer.status_code == 200
    body = answer.json()
    assert body["schema_version"] == 1 and body["total"] == 1 and body["campaigns"][0]["team"]["name"] == "A"
    assert (await client.get("/api/v1/factory/campaigns", params={"status": "other"})).status_code == 422
    assert (await client.get("/api/v1/factory/campaigns", params={"scope_id": 999999})).status_code == 404
    assert (await client.get("/api/v1/factory/campaigns", params={"limit": 0})).status_code == 422
    mismatch = await client.get("/api/v1/factory/campaigns", params={"team_id": world.other, "scope_id": world.scopes["a1"]})
    assert mismatch.status_code == 422
