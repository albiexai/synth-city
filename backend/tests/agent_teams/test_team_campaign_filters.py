"""#1142: the Overview Repository and Assigned-harness filters narrow the campaign Progress, remaining work and next
actors, not only the Needs-you lists (#1141).

The real summary service and the real campaign service run on a seeded two-team, two-repository, two-harness database.
A repository filter keeps that repository's work and backlog; a harness filter keeps the work that an owner of that
harness holds (the Work page's strict rule), so ownerless work (queued items, the not-started backlog) is not counted.
"""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.database import AgentTeamPreset, AgentTeamSlot, GithubWorkItem, TeamGithubScope
from app.models.github_work_progress_schemas import (
    GithubPublicationObservation, GithubWorkProgressResponse, GithubWorkRemainingView,
)
from app.services.agent_team_campaign_service import AgentTeamCampaignService, BacklogObservation
from app.services.agent_team_summary_service import AgentTeamSummaryService


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'campaign.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _no_hold(monkeypatch):
    monkeypatch.setattr("app.services.agent_team_summary_service.hold_code", lambda: None)


def _progress(item_id, actor):
    now = datetime.now(timezone.utc)
    return GithubWorkProgressResponse(work_item_id=item_id, dispatch_nonce="n", owner_slot_id=1,
        checked_at=now, valid_until=now + timedelta(seconds=15), phase="implementation", next_actor=actor,
        next_action="Continue.", publication=GithubPublicationObservation(state="current"),
        remaining_work=GithubWorkRemainingView(state="current", effort_low_minutes=10, effort_high_minutes=20,
            confidence="medium", reason=None, reported_at=now))


class _Progress:
    def __init__(self, actors):
        self.actors = actors

    async def summary(self, db, item_id):
        return _progress(item_id, self.actors[item_id])


class _Attention:
    def __init__(self, actions):
        self.actions = actions

    async def summary(self, db, preset_id):
        return {"coverage_complete": True, "actions": self.actions,
                "observation_expires_at": datetime.now(timezone.utc) + timedelta(seconds=60)}

    async def dispatch_snapshot(self, db, scopes):
        return {"complete": True, "actions": []}


def _slot(preset, name, provider, position):
    return AgentTeamSlot(preset_id=preset.id, position=position, display_name=name, provider=provider,
                         repo_id="r", repo_path="/private/r", repo_name="r", bootstrap_prompt="private", launch_options={})


def _scope(preset, repo):
    return TeamGithubScope(preset_id=preset.id, repo_owner="o", repo_name=repo, repo_path=f"/private/{repo}", enabled=True)


def _item(scope, slot, number, status, **extra):
    return GithubWorkItem(scope_id=scope.id, issue_number=number, issue_title=f"Issue {number}",
        issue_url=f"https://x/{number}", github_updated_at=datetime.utcnow(), dispatch_status=status,
        owner_slot_id=slot.id if slot else None, dispatch_nonce=f"n{number}", **extra)


class World:
    pass


@pytest_asyncio.fixture
async def world(db):
    w = World()
    team = AgentTeamPreset(name="A", autonomy_enabled=True,
                           planned_decisions=[{"text": "Plan the next release.", "state": "later"}])
    other = AgentTeamPreset(name="B", autonomy_enabled=True)
    db.add_all([team, other])
    await db.flush()
    a1, a2, b1 = _scope(team, "a1"), _scope(team, "a2"), _scope(other, "b1")
    codex, claude, b_codex = _slot(team, "Codex owner", "codex-cli", 0), _slot(team, "Claude owner", "claude-code", 1), \
        _slot(other, "B owner", "codex-cli", 0)
    db.add_all([a1, a2, b1, codex, claude, b_codex])
    await db.flush()
    rows = {
        1: _item(a1, codex, 1, "merged"), 2: _item(a1, codex, 2, "dispatched"), 3: _item(a1, claude, 3, "dispatched"),
        4: _item(a1, codex, 4, "escalated", escalation_reason="owner_unavailable"), 5: _item(a1, None, 5, "pending"),
        6: _item(a2, claude, 6, "verifying"), 7: _item(a2, claude, 7, "merged"), 8: _item(a2, None, 8, "pending"),
        9: _item(b1, b_codex, 9, "dispatched"),
    }
    db.add_all(rows.values())
    await db.commit()
    w.team, w.other = team.id, other.id
    w.scopes = {"a1": a1.id, "a2": a2.id, "b1": b1.id}
    w.items = {number: row.id for number, row in rows.items()}
    return w


