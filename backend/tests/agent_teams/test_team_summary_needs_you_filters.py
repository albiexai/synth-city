"""The Overview 'Needs you' read honours the Repository and Assigned-harness filters (#1141).

Only the lists that 'Needs you' reads are narrowed. Counts, the campaign, remaining work and the coverage
flags stay exactly as the unfiltered summary reports them, and no filter is the plain summary.
"""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio

from app.api.v1.agent_teams import router  # noqa: F401  (the route under test)
from app.config import settings
from app.database import get_db
from app.main import app
from app.models.database import AgentTeamPreset, AgentTeamSlot, GithubWorkItem, TeamGithubScope
from app.models.factory_schemas import (
    ReminderAttention, TeamRequiredCIUnreadable, TeamSummaryAttention, TeamSummaryHumanAction, TeamWaitingInput,
)
from app.models.schemas import SupervisorObservation
from app.services.agent_team_summary_service import AgentTeamSummaryService, agent_team_summary_service

NOW = datetime.now(timezone.utc)


class _Attention:
    async def summary(self, db, preset_id):
        return {"coverage_complete": True, "actions": [], "observation_expires_at": NOW + timedelta(seconds=60)}

    async def dispatch_snapshot(self, db, scopes):
        return {"complete": True, "actions": []}


class _Progress:
    async def summary(self, db, item_id):
        return None


async def _team(db, name, scope_names, providers):
    team = AgentTeamPreset(name=name, autonomy_enabled=True)
    db.add(team)
    await db.flush()
    scopes = []
    for repo in scope_names:
        scope = TeamGithubScope(preset_id=team.id, repo_owner="example", repo_name=repo,
                                repo_path=f"/private/{repo}", enabled=True)
        db.add(scope)
        scopes.append(scope)
    slots = []
    for provider in providers:
        slot = AgentTeamSlot(preset_id=team.id, display_name=provider, provider=provider, repo_path="/private/repo",
                             repo_id="private", repo_name="repo", bootstrap_prompt="private", launch_options={})
        db.add(slot)
        slots.append(slot)
    await db.flush()
    return team, scopes, slots


async def _item(db, scope, slot, number):
    item = GithubWorkItem(scope_id=scope.id, issue_number=number, issue_title="Item", issue_url=f"https://x/{number}",
                          github_updated_at=datetime.utcnow(), dispatch_status="dispatched",
                          owner_slot_id=slot.id, dispatch_nonce=f"nonce-{number}")
    db.add(item)
    await db.flush()
    return item


@pytest.fixture(autouse=True)
def _no_hold(monkeypatch):
    monkeypatch.setattr("app.services.agent_team_summary_service.hold_code", lambda: None)


class World:
    """Team A: repositories a1 and a2, a codex and a claude slot. Team B: repository b1."""


@pytest_asyncio.fixture
async def world(db):
    w = World()
    team, (a1, a2), (codex, claude) = await _team(db, "A", ["a1", "a2"], ["codex-cli", "claude-code"])
    other, (b1,), (b_codex,) = await _team(db, "B", ["b1"], ["codex-cli"])
    w.i1 = await _item(db, a1, codex, 1)     # a1, codex
    w.i2 = await _item(db, a1, claude, 2)    # a1, claude
    w.i3 = await _item(db, a2, codex, 3)     # a2, codex
    w.ib = await _item(db, b1, b_codex, 4)   # the other team
    await db.commit()
    w.team, w.a1, w.a2, w.b1, w.codex, w.claude, w.other = team, a1, a2, b1, codex, claude, other
    w.ids = {name: getattr(w, name).id for name in ("team", "a1", "a2", "b1", "codex", "claude", "other")}
    w.items = {n: getattr(w, n).id for n in ("i1", "i2", "i3", "ib")}
    return w


