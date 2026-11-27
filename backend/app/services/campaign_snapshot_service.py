"""#1244: the campaign snapshot (design §3), its short cache, and frozen snapshot versions (design §9).

Separate from ``build_metrics_window``: that is a time-window summary of single-value samples. This reuses its
coverage helper and its ``FactoryMetricSample`` shape, so the coverage rules stay shared, and every sample names the
calculation that produced it. A snapshot is built from the campaign, its live membership and the recorded facts up to
``as_of``; it reads no clock, so the same inputs give the same snapshot. A partial history is unknown with a reason,
never zero. Later measures (#1245-#1250) add their samples here.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.models.database import (
    Campaign,
    CampaignSnapshot,
    FactoryAuditEvent,
    FactoryContextKey,
    GithubWorkItem,
    TeamGithubScope,
)
from app.models.schemas import FactoryMetricSample
from app.services import campaign_record_service as campaigns
from app.services import campaign_upgrade_service as upgrade
from app.services import factory_audit_service as audit
from app.services.factory_metrics_service import _coverage, _naive_utc
from app.services.github_rate_limit_history import campaign_rate_limit_time

CALCULATION_VERSION = "campaign-snapshot-v2"  # v2 (#1395): missing delivery history is unknown
CACHE_SECONDS = 30.0  # the Overview panel polls
_clock = time.monotonic
_cache: dict[int, tuple[float, tuple, Snapshot]] = {}


class MeasureCoverageEntry(BaseModel):
    measure: str
    started_at: datetime | None
    covered: bool
    reason: str | None = None


class SnapshotRepository(BaseModel):
    """#1252: one repository's issue count in the campaign. ``issues`` is None while its members are unknown."""

    repo: str
    issues: int | None
    complete: bool
    reason: str | None = None
    cutoff_gaps: list[str] = []  # why the count is unknown although the read was complete


class Snapshot(BaseModel):
    campaign_id: int
    selector: str
    started_at: datetime
    as_of: datetime
    calculation_version: str
    metrics: list[FactoryMetricSample]
    recorded_coverage: list[MeasureCoverageEntry]
    # Added by #1252. A snapshot stored before it has no such field and reads as an empty list.
    repositories: list[SnapshotRepository] = []


def clear_cache() -> None:
    _cache.clear()


def cached_snapshot(campaign_id: int) -> Snapshot | None:
    """The last snapshot built for the campaign in this process, or None. Builds nothing and reads no GitHub (#1252).
    It may be older than the cache lifetime: its ``as_of`` says how old."""
    hit = _cache.get(campaign_id)
    return hit[2] if hit is not None else None


def _sample(name: str, unit: str, value: float | None, sample_count: int, *, source: str,
            reasons: list[str] | None = None, coverage: str = "full", unknown: int | None = None) -> FactoryMetricSample:
    reasons = sorted(reasons or [])
    unknown = unknown if unknown is not None else (1 if value is None else 0)
    return FactoryMetricSample(name=name, counting_unit=unit, value=value, sample_count=sample_count,
                               unknown_count=unknown, unknown_reasons=reasons, source=source,
                               coverage=coverage, calculation_version=CALCULATION_VERSION)


def _lifetime(key: str):
    """The team lifetime's own scopes, while it is current: a replacement team reusing the preset id never joins the
    old campaign, even when it arrives during a read (V1 review 6086713296)."""
    return ((FactoryContextKey.key_kind == "team") & (FactoryContextKey.numeric_id == TeamGithubScope.preset_id)
            & (FactoryContextKey.context_key == key) & FactoryContextKey.retired_at.is_(None))


