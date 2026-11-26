"""#1243: campaigns created on upgrade for existing teams (design §2).

The first start of this version enqueues every team with a backlog milestone or a parent issue, as `CampaignProgress`
reads them. The poll then gives each one campaign, once, starting at its first admission of a matching item. Teams
configured later, or without either setting, get none until the Director starts one. A campaign from before the
recording reads unknown for every recorded-only measure.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.database import (
    AgentTeamPreset,
    Campaign,
    CampaignUpgradeBackfill,
    CampaignUpgradeCoverage,
    FactoryAuditEvent,
    GithubCheckRunHistoryCoverage,
    GithubMergeAttemptCoverage,
    GithubRateLimitHistoryCoverage,
    GithubWorkItem,
    TeamGithubScope,
)
from app.services import campaign_record_service as campaigns
from app.services import campaign_upgrade_service as upgrade
from app.services import factory_audit_service as audit
from app.services.github_client import GithubClientResponseError
from app.services.github_rate_limit_history import campaign_rate_limit_time

pytestmark = pytest.mark.asyncio
T0 = datetime(2026, 11, 1, 9, 0, 0)
OPERATOR = audit.derive_actor(actor_kind="operator")
NEW_TABLES = ("campaign_upgrade_backfill", "campaign_upgrade_coverage", "campaigns")


def _pragmas(connection, _record):
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")


@pytest_asyncio.fixture
async def maker(tmp_path):
    """A pre-4.4 database: the current schema without the campaign tables."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'upgrade.db'}")
    event.listen(engine.sync_engine, "connect", _pragmas)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for table in NEW_TABLES:
            await conn.execute(text(f"DROP TABLE {table}"))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _upgrade(maker):
    """The first start of this version: the missing tables, then the once-only install."""
    async with maker() as db:
        await db.run_sync(lambda session: Base.metadata.create_all(session.connection()))
        await db.commit()
        return await upgrade.install(db)


def _issue(number, title=None):
    return {"number": number, "title": title or f"Issue {number}", "state": "open", "labels": []}


class FakeGithub:
    """Milestones and their issues per repository, and single issues; a repository in ``broken`` can't be read."""

    def __init__(self, milestones=None, milestone_issues=None, issues=None, broken=()):
        self.milestones, self.milestone_issues = milestones or {}, milestone_issues or {}
        self.issues, self.broken = issues or {}, set(broken)

    def _check(self, repo):
        if repo in self.broken:
            raise GithubClientResponseError("unreadable")

    async def list_milestones(self, owner, repo):
        self._check(repo)
        return self.milestones.get(repo, [])

    async def list_milestone_issues(self, owner, repo, milestone):
        self._check(repo)
        return self.milestone_issues.get((repo, milestone), [])

    async def list_label_issues(self, owner, repo, label):
        self._check(repo)
        return []

    async def get_issue(self, owner, repo, number):
        self._check(repo)
        return self.issues.get((repo, number))


def _client(**changes):
    """Team M routes milestone "v4.4" in o/m1 and o/m2; team P's parent o/p1#50 sits in milestone "Sprint 9"."""
    values = {
        "milestones": {"m1": [{"number": 1, "title": "v4.4"}], "m2": [{"number": 4, "title": "v4.4"}],
                       "p1": [{"number": 7, "title": "Sprint 9"}]},
        "milestone_issues": {("m1", 1): [_issue(1)], ("m2", 4): [_issue(3)], ("p1", 7): [_issue(50), _issue(5)]},
        "issues": {("p1", 50): {"number": 50, "milestone": {"number": 7, "title": "Sprint 9"}}}}
    values.update(changes)
    return FakeGithub(**values)


async def _team(db, name, repos, *, milestone=None, parent=None, items=()):
    team = AgentTeamPreset(name=name, backlog_milestone=milestone, parent_issue_url=parent)
    db.add(team)
    await db.flush()
    scopes = {}
    for repo in repos:
        scopes[repo] = TeamGithubScope(preset_id=team.id, repo_owner="o", repo_name=repo,
                                       repo_path=f"/private/{repo}", enabled=True)
        db.add(scopes[repo])
    await db.flush()
    for repo, number, admitted in items:
        db.add(GithubWorkItem(scope_id=scopes[repo].id, issue_number=number, issue_title=f"Issue {number}",
                              issue_url=f"https://x/{repo}/{number}", github_updated_at=admitted,
                              dispatch_status="pending", created_at=admitted))
    await db.commit()
    return team.id


