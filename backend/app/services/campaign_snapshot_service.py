"""#1244: the campaign snapshot (design §3), its short cache, and frozen snapshot versions (design §9).

Separate from ``build_metrics_window``: that is a time-window summary of single-value samples. This reuses its
coverage helper and its ``FactoryMetricSample`` shape, so the coverage rules stay shared, and every sample names the
calculation that produced it. A snapshot is built from the campaign, its live membership and the recorded facts up to
``as_of``; it reads no clock, so the same inputs give the same snapshot. A partial history is unknown with a reason,
never zero. Later measures (#1245-#1250) add their samples here.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models.database import (
    Campaign,
    CampaignSnapshot,
    FactoryAuditEvent,
    FactoryContextKey,
    GithubRateLimitHistoryGap,
    GithubRateLimitWindow,
    GithubWorkItem,
    TeamGithubScope,
)
from app.models.schemas import FactoryMetricSample
from app.services import campaign_record_service as campaigns
from app.services import campaign_upgrade_service as upgrade
from app.services.factory_metrics_service import _coverage, _naive_utc
from app.services.github_rate_limit_history import campaign_rate_limit_time
from app.services.work_item_states import is_done

CALCULATION_VERSION = "campaign-snapshot-v1"
CACHE_SECONDS = 30.0  # the Overview panel polls
_clock = time.monotonic
_cache: dict[int, tuple[float, tuple, Snapshot]] = {}


class MeasureCoverageEntry(BaseModel):
    measure: str
    started_at: datetime | None
    covered: bool
    reason: str | None = None


class Snapshot(BaseModel):
    campaign_id: int
    selector: str
    started_at: datetime
    as_of: datetime
    calculation_version: str
    metrics: list[FactoryMetricSample]
    recorded_coverage: list[MeasureCoverageEntry]


def clear_cache() -> None:
    _cache.clear()


def _sample(name: str, unit: str, value: float | None, sample_count: int, *, source: str,
            reasons: list[str] | None = None, coverage: str = "full") -> FactoryMetricSample:
    reasons = sorted(reasons or [])
    return FactoryMetricSample(name=name, counting_unit=unit, value=value, sample_count=sample_count,
                               unknown_count=1 if value is None else 0, unknown_reasons=reasons, source=source,
                               coverage=coverage, calculation_version=CALCULATION_VERSION)


async def _fingerprint(db, campaign: Campaign) -> tuple:
    """Everything the measures read from the database: any new or changed record misses the cache."""
    events = await db.scalar(select(func.max(FactoryAuditEvent.id)))
    items = (await db.execute(select(func.count(GithubWorkItem.id), func.max(GithubWorkItem.id),
                                     func.max(GithubWorkItem.updated_at)))).one()
    windows = (await db.execute(select(func.count(), func.max(GithubRateLimitWindow.reset_at),
                                       func.max(GithubRateLimitWindow.started_at))
                                .select_from(GithubRateLimitWindow))).one()
    gaps = (await db.execute(select(func.count(GithubRateLimitHistoryGap.id), func.max(GithubRateLimitHistoryGap.id),
                                    func.max(GithubRateLimitHistoryGap.ended_at),
                                    func.count(GithubRateLimitHistoryGap.ended_at)))).one()
    markers = tuple((entry.measure, entry.started_at) for entry in await upgrade.recorded_coverage(db, campaign))
    return (campaign.id, campaign.team_context_key, campaign.selector, campaign.status, campaign.started_at,
            campaign.finished_at, events, tuple(items), tuple(windows), tuple(gaps), markers)


async def build_snapshot(db, campaign: Campaign, client, *, as_of: datetime, cached: bool = True) -> Snapshot:
    """The campaign's snapshot up to ``as_of``; served from the cache while no record it reads has changed."""
    as_of = _naive_utc(as_of)
    if cached:
        key = (await _fingerprint(db, campaign), as_of)
        hit = _cache.get(campaign.id)
        if hit is not None and hit[0] > _clock() and hit[1] == key:
            return hit[2]
    snapshot = await _build(db, campaign, client, as_of)
    if cached:
        _cache[campaign.id] = (_clock() + CACHE_SECONDS, key, snapshot)
    return snapshot


