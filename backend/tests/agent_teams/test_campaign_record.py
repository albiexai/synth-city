"""#1242: the campaign record across a team's repositories (design §2).

One active or finishing campaign per team lifetime, with a `label:` or `milestone:` selector matched in every
repository the team watches. Membership is read live; #1219's deferrals and supersessions take an issue out of it,
with their reason. A repository that can't be read leaves the membership incomplete, never empty.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.database import (
    AgentTeamPreset,
    Campaign,
    FactoryAuditEvent,
    FactoryContextKey,
    GithubWorkItem,
    TeamGithubScope,
)
from app.services import campaign_record_service as campaigns
from app.services import factory_audit_service as audit
from app.services.github_client import GithubClient, GithubClientResponseError

pytestmark = pytest.mark.asyncio
T0 = datetime(2026, 11, 20, 9, 0, 0)
OPERATOR = audit.derive_actor(actor_kind="operator")


def _app_pragmas(connection, _record):
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'campaigns.db'}")
    # As the application runs SQLite (V1 review 6072435444): with foreign keys enforced.
    event.listen(engine.sync_engine, "connect", _app_pragmas)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()


def _issue(repo, number, state="open", labels=("v4.4",), milestone=None):
    issue = {"number": number, "title": f"Issue {number}", "state": state,
             "html_url": f"https://github.com/o/{repo}/issues/{number}",
             "labels": [{"name": name} for name in labels]}
    if milestone is not None:
        issue["milestone"] = {"number": milestone}
    return issue


class FakeGithub:
    """Per repository: labelled issues, milestones and milestone issues. A repository in ``broken`` can't be read."""

    def __init__(self, labelled=None, milestones=None, milestone_issues=None, broken=()):
        self.labelled, self.milestones = labelled or {}, milestones or {}
        self.milestone_issues, self.broken = milestone_issues or {}, set(broken)

    def _check(self, repo):
        self.read = getattr(self, "read", []) + [repo]
        if repo in self.broken:
            raise GithubClientResponseError("unreadable")

    async def list_label_issues(self, owner, repo, label):
        self._check(repo)
        return [i for i in self.labelled.get(repo, []) if label in {x["name"] for x in i["labels"]}]

    async def list_milestones(self, owner, repo):
        self._check(repo)
        return self.milestones.get(repo, [])

    async def list_milestone_issues(self, owner, repo, milestone):
        self._check(repo)
        return self.milestone_issues.get((repo, milestone), [])


class World:
    pass


@pytest_asyncio.fixture
async def world(db):
    """Team A watches o/a1 and o/a2; team B watches o/b1."""
    w = World()
    team, other = AgentTeamPreset(name="A"), AgentTeamPreset(name="B")
    db.add_all([team, other])
    await db.flush()
    scopes = {repo: TeamGithubScope(preset_id=preset.id, repo_owner="o", repo_name=repo,
                                    repo_path=f"/private/{repo}", enabled=True)
              for preset, repo in ((team, "a1"), (team, "a2"), (other, "b1"))}
    db.add_all(scopes.values())
    await db.flush()
    items = {(repo, number): GithubWorkItem(scope_id=scopes[repo].id, issue_number=number,
                                            issue_title=f"Issue {number}", issue_url=f"https://x/{repo}/{number}",
                                            github_updated_at=T0, dispatch_status="pending")
             for repo, number in (("a1", 1), ("a1", 2), ("a2", 3), ("b1", 9))}
    db.add_all(items.values())
    await db.commit()
    w.team, w.other, w.scopes, w.items = team.id, other.id, {k: v.id for k, v in scopes.items()}, \
        {k: v.id for k, v in items.items()}
    return w


async def _stopped(db, item_id, outcome, at, **evidence):
    """#1219's record of an ended issue, as end_stopped_item writes it."""
    await audit.record_event(
        db, event_kind="stopped_work_item_ended", source="test", occurred_at=at, actor=OPERATOR, item_id=item_id,
        action_outcome="applied", operation_id=f"end_stopped_item:test-{item_id}-{outcome}",
        sanitized_reason="Tracking moved to the recorded destination.",
        after_values={"dispatch_status": "cancelled", "outcome": outcome, **evidence})
    await db.commit()


