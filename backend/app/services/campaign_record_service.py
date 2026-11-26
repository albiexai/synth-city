"""#1242: the campaign record across a team's repositories (design §2).

A campaign belongs to one team lifetime and selects issues with ``label:<name>`` or ``milestone:<title>`` in every
repository the team watches: a label by name, a milestone by title, since GitHub milestones are per repository.
Membership is read live from GitHub; #1219's deferrals and supersessions since the start take an issue out of it, with
their reason. A repository that can't be read leaves the membership incomplete, never empty. The Director can adjust
the start (#1243). Finishing (#1261) and creation on upgrade (#1243, ``campaign_upgrade_service``) live elsewhere.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from sqlalchemy import and_, select, update

from app.models.database import (
    AgentTeamPreset,
    Campaign,
    FactoryAuditEvent,
    FactoryContextKey,
    TeamGithubScope,
)
from app.services import factory_audit_service as audit
from app.services.github_client import GithubClientResponseError

OPEN_STATUSES = ("active", "finishing")
_PREFIXES = ("label:", "milestone:")


class CampaignError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CampaignMember:
    repo: str
    issue_number: int
    title: str
    state: str


@dataclass(frozen=True)
class CampaignDeparture:
    repo: str
    issue_number: int
    outcome: str  # "deferred" or "superseded"
    destination: str  # the target milestone, or the superseding PR
    reason: str | None
    at: datetime


@dataclass(frozen=True)
class RepositoryCoverage:
    repo: str
    complete: bool
    reason: str | None = None  # why the repository's members are unknown


@dataclass
class CampaignMembership:
    members: list[CampaignMember] = field(default_factory=list)
    departures: list[CampaignDeparture] = field(default_factory=list)
    repositories: list[RepositoryCoverage] = field(default_factory=list)
    # Why no repository could be read at all, e.g. "team_retired": the campaign's team lifetime has ended.
    unavailable: str | None = None

    @property
    def complete(self) -> bool:
        return self.unavailable is None and all(repository.complete for repository in self.repositories)


def parse_selector(selector: str) -> tuple[str, str]:
    """The selector's kind and value; refuses anything but a non-empty ``label:`` or ``milestone:``."""
    for prefix in _PREFIXES:
        if isinstance(selector, str) and selector.startswith(prefix) and selector[len(prefix):].strip():
            return prefix[:-1], selector[len(prefix):]
    raise CampaignError("campaign_selector_invalid")


async def current(db, preset_id: int) -> Campaign | None:
    """The team's active or finishing campaign in its current lifetime, or none."""
    key = await audit.current_context_key(db, "team", preset_id)
    if key is None:
        return None
    return await db.scalar(select(Campaign).where(
        Campaign.team_context_key == key, Campaign.status.in_(OPEN_STATUSES)))


async def start(db, preset_id: int, selector: str, *, started_at: datetime, actor: dict) -> Campaign:
    """Start the team's campaign. A second active or finishing one is refused (the database refuses it too)."""
    parse_selector(selector)
    # Before any key, campaign or event (V1 review 6072435444): a later team with this id must not inherit it.
    if await db.get(AgentTeamPreset, preset_id) is None:
        raise CampaignError("campaign_team_not_found")
    if await current(db, preset_id) is not None:
        raise CampaignError("campaign_already_active")
    key = await audit.current_context_key(db, "team", preset_id) or await audit.context_key_for(db, "team", preset_id)
    campaign = await insert(db, preset_id, key, selector, started_at=started_at, actor=actor,
                            source="campaign_record_service.start")
    await db.commit()
    await db.refresh(campaign)
    return campaign


async def insert(db, preset_id: int, key: str, selector: str, *, started_at: datetime, actor: dict,
                 source: str) -> Campaign:
    """Add (never commit) an active campaign and its start event; the open-campaign index refuses a second one."""
    campaign = Campaign(team_context_key=key, preset_id=preset_id, selector=selector, status="active",
                        started_at=audit._naive(started_at))
    db.add(campaign)
    await db.flush()
    await audit.record_event(
        db, event_kind="campaign_started", source=source, occurred_at=datetime.now(UTC),
        actor=actor, team_preset_id=preset_id, team_context_key=key, after_values={"status": "active"},
        action_outcome="applied", sanitized_reason=f"campaign started: {selector}",
        operation_id=f"campaign_started:{campaign.id}", correlation_id=f"campaign:{campaign.id}")
    return campaign