async def _fingerprint(db, campaign: Campaign) -> tuple:
    """Everything the membership measures read from the database: any new or changed record misses the cache.
    The rate-limit measure is read again on every hit instead, with its windows, gaps and the recorder's state.

    The team lifetime's own repositories and work items are compared row by row (V1 review 6079984028): a newly
    watched repository, or a status change that leaves every maximum as it was, misses too.
    """
    events = await db.scalar(select(func.max(FactoryAuditEvent.id)))
    scopes = tuple((await db.execute(select(TeamGithubScope.id, TeamGithubScope.repo_owner, TeamGithubScope.repo_name)
                                     .join(FactoryContextKey, _lifetime(campaign.team_context_key))
                                     .order_by(TeamGithubScope.id))).all())
    items = tuple((await db.execute(select(GithubWorkItem.id, GithubWorkItem.dispatch_status, GithubWorkItem.updated_at,
                                           GithubWorkItem.auto_merged_at)
                                    .join(TeamGithubScope, TeamGithubScope.id == GithubWorkItem.scope_id)
                                    .join(FactoryContextKey, _lifetime(campaign.team_context_key))
                                    .order_by(GithubWorkItem.id))).all())
    markers = tuple((entry.measure, entry.started_at) for entry in await upgrade.recorded_coverage(db, campaign))
    # Retiring the team ends the cache even with no repositories or work items (V1 review 6080664549).
    live = await audit.current_context_key(db, "team", campaign.preset_id) == campaign.team_context_key
    return (campaign.id, campaign.team_context_key, campaign.selector, campaign.status, campaign.started_at,
            campaign.finished_at, live, events, scopes, items, markers)


async def build_snapshot(db, campaign: Campaign, client, *, as_of: datetime, observed_at: datetime | None = None,
                         cached: bool = True) -> Snapshot:
    """The campaign's snapshot up to ``as_of``; served from the cache while no record it reads has changed.

    ``observed_at`` is when the caller reads GitHub, ``as_of`` by default. GitHub reports the membership as it is
    then, so a read after the cutoff leaves the historical membership unknown (V1 review 6079984028).
    """
    as_of = _naive_utc(as_of)
    observed = _naive_utc(observed_at) if observed_at is not None else as_of
    if cached:
        key = (await _fingerprint(db, campaign), as_of, observed)
        hit = _cache.get(campaign.id)
        if hit is not None and hit[0] > _clock() and hit[1] == key:
            # The recorder's pending windows are not records: its coverage is read again on every hit.
            return await _refresh_rate_limited(db, campaign, hit)
    snapshot = await _build(db, campaign, client, as_of, observed)
    if cached:
        _cache[campaign.id] = (_clock() + CACHE_SECONDS, key, snapshot)
    return snapshot


async def _refresh_rate_limited(db, campaign: Campaign, hit: tuple) -> Snapshot:
    """The caller's cached snapshot with its rate-limit sample read again. The cache entry is replaced only while it
    is still the one this caller read (V1 review 6080664549): a newer cutoff's entry that landed meanwhile stays."""
    snapshot = hit[2]
    coverage = await upgrade.recorded_coverage(db, campaign)
    fresh = await _rate_limited(db, coverage, snapshot.started_at, snapshot.as_of)
    metrics = sorted([m for m in snapshot.metrics if m.name != fresh.name] + [fresh], key=lambda m: m.name)
    if metrics == snapshot.metrics:
        return snapshot
    refreshed = snapshot.model_copy(update={"metrics": metrics})
    if _cache.get(campaign.id) is hit:
        _cache[campaign.id] = (hit[0], hit[1], refreshed)
    return refreshed