def _actions(w):
    return [
        {"scope_id": w.scopes["a1"], "issue_number": 2, "work_item_id": w.items[2], "kind": "review_pr",
         "state": "requested", "source": "leader", "assessment_current": True},
        {"scope_id": w.scopes["a2"], "issue_number": 6, "work_item_id": w.items[6], "kind": "merge_pr",
         "state": "requested", "source": "leader", "assessment_current": True},
    ]


async def _service(db, w):
    campaign = AgentTeamCampaignService(interval_seconds=60, clock=lambda: 0.0)
    backlog = {"a1": [1, 2, 3, 4, 5, 20], "a2": [6, 7, 8, 21]}
    for scope in await campaign._scopes(db, preset_id=w.team):
        name = scope.repo_name
        campaign._cache[campaign._key(scope)] = BacklogObservation(1e9, tuple((number, ()) for number in backlog[name]))
    actors = {w.items[2]: "owner", w.items[3]: "reviewer", w.items[4]: "leader", w.items[5]: "owner",
              w.items[6]: "controller", w.items[8]: "owner", w.items[9]: "owner"}
    return AgentTeamSummaryService(progress=_Progress(actors), attention=_Attention(_actions(w)), campaign=campaign)


def _shape(result):
    c = result.campaign
    return {
        "done": c.done_count, "under_way": c.under_way_count, "attention": c.attention_count,
        "queued": c.queued_count, "not_started": c.not_started_count,
        "dispatch_order": [item.issue_number for item in c.dispatch_order],
        "latest_done": [item.title for item in c.latest_done],
        "unfinished": result.remaining_work.unfinished_items, "effort": result.remaining_work.effort_low_minutes,
        "actors": result.next_actors,
    }


UNFILTERED = {"done": 2, "under_way": 3, "attention": 1, "queued": 2, "not_started": 2, "dispatch_order": [5, 8],
              "unfinished": 6, "effort": 60, "actors": ["owner", "leader", "operator", "controller", "reviewer"]}


@pytest.mark.asyncio
async def test_no_filter_is_the_unfiltered_summary(db, world):
    service = await _service(db, world)
    result = await service.summary(db, world.team)
    shape = _shape(result)
    assert {k: shape[k] for k in UNFILTERED} == UNFILTERED
    assert result.campaign.backlog_state == "available"
    assert [d.text for d in result.campaign.decisions_ahead] == [
        "Review the pull request.", "Decide whether to merge the pull request.", "Plan the next release."]
    # The explicit no-filter arguments are the same call.
    assert (await service.summary(db, world.team, scope_id=None, provider=None)).model_dump(
        exclude={"checked_at", "valid_until"}) == result.model_dump(exclude={"checked_at", "valid_until"})


@pytest.mark.asyncio
async def test_a_repository_filter_keeps_only_that_repositorys_work_backlog_and_remaining_work(db, world):
    service = await _service(db, world)
    one = await service.summary(db, world.team, scope_id=world.scopes["a1"])
    assert _shape(one) == {"done": 1, "under_way": 2, "attention": 1, "queued": 1, "not_started": 1,
                           "dispatch_order": [5], "latest_done": ["Issue 1"], "unfinished": 4, "effort": 40,
                           "actors": ["owner", "leader", "operator", "reviewer"]}
    assert [d.text for d in one.campaign.decisions_ahead] == ["Review the pull request.", "Plan the next release."]
    two = await service.summary(db, world.team, scope_id=world.scopes["a2"])
    assert _shape(two) == {"done": 1, "under_way": 1, "attention": 0, "queued": 1, "not_started": 1,
                           "dispatch_order": [8], "latest_done": ["Issue 7"], "unfinished": 2, "effort": 20,
                           "actors": ["owner", "operator", "controller"]}
    assert [d.text for d in two.campaign.decisions_ahead] == [
        "Decide whether to merge the pull request.", "Plan the next release."]
    assert one.remaining_work.reason == "complete" and one.next_actor_coverage_complete


@pytest.mark.asyncio
async def test_a_harness_filter_keeps_only_owned_work_and_does_not_count_ownerless_work(db, world):
    service = await _service(db, world)
    codex = await service.summary(db, world.team, provider="codex-cli")
    assert _shape(codex) == {"done": 1, "under_way": 1, "attention": 1, "queued": 0, "not_started": 0,
                             "dispatch_order": [], "latest_done": ["Issue 1"], "unfinished": 2, "effort": 20,
                             "actors": ["owner", "leader", "operator"]}
    claude = await service.summary(db, world.team, provider="claude-code")
    assert _shape(claude) == {"done": 1, "under_way": 2, "attention": 0, "queued": 0, "not_started": 0,
                              "dispatch_order": [], "latest_done": ["Issue 7"], "unfinished": 2, "effort": 20,
                              "actors": ["operator", "controller", "reviewer"]}
    assert claude.campaign.backlog_state == "available"  # zero, not unknown


