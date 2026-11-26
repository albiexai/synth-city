"""#1243: campaigns created on upgrade for existing teams, and the coverage of recorded-only measures (design §2).

The first start of this version records each team that has a backlog milestone or a parent issue, as
``CampaignProgress`` reads them. Later starts record none: a team configured afterwards, or one with neither setting,
gets no campaign until the Director starts one. The poll then resolves each recorded team once, with GitHub: its
selector, and a start at the first admission of a matching item. The Director can adjust that start.

A measure recorded only from this version on is unknown for a campaign that began before its recording, never zero.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from app.models.database import (
    AgentTeamPreset,
    Campaign,
    CampaignUpgradeBackfill,
    CampaignUpgradeCoverage,
    FactoryContextKey,
    GithubCheckRunHistoryCoverage,
    GithubMergeAttemptCoverage,
    GithubRateLimitHistoryCoverage,
    GithubWorkItem,
    TeamGithubScope,
)
from app.services import campaign_record_service as campaigns
from app.services import factory_audit_service as audit
from app.services.github_client import GithubClientResponseError

logger = logging.getLogger(__name__)

PENDING, CREATED, NONE = "pending", "created", "none"
SOURCE = "campaign_upgrade_service.resolve_pending"
RECORDED_MEASURES = ("check_runs", "merge_path", "rate_limit_windows", "pull_request_facts", "decision_waits")
_READ_ERRORS = (GithubClientResponseError, httpx.HTTPError)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def install(db) -> int:
    """Once, at the first start of this version: record the teams to give a campaign. Returns how many."""
    if await db.get(CampaignUpgradeCoverage, 1) is not None:
        return 0
    db.add(CampaignUpgradeCoverage(id=1, started_at=_now()))
    try:
        await db.flush()
    except IntegrityError:  # another process installed it first
        await db.rollback()
        return 0
    teams = (await db.scalars(select(AgentTeamPreset.id).where(or_(
        AgentTeamPreset.backlog_milestone.is_not(None), AgentTeamPreset.parent_issue_url.is_not(None),
    )).order_by(AgentTeamPreset.id))).all()
    count = 0
    for preset_id in teams:
        team = await db.get(AgentTeamPreset, preset_id)
        if not (team.backlog_milestone or team.parent_issue_url):
            continue
        key = await audit.current_context_key(db, "team", preset_id) or await audit.context_key_for(
            db, "team", preset_id)
        if await db.scalar(select(Campaign.id).where(Campaign.team_context_key == key,
                                                     Campaign.status.in_(campaigns.OPEN_STATUSES))) is not None:
            continue
        db.add(CampaignUpgradeBackfill(preset_id=preset_id, team_context_key=key, status=PENDING))
        count += 1
    await db.commit()
    return count


async def _open_campaign(db, preset_id: int) -> Campaign | None:
    return await campaigns.current(db, preset_id)


async def _finish(db, row_id: int, status: str, reason: str | None) -> str | None:
    """A terminal outcome, only while the row is pending (V1 review 6078808159): a later resolver's stale view never
    overwrites the outcome another resolver recorded. ``None`` when the row was no longer pending."""
    result = await db.execute(update(CampaignUpgradeBackfill).where(
        CampaignUpgradeBackfill.id == row_id, CampaignUpgradeBackfill.status == PENDING,
    ).values(status=status, reason=reason, resolved_at=_now()).execution_options(synchronize_session=False))
    await db.commit()
    return status if result.rowcount == 1 else None


async def resolve_pending(db, preset_id: int, client: Any) -> str | None:
    """Give the team its upgrade campaign, once, or decide it gets none. ``None`` when nothing is pending.

    An unreadable GitHub answer or an incomplete membership leaves the row pending for a later poll. The creation is
    one transaction that first claims the row: under SQLite's write lock the team's lifetime and its open campaign are
    read again, so a reused team id, a Director's campaign or a second resolver makes it yield.
    """
    row = await db.scalar(select(CampaignUpgradeBackfill).where(
        CampaignUpgradeBackfill.preset_id == preset_id, CampaignUpgradeBackfill.status == PENDING))
    if row is None:
        return None
    row_id, key = row.id, row.team_context_key
    if await audit.current_context_key(db, "team", preset_id) != key:
        return await _finish(db, row_id, NONE, "team_retired")
    team = await db.get(AgentTeamPreset, preset_id)
    try:
        selector = await _selector(db, team, client)
    except _READ_ERRORS:
        await db.rollback()
        return PENDING
    if selector.startswith("none:"):
        return await _finish(db, row_id, NONE, selector.removeprefix("none:"))
    probe = Campaign(team_context_key=key, preset_id=preset_id, selector=selector, status="active",
                     started_at=datetime(1970, 1, 1))
    membership = await campaigns.members(db, probe, client)
    if not membership.complete:
        await db.rollback()
        return PENDING
    started_at = await _first_admission(db, key, {(m.repo, m.issue_number) for m in membership.members})
    await db.rollback()  # no read transaction is held into the claim
    claim = await db.execute(update(CampaignUpgradeBackfill).where(
        CampaignUpgradeBackfill.id == row_id, CampaignUpgradeBackfill.status == PENDING,
    ).values(status=CREATED, reason=None, resolved_at=_now()).execution_options(synchronize_session=False))
    if claim.rowcount != 1:
        await db.rollback()
        return None  # another resolver took it
    if await audit.current_context_key(db, "team", preset_id) != key:
        await db.rollback()
        return await _finish(db, row_id, NONE, "team_retired")
    if await _open_campaign(db, preset_id) is not None:
        await db.rollback()
        return await _finish(db, row_id, NONE, "director_campaign")
    try:
        await campaigns.insert(db, preset_id, key, selector, started_at=started_at or _now(),
                               actor=audit.derive_actor(actor_kind="system"), source=SOURCE)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        # Only a confirmed open campaign is the Director's (V1 review 6078808159); any other refusal stays pending.
        if await _open_campaign(db, preset_id) is not None:
            return await _finish(db, row_id, NONE, "director_campaign")
        await db.rollback()
        logger.info("Upgrade campaign for team %s stays pending after a storage refusal", preset_id)
        return PENDING
    return CREATED


async def _selector(db, team: AgentTeamPreset, client: Any) -> str:
    """The selector as ``CampaignProgress`` reads the team's settings, or ``none:<reason>``."""
    if team.backlog_milestone:
        return f"milestone:{team.backlog_milestone}"
    from app.services.agent_team_campaign_service import _parent

    parent = _parent(team.parent_issue_url)
    if parent is None:
        return "none:parent_invalid"
    owner, repo, number = parent
    watched = await db.scalar(select(TeamGithubScope.id).where(
        TeamGithubScope.preset_id == team.id, TeamGithubScope.repo_owner == owner,
        TeamGithubScope.repo_name == repo).limit(1))
    if watched is None:
        return "none:parent_outside_team"
    issue = await client.get_issue(owner, repo, number)
    if not isinstance(issue, dict):
        return "none:parent_not_found"
    milestone = issue.get("milestone")
    title = milestone.get("title") if isinstance(milestone, dict) else None
    if not isinstance(title, str) or not title.strip():
        return "none:parent_without_milestone"
    return f"milestone:{title}"