async def _build(db, campaign: Campaign, client, as_of: datetime, observed: datetime) -> Snapshot:
    started = _naive_utc(campaign.started_at)
    membership = await campaigns.members(db, campaign, client)
    gaps = [f"{repository.reason}: {repository.repo}" for repository in membership.repositories
            if not repository.complete]
    if membership.unavailable:
        gaps.append(membership.unavailable)
    complete = membership.complete
    facts = await _deliveries(db, campaign)
    # One member per GitHub issue: two scopes may spell the same repository differently (V1 review 6086115646).
    members: dict[tuple[str, int], object] = {}
    for m in membership.members:
        members.setdefault(_identity(m.repo, m.issue_number), m)
    delivered = {key: facts.delivered.get(key, []) for key in members}
    # The cutoff (V1 review 6079984028): only facts recorded by ``as_of`` count. GitHub reports today's membership,
    # so a later departure, a later delivery of a member, or a read after the cutoff leaves it unknown.
    if observed > as_of:
        gaps.append("membership_read_after_cutoff")
    if any(_naive_utc(d.at) > as_of for d in membership.departures):
        gaps.append("departure_after_cutoff")
    if any(at > as_of for times in delivered.values() for at in times):
        gaps.append("delivery_after_cutoff")
    known = complete and len(gaps) == 0
    label = "full" if known else ("partial" if any(r.complete for r in membership.repositories) else "unavailable")
    done = {key for key, times in delivered.items() if times}  # a delivery after the cutoff made these unknown
    merged = len(done)
    # #1395: a closed issue is undelivered only on a recorded non-delivery by the cutoff. Missing history is unknown,
    # and so is every closed issue while a delivery can't be placed: it may be that issue's merge.
    closed = [key for key, m in members.items() if m.state == "closed" and key not in done]
    undelivered = [key for key in closed if not facts.unidentified
                   and any(at <= as_of for at in facts.undelivered.get(key, ()))]
    unknown = len(closed) - len(undelivered)
    reason = "delivery_identity_unknown" if facts.unidentified else "delivery_history_missing"

    def membership_sample(name: str, value: int) -> FactoryMetricSample:
        return _sample(name, "issue", value if known else None, len(members), source="github_membership",
                       reasons=gaps, coverage=label)

    def delivery_sample(name: str, value: int) -> FactoryMetricSample:
        if not known or not unknown:
            return membership_sample(name, value)
        if not merged and unknown == sum(1 for m in members.values() if m.state == "closed"):  # nothing known
            return _sample(name, "issue", None, len(members), source="github_membership", reasons=[reason],
                           coverage="unavailable", unknown=unknown)
        return _sample(name, "issue", value, len(members), source="github_membership", reasons=[reason],
                       coverage="partial", unknown=unknown)

    deferred = sum(1 for departure in membership.departures
                   if departure.outcome == "deferred" and _naive_utc(departure.at) <= as_of)
    metrics = [
        membership_sample("issues_in_scope", len(members)),
        delivery_sample("merged", merged),
        delivery_sample("closed_without_delivery", len(undelivered)),
        _sample("deferred", "issue", deferred, deferred, source="factory_audit_events"),
    ]
    coverage = await upgrade.recorded_coverage(db, campaign)
    metrics.append(await _rate_limited(db, coverage, started, as_of))
    return Snapshot(
        campaign_id=campaign.id, selector=campaign.selector, started_at=started, as_of=as_of,
        calculation_version=CALCULATION_VERSION, metrics=sorted(metrics, key=lambda sample: sample.name),
        recorded_coverage=[MeasureCoverageEntry(measure=c.measure, started_at=c.started_at, covered=c.covered,
                                                reason=c.reason) for c in coverage],
        repositories=_repositories(membership, members, [gap for gap in gaps if gap in _CUTOFF_GAPS]))


_CUTOFF_GAPS = ("membership_read_after_cutoff", "departure_after_cutoff", "delivery_after_cutoff")