async def _build(db, campaign: Campaign, client, as_of: datetime) -> Snapshot:
    started = _naive_utc(campaign.started_at)
    membership = await campaigns.members(db, campaign, client)
    gaps = [f"{repository.reason}: {repository.repo}" for repository in membership.repositories
            if not repository.complete]
    if membership.unavailable:
        gaps.append(membership.unavailable)
    known = membership.complete
    label = "full" if known else ("partial" if any(r.complete for r in membership.repositories) else "unavailable")
    done = await _delivered(db, campaign.team_context_key)
    members = {(m.repo, m.issue_number): m for m in membership.members}
    merged = sum(1 for key in members if key in done)
    closed = sum(1 for key, m in members.items() if m.state == "closed" and key not in done)

    def membership_sample(name: str, value: int) -> FactoryMetricSample:
        return _sample(name, "issue", value if known else None, len(members), source="github_membership",
                       reasons=gaps, coverage=label)

    deferred = sum(1 for departure in membership.departures if departure.outcome == "deferred")
    metrics = [
        membership_sample("issues_in_scope", len(members)),
        membership_sample("merged", merged),
        membership_sample("closed_without_delivery", closed),
        _sample("deferred", "issue", deferred, deferred, source="factory_audit_events"),
    ]
    coverage = await upgrade.recorded_coverage(db, campaign)
    metrics.append(await _rate_limited(db, coverage, started, as_of))
    return Snapshot(
        campaign_id=campaign.id, selector=campaign.selector, started_at=started, as_of=as_of,
        calculation_version=CALCULATION_VERSION, metrics=sorted(metrics, key=lambda sample: sample.name),
        recorded_coverage=[MeasureCoverageEntry(measure=c.measure, started_at=c.started_at, covered=c.covered,
                                                reason=c.reason) for c in coverage])


async def _delivered(db, key: str) -> set[tuple[str, int]]:
    """The issues delivered in the campaign team lifetime's own repositories."""
    rows = (await db.execute(select(TeamGithubScope.repo_owner, TeamGithubScope.repo_name,
                                    GithubWorkItem.issue_number, GithubWorkItem.dispatch_status)
        .join(GithubWorkItem, GithubWorkItem.scope_id == TeamGithubScope.id)
        .join(FactoryContextKey, (FactoryContextKey.key_kind == "team")
              & (FactoryContextKey.numeric_id == TeamGithubScope.preset_id)
              & (FactoryContextKey.context_key == key)))).all()
    return {(f"{row.repo_owner}/{row.repo_name}", row.issue_number) for row in rows if is_done(row.dispatch_status)}


async def _rate_limited(db, coverage, started: datetime, as_of: datetime) -> FactoryMetricSample:
    entry = next(c for c in coverage if c.measure == "rate_limit_windows")
    label, _available, missing = _coverage(entry.started_at and _naive_utc(entry.started_at), started, as_of)
    if not entry.covered or label != "full":
        return _sample("rate_limited_seconds", "second", None, 0, source="github_rate_limit_windows",
                       reasons=[entry.reason or "before_recording", *missing], coverage=label)
    if as_of <= started:
        return _sample("rate_limited_seconds", "second", 0.0, 0, source="github_rate_limit_windows")
    measured = await campaign_rate_limit_time(db, started.replace(tzinfo=UTC), as_of.replace(tzinfo=UTC))
    if measured.seconds is None:
        return _sample("rate_limited_seconds", "second", None, measured.sample_count,
                       source="github_rate_limit_windows", reasons=["rate_limit_history_incomplete"],
                       coverage="partial")
    return _sample("rate_limited_seconds", "second", measured.seconds, measured.sample_count,
                   source="github_rate_limit_windows")


# Frozen snapshots
async def latest(db, campaign_id: int) -> CampaignSnapshot | None:
    return await db.scalar(select(CampaignSnapshot).where(CampaignSnapshot.campaign_id == campaign_id)
                           .order_by(CampaignSnapshot.version.desc()).limit(1))


async def freeze(db, campaign: Campaign, client, *, actor: dict, as_of: datetime | None = None) -> Snapshot:
    """Store the next version of the campaign's snapshot; the finish flow (#1261) calls this."""
    end = as_of or campaign.finished_at or datetime.now(UTC).replace(tzinfo=None)
    snapshot = await build_snapshot(db, campaign, client, as_of=end, cached=False)
    for _attempt in range(2):
        version = (await db.scalar(select(func.max(CampaignSnapshot.version)).where(
            CampaignSnapshot.campaign_id == campaign.id)) or 0) + 1
        db.add(CampaignSnapshot(campaign_id=campaign.id, version=version, calculation_version=CALCULATION_VERSION,
                                payload=snapshot.model_dump(mode="json"), created_by=actor["actor_kind"]))
        try:
            await db.commit()
            return snapshot
        except IntegrityError:  # another freeze took this version: take the next one
            await db.rollback()
    raise campaigns.CampaignError("campaign_snapshot_version_conflict")


async def recompute(db, campaign: Campaign, client, *, actor: dict, as_of: datetime | None = None) -> Snapshot:
    """Only the Director recomputes a frozen snapshot (design §9); it creates a new version beside the old ones."""
    if actor.get("actor_kind") != "operator":
        raise campaigns.CampaignError("campaign_recompute_director_only")
    if await latest(db, campaign.id) is None:
        raise campaigns.CampaignError("campaign_snapshot_not_frozen")
    return await freeze(db, campaign, client, actor=actor, as_of=as_of)