async def _first_admission(db, key: str, members: set[tuple[str, int]]) -> datetime | None:
    """The earliest admission of a member issue in the team lifetime's own repositories."""
    if not members:
        return None
    rows = (await db.execute(select(TeamGithubScope.repo_owner, TeamGithubScope.repo_name,
                                    GithubWorkItem.issue_number, GithubWorkItem.created_at)
        .join(GithubWorkItem, GithubWorkItem.scope_id == TeamGithubScope.id)
        .join(FactoryContextKey, (FactoryContextKey.key_kind == "team")
              & (FactoryContextKey.numeric_id == TeamGithubScope.preset_id)
              & (FactoryContextKey.context_key == key) & FactoryContextKey.retired_at.is_(None)))).all()
    admitted = [row.created_at for row in rows if (f"{row.repo_owner}/{row.repo_name}", row.issue_number) in members]
    return min(admitted) if admitted else None


@dataclass(frozen=True)
class MeasureCoverage:
    measure: str
    started_at: datetime | None  # when its recording began
    covered: bool
    reason: str | None  # "before_recording" or "recording_not_installed" when not covered


async def recorded_coverage(db, campaign: Campaign) -> tuple[MeasureCoverage, ...]:
    """Whether each recorded-only measure covers the campaign; #1244's snapshot reads these, as every measure must.

    Check runs, the merge path and rate-limit windows have their own recording markers. PR facts and decision waits
    have none and began with this version, so its first start stands for them: conservative, never known too early.
    """
    upgraded = await db.scalar(select(CampaignUpgradeCoverage.started_at).where(CampaignUpgradeCoverage.id == 1))
    starts = {
        "check_runs": await db.scalar(select(GithubCheckRunHistoryCoverage.started_at).where(
            GithubCheckRunHistoryCoverage.id == 1)),
        "merge_path": await db.scalar(select(GithubMergeAttemptCoverage.started_at).where(
            GithubMergeAttemptCoverage.id == 1)),
        "rate_limit_windows": await db.scalar(select(GithubRateLimitHistoryCoverage.started_at).where(
            GithubRateLimitHistoryCoverage.id == 1)),
        "pull_request_facts": upgraded,
        "decision_waits": upgraded,
    }
    began = audit._naive(campaign.started_at)
    result = []
    for measure in RECORDED_MEASURES:
        started = starts[measure]
        if started is None:
            result.append(MeasureCoverage(measure, None, False, "recording_not_installed"))
        elif audit._naive(started) > began:
            result.append(MeasureCoverage(measure, started, False, "before_recording"))
        else:
            result.append(MeasureCoverage(measure, started, True, None))
    return tuple(result)