def _repositories(membership, members, cutoff: list[str]) -> list[SnapshotRepository]:
    """Each repository once (the spelling is not case-sensitive) with its issue count, unknown while any read of it
    is incomplete (#1252). The counts use the same members as ``issues_in_scope``. A gap at the cutoff leaves every
    count unknown, as it does the total (V1 review 114): today's membership is not the membership at ``as_of``."""
    per_repository: dict[str, int] = {}
    for repo, _number in members:
        per_repository[repo] = per_repository.get(repo, 0) + 1
    rows: dict[str, SnapshotRepository] = {}
    for coverage in membership.repositories:
        key = coverage.repo.lower()
        known = rows.get(key)
        complete = coverage.complete and (known is None or known.complete)
        reason = known.reason if known is not None and known.reason else coverage.reason
        counted = complete and not cutoff
        rows[key] = SnapshotRepository(repo=known.repo if known else coverage.repo,
                                       issues=per_repository.get(key, 0) if counted else None,
                                       complete=complete, reason=None if complete else reason,
                                       cutoff_gaps=cutoff)
    return list(rows.values())


@dataclass
class _Deliveries:
    delivered: dict[tuple[str, int], list[datetime]] = field(default_factory=dict)
    undelivered: dict[tuple[str, int], list[datetime]] = field(default_factory=dict)
    unidentified: bool = False  # a recorded delivery whose issue can't be derived


def _identity(repo: str, number: int) -> tuple[str, int]:
    return repo.lower(), number  # GitHub's owner and repository names are case-insensitive


async def _deliveries(db, campaign: Campaign) -> _Deliveries:
    """Each issue's recorded delivery outcomes in the campaign team lifetime (#1395).

    A fact is read by the team, repository and issue it recorded (T05), so deleting the work item loses nothing; a row
    recorded before that snapshot falls back to its live item for the issue. A fact's recorded team key decides its
    lifetime, so another team's fact never counts here, whatever its item is now (V1 review 6086115646). Only a row
    without that key is placed by its surviving references: its live item, its live scope, or its team (the foreign
    keys are ``SET NULL``, so a value names the live resource). A legacy item's ``auto_merged_at`` is a merge. The time
    is the fact's own, or its record time when GitHub gave none.
    """
    key = campaign.team_context_key
    lifetime = (select(GithubWorkItem.id).join(TeamGithubScope, TeamGithubScope.id == GithubWorkItem.scope_id)
                .join(FactoryContextKey, _lifetime(key)))
    scopes = select(TeamGithubScope.id).join(FactoryContextKey, _lifetime(key))
    team = select(FactoryContextKey.numeric_id).where(  # the team reference counts only while this lifetime is current
        FactoryContextKey.key_kind == "team", FactoryContextKey.context_key == key, FactoryContextKey.retired_at.is_(None))
    references = [FactoryAuditEvent.item_id.in_(lifetime), FactoryAuditEvent.scope_id.in_(scopes),
                  FactoryAuditEvent.team_preset_id.in_(team)]
    rows = (await db.execute(
        select(FactoryAuditEvent.delivery_outcome, FactoryAuditEvent.fact_time, FactoryAuditEvent.occurred_at,
               FactoryAuditEvent.context_snapshot, GithubWorkItem.issue_number, TeamGithubScope.repo_owner,
               TeamGithubScope.repo_name)
        .outerjoin(GithubWorkItem, GithubWorkItem.id == FactoryAuditEvent.item_id)
        .outerjoin(TeamGithubScope, TeamGithubScope.id == GithubWorkItem.scope_id)
        .where(FactoryAuditEvent.event_kind == "delivery_evidence",
               FactoryAuditEvent.delivery_outcome.in_(("delivered", "closed_without_delivery")),
               or_(FactoryAuditEvent.team_context_key == key,
                   FactoryAuditEvent.team_context_key.is_(None) & or_(*references))))).all()
    facts = _Deliveries()
    for row in rows:
        recorded = row.context_snapshot or {}
        owner, name, number = (recorded.get("repo_owner"), recorded.get("repo_name"), recorded.get("issue_number"))
        if not (owner and name and isinstance(number, int)):
            owner, name, number = row.repo_owner, row.repo_name, row.issue_number
        delivered = row.delivery_outcome == "delivered"
        if not (owner and name and isinstance(number, int)):
            facts.unidentified = facts.unidentified or delivered
            continue
        outcomes = facts.delivered if delivered else facts.undelivered
        outcomes.setdefault(_identity(f"{owner}/{name}", number), []).append(
            _naive_utc(row.fact_time or row.occurred_at))
    legacy = (await db.execute(
        select(TeamGithubScope.repo_owner, TeamGithubScope.repo_name, GithubWorkItem.issue_number,
               GithubWorkItem.auto_merged_at)
        .join(TeamGithubScope, TeamGithubScope.id == GithubWorkItem.scope_id)
        .join(FactoryContextKey, _lifetime(key))
        .where(GithubWorkItem.auto_merged_at.is_not(None)))).all()
    for row in legacy:
        facts.delivered.setdefault(_identity(f"{row.repo_owner}/{row.repo_name}", row.issue_number), []).append(
            _naive_utc(row.auto_merged_at))
    return facts


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