# The record
async def test_a_campaign_is_bound_to_the_team_lifetime_with_its_selector(db, world):
    campaign = await campaigns.start(db, world.team, "label:v4.4", started_at=T0, actor=OPERATOR)
    key = await audit.current_context_key(db, "team", world.team)
    assert key is not None and campaign.team_context_key == key
    assert (campaign.preset_id, campaign.selector, campaign.status, campaign.started_at) == (
        world.team, "label:v4.4", "active", T0)
    assert (campaign.finished_at, campaign.finished_by) == (None, None)
    assert (await campaigns.current(db, world.team)).id == campaign.id
    assert await campaigns.current(db, world.other) is None
    started = (await db.scalars(select(FactoryAuditEvent).where(
        FactoryAuditEvent.event_kind == "campaign_started"))).one()
    assert (started.team_context_key, started.actor_kind) == (key, "operator")


@pytest.mark.parametrize("selector", ["", "label:", "milestone:  ", "parent:1238", "Label:v4.4", "v4.4"])
async def test_a_malformed_selector_is_refused(db, world, selector):
    with pytest.raises(campaigns.CampaignError) as refused:
        await campaigns.start(db, world.team, selector, started_at=T0, actor=OPERATOR)
    assert refused.value.code == "campaign_selector_invalid"
    assert (await db.scalars(select(Campaign))).all() == []


@pytest.mark.parametrize("status", ["active", "finishing"])
async def test_a_team_has_at_most_one_active_or_finishing_campaign(db, world, status):
    first = await campaigns.start(db, world.team, "label:v4.4", started_at=T0, actor=OPERATOR)
    await db.execute(update(Campaign).where(Campaign.id == first.id).values(status=status))
    await db.commit()
    with pytest.raises(campaigns.CampaignError) as refused:
        await campaigns.start(db, world.team, "milestone:v4.5", started_at=T0, actor=OPERATOR)
    assert refused.value.code == "campaign_already_active"
    # The database itself refuses a second one, whatever wrote it.
    db.add(Campaign(team_context_key=first.team_context_key, preset_id=world.team, selector="label:x",
                    status="active", started_at=T0))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_a_finished_campaign_makes_room_for_the_next(db, world):
    first = await campaigns.start(db, world.team, "label:v4.4", started_at=T0, actor=OPERATOR)
    await db.execute(update(Campaign).where(Campaign.id == first.id).values(
        status="finished", finished_at=T0 + timedelta(days=3), finished_by="shared-operator-credential"))
    await db.commit()
    second = await campaigns.start(db, world.team, "milestone:v4.5", started_at=T0 + timedelta(days=3),
                                   actor=OPERATOR)
    assert (await campaigns.current(db, world.team)).id == second.id


async def test_an_unknown_status_is_refused_by_the_database(db, world):
    db.add(Campaign(team_context_key="team:1:x", preset_id=world.team, selector="label:x", status="paused",
                    started_at=T0))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_a_new_team_lifetime_does_not_inherit_the_old_campaign(db, world):
    await campaigns.start(db, world.team, "label:v4.4", started_at=T0, actor=OPERATOR)
    # The preset id is reused after its team was deleted: the old lifetime key is retired.
    await db.execute(update(FactoryContextKey).where(FactoryContextKey.key_kind == "team")
                     .values(retired_at=T0 + timedelta(days=1)))
    await audit.context_key_for(db, "team", world.team)  # the new team's own lifetime begins
    await db.commit()
    assert await campaigns.current(db, world.team) is None
    fresh = await campaigns.start(db, world.team, "label:v4.4", started_at=T0 + timedelta(days=1), actor=OPERATOR)
    old = (await db.scalars(select(Campaign).order_by(Campaign.id))).first()
    assert fresh.status == "active" and fresh.team_context_key != old.team_context_key
    assert fresh.team_context_key == await audit.current_context_key(db, "team", world.team)


# Membership across the team's repositories
async def test_a_label_campaign_has_members_from_every_repository_the_team_watches(db, world):
    campaign = await campaigns.start(db, world.team, "label:v4.4", started_at=T0, actor=OPERATOR)
    github = FakeGithub(labelled={"a1": [_issue("a1", 1), _issue("a1", 2, state="closed"),
                                         _issue("a1", 4, labels=("other",))],
                                  "a2": [_issue("a2", 3)], "b1": [_issue("b1", 9)]})
    membership = await campaigns.members(db, campaign, github)
    assert [(m.repo, m.issue_number, m.state) for m in membership.members] == [
        ("o/a1", 1, "open"), ("o/a1", 2, "closed"), ("o/a2", 3, "open")]
    assert membership.complete is True and membership.departures == []
    assert [(r.repo, r.complete) for r in membership.repositories] == [("o/a1", True), ("o/a2", True)]


