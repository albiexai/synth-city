"""#1243: the Director adjusts a campaign's start (design §2), for a campaign created on upgrade or started by hand.

Director only: the operator key over the UI or the operator API, never a Mail session. The change is a compare-and-set
on the start the Director saw, for the team's active campaign in its current lifetime, with one audit event.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models.database import (
    AgentTeamPreset,
    Campaign,
    CampaignUpgradeCoverage,
    FactoryAuditEvent,
    GithubCheckRunHistoryCoverage,
    GithubMergeAttemptCoverage,
    GithubRateLimitHistoryCoverage,
    TeamGithubScope,
)
from app.services import campaign_record_service as campaigns
from app.services import campaign_upgrade_service as upgrade
from app.services import factory_audit_service as audit
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.test_audit_via import SESSION_TOKEN, _session

pytestmark = pytest.mark.asyncio
OPERATOR_KEY = "campaign-start-test-operator-key"
T0 = datetime(2026, 11, 10, 9, 0, 0)
OPERATOR = audit.derive_actor(actor_kind="operator")


def _pragmas(connection, _record):
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")


@pytest_asyncio.fixture
async def maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'adjust.db'}")
    event.listen(engine.sync_engine, "connect", _pragmas)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db(maker):
    async with maker() as session:
        yield session


@pytest_asyncio.fixture
async def client(db, monkeypatch):
    monkeypatch.setattr(settings, "operator_token", OPERATOR_KEY)

    async def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 headers={"X-Synth-City-Operator-Token": OPERATOR_KEY}) as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def team(db):
    preset = AgentTeamPreset(name="Campaign team", backlog_milestone="v4.4")
    db.add(preset)
    await db.flush()
    db.add(TeamGithubScope(preset_id=preset.id, repo_owner="o", repo_name="r", repo_path="/private/r"))
    await db.commit()
    await campaigns.start(db, preset.id, "milestone:v4.4", started_at=T0, actor=OPERATOR)
    return preset.id


def _path(preset_id):
    return f"/api/v1/agent-teams/{preset_id}/campaign/start"


def _body(started_at, expected=T0):
    return {"started_at": started_at.isoformat(), "expected_started_at": expected.isoformat()}


async def _adjusted(maker):
    async with maker() as db:
        return (await db.scalars(select(FactoryAuditEvent).where(
            FactoryAuditEvent.event_kind == "campaign_start_adjusted"))).all()


async def _start(maker, preset_id):
    async with maker() as db:
        return (await campaigns.current(db, preset_id)).started_at


async def test_the_director_adjusts_the_start_with_one_audit_event(client, maker, team):
    new = T0 - timedelta(days=2)
    response = await client.patch(_path(team), json=_body(new))
    assert response.status_code == 200, response.text
    assert response.json()["started_at"].startswith(new.isoformat())
    assert await _start(maker, team) == new
    (event_row,) = await _adjusted(maker)
    assert (event_row.actor_kind, event_row.action_outcome, event_row.team_preset_id) == ("operator", "applied", team)
    assert T0.isoformat() in event_row.sanitized_reason and new.isoformat() in event_row.sanitized_reason
    campaign_id = (await client.patch(_path(team), json=_body(new, new))).json()["campaign_id"]
    assert event_row.correlation_id == f"campaign:{campaign_id}"


@pytest.mark.parametrize("token,status,code", [
    (None, 401, "operator_token_required"), ("wrong-key", 401, "operator_token_invalid"),
], ids=["missing", "invalid"])
async def test_an_adjustment_without_the_operator_token_is_refused(client, maker, team, token, status, code):
    headers = {"X-Synth-City-Operator-Token": token} if token else {"X-Synth-City-Operator-Token": ""}
    response = await client.patch(_path(team), json=_body(T0 - timedelta(days=1)), headers=headers)
    assert response.status_code == status and code in response.text
    assert await _start(maker, team) == T0
    assert await _adjusted(maker) == []


async def test_an_unconfigured_operator_token_refuses_the_adjustment(client, maker, team, monkeypatch):
    monkeypatch.setattr(settings, "operator_token", "")
    response = await client.patch(_path(team), json=_body(T0 - timedelta(days=1)))
    assert response.status_code == 503 and "operator_token_unconfigured" in response.text
    assert await _start(maker, team) == T0


async def test_an_adjustment_from_a_mail_session_is_refused_and_audited(client, db, maker, team, tmp_path):
    await _session(db, tmp_path)
    response = await client.patch(_path(team), json=_body(T0 - timedelta(days=1)),
                                  headers={"X-Synth-City-Session-Token": SESSION_TOKEN})
    assert response.status_code == 403 and response.json()["detail"]["code"] == "director_action_not_via_mail"
    assert await _start(maker, team) == T0
    assert await _adjusted(maker) == []
    async with maker() as other:
        refusal = await other.scalar(select(FactoryAuditEvent).where(
            FactoryAuditEvent.event_kind == "policy_change", FactoryAuditEvent.action_outcome == "rejected"))
    assert refusal.sanitized_reason == "director_action_not_via_mail" and refusal.via == "mail_tool"


async def test_a_stale_expected_start_is_refused(client, maker, team):
    response = await client.patch(_path(team), json=_body(T0 - timedelta(days=1), expected=T0 + timedelta(hours=1)))
    assert response.status_code == 409 and response.json()["detail"] == "campaign_start_changed"
    assert await _start(maker, team) == T0
    assert await _adjusted(maker) == []


async def test_two_concurrent_adjustments_apply_one(maker, team):
    async def adjust(new):
        async with maker() as db:
            try:
                await campaigns.adjust_start(db, team, started_at=new, expected_started_at=T0, actor=OPERATOR)
                return "applied"
            except campaigns.CampaignError as error:
                return error.code

    outcomes = await asyncio.gather(adjust(T0 - timedelta(days=1)), adjust(T0 - timedelta(days=2)))
    assert sorted(outcomes) == ["applied", "campaign_start_changed"]
    assert len(await _adjusted(maker)) == 1
    assert await _start(maker, team) in {T0 - timedelta(days=1), T0 - timedelta(days=2)}


async def test_a_finishing_campaign_keeps_its_start(client, db, maker, team):
    campaign = await campaigns.current(db, team)
    campaign.status = "finishing"
    await db.commit()
    response = await client.patch(_path(team), json=_body(T0 - timedelta(days=1)))
    assert response.status_code == 409 and response.json()["detail"] == "campaign_not_active"
    async with maker() as other:
        assert (await other.get(Campaign, campaign.id)).started_at == T0
    assert await _adjusted(maker) == []


async def test_a_future_start_is_refused(client, maker, team):
    response = await client.patch(_path(team), json=_body(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)))
    assert response.status_code == 422 and response.json()["detail"] == "campaign_start_in_future"
    assert await _start(maker, team) == T0


async def test_another_lifetimes_campaign_is_not_adjusted(client, db, maker, team):
    """A team without an open campaign in its current lifetime has nothing to adjust, whatever its id held before."""
    other = AgentTeamPreset(name="No campaign")
    db.add(other)
    await db.commit()
    assert (await client.patch(_path(other.id), json=_body(T0 - timedelta(days=1)))).status_code == 404
    assert (await client.patch(_path(999999), json=_body(T0 - timedelta(days=1)))).status_code == 404
    assert await _start(maker, team) == T0


async def test_an_adjusted_start_after_coverage_becomes_covered(client, db, maker, team):
    boundary = T0 + timedelta(days=1)
    for model in (GithubCheckRunHistoryCoverage, GithubMergeAttemptCoverage, GithubRateLimitHistoryCoverage,
                  CampaignUpgradeCoverage):
        await db.merge(model(id=1, started_at=boundary))
    await db.commit()
    async with maker() as other:
        before = await upgrade.recorded_coverage(other, await campaigns.current(other, team))
    assert not any(c.covered for c in before)
    later = boundary + timedelta(hours=1)
    assert (await client.patch(_path(team), json=_body(later))).status_code == 200
    async with maker() as other:
        after = await upgrade.recorded_coverage(other, await campaigns.current(other, team))
    assert all(c.covered for c in after)
