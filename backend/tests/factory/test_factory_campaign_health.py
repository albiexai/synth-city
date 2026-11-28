"""#1256: the read-only health route behind the Overview campaign health block."""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

from app.database import get_db
from app.main import app
from app.models.database import CampaignSnapshot
from app.services import campaign_record_service as campaigns
from app.services import campaign_snapshot_service as snapshots
from tests.agent_teams.test_campaign_record import db, world  # noqa: F401  (fixtures)
from tests.factory.test_factory_campaign_bottlenecks import _rankings

pytestmark = pytest.mark.asyncio
PATH = "/api/v1/factory/campaigns"
START = datetime(2026, 11, 26, 8)
CUTOFF = START + timedelta(hours=10)


@pytest.fixture(autouse=True)
def fresh_cache():
    snapshots.clear_cache()
    yield
    snapshots.clear_cache()


@pytest_asyncio.fixture
async def client(db):
    async def use_fixture_db():
        yield db

    app.dependency_overrides[get_db] = use_fixture_db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


def _item_time():
    band = snapshots.BandRow(band="0-100", items=5, timed=4, median_seconds=2280.0, p85_seconds=4260.0,
                             small_sample=True, coverage="full", unknown_reasons=[])
    return snapshots.ItemTimeSection(items=5, timed=4, coverage="full", unknown_reasons=[], bands=[band])


def _snapshot(campaign, **extra):
    return snapshots.Snapshot(campaign_id=campaign.id, selector=campaign.selector, started_at=START, as_of=CUTOFF,
                              calculation_version=snapshots.CALCULATION_VERSION, metrics=[], recorded_coverage=[],
                              item_time=_item_time(), bottlenecks=_rankings(CUTOFF), **extra)


async def _campaign(db, world):
    return await campaigns.start(db, world.team, "label:v4.4", started_at=START, actor={"actor_kind": "operator"})


async def _finish(db, campaign, payload):
    campaign.status = "finished"
    campaign.finished_at = CUTOFF
    if payload is not None:
        db.add(CampaignSnapshot(campaign_id=campaign.id, version=1, calculation_version=snapshots.CALCULATION_VERSION,
                                payload=payload, created_by="operator"))
    await db.commit()


async def test_a_live_campaign_reports_item_time_the_window_and_an_unrecorded_cost(db, world, client, monkeypatch):
    campaign = await _campaign(db, world)
    builder = AsyncMock(return_value=_snapshot(campaign))
    monkeypatch.setattr(snapshots, "_build", builder)
    result = await client.get(f"{PATH}/{campaign.id}/health")
    assert result.status_code == 200
    data = result.json()
    assert (data["campaign_id"], data["source"], data["as_of"]) == (campaign.id, "live", CUTOFF.isoformat())
    band = data["item_time"]["bands"][0]
    assert (band["band"], band["timed"], band["median_seconds"], band["p85_seconds"], band["small_sample"]) == (
        "0-100", 4, 2280.0, 4260.0, True)
    # No snapshot has a cost section yet: unavailable with the reason, never zero.
    assert data["cost"] == {"value_usd": None, "coverage": "unavailable", "members_total": None, "members_known": None,
                            "unknown_members": [], "prices_as_of": None, "reasons": ["cost_not_recorded"]}
    assert (data["bottleneck"]["label"], data["bottleneck"]["window"]) == ("Last 4 hours", "last_four_hours")
    assert data["item_time_reason"] is None and data["bottleneck_reason"] is None


async def test_a_frozen_campaign_reads_its_version_and_never_builds_a_snapshot(db, world, client, monkeypatch):
    campaign = await _campaign(db, world)
    payload = _snapshot(campaign).model_dump(mode="json") | {"cost": {
        "value_usd": 412.5, "coverage": "partial", "members_total": 7, "members_known": 6,
        "unknown_members": [{"member": "B3", "reason": "harness_not_read"}], "prices_as_of": "2026-11-15",
        "reasons": ["member_unknown"]}}
    await _finish(db, campaign, payload)
    builder = AsyncMock(side_effect=AssertionError("a frozen read builds nothing"))
    monkeypatch.setattr(snapshots, "_build", builder)
    data = (await client.get(f"{PATH}/{campaign.id}/health")).json()
    assert data["source"] == "frozen" and builder.await_count == 0
    # #1250 writes the section; the route passes it through and nothing else has to change.
    assert data["cost"] == {"value_usd": 412.5, "coverage": "partial", "members_total": 7, "members_known": 6,
                            "unknown_members": [{"member": "B3", "reason": "harness_not_read"}],
                            "prices_as_of": "2026-11-15", "reasons": ["member_unknown"]}
    assert data["item_time"]["timed"] == 4 and data["bottleneck"]["label"] == "Last 4 hours"


async def test_a_finished_campaign_without_a_snapshot_is_unavailable_with_reasons(db, world, client):
    campaign = await _campaign(db, world)
    await _finish(db, campaign, None)
    data = (await client.get(f"{PATH}/{campaign.id}/health")).json()
    assert (data["source"], data["as_of"], data["item_time"], data["bottleneck"]) == ("unavailable", None, None, None)
    assert (data["item_time_reason"], data["bottleneck_reason"], data["cost"]["reasons"]) == (
        "no_snapshot", "no_snapshot", ["no_snapshot"])


async def test_an_older_version_without_sections_says_which_is_missing(db, world, client):
    campaign = await _campaign(db, world)
    payload = _snapshot(campaign).model_dump(mode="json")
    for key in ("item_time", "bottlenecks"):
        payload.pop(key)
    await _finish(db, campaign, payload)
    data = (await client.get(f"{PATH}/{campaign.id}/health")).json()
    assert (data["item_time"], data["bottleneck"]) == (None, None)
    assert (data["item_time_reason"], data["bottleneck_reason"]) == (
        "snapshot_without_item_time", "snapshot_without_bottlenecks")
    assert data["cost"]["reasons"] == ["cost_not_recorded"] and data["cost"]["value_usd"] is None


async def test_a_cost_section_that_does_not_fit_reads_unavailable_never_a_number(db, world, client):
    campaign = await _campaign(db, world)
    payload = _snapshot(campaign).model_dump(mode="json") | {"cost": {"value_usd": "lots", "coverage": "everything"}}
    await _finish(db, campaign, payload)
    cost = (await client.get(f"{PATH}/{campaign.id}/health")).json()["cost"]
    assert (cost["value_usd"], cost["coverage"], cost["reasons"]) == (None, "unavailable", ["cost_section_invalid"])


async def test_an_unknown_campaign_is_404_and_a_bad_id_is_422(client):
    assert (await client.get(f"{PATH}/9999/health")).status_code == 404
    assert (await client.get(f"{PATH}/0/health")).status_code == 422