@pytest_asyncio.fixture
async def teams(maker):
    """Seeded before the upgrade: a milestone team, a parent-issue team and an unconfigured one."""
    async with maker() as db:
        m = await _team(db, "M", ("m1", "m2"), milestone="v4.4", items=(
            ("m1", 2, T0 + timedelta(hours=1)),  # admitted first, but not in the milestone
            ("m1", 1, T0 + timedelta(hours=2)),
            ("m2", 3, T0 + timedelta(hours=3))))
        p = await _team(db, "P", ("p1",), parent="https://github.com/o/p1/issues/50",
                        items=(("p1", 5, T0 + timedelta(hours=4)),))
        u = await _team(db, "U", ("u1",), items=(("u1", 8, T0),))
    return {"M": m, "P": p, "U": u}


async def _resolve(maker, preset_id, client=None):
    async with maker() as db:
        return await upgrade.resolve_pending(db, preset_id, client or _client())


async def _open(maker, preset_id):
    async with maker() as db:
        return await campaigns.current(db, preset_id)


async def _rows(maker):
    async with maker() as db:
        return {row.preset_id: (row.status, row.reason)
                for row in (await db.scalars(select(CampaignUpgradeBackfill))).all()}


async def _started_events(maker):
    async with maker() as db:
        return (await db.scalars(select(FactoryAuditEvent).where(
            FactoryAuditEvent.event_kind == "campaign_started"))).all()


# The acceptance: an upgrade test with a pre-4.4 team
async def test_an_upgraded_pre_44_database_gives_each_configured_team_one_campaign(maker, teams):
    assert await _upgrade(maker) == 2
    for preset_id in teams.values():
        await _resolve(maker, preset_id)
    m, p = await _open(maker, teams["M"]), await _open(maker, teams["P"])
    assert (m.selector, m.status, m.started_at) == ("milestone:v4.4", "active", T0 + timedelta(hours=2))
    assert (p.selector, p.status, p.started_at) == ("milestone:Sprint 9", "active", T0 + timedelta(hours=4))
    assert await _open(maker, teams["U"]) is None
    assert await _rows(maker) == {teams["M"]: ("created", None), teams["P"]: ("created", None)}
    assert len(await _started_events(maker)) == 2


async def test_a_team_configured_after_the_upgrade_gets_none(maker, teams):
    await _upgrade(maker)
    async with maker() as db:
        late = await _team(db, "Late", ("l1",), milestone="v4.4")
        team = await db.get(AgentTeamPreset, teams["U"])
        team.backlog_milestone = "v4.4"
        await db.commit()
    for preset_id in (late, teams["U"]):
        await _resolve(maker, preset_id)
        assert await _open(maker, preset_id) is None


async def test_a_second_start_enqueues_nothing(maker, teams):
    await _upgrade(maker)
    async with maker() as db:
        first = await db.scalar(select(CampaignUpgradeCoverage.started_at))
        await _team(db, "Later", ("l1",), milestone="v4.4")
    assert await _upgrade(maker) == 0
    async with maker() as db:
        assert await db.scalar(select(CampaignUpgradeCoverage.started_at)) == first
    assert set(await _rows(maker)) == {teams["M"], teams["P"]}


async def test_a_team_with_empty_settings_is_not_enqueued(maker, teams):
    """An empty milestone or parent is unset, as CampaignProgress reads it."""
    async with maker() as db:
        team = await db.get(AgentTeamPreset, teams["U"])
        team.backlog_milestone, team.parent_issue_url = "", ""
        await db.commit()
    await _upgrade(maker)
    assert set(await _rows(maker)) == {teams["M"], teams["P"]}


async def test_a_team_with_an_open_campaign_at_the_upgrade_is_not_enqueued(maker, teams):
    async with maker() as db:
        await db.run_sync(lambda session: Base.metadata.create_all(session.connection()))
        await campaigns.start(db, teams["M"], "label:mine", started_at=T0, actor=OPERATOR)
    await _upgrade(maker)
    assert set(await _rows(maker)) == {teams["P"]}