async def test_a_milestone_campaign_matches_the_title_in_each_repository(db, world):
    campaign = await campaigns.start(db, world.team, "milestone:v4.4", started_at=T0, actor=OPERATOR)
    github = FakeGithub(  # GitHub milestones are per repository: the same title has a different number in each
        milestones={"a1": [{"number": 7, "title": "v4.4"}, {"number": 8, "title": "v4.5"}],
                    "a2": [{"number": 2, "title": "v4.4"}]},
        milestone_issues={("a1", 7): [_issue("a1", 1, milestone=7)], ("a1", 8): [_issue("a1", 5, milestone=8)],
                          ("a2", 2): [_issue("a2", 3, milestone=2, state="closed")]})
    membership = await campaigns.members(db, campaign, github)
    assert [(m.repo, m.issue_number) for m in membership.members] == [("o/a1", 1), ("o/a2", 3)]
    assert membership.complete is True


@pytest.mark.parametrize("milestones,reason", [
    ([], "milestone_not_found"),
    ([{"number": 2, "title": "v4.4"}, {"number": 3, "title": "v4.4"}], "milestone_ambiguous"),
])
async def test_a_milestone_that_is_not_exactly_one_leaves_that_repository_incomplete(db, world, milestones, reason):
    campaign = await campaigns.start(db, world.team, "milestone:v4.4", started_at=T0, actor=OPERATOR)
    github = FakeGithub(milestones={"a1": [{"number": 7, "title": "v4.4"}], "a2": milestones},
                        milestone_issues={("a1", 7): [_issue("a1", 1, milestone=7)]})
    membership = await campaigns.members(db, campaign, github)
    assert membership.complete is False
    assert [(r.repo, r.complete, r.reason) for r in membership.repositories] == [
        ("o/a1", True, None), ("o/a2", False, reason)]
    assert [(m.repo, m.issue_number) for m in membership.members] == [("o/a1", 1)]


async def test_an_unreadable_repository_leaves_the_membership_incomplete_not_empty(db, world):
    campaign = await campaigns.start(db, world.team, "label:v4.4", started_at=T0, actor=OPERATOR)
    membership = await campaigns.members(db, campaign, FakeGithub(labelled={"a1": [_issue("a1", 1)]},
                                                                  broken={"a2"}))
    assert membership.complete is False
    assert [(r.repo, r.complete, r.reason) for r in membership.repositories] == [
        ("o/a1", True, None), ("o/a2", False, "github_unreadable")]


# Deferral and supersession (#1219)
@pytest.mark.parametrize("outcome,evidence,destination", [
    ("deferred", {"target_milestone": "v4.5"}, "v4.5"),
    ("superseded", {"superseding_pr_number": 1400, "merge_commit_sha": "a" * 40}, "PR #1400"),
])
async def test_a_deferred_or_superseded_issue_leaves_membership_with_its_reason(db, world, outcome, evidence,
                                                                               destination):
    campaign = await campaigns.start(db, world.team, "label:v4.4", started_at=T0, actor=OPERATOR)
    await _stopped(db, world.items[("a1", 2)], outcome, T0 + timedelta(hours=2), **evidence)
    github = FakeGithub(labelled={"a1": [_issue("a1", 1), _issue("a1", 2)], "a2": []})  # it still carries the label
    membership = await campaigns.members(db, campaign, github)
    assert [(m.repo, m.issue_number) for m in membership.members] == [("o/a1", 1)]
    (departure,) = membership.departures
    assert (departure.repo, departure.issue_number, departure.outcome, departure.destination, departure.reason,
            departure.at) == ("o/a1", 2, outcome, destination, "Tracking moved to the recorded destination.",
                              T0 + timedelta(hours=2))


