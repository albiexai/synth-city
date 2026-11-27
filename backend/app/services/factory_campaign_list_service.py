"""#1252: the campaigns list behind Insights > Retrospectives (design section 8).

Read only. It reads the database and the snapshot service's in-memory cache. It never calls GitHub and never builds,
refreshes or freezes a snapshot. A number the factory has not recorded is None with a reason, never zero.

Repository counts come from the latest frozen snapshot, else from the last live snapshot built in this process, else
there are none. The row says which and when (``source``), so the page can show its age.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select

from app.models.database import (
    AgentTeamPreset,
    Campaign,
    CampaignSnapshot,
    CampaignUpgradeCoverage,
    FactoryContextKey,
    TeamGithubScope,
)
from app.services import campaign_snapshot_service as snapshots
from app.services.factory_metrics_service import _naive_utc
from app.services.factory_projection_service import FactoryReadError

STATUSES = ("live", "finished", "all")
LIVE = ("active", "finishing")


class CampaignTeam(BaseModel):
    id: int
    name: str


class CampaignRepositoryCount(BaseModel):
    repo: str
    issues: int | None
    reason: str | None = None


class CampaignCountSource(BaseModel):
    kind: Literal["frozen", "live"]
    as_of: datetime


class CampaignMeasure(BaseModel):
    value: int | None
    reason: str | None = None


class CampaignRow(BaseModel):
    id: int
    team: CampaignTeam | None  # None: the team was retired
    selector: str
    status: Literal["active", "finishing", "finished"]
    started_at: datetime
    finished_at: datetime | None
    merged: CampaignMeasure
    in_scope: CampaignMeasure
    repositories: list[CampaignRepositoryCount]
    # Why the repositories are not known at all: ``no_snapshot`` (neutral), or ``before_recording`` only when the
    # campaign began before this version started recording.
    repositories_unknown_reason: str | None
    source: CampaignCountSource | None


class CampaignStatusCounts(BaseModel):
    live: int
    finished: int


class CampaignListResponse(BaseModel):
    schema_version: Literal[1] = 1
    generated_at: datetime
    campaigns: list[CampaignRow]
    total: int
    counts: CampaignStatusCounts
    # A repository filter keeps only campaigns whose repositories are known to include it.
    hidden_unknown_repositories: int
    # Finished campaigns that ended before recording began, by the recording marker. Never inferred from a missing snapshot.
    finished_before_recording: int
    has_more: bool
    next_cursor: str | None


def _measure(metrics: list[dict], name: str, missing: str) -> CampaignMeasure:
    for metric in metrics:
        if metric.get("name") == name:
            value = metric.get("value")
            if value is None:
                reasons = metric.get("unknown_reasons") or []
                return CampaignMeasure(value=None, reason=reasons[0] if reasons else "unknown")
            return CampaignMeasure(value=int(value))
    return CampaignMeasure(value=None, reason=missing)


async def _latest_frozen(db, ids: list[int]) -> dict[int, CampaignSnapshot]:
    found: dict[int, CampaignSnapshot] = {}
    if not ids:
        return found
    rows = await db.scalars(select(CampaignSnapshot).where(CampaignSnapshot.campaign_id.in_(ids))
                            .order_by(CampaignSnapshot.version.desc()))
    for row in rows:
        found.setdefault(row.campaign_id, row)
    return found


async def campaign_list(db, *, team_id: int | None, scope_id: int | None, status: str, limit: int,
                        cursor: str | None) -> CampaignListResponse:
    if status not in STATUSES:
        raise FactoryReadError("invalid_filter", 422)
    try:
        offset = int(cursor) if cursor else 0
    except ValueError:
        raise FactoryReadError("invalid_filter", 422) from None
    if offset < 0:
        raise FactoryReadError("invalid_filter", 422)

    wanted_repository: str | None = None
    if scope_id is not None:
        scope = await db.get(TeamGithubScope, scope_id)
        if scope is None:
            raise FactoryReadError("resource_not_found", 404)
        wanted_repository = f"{scope.repo_owner}/{scope.repo_name}".lower()

    # The current team lifetimes: a campaign of a retired team has no name.
    keys = dict((await db.execute(select(FactoryContextKey.numeric_id, FactoryContextKey.context_key).where(
        FactoryContextKey.key_kind == "team", FactoryContextKey.retired_at.is_(None)))).all())
    teams = {team.id: team for team in await db.scalars(select(AgentTeamPreset))}
    scopes_by_team: dict[int, list[str]] = {}
    for scope_row in await db.scalars(select(TeamGithubScope).order_by(TeamGithubScope.id)):
        scopes_by_team.setdefault(scope_row.preset_id, []).append(f"{scope_row.repo_owner}/{scope_row.repo_name}")
    marker = await db.scalar(select(CampaignUpgradeCoverage.started_at).where(CampaignUpgradeCoverage.id == 1))
    marker = _naive_utc(marker) if marker is not None else None

    query = select(Campaign).order_by(Campaign.started_at.desc(), Campaign.id.desc())
    if team_id is not None:
        query = query.where(Campaign.preset_id == team_id)
    campaigns = list((await db.scalars(query)).all())
    frozen = await _latest_frozen(db, [campaign.id for campaign in campaigns])

    rows: list[CampaignRow] = []
    for campaign in campaigns:
        current = keys.get(campaign.preset_id) == campaign.team_context_key
        team = teams.get(campaign.preset_id) if current else None
        stored = frozen.get(campaign.id)
        live = None if stored is not None else snapshots.cached_snapshot(campaign.id)
        evidenced = marker is not None and _naive_utc(campaign.started_at) < marker
        is_live = campaign.status in LIVE
        repositories: list[CampaignRepositoryCount] = []
        unknown: str | None = None
        source = None
        metrics: list[dict] = []
        if stored is not None or live is not None:
            payload = stored.payload if stored is not None else live.model_dump(mode="json")
            source = CampaignCountSource(
                kind="frozen" if stored is not None else "live",
                as_of=_naive_utc(stored.created_at) if stored is not None else _naive_utc(live.as_of))
            metrics = payload.get("metrics") or []
            counted = payload.get("repositories") or []
            repositories = [CampaignRepositoryCount(repo=item["repo"], issues=item.get("issues"),
                                                    reason=None if item.get("complete") else (item.get("reason") or "incomplete_read"))
                            for item in counted]
            if not repositories:
                unknown = "snapshot_without_repository_counts"
        elif is_live and current:
            repositories = [CampaignRepositoryCount(repo=name, issues=None, reason="not_read_yet")
                            for name in scopes_by_team.get(campaign.preset_id, [])]
            unknown = None if repositories else "not_read_yet"
        else:
            unknown = "before_recording" if evidenced else "no_snapshot"
        missing = "not_read_yet" if is_live else (unknown or "no_snapshot")
        rows.append(CampaignRow(
            id=campaign.id, team=CampaignTeam(id=team.id, name=team.name) if team is not None else None,
            selector=campaign.selector, status=campaign.status, started_at=_naive_utc(campaign.started_at),
            finished_at=_naive_utc(campaign.finished_at) if campaign.finished_at is not None else None,
            merged=_measure(metrics, "merged", missing), in_scope=_measure(metrics, "issues_in_scope", missing),
            repositories=repositories, repositories_unknown_reason=unknown, source=source))

    hidden = 0
    if wanted_repository is not None:
        kept = []
        for row in rows:
            if any(item.repo.lower() == wanted_repository for item in row.repositories):
                kept.append(row)
            elif not row.repositories:
                hidden += 1
        rows = kept
    counts = CampaignStatusCounts(live=sum(1 for row in rows if row.status in LIVE),
                                  finished=sum(1 for row in rows if row.status == "finished"))
    before = sum(1 for row in rows if row.status == "finished" and marker is not None and row.finished_at is not None
                 and row.finished_at < marker)
    if status == "live":
        rows = [row for row in rows if row.status in LIVE]
    elif status == "finished":
        rows = [row for row in rows if row.status == "finished"]
    page = rows[offset:offset + limit]
    more = offset + limit < len(rows)
    return CampaignListResponse(
        generated_at=datetime.now(timezone.utc).replace(tzinfo=None), campaigns=page, total=len(rows), counts=counts,
        hidden_unknown_repositories=hidden, finished_before_recording=before, has_more=more,
        next_cursor=str(offset + limit) if more else None)
