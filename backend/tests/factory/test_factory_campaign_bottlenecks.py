"""The ranked window contract exposed by the read-only campaign route."""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.database import get_db
from app.main import app
from app.models.database import CampaignSnapshot
from app.services import campaign_record_service as campaigns
from app.services import campaign_snapshot_service as snapshots
from app.services.retro_bottleneck_service import RankingAttempt, build_bottlenecks
from app.services.retro_stage_engine import AttemptWindow, StageInterval
from tests.agent_teams.test_campaign_record import db, world  # noqa: F401  (fixtures)

pytestmark = pytest.mark.asyncio
PATH = "/api/v1/factory/campaigns"
START = datetime(2026, 11, 26, 8)
END = START + timedelta(hours=10)


def _rankings(cutoff):
    attempts = [RankingAttempt(("o/a1", issue), AttemptWindow(START, cutoff), [
        StageInterval("waiting", "decision", "leader", START, cutoff - timedelta(hours=4)),
        StageInterval("process", "required_check", "e2e", cutoff - timedelta(hours=4), cutoff),
    ]) for issue in range(1, 6)]
    return build_bottlenecks(attempts, start=START, as_of=cutoff,
                             group_coverage={"waiting": "full", "process": "full"}, unknown_reasons=[])


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


async def _campaign(db, world):
    return await campaigns.start(db, world.team, "label:v4.4", started_at=START,
                                 actor={"actor_kind": "operator"})


async def test_live_route_labels_both_windows_and_compares_only_for_the_caller(db, world, client, monkeypatch):
    campaign = await _campaign(db, world)

    async def build(db, campaign, github, cutoff, observed):
        return snapshots.Snapshot(campaign_id=campaign.id, selector=campaign.selector, started_at=START,
                                  as_of=cutoff, calculation_version=snapshots.CALCULATION_VERSION,
                                  metrics=[], recorded_coverage=[], bottlenecks=_rankings(cutoff))

    builder = AsyncMock(side_effect=build)
    monkeypatch.setattr(snapshots, "_build", builder)
    path = f"{PATH}/{campaign.id}/bottlenecks"
    result = await client.get(path)
    assert result.status_code == 200
    payload = result.json()
    assert (payload["campaign_id"], payload["source"]) == (campaign.id, "live")
    data = payload["bottlenecks"]
    assert (data["whole_campaign"]["label"], data["last_four_hours"]["label"]) == ("Whole campaign", "Last 4 hours")
    assert data["whole_campaign"]["stages"][0]["actor"] == "leader"
    assert data["last_four_hours"]["stages"][0]["actor"] == "e2e"
    assert data["new_since_last_visit"] is None
    top = data["whole_campaign"]["stages"][0]["key"]
    assert (await client.get(path, params={"previous_top": top})).json()["bottlenecks"]["new_since_last_visit"] is False
    assert (await client.get(path, params={"previous_top": ""})).json()["bottlenecks"]["new_since_last_visit"] is True
    assert (await client.get(path)).json()["bottlenecks"]["new_since_last_visit"] is None
    builder.assert_awaited_once()
    campaign.selector = "label:renamed"
    await db.commit()
    assert (await client.get(path)).status_code == 200
    assert builder.await_count == 2
    expired = snapshots._clock() + snapshots.CACHE_SECONDS
    monkeypatch.setattr(snapshots, "_clock", lambda: expired)
    assert (await client.get(path)).status_code == 200
    assert builder.await_count == 3
    assert await db.scalar(select(func.count(CampaignSnapshot.id))) == 0


@pytest.mark.parametrize("recorded", [False, True])
async def test_finished_route_uses_stored_cutoff_and_handles_old_payloads(db, world, client, monkeypatch, recorded):
    campaign = await _campaign(db, world)
    campaign.status, campaign.finished_at = "finished", END
    payload = {"campaign_id": campaign.id, "selector": campaign.selector, "started_at": START.isoformat(),
               "as_of": END.isoformat(), "calculation_version": "fixture", "metrics": [], "recorded_coverage": []}
    if recorded:
        payload["bottlenecks"] = _rankings(END).model_dump(mode="json")
    db.add(CampaignSnapshot(campaign_id=campaign.id, version=1, calculation_version="fixture",
                            payload=payload, created_by="operator"))
    await db.commit()
    builder = AsyncMock(side_effect=AssertionError("finished reads may not build live data"))
    monkeypatch.setattr(snapshots, "_build", builder)
    response = await client.get(f"{PATH}/{campaign.id}/bottlenecks")
    assert response.status_code == 200
    value = response.json()
    assert value["source"] == "frozen"
    data = value["bottlenecks"]
    assert data["last_four_hours"]["as_of"] == "2026-11-26T18:00:00Z"
    if recorded:
        assert data["last_four_hours"]["stages"][0]["actor"] == "e2e"
    else:
        assert data["whole_campaign"]["coverage"] == "unavailable"
        assert data["whole_campaign"]["credited_seconds"] is None
        assert "snapshot_without_bottlenecks" in data["whole_campaign"]["unknown_reasons"]
    builder.assert_not_awaited()
    assert await db.scalar(select(func.count(CampaignSnapshot.id))) == 1


async def test_validation_and_missing_campaign_use_factory_read_errors(client):
    response = await client.get(f"{PATH}/999999/bottlenecks")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "resource_not_found"
    response = await client.get(f"{PATH}/1/bottlenecks", params={"previous_top": "bad-shape"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_filter"


async def test_finished_campaign_without_snapshot_returns_windows_with_unknown_time(db, world, client):
    campaign = await _campaign(db, world)
    campaign.status, campaign.finished_at = "finished", END
    await db.commit()
    response = await client.get(f"{PATH}/{campaign.id}/bottlenecks")
    assert response.status_code == 200
    assert response.json()["source"] == "unavailable"
    for window in ("whole_campaign", "last_four_hours"):
        data = response.json()["bottlenecks"][window]
        assert (data["coverage"], data["credited_seconds"]) == ("unavailable", None)
        assert "no_snapshot" in data["unknown_reasons"]