def _rows(w):
    ids, items = w.ids, w.items
    scope_of = {"i1": ids["a1"], "i2": ids["a1"], "i3": ids["a2"]}
    number = {"i1": 1, "i2": 2, "i3": 3}
    actions, attention, ci, reminders = [], [], [], []
    for name in ("i1", "i2", "i3"):
        actions.append(TeamSummaryHumanAction(scope_id=scope_of[name], issue_number=number[name],
            work_item_id=items[name], kind="review_pr", state="requested", source="leader", assessment_current=True))
        attention.append(TeamSummaryAttention(code="attempt_stopped", work_item_id=items[name],
            scope_id=scope_of[name], issue_number=number[name]))
        ci.append(TeamRequiredCIUnreadable(since=NOW, reason="unknown", work_item_id=items[name],
            scope_id=scope_of[name], issue_number=number[name]))
    owner = {"i1": ids["codex"], "i2": ids["claude"], "i3": ids["codex"]}
    for name in ("i1", "i2", "i3"):
        reminders.append(ReminderAttention(kind="workspace_release", scope_id=scope_of[name],
            work_item_id=items[name], issue_number=number[name], owner_slot_id=owner[name],
            reminder_count=4, reminder_cap=4))
    # A repository row without an owner, a repository-wide reminder, and a team-level signal.
    actions.append(TeamSummaryHumanAction(scope_id=ids["a1"], issue_number=9, kind="pilot_decision",
        state="requested", source="leader", assessment_current=True))
    attention.append(TeamSummaryAttention(code="human_review", scope_id=ids["a1"], issue_number=9))
    attention.append(TeamSummaryAttention(code="hold"))
    reminders.append(ReminderAttention(kind="backlog_reconcile", scope_id=ids["a2"], issue_numbers=[7],
        reminder_count=1, reminder_cap=4))
    # A reminder that names its owner slot but no work item: the owner alone decides the harness.
    reminders.append(ReminderAttention(kind="backlog_reconcile", scope_id=ids["a2"], issue_numbers=[8],
        owner_slot_id=ids["claude"], reminder_count=2, reminder_cap=4))
    waiting = [TeamWaitingInput(slot_id=ids["codex"], reason="question"),
               TeamWaitingInput(slot_id=ids["claude"], reason="trust")]
    supervisor = SupervisorObservation(status="late", actor_id=None, address=None, checked_at=None,
        next_check_at=None, grace_seconds=1800, note=None, attention="Supervisor check-in overdue")
    return {"human_actions": actions, "attention": attention, "required_ci_unreadable": ci,
            "reminder_attention": reminders, "waiting_input": waiting, "supervisor_observation": supervisor}


async def _service(db, w, monkeypatch):
    service = AgentTeamSummaryService(progress=_Progress(), attention=_Attention())
    base = await service.summary(db, w.ids["team"])
    full = base.model_copy(update=_rows(w))
    asked = []

    async def summary(db, preset_id, **filters):
        asked.append(filters)
        return full if preset_id == w.ids["team"] else await AgentTeamSummaryService(
            progress=_Progress(), attention=_Attention()).summary(db, preset_id, **filters)

    monkeypatch.setattr(service, "summary", summary)
    service.asked = asked
    return service, full


def _picture(result, w):
    """What 'Needs you' would show, as comparable names."""
    reverse = {v: k for k, v in w.items.items()}
    def work(rows):
        return sorted(reverse.get(r.work_item_id, f"scope{r.scope_id}-{getattr(r, 'issue_number', None)}")
                      for r in rows)

    return {
        "actions": work(result.human_actions), "attention": sorted(
            f"{r.code}:{reverse.get(r.work_item_id, r.scope_id)}" for r in result.attention),
        "ci": work(result.required_ci_unreadable),
        "reminders": sorted(f"{r.kind}:{reverse.get(r.work_item_id, r.scope_id)}" for r in result.reminder_attention),
        "waiting": sorted(r.slot_id for r in result.waiting_input or []),
        "supervisor": result.supervisor_observation.attention if result.supervisor_observation else None,
    }


@pytest.mark.asyncio
async def test_no_filter_is_the_plain_summary(db, world, monkeypatch):
    service, full = await _service(db, world, monkeypatch)
    assert await service.needs_you(db, world.ids["team"]) is full