async def test_departures_outside_the_campaign_or_the_team_do_not_count(db, world):
    campaign = await campaigns.start(db, world.team, "label:v4.4", started_at=T0, actor=OPERATOR)
    await _stopped(db, world.items[("a1", 1)], "deferred", T0 - timedelta(days=1), target_milestone="v4.3")
    await _stopped(db, world.items[("b1", 9)], "deferred", T0 + timedelta(hours=1), target_milestone="v4.5")
    github = FakeGithub(labelled={"a1": [_issue("a1", 1)], "a2": []})
    membership = await campaigns.members(db, campaign, github)
    assert [(m.repo, m.issue_number) for m in membership.members] == [("o/a1", 1)]
    assert membership.departures == []


async def test_a_finished_campaign_ignores_departures_after_its_finish(db, world):
    campaign = await campaigns.start(db, world.team, "label:v4.4", started_at=T0, actor=OPERATOR)
    await db.execute(update(Campaign).where(Campaign.id == campaign.id).values(
        status="finished", finished_at=T0 + timedelta(days=1)))
    await db.commit()
    await db.refresh(campaign)
    await _stopped(db, world.items[("a1", 2)], "deferred", T0 + timedelta(days=2), target_milestone="v4.5")
    membership = await campaigns.members(db, campaign, FakeGithub(labelled={"a1": [_issue("a1", 2)], "a2": []}))
    assert [(m.issue_number) for m in membership.members] == [2] and membership.departures == []


# Team lifetime and retained history (V1 review 6072435444)
async def _delete_team(db, preset_id):
    """Delete a team as the application does; the lifetime trigger retires its key."""
    scopes = "SELECT id FROM team_github_scopes WHERE preset_id = :team"
    await db.execute(text(f"DELETE FROM github_work_items WHERE scope_id IN ({scopes})"), {"team": preset_id})
    await db.execute(text("DELETE FROM team_github_scopes WHERE preset_id = :team"), {"team": preset_id})
    await db.execute(text("DELETE FROM agent_team_presets WHERE id = :team"), {"team": preset_id})
    await db.commit()


async def test_a_retired_campaign_never_reads_the_replacement_teams_repositories(db, world):
    campaign = await campaigns.start(db, world.team, "label:v4.4", started_at=T0, actor=OPERATOR)
    await _delete_team(db, world.team)
    # A new team takes the same id and watches another repository.
    db.add(AgentTeamPreset(id=world.team, name="Replacement"))
    await db.flush()
    db.add(TeamGithubScope(preset_id=world.team, repo_owner="o", repo_name="c1", repo_path="/private/c1",
                           enabled=True))
    await audit.context_key_for(db, "team", world.team)  # the new team's own lifetime is live
    await db.commit()
    github = FakeGithub(labelled={"c1": [_issue("c1", 7)]})
    membership = await campaigns.members(db, campaign, github)
    assert membership.members == [] and membership.complete is False
    assert membership.unavailable == "team_retired" and not getattr(github, "read", [])
    assert await campaigns.current(db, world.team) is None