@pytest.mark.parametrize("client", [
    _client(broken={"p1"}),  # the parent issue can't be read
    _client(milestone_issues={("p1", 7): [_issue(5)]}, broken={"m2"}),  # one repository's membership is unknown
], ids=["parent_unreadable", "membership_incomplete"])
async def test_an_unreadable_parent_or_incomplete_membership_stays_pending(maker, teams, client):
    await _upgrade(maker)
    preset_id = teams["P"] if "p1" in client.broken else teams["M"]
    await _resolve(maker, preset_id, client)
    assert await _open(maker, preset_id) is None
    assert (await _rows(maker))[preset_id] == ("pending", None)
    await _resolve(maker, preset_id)  # a later readable poll resolves it
    assert (await _rows(maker))[preset_id] == ("created", None)


@pytest.mark.parametrize("issues,reason", [
    ({("p1", 50): {"number": 50, "milestone": None}}, "parent_without_milestone"),
    ({}, "parent_not_found"),
], ids=["no_milestone", "missing"])
async def test_a_parent_without_a_milestone_gets_none(maker, teams, issues, reason):
    await _upgrade(maker)
    await _resolve(maker, teams["P"], _client(issues=issues))
    assert await _open(maker, teams["P"]) is None
    assert (await _rows(maker))[teams["P"]] == ("none", reason)


async def test_a_parent_outside_the_teams_repositories_gets_none(maker, teams):
    async with maker() as db:
        team = await db.get(AgentTeamPreset, teams["P"])
        team.parent_issue_url = "https://github.com/o/elsewhere/issues/50"
        await db.commit()
    await _upgrade(maker)
    await _resolve(maker, teams["P"])
    assert (await _rows(maker))[teams["P"]] == ("none", "parent_outside_team")


async def test_no_admitted_member_starts_at_resolution(maker, teams):
    await _upgrade(maker)
    before = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    await _resolve(maker, teams["M"], _client(milestone_issues={("m1", 1): [_issue(40)], ("m2", 4): []}))
    started = (await _open(maker, teams["M"])).started_at
    assert before <= started <= datetime.now(UTC).replace(tzinfo=None)


# Atomic creation: one campaign per team lifetime, and the Director's own campaign wins
async def test_a_director_campaign_started_during_resolution_wins(maker, teams, monkeypatch):
    await _upgrade(maker)
    real = campaigns.members

    async def members_then_director(db, campaign, client):
        result = await real(db, campaign, client)
        async with maker() as other:
            await campaigns.start(other, teams["M"], "label:director", started_at=T0, actor=OPERATOR)
        return result

    monkeypatch.setattr(campaigns, "members", members_then_director)
    await _resolve(maker, teams["M"])
    assert (await _open(maker, teams["M"])).selector == "label:director"
    assert (await _rows(maker))[teams["M"]] == ("none", "director_campaign")
    assert len(await _started_events(maker)) == 1


async def test_a_director_campaign_the_check_misses_is_refused_by_the_database(maker, teams, monkeypatch):
    """The unique index is the last guard: an insert beside an open campaign fails, and the backfill yields."""
    await _upgrade(maker)
    async with maker() as db:
        await campaigns.start(db, teams["M"], "label:director", started_at=T0, actor=OPERATOR)

    real, checks = upgrade._open_campaign, []

    async def misses_once(db, preset_id):
        checks.append(preset_id)
        return None if len(checks) == 1 else await real(db, preset_id)  # the confirmation after the refusal reads

    monkeypatch.setattr(upgrade, "_open_campaign", misses_once)
    await _resolve(maker, teams["M"])
    assert (await _open(maker, teams["M"])).selector == "label:director"
    assert (await _rows(maker))[teams["M"]] == ("none", "director_campaign")
    assert len(await _started_events(maker)) == 1


async def test_two_resolvers_create_one_campaign(maker, teams):
    await _upgrade(maker)
    await asyncio.gather(_resolve(maker, teams["M"]), _resolve(maker, teams["M"]))
    async with maker() as db:
        assert len((await db.scalars(select(Campaign).where(Campaign.preset_id == teams["M"]))).all()) == 1
    assert (await _rows(maker))[teams["M"]] == ("created", None)
    assert len(await _started_events(maker)) == 1


async def _delete_team(maker, preset_id):
    """Delete a team as the application does; the lifetime trigger retires its key."""
    async with maker() as db:
        scopes = "SELECT id FROM team_github_scopes WHERE preset_id = :team"
        await db.execute(text(f"DELETE FROM github_work_items WHERE scope_id IN ({scopes})"), {"team": preset_id})
        await db.execute(text("DELETE FROM team_github_scopes WHERE preset_id = :team"), {"team": preset_id})
        await db.execute(text("DELETE FROM agent_team_presets WHERE id = :team"), {"team": preset_id})
        await db.commit()