@pytest.mark.asyncio
async def test_scope_filter_keeps_that_repository_and_the_team_level_rows(db, world, monkeypatch):
    service, _ = await _service(db, world, monkeypatch)
    ids = world.ids
    result = await service.needs_you(db, ids["team"], scope_id=ids["a1"])
    got = _picture(result, world)
    assert got["actions"] == ["i1", "i2", f"scope{ids['a1']}-9"]
    assert got["ci"] == ["i1", "i2"]
    assert {r.work_item_id for r in result.attention if r.work_item_id} == {world.items["i1"], world.items["i2"]}
    assert {r.code for r in result.attention if not r.work_item_id} == {"human_review", "hold"}
    assert got["reminders"] == ["workspace_release:i1", "workspace_release:i2"]
    # The team owns a1: its team-level rows and the waiting members stay.
    assert got["waiting"] == sorted([ids["codex"], ids["claude"]])
    assert got["supervisor"] == "Supervisor check-in overdue"


@pytest.mark.asyncio
async def test_scope_filter_drops_every_other_repository_row(db, world, monkeypatch):
    service, _ = await _service(db, world, monkeypatch)
    ids = world.ids
    result = await service.needs_you(db, ids["team"], scope_id=ids["a2"])
    assert [r.work_item_id for r in result.human_actions] == [world.items["i3"]]
    assert [r.work_item_id for r in result.required_ci_unreadable] == [world.items["i3"]]
    assert sorted((r.kind, r.work_item_id or 0) for r in result.reminder_attention) == sorted([
        ("workspace_release", world.items["i3"]), ("backlog_reconcile", 0), ("backlog_reconcile", 0)])
    assert all(r.scope_id in (None, ids["a2"]) for r in result.attention)


@pytest.mark.asyncio
async def test_a_repository_of_another_team_gives_this_team_no_needs_you_rows(db, world, monkeypatch):
    service, _ = await _service(db, world, monkeypatch)
    result = await service.needs_you(db, world.ids["team"], scope_id=world.ids["b1"])
    assert _picture(result, world) == {"actions": [], "attention": [], "ci": [], "reminders": [],
                                       "waiting": [], "supervisor": None}


@pytest.mark.asyncio
async def test_provider_filter_keeps_rows_whose_owner_slot_has_that_provider(db, world, monkeypatch):
    service, _ = await _service(db, world, monkeypatch)
    ids = world.ids
    result = await service.needs_you(db, ids["team"], provider="codex-cli")
    assert [r.work_item_id for r in result.human_actions] == [world.items["i1"], world.items["i3"]]
    assert [r.work_item_id for r in result.required_ci_unreadable] == [world.items["i1"], world.items["i3"]]
    assert [r.owner_slot_id for r in result.reminder_attention] == [ids["codex"], ids["codex"]]
    assert [r.slot_id for r in result.waiting_input] == [ids["codex"]]
    # The repository rows without an owner have no harness and are dropped; the team-level row follows the roster.
    assert all(r.work_item_id is not None or r.code == "hold" for r in result.attention)
    assert any(r.code == "hold" for r in result.attention)
    claude = await service.needs_you(db, ids["team"], provider="claude-code")
    assert [(r.kind, r.owner_slot_id, r.work_item_id) for r in claude.reminder_attention] == [
        ("workspace_release", ids["claude"], world.items["i2"]), ("backlog_reconcile", ids["claude"], None)]
    assert result.supervisor_observation.attention == "Supervisor check-in overdue"


@pytest.mark.asyncio
async def test_a_harness_outside_the_roster_hides_the_team_level_rows_too(db, world, monkeypatch):
    service, _ = await _service(db, world, monkeypatch)
    result = await service.needs_you(db, world.ids["team"], provider="antigravity-cli")
    assert _picture(result, world) == {"actions": [], "attention": [], "ci": [], "reminders": [],
                                       "waiting": [], "supervisor": None}