async def adjust_start(db, preset_id: int, *, started_at: datetime, expected_started_at: datetime,
                       actor: dict) -> Campaign:
    """The Director's change of the start of the team's active campaign (design §2), with one audit event.

    A compare-and-set on the start the Director saw, the current team lifetime and the ``active`` status: a stale
    view, a concurrent change or a campaign that began finishing changes nothing.
    """
    new, expected = audit._naive(started_at), audit._naive(expected_started_at)
    if new > datetime.now(UTC).replace(tzinfo=None):
        raise CampaignError("campaign_start_in_future")
    campaign = await current(db, preset_id)
    if campaign is None:
        raise CampaignError("campaign_not_found")
    if campaign.status != "active":
        raise CampaignError("campaign_not_active")
    result = await db.execute(update(Campaign).where(
        Campaign.id == campaign.id, Campaign.team_context_key == campaign.team_context_key,
        Campaign.status == "active", Campaign.started_at == expected,
    ).values(started_at=new).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        await db.rollback()
        raise CampaignError("campaign_start_changed")
    await audit.record_event(
        db, event_kind="campaign_start_adjusted", source="campaign_record_service.adjust_start",
        occurred_at=datetime.now(UTC), actor=actor, team_preset_id=preset_id,
        team_context_key=campaign.team_context_key, action_outcome="applied",
        sanitized_reason=f"campaign start adjusted from {expected.isoformat()} to {new.isoformat()}",
        operation_id=f"campaign_start_adjusted:{campaign.id}:{expected.isoformat()}:{new.isoformat()}",
        correlation_id=f"campaign:{campaign.id}")
    await db.commit()
    await db.refresh(campaign)
    return campaign


async def members(db, campaign: Campaign, client) -> CampaignMembership:
    """The campaign's members in every repository its team watches, and the issues that left it."""
    kind, value = parse_selector(campaign.selector)
    membership = CampaignMembership(departures=await _departures(db, campaign))
    # Only the campaign's own team lifetime: a preset id reused by a new team never lends it its repositories. The
    # lifetime and its scopes come from one statement, one snapshot (V1 review 6073257579): a replacement landing
    # between two reads can't hand over its repositories.
    scopes = (await db.scalars(select(TeamGithubScope).join(FactoryContextKey, and_(
        FactoryContextKey.key_kind == "team", FactoryContextKey.numeric_id == TeamGithubScope.preset_id,
        FactoryContextKey.context_key == campaign.team_context_key, FactoryContextKey.retired_at.is_(None),
    )).where(TeamGithubScope.preset_id == campaign.preset_id)
        .order_by(TeamGithubScope.repo_owner, TeamGithubScope.repo_name))).all()
    if not scopes and await audit.current_context_key(db, "team", campaign.preset_id) != campaign.team_context_key:
        membership.unavailable = "team_retired"
        return membership
    departed = {(d.repo, d.issue_number) for d in membership.departures}
    for scope in scopes:
        repo = f"{scope.repo_owner}/{scope.repo_name}"
        try:
            if kind == "label":
                issues = await client.list_label_issues(scope.repo_owner, scope.repo_name, value)
            else:
                matches = [m for m in await client.list_milestones(scope.repo_owner, scope.repo_name)
                           if m.get("title") == value]
                if len(matches) != 1:
                    reason = "milestone_not_found" if not matches else "milestone_ambiguous"
                    membership.repositories.append(RepositoryCoverage(repo, False, reason))
                    continue
                issues = await client.list_milestone_issues(scope.repo_owner, scope.repo_name, matches[0]["number"])
        except (GithubClientResponseError, httpx.HTTPError):
            membership.repositories.append(RepositoryCoverage(repo, False, "github_unreadable"))
            continue
        membership.repositories.append(RepositoryCoverage(repo, True))
        membership.members.extend(
            CampaignMember(repo, issue["number"], issue["title"], issue["state"])
            for issue in sorted(issues, key=lambda issue: issue["number"])
            if (repo, issue["number"]) not in departed)
    return membership


async def _departures(db, campaign: Campaign) -> list[CampaignDeparture]:
    """#1219's deferrals and supersessions of the team's issues while the campaign ran.

    Read from each event's own record (V1 review 6072435444): the team lifetime key and the repository and issue it
    named. Deleting the local work item, or reusing its id, changes nothing.
    """
    query = select(FactoryAuditEvent).where(
        FactoryAuditEvent.event_kind == "stopped_work_item_ended",
        FactoryAuditEvent.team_context_key == campaign.team_context_key,
        FactoryAuditEvent.occurred_at >= campaign.started_at)
    if campaign.finished_at is not None:
        query = query.where(FactoryAuditEvent.occurred_at <= campaign.finished_at)
    departures = []
    for event in (await db.scalars(query.order_by(FactoryAuditEvent.occurred_at, FactoryAuditEvent.id))).all():
        values, snapshot = event.after_values or {}, event.context_snapshot or {}
        owner, name, number = snapshot.get("repo_owner"), snapshot.get("repo_name"), snapshot.get("issue_number")
        if not (isinstance(owner, str) and isinstance(name, str) and isinstance(number, int)):
            continue
        outcome = values.get("outcome")
        if outcome == "deferred":
            destination = str(values.get("target_milestone") or "no milestone")
        elif outcome == "superseded":
            destination = f"PR #{values.get('superseding_pr_number')}"
        else:
            continue
        departures.append(CampaignDeparture(f"{owner}/{name}", number, outcome, destination, event.sanitized_reason,
                                            event.occurred_at))
    return departures