async def test_a_team_deleted_before_resolution_gets_no_campaign(maker, teams):
    await _upgrade(maker)
    await _delete_team(maker, teams["M"])
    await _resolve(maker, teams["M"])
    assert await _open(maker, teams["M"]) is None
    assert (await _rows(maker))[teams["M"]] == ("none", "team_retired")


async def test_a_team_deleted_during_resolution_gets_no_campaign(maker, teams, monkeypatch):
    """Deleted after the first lifetime check, before the claim: the check under the claim's lock catches it."""
    await _upgrade(maker)
    real = campaigns.members

    async def members_then_delete(db, campaign, client):
        result = await real(db, campaign, client)
        await _delete_team(maker, teams["M"])
        return result

    monkeypatch.setattr(campaigns, "members", members_then_delete)
    await _resolve(maker, teams["M"])
    async with maker() as db:
        assert (await db.scalars(select(Campaign).where(Campaign.preset_id == teams["M"]))).all() == []
    assert (await _rows(maker))[teams["M"]] == ("none", "team_retired")


async def test_a_reused_team_id_does_not_inherit_the_backfill(maker, teams):
    await _upgrade(maker)
    await _delete_team(maker, teams["M"])
    async with maker() as db:  # a new team takes the old id, with the same milestone and repositories
        db.add(AgentTeamPreset(id=teams["M"], name="Reused", backlog_milestone="v4.4"))
        await db.flush()
        scope = TeamGithubScope(preset_id=teams["M"], repo_owner="o", repo_name="m1", repo_path="/private/m1")
        db.add(scope)
        await db.flush()
        db.add(GithubWorkItem(scope_id=scope.id, issue_number=1, issue_title="Issue 1", issue_url="https://x/m1/1",
                              github_updated_at=T0, dispatch_status="pending", created_at=T0))
        await db.commit()
    await _resolve(maker, teams["M"])
    assert await _open(maker, teams["M"]) is None
    assert (await _rows(maker))[teams["M"]] == ("none", "team_retired")


async def test_the_start_counts_only_the_lifetimes_own_admissions(maker, teams):
    """Another team watching the same repository admitted the same issue earlier: that doesn't move the start."""
    async with maker() as db:
        await _team(db, "Other", ("m1",), items=(("m1", 1, T0 - timedelta(days=3)),))
    await _upgrade(maker)
    await _resolve(maker, teams["M"])
    assert (await _open(maker, teams["M"])).started_at == T0 + timedelta(hours=2)


# Recorded-only measures: a campaign from before the recording is unknown, never zero
async def _install_recording(maker, at):
    async with maker() as db:
        for model in (GithubCheckRunHistoryCoverage, GithubMergeAttemptCoverage, GithubRateLimitHistoryCoverage):
            await db.merge(model(id=1, started_at=at))
        coverage = await db.get(CampaignUpgradeCoverage, 1)
        coverage.started_at = at
        await db.commit()


async def test_a_campaign_from_before_the_recording_is_unknown_for_every_recorded_only_measure(maker, teams):
    await _upgrade(maker)
    await _install_recording(maker, T0 + timedelta(days=5))
    await _resolve(maker, teams["M"])
    async with maker() as db:
        campaign = await campaigns.current(db, teams["M"])
        coverage = await upgrade.recorded_coverage(db, campaign)
        assert {c.measure for c in coverage} == set(upgrade.RECORDED_MEASURES)
        assert all(not c.covered and c.reason == "before_recording" for c in coverage)
        # The existing campaign metric reads the same boundary: unknown, not zero.
        rate_limited = await campaign_rate_limit_time(db, campaign.started_at, T0 + timedelta(days=9))
        assert rate_limited.seconds is None


async def test_a_campaign_started_after_coverage_is_covered(maker, teams):
    await _upgrade(maker)
    await _install_recording(maker, T0 - timedelta(days=1))
    await _resolve(maker, teams["M"])
    async with maker() as db:
        campaign = await campaigns.current(db, teams["M"])
        assert all(c.covered and c.reason is None for c in await upgrade.recorded_coverage(db, campaign))