@pytest.mark.asyncio
async def test_both_filters_must_match(db, world, monkeypatch):
    service, _ = await _service(db, world, monkeypatch)
    ids = world.ids
    result = await service.needs_you(db, ids["team"], scope_id=ids["a1"], provider="claude-code")
    assert [r.work_item_id for r in result.human_actions] == [world.items["i2"]]
    assert [r.work_item_id for r in result.required_ci_unreadable] == [world.items["i2"]]
    assert [r.slot_id for r in result.waiting_input] == [ids["claude"]]
    result = await service.needs_you(db, ids["team"], scope_id=ids["a2"], provider="claude-code")
    assert result.human_actions == [] and result.required_ci_unreadable == []
    # Only the reminder that names a claude owner in a2 matches both.
    assert [(r.scope_id, r.owner_slot_id) for r in result.reminder_attention] == [(ids["a2"], ids["claude"])]


@pytest.mark.asyncio
@pytest.mark.parametrize("filters", [{"scope_id": "a1"}, {"provider": "codex-cli"}, {"scope_id": "b1"},
                                     {"scope_id": "a1", "provider": "claude-code"}])
async def test_the_filter_reaches_the_summary_and_the_trim_only_touches_the_team_wide_lists(
        db, world, monkeypatch, filters):
    """The work-based parts are narrowed where the summary builds them (see test_team_campaign_filters.py); this
    method passes the filter down and trims only the lists that come from team-wide services."""
    service, full = await _service(db, world, monkeypatch)
    kwargs = {k: (world.ids[v] if k == "scope_id" else v) for k, v in filters.items()}
    result = await service.needs_you(db, world.ids["team"], **kwargs)
    assert service.asked == [{"scope_id": kwargs.get("scope_id"), "provider": kwargs.get("provider")}]
    trimmed = {"human_actions", "attention", "required_ci_unreadable", "reminder_attention",
               "waiting_input", "supervisor_observation"}
    assert result.model_dump(exclude=trimmed) == full.model_dump(exclude=trimmed)