async def test_a_replacement_during_the_membership_read_is_never_queried(db, world, tmp_path):
    """V1 review 6073257579: a second session replaces the team while the first is reading its lifetime."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    campaign = await campaigns.start(db, world.team, "label:v4.4", started_at=T0, actor=OPERATOR)
    other = create_engine(f"sqlite:///{tmp_path / 'campaigns.db'}")
    event.listen(other, "connect", _app_pragmas)
    fired = []

    def replace(_conn, _cursor, statement, _parameters, _context, _many):
        if fired or "factory_context_keys" not in statement:
            return
        fired.append(statement)
        scopes = "SELECT id FROM team_github_scopes WHERE preset_id = :team"
        with other.begin() as writer:  # a real second connection commits the replacement mid-read
            writer.execute(text(f"DELETE FROM github_work_items WHERE scope_id IN ({scopes})"), {"team": world.team})
            writer.execute(text("DELETE FROM team_github_scopes WHERE preset_id = :team"), {"team": world.team})
            writer.execute(text("DELETE FROM agent_team_presets WHERE id = :team"), {"team": world.team})
        with Session(other) as writer:
            writer.add(AgentTeamPreset(id=world.team, name="Replacement"))
            writer.flush()
            writer.add(TeamGithubScope(preset_id=world.team, repo_owner="o", repo_name="c1",
                                       repo_path="/private/c1", enabled=True))
            writer.commit()

    sync_engine = db.bind.sync_engine
    event.listen(sync_engine, "after_cursor_execute", replace)
    github = FakeGithub(labelled={"a1": [_issue("a1", 1)], "a2": [], "c1": [_issue("c1", 7)]})
    try:
        membership = await campaigns.members(db, campaign, github)
    finally:
        event.remove(sync_engine, "after_cursor_execute", replace)
        other.dispose()
    assert fired  # the replacement really landed during the read
    assert "c1" not in getattr(github, "read", [])
    assert [(m.repo, m.issue_number) for m in membership.members] == [("o/a1", 1)]


async def test_a_deferral_survives_deleting_its_work_item(db, world):
    campaign = await campaigns.start(db, world.team, "label:v4.4", started_at=T0, actor=OPERATOR)
    await _stopped(db, world.items[("a1", 2)], "deferred", T0 + timedelta(hours=2), target_milestone="v4.5")
    await db.execute(text("DELETE FROM github_work_items WHERE id = :id"), {"id": world.items[("a1", 2)]})
    await db.commit()
    membership = await campaigns.members(db, campaign, FakeGithub(labelled={"a1": [_issue("a1", 2)], "a2": []}))
    assert membership.members == []  # the still-labelled issue stays out
    (departure,) = membership.departures
    assert (departure.repo, departure.issue_number, departure.destination, departure.reason) == (
        "o/a1", 2, "v4.5", "Tracking moved to the recorded destination.")


async def test_a_reused_work_item_id_cannot_redirect_a_recorded_departure(db, world):
    team_campaign = await campaigns.start(db, world.team, "label:v4.4", started_at=T0, actor=OPERATOR)
    other_campaign = await campaigns.start(db, world.other, "label:v4.4", started_at=T0, actor=OPERATOR)
    item_id = world.items[("a1", 2)]
    await _stopped(db, item_id, "deferred", T0 + timedelta(hours=2), target_milestone="v4.5")
    await db.execute(text("DELETE FROM github_work_items WHERE id = :id"), {"id": item_id})
    await db.commit()
    # The id comes back as another team's issue.
    db.add(GithubWorkItem(id=item_id, scope_id=world.scopes["b1"], issue_number=2, issue_title="Other",
                          issue_url="https://x/b1/2", github_updated_at=T0, dispatch_status="pending"))
    await db.commit()
    other = await campaigns.members(db, other_campaign, FakeGithub(labelled={"b1": [_issue("b1", 2)]}))
    assert [(m.repo, m.issue_number) for m in other.members] == [("o/b1", 2)] and other.departures == []
    team = await campaigns.members(db, team_campaign, FakeGithub(labelled={"a1": [], "a2": []}))
    assert [(d.repo, d.issue_number) for d in team.departures] == [("o/a1", 2)]


async def test_a_campaign_for_a_missing_team_is_refused_before_anything_is_written(db, world):
    with pytest.raises(campaigns.CampaignError) as refused:
        await campaigns.start(db, 999, "label:v4.4", started_at=T0, actor=OPERATOR)
    assert refused.value.code == "campaign_team_not_found"
    assert (await db.scalars(select(Campaign))).all() == []
    assert await audit.current_context_key(db, "team", 999) is None
    assert (await db.scalars(select(FactoryAuditEvent).where(
        FactoryAuditEvent.event_kind == "campaign_started"))).all() == []


# The label reader
async def test_the_label_reader_reads_every_page_in_all_states_and_skips_pull_requests():
    requests = []

    def handler(request):
        requests.append(request)
        assert (request.url.params["labels"], request.url.params["state"]) == ("v4.4", "all")
        if request.url.params.get("page") == "2":
            data = [_issue("r", 2, state="closed")]
            headers = {}
        else:
            data = [_issue("r", 1), {"number": 5, "pull_request": {}}]
            headers = {"Link": '<https://api.github.com/repos/o/r/issues?labels=v4.4&state=all&per_page=100'
                               '&page=2>; rel="next"'}
        return httpx.Response(200, json=data, headers=headers)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.com") as http:
        issues = await GithubClient(http=http, token="synthetic-test-key").list_label_issues("o", "r", "v4.4")
    assert [issue["number"] for issue in issues] == [1, 2] and len(requests) == 2


async def test_the_label_reader_refuses_an_issue_without_the_label():
    data = [_issue("r", 1, labels=("other",))]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data)),
                                 base_url="https://api.github.com") as http:
        with pytest.raises(GithubClientResponseError):
            await GithubClient(http=http).list_label_issues("o", "r", "v4.4")