async def test_a_measure_without_its_coverage_marker_is_unknown(maker, teams):
    await _upgrade(maker)
    await _install_recording(maker, T0 - timedelta(days=1))
    async with maker() as db:
        await db.execute(text("DELETE FROM github_check_run_history_coverage"))
        await db.commit()
    await _resolve(maker, teams["M"])
    async with maker() as db:
        campaign = await campaigns.current(db, teams["M"])
        by_measure = {c.measure: c for c in await upgrade.recorded_coverage(db, campaign)}
        assert (by_measure["check_runs"].covered, by_measure["check_runs"].reason) == (False, "recording_not_installed")
        assert by_measure["merge_path"].covered


async def test_the_poll_resolves_the_backfill_through_the_campaign_refresh(maker, teams):
    from app.services.agent_team_campaign_service import AgentTeamCampaignService

    await _upgrade(maker)
    async with maker() as db:
        scope = await db.scalar(select(TeamGithubScope).where(TeamGithubScope.preset_id == teams["M"]))
        await AgentTeamCampaignService().refresh_scope(db, scope, _client())
    assert (await _open(maker, teams["M"])).selector == "milestone:v4.4"


# V1 review 6078808159
async def test_a_stale_resolver_does_not_overwrite_a_created_backfill(maker, teams, monkeypatch):
    """Resolver A reads a missing parent; resolver B creates the campaign before A records its outcome."""
    await _upgrade(maker)
    real, calls = upgrade._selector, []

    async def missing_after_the_other_resolved(db, team, client):
        if calls:
            return await real(db, team, client)
        calls.append(team.id)
        await _resolve(maker, teams["P"])  # B, with a readable parent
        return "none:parent_not_found"

    monkeypatch.setattr(upgrade, "_selector", missing_after_the_other_resolved)
    await _resolve(maker, teams["P"], _client(issues={}))
    assert (await _rows(maker))[teams["P"]] == ("created", None)
    assert (await _open(maker, teams["P"])).selector == "milestone:Sprint 9"


async def test_an_unrelated_integrity_error_leaves_the_backfill_pending(maker, teams, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    await _upgrade(maker)
    real = audit.record_event

    async def failing_start_event(db, **kwargs):
        if kwargs.get("event_kind") == "campaign_started":
            raise IntegrityError("INSERT INTO factory_audit_events", {}, Exception("injected"))
        return await real(db, **kwargs)

    monkeypatch.setattr(audit, "record_event", failing_start_event)
    await _resolve(maker, teams["M"])
    assert await _open(maker, teams["M"]) is None
    assert (await _rows(maker))[teams["M"]] == ("pending", None)
    monkeypatch.setattr(audit, "record_event", real)
    await _resolve(maker, teams["M"])  # retryable: the next poll creates it
    assert (await _rows(maker))[teams["M"]] == ("created", None)


@pytest.mark.parametrize("client", [_client(), _client(broken={"m1", "m2", "p1"})], ids=["created", "pending"])
async def test_resolution_never_expires_the_callers_scopes(maker, teams, client):
    """The test-instance poller refreshes every scope it loaded, in one session."""
    from app.services.agent_team_campaign_service import AgentTeamCampaignService

    await _upgrade(maker)
    service = AgentTeamCampaignService()
    async with maker() as db:
        scopes = (await db.scalars(select(TeamGithubScope).order_by(TeamGithubScope.id))).all()
        seen = []
        for scope in scopes:
            await service.refresh_scope(db, scope, client)
            seen.append(scope.id)  # a rollback in the caller's session would expire it (MissingGreenlet)
    assert len(seen) == len(scopes) == 4


async def test_a_backfill_resolved_meanwhile_is_not_claimed_again(maker, teams, monkeypatch):
    """Another resolver recorded "none" while this one read the membership: the claim fails, nothing is created."""
    await _upgrade(maker)
    real = campaigns.members

    async def members_then_other_outcome(db, campaign, client):
        result = await real(db, campaign, client)
        async with maker() as other:
            await other.execute(text("UPDATE campaign_upgrade_backfill SET status = 'none', reason = 'other'"
                                     " WHERE preset_id = :team"), {"team": teams["M"]})
            await other.commit()
        return result

    monkeypatch.setattr(campaigns, "members", members_then_other_outcome)
    await _resolve(maker, teams["M"])
    assert await _open(maker, teams["M"]) is None
    assert (await _rows(maker))[teams["M"]] == ("none", "other")