@pytest.mark.asyncio
async def test_an_unknown_preset_stays_none(db, world, monkeypatch):
    service, _ = await _service(db, world, monkeypatch)
    assert await service.needs_you(db, 999999, scope_id=world.ids["a1"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("own_attention", [False, True])
async def test_pr_guard_ignores_other_items_and_keeps_team_coverage(db, world, client, monkeypatch, own_attention):
    ids, items = world.ids, world.items
    world.i1.pr_number = 101
    world.i2.dispatch_status = "escalated"
    await db.commit()
    _, full = await _service(db, world, monkeypatch)
    actions = [full.human_actions[1], full.human_actions[2].model_copy(update={"work_item_id": None})]
    attention = [full.attention[1]]
    if own_attention:
        actions.append(full.human_actions[0])
        attention.append(full.attention[0])
    full = full.model_copy(update={"coverage_complete": True, "waiting_input": [], "hold_code": None,
                                  "human_actions": actions, "attention": attention})

    async def summary(db, preset_id, **filters):
        return full

    monkeypatch.setattr(agent_team_summary_service, "summary", summary)
    url = f"/api/v1/agent-teams/presets/{ids['team']}/summary"
    plain = (await client.get(url)).json()
    scoped_response = await client.get(url, params={"work_item_id": items["i1"]})
    assert scoped_response.status_code == 200
    scoped = scoped_response.json()
    assert scoped["attention"] == ([plain["attention"][-1]] if own_attention else [])
    assert scoped["human_actions"] == ([plain["human_actions"][-1]] if own_attention else [])
    for field in ("coverage_complete", "next_actor_coverage_complete", "valid_until", "hold_code", "waiting_input"):
        assert scoped[field] == plain[field]
    assert (await client.get(url, params={"work_item_id": items["ib"]})).status_code == 404


@pytest.mark.asyncio
async def test_pr_guard_preserves_global_hold_waits_and_unknown_coverage(db, world, monkeypatch):
    service, full = await _service(db, world, monkeypatch)
    full = full.model_copy(update={"coverage_complete": False, "hold_code": "hold",
                                  "valid_until": datetime.now(timezone.utc) + timedelta(seconds=30)})
    monkeypatch.setattr("app.services.agent_team_summary_service.hold_code", lambda: "hold")

    async def summary(db, preset_id):
        return full

    monkeypatch.setattr(service, "summary", summary)
    scoped = await service.for_work_item(db, world.ids["team"], world.items["i1"])
    assert scoped.coverage_complete is False and scoped.hold_code == "hold"
    assert scoped.waiting_input == full.waiting_input
    assert [row.code for row in scoped.attention] == ["attempt_stopped", "human_review", "hold"]
    assert [row.kind for row in scoped.human_actions] == ["review_pr", "pilot_decision"]


@pytest.mark.asyncio
async def test_pr_guard_rechecks_hold_after_binding_an_unbound_action(db, world, monkeypatch):
    service, full = await _service(db, world, monkeypatch)
    unbound = full.human_actions[1].model_copy(update={"work_item_id": None})
    full = full.model_copy(update={"coverage_complete": True, "waiting_input": [], "hold_code": None,
                                  "valid_until": datetime.now(timezone.utc) + timedelta(seconds=30),
                                  "human_actions": [unbound], "attention": []})
    hold = None
    original_execute = db.execute
    summary_returned = False

    async def summary(db, preset_id):
        nonlocal summary_returned
        summary_returned = True
        return full

    async def execute(statement, *args, **kwargs):
        nonlocal hold
        result = await original_execute(statement, *args, **kwargs)
        if summary_returned:
            hold = "hold"
        return result

    monkeypatch.setattr(service, "summary", summary)
    monkeypatch.setattr(db, "execute", execute)
    monkeypatch.setattr("app.services.agent_team_summary_service.hold_code", lambda: hold)
    scoped = await service.for_work_item(db, world.ids["team"], world.items["i1"])
    assert scoped.human_actions == []
    assert scoped.hold_code == "hold" and not scoped.coverage_complete
    assert scoped.waiting_input is None


@pytest_asyncio.fixture
async def client(db, monkeypatch):
    monkeypatch.setattr(settings, "operator_token", "needs-you-filter-test-token")

    async def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 headers={"X-Synth-City-Operator-Token": "needs-you-filter-test-token"}) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_http_filters_are_validated_and_optional(db, world, client, monkeypatch):
    service, full = await _service(db, world, monkeypatch)
    monkeypatch.setattr(agent_team_summary_service, "summary", service.summary)
    url = f"/api/v1/agent-teams/presets/{world.ids['team']}/summary"
    plain = await client.get(url)
    assert plain.status_code == 200 and plain.headers["Cache-Control"] == "no-store"
    assert len(plain.json()["human_actions"]) == 4
    assert (await client.get(url, params={"provider": "not-a-harness"})).status_code == 422
    assert (await client.get(url, params={"provider": ""})).status_code == 422
    assert (await client.get(url, params={"scope_id": 999999})).status_code == 404
    assert (await client.get(url, params={"scope_id": 0})).status_code == 422
    assert (await client.get("/api/v1/agent-teams/presets/999999/summary",
                             params={"scope_id": world.ids["a1"]})).status_code == 404


@pytest.mark.asyncio
async def test_http_filtered_read_narrows_only_the_needs_you_lists(db, world, client, monkeypatch):
    service, full = await _service(db, world, monkeypatch)
    monkeypatch.setattr(agent_team_summary_service, "summary", service.summary)
    url = f"/api/v1/agent-teams/presets/{world.ids['team']}/summary"
    plain = (await client.get(url)).json()
    filtered = await client.get(url, params={"scope_id": world.ids["a2"], "provider": "codex-cli"})
    assert filtered.status_code == 200 and filtered.headers["Cache-Control"] == "no-store"
    data = filtered.json()
    assert [r["work_item_id"] for r in data["human_actions"]] == [world.items["i3"]]
    assert data["campaign"] == plain["campaign"] and data["remaining_work"] == plain["remaining_work"]
    # Without the query the same route still returns every row.
    again = (await client.get(url)).json()
    assert again == plain