@pytest.mark.asyncio
async def test_both_filters_must_match(db, world):
    service = await _service(db, world)
    result = await service.summary(db, world.team, scope_id=world.scopes["a1"], provider="claude-code")
    assert _shape(result) == {"done": 0, "under_way": 1, "attention": 0, "queued": 0, "not_started": 0,
                              "dispatch_order": [], "latest_done": [], "unfinished": 1, "effort": 10,
                              "actors": ["reviewer"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("filters", [{"scope": "b1"}, {"provider": "antigravity-cli"},
                                     {"scope": "a1", "provider": "antigravity-cli"}])
async def test_a_team_outside_the_filter_has_nothing_to_show(db, world, filters):
    service = await _service(db, world)
    kwargs = {"scope_id": world.scopes[filters["scope"]]} if "scope" in filters else {}
    if "provider" in filters:
        kwargs["provider"] = filters["provider"]
    result = await service.summary(db, world.team, **kwargs)
    shape = _shape(result)
    assert (shape["done"], shape["under_way"], shape["attention"], shape["queued"]) == (0, 0, 0, 0)
    assert shape["unfinished"] == 0 and shape["actors"] == []
    # The team-level planned decisions do not show for a team that the filter excludes.
    assert result.campaign.decisions_ahead == []


@pytest.mark.asyncio
async def test_another_teams_repository_and_harness_are_not_counted_in_this_team(db, world):
    service = await _service(db, world)
    other = await service.summary(db, world.other, scope_id=world.scopes["b1"], provider="codex-cli")
    assert other.remaining_work.unfinished_items == 1 and other.campaign.under_way_count == 1
    mine = await service.summary(db, world.team, scope_id=world.scopes["b1"])
    assert mine.remaining_work.unfinished_items == 0


@pytest.mark.asyncio
async def test_needs_you_returns_the_same_narrowed_work_parts(db, world):
    service = await _service(db, world)
    direct = await service.summary(db, world.team, provider="codex-cli")
    trimmed = await service.needs_you(db, world.team, provider="codex-cli")
    assert _shape(trimmed) == _shape(direct)
    assert [a.work_item_id for a in trimmed.human_actions] == [world.items[2]]


@pytest.mark.asyncio
async def test_the_http_summary_applies_the_filters(db, world, monkeypatch):
    import httpx

    from app.api.v1 import agent_teams as routes
    from app.config import settings
    from app.database import get_db
    from app.main import app

    service = await _service(db, world)
    monkeypatch.setattr(routes, "agent_team_summary_service", service)
    monkeypatch.setattr(settings, "operator_token", "campaign-filter-token")

    async def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                     headers={"X-Synth-City-Operator-Token": "campaign-filter-token"}) as client:
            url = f"/api/v1/agent-teams/presets/{world.team}/summary"
            plain = (await client.get(url)).json()
            filtered = (await client.get(url, params={"scope_id": world.scopes["a2"], "provider": "claude-code"})).json()
    finally:
        app.dependency_overrides.clear()
    assert plain["campaign"]["done_count"] == 2 and plain["remaining_work"]["unfinished_items"] == 6
    assert filtered["campaign"]["done_count"] == 1 and filtered["campaign"]["under_way_count"] == 1
    assert filtered["remaining_work"]["unfinished_items"] == 1
    assert filtered["next_actors"] == ["operator", "controller"]


# ---- in-memory negative controls: patched in this process only, no source edit ----------------------------------


@pytest.mark.asyncio
async def test_control_a_campaign_that_ignores_the_filter_is_seen(db, world, monkeypatch):
    service = await _service(db, world)
    original = service.campaign.summary

    async def ignoring(db, preset_id, human_actions=(), **filters):
        return await original(db, preset_id, human_actions)

    monkeypatch.setattr(service.campaign, "summary", ignoring)
    result = await service.summary(db, world.team, scope_id=world.scopes["a1"])
    assert _shape(result)["done"] == UNFILTERED["done"] != 1


@pytest.mark.asyncio
async def test_control_a_snapshot_that_ignores_the_filter_is_seen(db, world, monkeypatch):
    service = await _service(db, world)
    original = service._snapshot

    async def ignoring(db, preset_id, scope_id=None, provider=None):
        return await original(db, preset_id)

    monkeypatch.setattr(service, "_snapshot", ignoring)
    result = await service.summary(db, world.team, provider="codex-cli")
    assert result.remaining_work.unfinished_items == UNFILTERED["unfinished"] != 2