async def _first(db, campaign_id: int) -> CampaignSnapshot | None:
    return await db.scalar(select(CampaignSnapshot).where(CampaignSnapshot.campaign_id == campaign_id,
                                                          CampaignSnapshot.version == 1))


async def freeze(db, campaign: Campaign, client, *, actor: dict, as_of: datetime | None = None) -> Snapshot:
    """The campaign's first frozen snapshot; the finish flow (#1261) calls this.

    Idempotent (V1 review 6079984028): a repeated finish, or one racing it, returns the stored first version
    unchanged. Only the Director's recompute adds versions.
    """
    campaign_id = campaign.id
    stored = await _first(db, campaign_id)
    if stored is not None:
        return Snapshot.model_validate(stored.payload)
    end = as_of or campaign.finished_at or datetime.now(UTC).replace(tzinfo=None)
    snapshot = await build_snapshot(db, campaign, client, as_of=end, cached=False)
    db.add(CampaignSnapshot(campaign_id=campaign_id, version=1, calculation_version=CALCULATION_VERSION,
                            payload=snapshot.model_dump(mode="json"), created_by=actor["actor_kind"]))
    try:
        await db.commit()
    except IntegrityError:  # a concurrent finish stored version 1 first: return it
        await db.rollback()
        return Snapshot.model_validate((await _first(db, campaign_id)).payload)
    return snapshot


_RECOMPUTE_ATTEMPTS = 5


async def recompute(db, campaign: Campaign, client, *, actor: dict, as_of: datetime | None = None,
                    observed_at: datetime | None = None) -> Snapshot:
    """Only the Director recomputes a frozen snapshot (design §9); it adds the next version beside the old ones.

    Two concurrent recomputes each keep their version: a collision on the version takes the next one, reading
    nothing from the campaign after the rollback.
    """
    if actor.get("actor_kind") != "operator":
        raise campaigns.CampaignError("campaign_recompute_director_only")
    campaign_id = campaign.id
    if await latest(db, campaign_id) is None:
        raise campaigns.CampaignError("campaign_snapshot_not_frozen")
    now = datetime.now(UTC).replace(tzinfo=None)
    end = as_of or campaign.finished_at or now
    # GitHub is read now (V1 review 6080664549): today's membership is never attributed to an earlier cutoff.
    snapshot = await build_snapshot(db, campaign, client, as_of=end, observed_at=observed_at or now, cached=False)
    payload = snapshot.model_dump(mode="json")
    for _attempt in range(_RECOMPUTE_ATTEMPTS):
        version = (await db.scalar(select(func.max(CampaignSnapshot.version)).where(
            CampaignSnapshot.campaign_id == campaign_id)) or 0) + 1
        db.add(CampaignSnapshot(campaign_id=campaign_id, version=version, calculation_version=CALCULATION_VERSION,
                                payload=payload, created_by=actor["actor_kind"]))
        try:
            await db.commit()
            return snapshot
        except IntegrityError:  # another recompute took this version
            await db.rollback()
    raise campaigns.CampaignError("campaign_snapshot_version_conflict")
