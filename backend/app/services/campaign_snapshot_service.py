"""#1244: the campaign snapshot (design §3), its short cache, and frozen snapshot versions (design §9).

Separate from ``build_metrics_window``: that is a time-window summary of single-value samples. This reuses its
coverage helper and its ``FactoryMetricSample`` shape, so the coverage rules stay shared, and every sample names the
calculation that produced it. A snapshot is built from the campaign, its live membership and the recorded facts up to
``as_of``; it reads no clock, so the same inputs give the same snapshot. A partial history is unknown with a reason,
never zero. Later measures (#1245-#1250) add their samples here.

#1246 split 1 adds a structured section, the item time from the first dispatch in the campaign to a sourced merge in
the campaign, by size band. Bands are by lines changed (additions plus deletions) in R2, the PR facts recorded at
merge (#1238): 0-100, 101-400 and over 400 (the Supervisor's answers on #1246, 2026-11-27). Only a recorded PR merge
time (or a legacy automatic merge time) ends an interval. The dispatch is read from the item lifetime that recorded
the merge, between the later of campaign start and item creation and the merge, and only where the C12
forward-coverage marker is at or before that window's start, so no earlier dispatch in it can be missing; the first
recorded event never stands in for that (V1 review 448). The median is the middle value; p85 is the nearest rank (the smallest time
with at least 85% of times at or below it). Fewer than five timed items is a small sample, shown with its value and n.

#1419 (#1245 part 4) adds "where the time went". Its item time is Q1's, recorded on #1245: per attempt, from waiting
for dispatch to the merge, or ``as_of`` while open, and before the first attempt from campaign membership. That
differs from #1246's dispatch-to-merge median. One item's attempt windows are contiguous (each starts where the last
ended), so their sum is the item's campaign time; the stage engine splits each into waiting on someone, process,
active and an explicit unaccounted remainder, so the stages plus unaccounted equal item time. A member with no
recorded attempt is unknown, never zero time.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from statistics import median

from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from app.models.database import (
    Campaign,
    CampaignSnapshot,
    FactoryAuditEvent,
    FactoryContextKey,
    GithubApprovalRequest,
    GithubAttemptScopeRevision,
    GithubCheckRunHistory,
    GithubHeadReview,
    GithubRequiredCheckRecipient,
    GithubRequiredCheckWatch,
    GithubRevisionReview,
    GithubWorkItem,
    MailMessage,
    TeamGithubScope,
)
from app.models.schemas import FactoryMetricSample
from app.services import campaign_record_service as campaigns
from app.services import campaign_upgrade_service as upgrade
from app.services import factory_audit_service as audit
from app.services.factory_metrics_service import _coverage, _naive_utc
from app.services.github_rate_limit_history import campaign_rate_limit_time
from app.services.retro_bottleneck_service import (
    CampaignBottleneckResponse, CampaignBottlenecks, RankingAttempt, build_bottlenecks,
)

FINAL, ASK = "final", "ask"  # #1260: the purpose of a stored version
# v2 (#1395): missing delivery history is unknown; v3 (#1246): item time; v4 (#1419): where the time went
CALCULATION_VERSION = "campaign-snapshot-v6"  # v5 (#1257): bottleneck windows; v6 (#1453): flow
SMALL_SAMPLE = 5
BANDS = (("0-100", 100), ("101-400", 400), ("over-400", None))  # by lines changed, upper bound inclusive
CACHE_SECONDS = 30.0  # the Overview panel polls
_clock = time.monotonic
_cache: dict[int, tuple[float, tuple, Snapshot]] = {}
_item_keys = aliased(FactoryContextKey)


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


class BandRow(BaseModel):
    band: str  # 0-100, 101-400, over-400, or unknown when the lines changed are not known
    items: int  # delivered members in the band
    timed: int  # of those, with an evidenced first dispatch
    median_seconds: float | None
    p85_seconds: float | None
    small_sample: bool
    coverage: str  # full, partial or unavailable
    unknown_reasons: list[str]


class ItemTimeSection(BaseModel):
    """#1246: item time from first dispatch to merge, by size band. Absent in snapshots frozen before v3."""
    items: int
    timed: int
    coverage: str
    unknown_reasons: list[str]
    bands: list[BandRow]


class StageRow(BaseModel):
    group: str
    stage: str
    actor: str
    share_seconds: float  # its part of item time
    wall_seconds: float  # its own time, whatever else ran (Q2)
    attempts: int = 0  # the attempts it was recorded in
    coverage: str = "full"  # its group's recording coverage
    basis: str | None = None  # e.g. waiting for dispatch from campaign membership, when ready time was not recorded


class TimeWentSection(BaseModel):
    """#1419: where the campaign's item time went. Absent in snapshots frozen before v4."""
    items: int  # campaign members with at least one attempt
    attempts: int
    unknown_items: int  # members with no recorded attempt: unknown, never zero time
    unattributed: int  # recorded evidence the collector could not place in an attempt
    total_seconds: float | None
    groups: dict[str, float | None]
    group_coverage: dict[str, str] = {}  # per group: full, partial (a lower bound) or unavailable
    unaccounted_seconds: float | None
    overlap_seconds: float | None
    stages: list[StageRow]
    coverage: str
    unknown_reasons: list[str]


class WeekRow(BaseModel):
    week_start: date  # the Monday of a UTC ISO week
    merges: int
    partial: bool  # the campaign start or the cutoff falls inside the week


class MarkerSide(BaseModel):
    items: int  # timed items wholly on this side of the marker
    median_seconds: float | None
    small_sample: bool


class ChangeMarker(BaseModel):
    """An applied configuration change, with the item time on each side of it. Never a verdict (design §8)."""
    at: datetime
    source: str
    fields: list[str]  # the changed field names, never their values
    before: MarkerSide  # dispatched and merged before the marker
    after: MarkerSide  # dispatched at or after it
    straddling: int  # dispatched before it, merged at or after it: excluded (design answer Q3)
    unknown: int  # merged in the campaign with an unknown item time: excluded


class FlowSection(BaseModel):
    """#1453: merges per week and the change markers. Absent in snapshots frozen before v5."""
    merges: int  # members whose first merge in the campaign has a time
    unknown_merges: int  # PR merges GitHub gave no time: never placed in a week
    weeks: list[WeekRow]
    markers: list[ChangeMarker]
    marker_coverage: str  # partial when audit recording began after the campaign start, or isn't marked
    coverage: str
    unknown_reasons: list[str]


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
    item_time: ItemTimeSection | None = None  # additive (#1246): an older frozen payload reads back unchanged
    time_went: TimeWentSection | None = None  # additive (#1419)
    bottlenecks: CampaignBottlenecks | None = None
    flow: FlowSection | None = None  # additive (#1453)


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


async def _team_events(db, campaign: Campaign):
    """The newest audit event that this team lifetime's measures can read (#1260 V1 review 128). The events are
    placed as ``_deliveries``, ``_markers`` and the pull facts place them: by the lifetime key, or, without a key,
    by a surviving reference to the lifetime's items, scopes or team. Another team's events never match."""
    key = campaign.team_context_key
    items = (select(GithubWorkItem.id).join(TeamGithubScope, TeamGithubScope.id == GithubWorkItem.scope_id)
             .join(FactoryContextKey, _lifetime(key)))
    scopes = select(TeamGithubScope.id).join(FactoryContextKey, _lifetime(key))
    team = select(FactoryContextKey.numeric_id).where(
        FactoryContextKey.key_kind == "team", FactoryContextKey.context_key == key, FactoryContextKey.retired_at.is_(None))
    item_keys = select(FactoryContextKey.context_key).where(
        FactoryContextKey.key_kind == "item", FactoryContextKey.numeric_id.in_(items))
    return await db.scalar(select(func.max(FactoryAuditEvent.id)).where(or_(
        FactoryAuditEvent.team_context_key == key, FactoryAuditEvent.item_context_key.in_(item_keys),
        FactoryAuditEvent.team_context_key.is_(None) & or_(
            FactoryAuditEvent.item_id.in_(items), FactoryAuditEvent.scope_id.in_(scopes),
            FactoryAuditEvent.team_preset_id.in_(team)))))


async def _fingerprint(db, campaign: Campaign, *, scoped: bool = False) -> tuple:
    """Everything the membership measures read from the database: any new or changed record misses the cache.
    The rate-limit measure is read again on every hit instead, with its windows, gaps and the recorder's state.

    The team lifetime's own repositories and work items are compared row by row (V1 review 6079984028): a newly
    watched repository, or a status change that leaves every maximum as it was, misses too.

    ``scoped`` (#1260) reads the audit events of this team lifetime only, for the identity of an ask version: another
    team's event never makes this campaign's ask stale. The cache keeps the global mark.
    """
    events = await (_team_events(db, campaign) if scoped else db.scalar(select(func.max(FactoryAuditEvent.id))))
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
    # #1246: installing the lifecycle coverage marker changes item time without any new event (V1 review 448).
    lifecycle = await audit.instrumentation_start(db)
    return (campaign.id, campaign.team_context_key, campaign.selector, campaign.status, campaign.started_at,
            campaign.finished_at, live, events, scopes, items, markers, lifecycle, await _retro_rows(db, campaign))


async def _retro_rows(db, campaign: Campaign) -> tuple:
    """#1419: every row "where the time went" reads that can change without an audit event, with its mutable fields,
    so an insert, update or deletion misses the cache (row by row, as for scopes and items above)."""
    items = (select(GithubWorkItem.id).join(TeamGithubScope, TeamGithubScope.id == GithubWorkItem.scope_id)
             .join(FactoryContextKey, _lifetime(campaign.team_context_key)))
    scopes = select(TeamGithubScope.id).join(FactoryContextKey, _lifetime(campaign.team_context_key))
    watches = select(GithubRequiredCheckWatch.id).where(GithubRequiredCheckWatch.work_item_id.in_(items))
    reads = [
        select(GithubCheckRunHistory.repository, GithubCheckRunHistory.check_run_id, GithubCheckRunHistory.head_sha,
               GithubCheckRunHistory.pr_number, GithubCheckRunHistory.check_name, GithubCheckRunHistory.app_slug,
               GithubCheckRunHistory.required,
               GithubCheckRunHistory.job_created_at,
               GithubCheckRunHistory.started_at, GithubCheckRunHistory.completed_at)
        .where(GithubCheckRunHistory.scope_id.in_(scopes))
        .order_by(GithubCheckRunHistory.repository, GithubCheckRunHistory.check_run_id),
        select(GithubHeadReview.id, GithubHeadReview.verdict, GithubHeadReview.status, GithubHeadReview.created_at,
               GithubHeadReview.ended_at, GithubHeadReview.head_sha, GithubHeadReview.pr_number,
               GithubHeadReview.scope_revision, GithubHeadReview.reviewer_member_id)
        .where(GithubHeadReview.work_item_id.in_(items)).order_by(GithubHeadReview.id),
        select(GithubRevisionReview.id, GithubRevisionReview.verdict, GithubRevisionReview.status,
               GithubRevisionReview.created_at, GithubRevisionReview.ended_at, GithubRevisionReview.watch_id,
               GithubRevisionReview.revision_id, GithubRevisionReview.reviewer_member_id)
        .where(GithubRevisionReview.work_item_id.in_(items)).order_by(GithubRevisionReview.id),
        select(GithubRequiredCheckWatch.id, GithubRequiredCheckWatch.head_sha, GithubRequiredCheckWatch.pr_number,
               GithubRequiredCheckWatch.reviewer_member_id, GithubRequiredCheckWatch.completion_kind,
               GithubRequiredCheckWatch.changed_at, GithubRequiredCheckWatch.completed_at,
               GithubRequiredCheckWatch.scope_revision)
        .where(GithubRequiredCheckWatch.work_item_id.in_(items)).order_by(GithubRequiredCheckWatch.id),
        select(GithubRequiredCheckRecipient.id, GithubRequiredCheckRecipient.watch_id,
               GithubRequiredCheckRecipient.member_id, GithubRequiredCheckRecipient.message_id,
               MailMessage.recipient_member_id, MailMessage.delivery_key, MailMessage.created_at)
        .outerjoin(MailMessage, MailMessage.id == GithubRequiredCheckRecipient.message_id)
        .where(GithubRequiredCheckRecipient.watch_id.in_(watches)).order_by(GithubRequiredCheckRecipient.id),
        select(GithubApprovalRequest.id, GithubApprovalRequest.request_kind, GithubApprovalRequest.status,
               GithubApprovalRequest.created_at, GithubApprovalRequest.decided_at,
               GithubApprovalRequest.owner_member_id)
        .where(GithubApprovalRequest.work_item_id.in_(items)).order_by(GithubApprovalRequest.id),
        select(GithubAttemptScopeRevision.id, GithubAttemptScopeRevision.dispatch_nonce,
               GithubAttemptScopeRevision.owner_member_id)
        .where(GithubAttemptScopeRevision.work_item_id.in_(items)).order_by(GithubAttemptScopeRevision.id),
    ]
    rows = []
    for read in reads:
        rows.append(tuple((await db.execute(read)).all()))
    return tuple(rows)


async def build_snapshot(db, campaign: Campaign, client, *, as_of: datetime | None = None, observed_at: datetime | None = None,
                         cached: bool = True, membership=None) -> Snapshot:
    """The campaign's snapshot up to ``as_of``; served from the cache while no record it reads has changed.

    Omit ``as_of`` for a current read that can reuse a fresh, matching live cutoff.
    ``observed_at`` is when the caller reads GitHub, ``as_of`` by default. GitHub reports the membership as it is
    then, so a read after the cutoff leaves the historical membership unknown (V1 review 6079984028).
    ``membership`` is a read that the caller already made (#1260): the snapshot is built from that one observation,
    never from a second read, and is not cached.
    """
    cached = cached and membership is None
    live_read = as_of is None and observed_at is None
    as_of = _naive_utc(as_of if as_of is not None else datetime.now(UTC))
    observed = _naive_utc(observed_at) if observed_at is not None else as_of
    if cached:
        key = (await _fingerprint(db, campaign), as_of, observed)
        hit = _cache.get(campaign.id)
        if hit is not None and hit[0] > _clock():
            earlier_key = hit[1]
            current_live_read = (live_read and earlier_key[0] == key[0] and earlier_key[1] == earlier_key[2]
                                 and 0 <= (as_of - hit[2].as_of).total_seconds() < CACHE_SECONDS)
            if earlier_key == key or current_live_read:
                # The recorder's pending windows are not records: its coverage is read again on every hit.
                return await _refresh_rate_limited(db, campaign, hit)
    snapshot = await _build(db, campaign, client, as_of, observed, membership)
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


async def _build(db, campaign: Campaign, client, as_of: datetime, observed: datetime, membership=None) -> Snapshot:
    started = _naive_utc(campaign.started_at)
    if membership is None:
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
    # Missing delivery history the counts already know of makes item time partial too (V1 review 448).
    spans: list[tuple[datetime | None, datetime | None, list[str]]] = []
    item_time = await _item_time(db, campaign, list(members), facts, coverage, as_of,
                                 gaps + ([reason] if unknown else []), label, spans)  # both kept (V1 review 115)
    time_went, bottlenecks = await _time_went(db, campaign, list(members), coverage, as_of, gaps)
    flow = await _flow(db, campaign, spans, as_of, gaps + ([reason] if unknown else []), label)
    return Snapshot(
        campaign_id=campaign.id, selector=campaign.selector, started_at=started, as_of=as_of,
        calculation_version=CALCULATION_VERSION, metrics=sorted(metrics, key=lambda sample: sample.name),
        recorded_coverage=[MeasureCoverageEntry(measure=c.measure, started_at=c.started_at, covered=c.covered,
                                                reason=c.reason) for c in coverage], item_time=item_time,
        time_went=time_went, bottlenecks=bottlenecks, flow=flow,
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


_LOST_GROUP = {source: "waiting" for source in ("dispatch", "decision", "effort", "ceiling", "hold", "pause", "mail",
                                                 "input")} | {
    source: "process" for source in ("required_check", "review", "check_watch", "head_review", "revision_review")} | {
    source: "active" for source in ("planning", "building", "approval_request")}


async def _time_went(db, campaign: Campaign, members: list[tuple[str, int]], coverage, as_of: datetime,
                     gaps: list[str]) -> tuple[TimeWentSection, CampaignBottlenecks]:
    """The campaign members' attempt time, split by the stage engine and summed; it reconciles with item time.

    A member whose attempts come from two overlapping item lifetimes (two rows tracking one issue) is ambiguous and
    unknown, never counted twice. Each group carries its recorder's coverage: a recorder not installed makes the group
    unknown, one that began inside the measured windows makes it a lower bound (V1 review 118)."""
    from app.services.retro_attempt_collector import FIRST_LABEL, collect_team_attempts
    from app.services.retro_stage_engine import GROUPS, breakdown

    started = _naive_utc(campaign.started_at)
    wanted = set(members)
    collected = await collect_team_attempts(db, campaign.preset_id, campaign_start=started, as_of=as_of,
                                            team_context_key=campaign.team_context_key)
    # Members come from the collector's own lifetime-bound item read, never a separate one (V1 review 119).
    member_items = collected.item_identity
    by_member: dict[tuple[str, int], list] = {}
    for attempt in collected.attempts:
        if member_items.get(attempt.item_id) in wanted:
            by_member.setdefault(member_items[attempt.item_id], []).append(attempt)
    reasons = set(gaps)
    attempts = []
    for found in by_member.values():
        if any(a.item_id != b.item_id and min(a.window.end, b.window.end) > max(a.window.start, b.window.start)
               for a in found for b in found):  # a shared positive duration, not a zero-length window
            reasons.add("ambiguous_tracking")  # two lifetimes track one issue at once: neither is counted
            continue
        attempts.extend(found)
    lost = [u for u in collected.unattributed if u.item_id is None or member_items.get(u.item_id) in wanted]
    unstable = {member_items[i] for i in collected.unstable_items if member_items.get(i) in wanted}
    if unstable:  # their lifetime changed during the read: unknown, never another lifetime's time (V1 review 121)
        reasons.add("item_lifetime_changed_during_read")
    timed = {member_items[a.item_id] for a in attempts}
    groups = dict.fromkeys(GROUPS, 0.0)
    total = unaccounted = overlap = 0.0
    stages: dict[tuple[str, str, str], list] = {}
    ranking_attempts = []
    for attempt in attempts:
        result = breakdown(attempt.window, attempt.intervals)
        ranking_attempts.append(RankingAttempt(member_items[attempt.item_id], attempt.window, attempt.intervals, result))
        total, unaccounted, overlap = (total + result.total_seconds, unaccounted + result.unaccounted_seconds,
                                       overlap + result.overlap_seconds)
        for group, seconds in result.groups.items():
            groups[group] += seconds
        for row in result.stages:
            sums = stages.setdefault((row.group, row.stage, row.actor), [0.0, 0.0, 0, False])
            sums[0], sums[1], sums[2] = sums[0] + row.share_seconds, sums[1] + row.wall_seconds, sums[2] + 1
            sums[3] = sums[3] or (row.stage == "dispatch" and attempt.window.start_label == FIRST_LABEL)
    reasons |= {u.reason for u in lost}
    if wanted - set(by_member) - unstable:  # members with no recorded attempt; ambiguous or unstable ones say so
        reasons.add("no_attempt_recorded")
    earliest = min((a.window.start for a in attempts), default=None)
    marker = await audit.instrumentation_start(db)
    if marker is None:
        reasons.add("no_forward_coverage_marker")
    elif earliest is not None and _naive_utc(marker) > earliest:
        reasons.add("dispatch_before_instrumentation")  # an earlier dispatch in the windows may be missing
    recorders = {c.measure: c for c in coverage}
    group_coverage = dict.fromkeys(GROUPS, "full")
    for group, measure in (("process", "check_runs"), ("waiting", "decision_waits")):
        entry = recorders.get(measure)
        if entry is None or entry.started_at is None:
            group_coverage[group] = "unavailable"
            reasons.add(f"{measure}: {entry.reason if entry else 'recording_not_installed'}")
        elif earliest is not None and _naive_utc(entry.started_at) > earliest:
            group_coverage[group] = "partial"  # what was recorded is kept, as a lower bound
            reasons.add(f"{measure}: recording_started_late")
    # Evidence the collector couldn't place makes the group it would have fed a lower bound (V1 review 119).
    for group in sorted({_LOST_GROUP[u.source] for u in lost if u.source in _LOST_GROUP}):
        if group_coverage[group] == "full":
            group_coverage[group] = "partial"
        reasons.add(f"{group}: unattributed_evidence")
    known = bool(attempts)
    if not known:
        group_coverage = dict.fromkeys(GROUPS, "unavailable")
    section = TimeWentSection(
        items=len(timed), attempts=len(attempts), unknown_items=len(wanted) - len(timed), unattributed=len(lost),
        total_seconds=total if known else None,
        groups={g: (v if group_coverage[g] != "unavailable" else None) for g, v in groups.items()},  # all with none
        group_coverage=group_coverage, unaccounted_seconds=unaccounted if known else None,
        overlap_seconds=overlap if known else None,
        stages=[StageRow(group=g, stage=st, actor=a, share_seconds=v[0], wall_seconds=v[1], attempts=v[2],
                         coverage=group_coverage[g], basis=FIRST_LABEL if v[3] else None)
                for (g, st, a), v in sorted(stages.items(), key=lambda kv: (GROUPS.index(kv[0][0]), kv[0][1:]))],
        coverage="unavailable" if not known else ("partial" if reasons else "full"), unknown_reasons=sorted(reasons))
    rankings = build_bottlenecks(ranking_attempts, start=started, as_of=as_of,
                                 group_coverage=group_coverage, unknown_reasons=section.unknown_reasons)
    return section, rankings


def _p85(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.85 * len(ordered)) - 1]


def _band_of(lines: int) -> str:
    return next(name for name, upper in BANDS if upper is None or lines <= upper)


async def _item_time(db, campaign: Campaign, members: list[tuple[str, int]], facts: _Deliveries, coverage,
                     as_of: datetime, gaps: list[str], membership: str,
                     spans: list | None = None) -> ItemTimeSection:
    """Each member delivered by the cutoff, from its evidenced first dispatch to its earliest merge, by size band.
    ``spans`` receives each merged member's (first dispatch or None, first merge or None when GitHub gave no time,
    why the start is unknown)."""
    marker = await audit.instrumentation_start(db)
    marker = marker and _naive_utc(marker)
    facts_entry = next(c for c in coverage if c.measure == "pull_request_facts")
    facts_from = facts_entry.started_at and _naive_utc(facts_entry.started_at)
    began = _naive_utc(campaign.started_at)
    # The live items of the team lifetime by their current item key: a merge's own lifetime finds its row, and a
    # replacement row (a new key) never stands in for an old merge (V1 review 448).
    live = {row.context_key: row for row in (await db.execute(
        select(GithubWorkItem.id, GithubWorkItem.created_at, FactoryContextKey.context_key)
        .join(TeamGithubScope, TeamGithubScope.id == GithubWorkItem.scope_id)
        .join(FactoryContextKey, _lifetime(campaign.team_context_key))
        .join(_item_keys, (_item_keys.key_kind == "item") & (_item_keys.numeric_id == GithubWorkItem.id)
              & _item_keys.retired_at.is_(None))
        .with_only_columns(GithubWorkItem.id, GithubWorkItem.created_at, _item_keys.context_key))).all()}
    sizes = {}
    for row in (await db.execute(select(FactoryAuditEvent.context_snapshot, FactoryAuditEvent.after_values).where(
            FactoryAuditEvent.event_kind == audit.PULL_FACTS_KIND,
            FactoryAuditEvent.team_context_key == campaign.team_context_key))).all():
        artifact = str((row.context_snapshot or {}).get("artifact") or "").lower()
        values = row.after_values or {}
        sizes.setdefault(artifact, (values.get("additions"), values.get("deletions")))
    rows: dict[str, list[tuple[float | None, list[str]]]] = {name: [] for name, _ in BANDS}
    rows["unknown"] = []
    for key in members:
        merges = [merge for merge in facts.merges.get(key, ()) if began <= merge[0] <= as_of]
        if not merges:
            # A PR merge GitHub gave no time is unknown; a non-merge delivery is outside this metric (V1 review 115).
            if any(began <= at <= as_of for at in facts.untimed.get(key, ())):
                rows["unknown"].append((None, ["merge_time_not_recorded"]))
                if spans is not None:
                    spans.append((None, None, []))
            continue
        end = min(at for at, _, _ in merges)
        tied = [merge for merge in merges if merge[0] == end]
        artifacts = {artifact.lower() for _, artifact, _ in tied if artifact}
        lifetimes = {lifetime for _, _, lifetime in tied}
        if len(lifetimes) > 1:
            start, reasons = None, ["ambiguous_tracking"]
        else:
            start, reasons = await _first_dispatch(db, live, next(iter(lifetimes)), marker, began, end)
        size = sizes.get(next(iter(artifacts))) if len(artifacts) == 1 else None
        if len(artifacts) > 1:
            band, why = "unknown", "conflicting_artifacts"
        elif facts_from is None:
            band, why = "unknown", "pull_facts_recording_not_installed"
        elif end < facts_from:
            band, why = "unknown", "merged_before_pull_facts_recording"
        elif not artifacts:
            band, why = "unknown", "artifact_not_recorded"
        elif size is None:
            band, why = "unknown", "pull_request_facts_missing"
        elif None in size:
            band, why = "unknown", "pull_request_facts_incomplete"
        else:
            band, why = _band_of(size[0] + size[1]), None
        rows[band].append(((end - start).total_seconds() if start is not None else None,
                           reasons + ([why] if why else [])))
        if spans is not None:
            spans.append((start, end, reasons))
    bands = []
    for band, entries in rows.items():
        times = [seconds for seconds, _ in entries if seconds is not None]
        reasons = sorted({reason for _, why in entries for reason in why})
        bands.append(BandRow(
            band=band, items=len(entries), timed=len(times), median_seconds=median(times) if times else None,
            p85_seconds=_p85(times) if times else None, small_sample=0 < len(times) < SMALL_SAMPLE,
            coverage="full" if times and len(times) == len(entries) else ("partial" if times else "unavailable"),
            unknown_reasons=reasons))
    items = sum(row.items for row in bands)
    timed = sum(row.timed for row in bands)
    reasons = sorted({reason for row in bands for reason in row.unknown_reasons} | set(gaps))
    complete = timed == items and not rows["unknown"] and not gaps
    label = "full" if complete and items else ("unavailable" if not timed or membership == "unavailable"
                                               else "partial")
    return ItemTimeSection(items=items, timed=timed, coverage=label, unknown_reasons=reasons, bands=bands)


# #1453: the configuration sources of ``policy_change`` (the Supervisor's answers on #1246). The Supervisor's clock and
# registration transitions share the kind but are not settings, and a dispatch pause is an operator intervention.
_MARKER_SOURCES = ("agent_teams.update_github_scope", "agent_teams.update_github_scope_continuation_policy",
                   "agent_team_service.update_preset", "agent_team_service.update_work_settings")


async def _flow(db, campaign: Campaign, spans: list, as_of: datetime, gaps: list[str], membership: str) -> FlowSection:
    """Merges per UTC ISO week, each member once in its first merge's week, and before-and-after at each marker."""
    began = _naive_utc(campaign.started_at)
    ends = [end for _, end, _ in spans if end is not None]
    unknown_merges = len(spans) - len(ends)
    weeks, monday = [], (began - timedelta(days=began.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    while monday <= as_of:
        following = monday + timedelta(days=7)
        weeks.append(WeekRow(week_start=monday.date(), merges=sum(monday <= end < following for end in ends),
                             partial=began > monday or as_of < following))
        monday = following
    timed = [(start, end) for start, end, _ in spans if start is not None and end is not None]
    markers = []
    for at, source, fields in await _markers(db, campaign, began, as_of):
        before = [(end - start).total_seconds() for start, end in timed if end < at]
        after = [(end - start).total_seconds() for start, end in timed if start >= at]
        markers.append(ChangeMarker(at=at, source=source, fields=fields, before=_side(before), after=_side(after),
                                    straddling=sum(start < at <= end for start, end in timed),
                                    unknown=len(spans) - len(timed)))
    # Changes before audit recording began are unknown, so the marker list is only complete from its start on
    # (V2 review 473); and an item whose time is unknown leaves the comparison incomplete with its reason.
    recording = await audit.instrumentation_start(db)
    recording = recording and _naive_utc(recording)
    marker_gap = ("no_forward_coverage_marker" if recording is None
                  else "change_markers_before_audit_recording" if recording > began else None)
    reasons = sorted(set(gaps) | ({"merge_time_not_recorded"} if unknown_merges else set())
                     | {why for start, _, whys in spans if start is None for why in whys}
                     | ({marker_gap} if marker_gap else set()))
    label = "unavailable" if membership == "unavailable" else ("partial" if reasons else "full")
    return FlowSection(merges=len(ends), unknown_merges=unknown_merges, weeks=weeks, markers=markers,
                       marker_coverage="partial" if marker_gap else "full", coverage=label, unknown_reasons=reasons)


def _side(seconds: list[float]) -> MarkerSide:
    return MarkerSide(items=len(seconds), median_seconds=median(seconds) if seconds else None,
                      small_sample=0 < len(seconds) < SMALL_SAMPLE)


async def _markers(db, campaign: Campaign, began: datetime, as_of: datetime) -> list[tuple[datetime, str, list[str]]]:
    """The team lifetime's applied configuration changes in the campaign, with the names of the fields they changed.
    A save that changed nothing is not a change."""
    found = []
    for row in (await db.execute(
            select(FactoryAuditEvent.occurred_at, FactoryAuditEvent.source, FactoryAuditEvent.context_snapshot,
                   FactoryAuditEvent.before_values, FactoryAuditEvent.after_values)
            .where(FactoryAuditEvent.event_kind == "policy_change", FactoryAuditEvent.action_outcome == "applied",
                   FactoryAuditEvent.source.in_(_MARKER_SOURCES),
                   FactoryAuditEvent.team_context_key == campaign.team_context_key)
            .order_by(FactoryAuditEvent.occurred_at, FactoryAuditEvent.id))).all():
        at = _naive_utc(row.occurred_at)
        if not began <= at <= as_of:
            continue
        named = (row.context_snapshot or {}).get("scope_changed_fields")
        before, after = row.before_values or {}, row.after_values or {}
        fields = sorted(named) if named is not None else sorted(k for k in after if before.get(k) != after.get(k))
        if fields:
            found.append((at, row.source, fields))
    return found


async def _first_dispatch(db, live: dict, lifetime: str | None, marker: datetime | None, began: datetime,
                          end: datetime) -> tuple[datetime | None, list[str]]:
    """The first dispatch in the campaign of the lifetime that merged, only where the coverage marker spans the whole
    search window, from the later of campaign start and item creation to the merge; else unknown and why."""
    if marker is None:
        return None, ["no_forward_coverage_marker"]
    item = live.get(lifetime) if lifetime is not None else None
    if item is None:  # a lifetime without its live row: its creation, and so the window, is unknown
        return None, ["item_lifetime_unknown"]
    window = max(began, _naive_utc(item.created_at))
    if marker > window:
        return None, ["dispatch_before_instrumentation"]
    first = await db.scalar(select(func.min(FactoryAuditEvent.occurred_at)).where(
        FactoryAuditEvent.event_kind == "work_lifecycle", FactoryAuditEvent.item_context_key == lifetime,
        FactoryAuditEvent.after_values["dispatch_status"].as_string() == "dispatched",
        FactoryAuditEvent.occurred_at >= window, FactoryAuditEvent.occurred_at <= end))
    return (_naive_utc(first), []) if first is not None else (None, ["dispatch_not_recorded"])


@dataclass
class _Deliveries:
    delivered: dict[tuple[str, int], list[datetime]] = field(default_factory=dict)
    # #1246: each sourced merge with its PR artifact and the item lifetime that recorded it
    merges: dict[tuple[str, int], list[tuple[datetime, str | None, str | None]]] = field(default_factory=dict)
    untimed: dict[tuple[str, int], list[datetime]] = field(default_factory=dict)  # PR merges GitHub gave no time
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
               FactoryAuditEvent.context_snapshot, FactoryAuditEvent.fact_source, FactoryAuditEvent.item_context_key,
               GithubWorkItem.issue_number, TeamGithubScope.repo_owner,
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
        at = _naive_utc(row.fact_time or row.occurred_at)
        outcomes.setdefault(_identity(f"{owner}/{name}", number), []).append(at)
        if delivered and row.fact_source == "github_pull_request_merged" and row.fact_time is not None:
            facts.merges.setdefault(_identity(f"{owner}/{name}", number), []).append(
                (at, recorded.get("artifact") or None, row.item_context_key))
        elif delivered and row.fact_source == "github_pull_request_merged":  # placed by its record time only
            facts.untimed.setdefault(_identity(f"{owner}/{name}", number), []).append(at)
    legacy = (await db.execute(
        select(TeamGithubScope.repo_owner, TeamGithubScope.repo_name, GithubWorkItem.issue_number,
               GithubWorkItem.auto_merged_at, GithubWorkItem.id)
        .join(TeamGithubScope, TeamGithubScope.id == GithubWorkItem.scope_id)
        .join(FactoryContextKey, _lifetime(key))
        .where(GithubWorkItem.auto_merged_at.is_not(None)))).all()
    for row in legacy:
        key = _identity(f"{row.repo_owner}/{row.repo_name}", row.issue_number)
        facts.delivered.setdefault(key, []).append(_naive_utc(row.auto_merged_at))
        facts.merges.setdefault(key, []).append(
            (_naive_utc(row.auto_merged_at), None, await audit.current_context_key(db, "item", row.id)))
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
    return await db.scalar(select(CampaignSnapshot).where(CampaignSnapshot.campaign_id == campaign_id,
                                                          CampaignSnapshot.purpose == FINAL)
                           .order_by(CampaignSnapshot.version.desc()).limit(1))


async def read_bottlenecks(db, campaign: Campaign, client, *, previous_top: str | None) -> CampaignBottleneckResponse:
    if campaign.status == "finished":
        stored = await latest(db, campaign.id)
        snapshot = Snapshot.model_validate(stored.payload) if stored is not None else None
        source = "frozen" if stored is not None else "unavailable"
    else:
        snapshot = await build_snapshot(db, campaign, client)
        source = "live"

    rankings = snapshot.bottlenecks if snapshot is not None else None
    if rankings is None:
        start = snapshot.started_at if snapshot is not None else campaign.started_at
        end = snapshot.as_of if snapshot is not None else campaign.finished_at or start
        reason = "snapshot_without_bottlenecks" if snapshot is not None else "no_snapshot"
        rankings = build_bottlenecks([], start=_naive_utc(start), as_of=_naive_utc(end),
                                     group_coverage={"waiting": "unavailable", "process": "unavailable"},
                                     unknown_reasons=[reason])
    return CampaignBottleneckResponse(campaign_id=campaign.id, source=source,
                                      bottlenecks=rankings.with_previous_top(previous_top))


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


# #1260: the version that a Mail ask pins. An ask row is as immutable as a final one. Its version is negative (-1, -2,
# ...), so freeze (version 1) and recompute (the next final) never see it, and ``latest`` reads final rows only.
def _membership_part(membership) -> tuple:
    """What the membership observation says: the repositories read and whether completely, each member with its
    state (a closed issue is another measurement), and the departures."""
    return (membership.unavailable or "",
            tuple(sorted((r.repo.lower(), r.complete, r.reason or "") for r in membership.repositories)),
            tuple(sorted((_identity(m.repo, m.issue_number), m.state) for m in membership.members)),
            tuple(sorted((_identity(d.repo, d.issue_number), d.outcome, d.destination, d.reason or "",
                          _naive_utc(d.at).isoformat()) for d in membership.departures)))


def _rate_part(sample) -> tuple:
    """The measure's value, samples and coverage, and why it is unknown. A reason may start with the measured window
    (``START/END: text``): the end moves with the clock, so only the text counts."""
    reasons = (re.sub(r"^\d{4}-\d\d-\d\dT[0-9:.]+/\d{4}-\d\d-\d\dT[0-9:.]+: ", "", reason) for reason in sample.unknown_reasons)
    return (sample.value, sample.sample_count, sample.coverage, tuple(sorted(reasons)))


async def _rate_sample(db, campaign: Campaign) -> FactoryMetricSample:
    """The rate-limit measure now: recorded windows, gaps, the coverage start and the recorder's pending state all
    show in it, with no audit event (V1 review 129)."""
    return await _rate_limited(db, await upgrade.recorded_coverage(db, campaign), _naive_utc(campaign.started_at),
                               datetime.now(UTC).replace(tzinfo=None))


async def _identity_of(db, campaign: Campaign, membership, rate: tuple) -> str:
    """The identity of the records that a snapshot reads (V1 reviews 128 and 129): this team lifetime's database
    records, the one membership observation, and the rate-limit evidence. Nothing here reads GitHub."""
    records = await _fingerprint(db, campaign, scoped=True)
    return hashlib.sha256(repr((records, _membership_part(membership), rate)).encode()).hexdigest()


async def _live_identity(db, campaign: Campaign, client) -> tuple[str | None, str | None]:
    """The identity of the campaign's records now, and why it cannot be told. A membership that cannot be read
    completely gives no identity."""
    if client is None:
        return None, "membership_not_read"
    membership = await campaigns.members(db, campaign, client)
    if not membership.complete:
        return None, membership.unavailable or "membership_incomplete"
    return await _identity_of(db, campaign, membership, _rate_part(await _rate_sample(db, campaign))), None


async def record_fingerprint(db, campaign: Campaign, client=None) -> str | None:
    """The hash of what the snapshot reads, or None when the live membership cannot be read. No clock."""
    return (await _live_identity(db, campaign, client))[0]


async def _ask_row(db, campaign_id: int, fingerprint: str) -> CampaignSnapshot | None:
    return await db.scalar(select(CampaignSnapshot).where(
        CampaignSnapshot.campaign_id == campaign_id, CampaignSnapshot.purpose == ASK,
        CampaignSnapshot.fingerprint == fingerprint, CampaignSnapshot.calculation_version == CALCULATION_VERSION))


async def ask_version(db, campaign: Campaign, client) -> CampaignSnapshot:
    """The ask version for the campaign's records now: the stored one for the same records, else a new one.

    GitHub is read once. That one membership observation gives the identity and builds the snapshot, so the stored
    payload and its identity cannot disagree. When the observation is not complete, the new version is stored with an
    ``incomplete`` fingerprint that no later read matches: the ask reads as stale, never as current.
    Flushes and leaves the commit to the caller, so a failed Mail send rolls the new row back with it.
    """
    campaign_id = campaign.id
    membership = await campaigns.members(db, campaign, client)
    fingerprint = None
    if membership.complete:
        fingerprint = await _identity_of(db, campaign, membership, _rate_part(await _rate_sample(db, campaign)))
        found = await _ask_row(db, campaign_id, fingerprint)
        if found is not None:
            return found
    snapshot = await build_snapshot(db, campaign, client, as_of=datetime.now(UTC).replace(tzinfo=None), cached=False,
                                    membership=membership)
    payload = snapshot.model_dump(mode="json")
    stored = f"incomplete:{uuid.uuid4().hex}"
    if fingerprint is not None:
        # The identity of what was built: the same observation, the rate-limit measure in the payload.
        rate = next(m for m in snapshot.metrics if m.name == "rate_limited_seconds")
        stored = await _identity_of(db, campaign, membership, _rate_part(rate))
    for _attempt in range(_RECOMPUTE_ATTEMPTS):
        lowest = await db.scalar(select(func.min(CampaignSnapshot.version)).where(
            CampaignSnapshot.campaign_id == campaign_id)) or 0
        row = CampaignSnapshot(campaign_id=campaign_id, version=min(lowest, 0) - 1, calculation_version=CALCULATION_VERSION,
                               payload=payload, created_by="operator", purpose=ASK, fingerprint=stored)
        try:
            async with db.begin_nested():
                db.add(row)
                await db.flush()
            return row
        except IntegrityError:  # another ask took this version, or stored the same records first
            found = await _ask_row(db, campaign_id, stored)
            if found is not None:
                return found
    raise campaigns.CampaignError("campaign_snapshot_version_conflict")


async def current_version(db, campaign: Campaign, client=None) -> tuple[CampaignSnapshot | None, str | None]:
    """The campaign's current version and, when it cannot be told, why not. A frozen campaign's current version is
    its latest final row; a finished campaign without one is unknown (``final_version_missing``). A live campaign's
    is the ask row for its records and membership now, or None while no ask holds them."""
    final = await latest(db, campaign.id)
    if final is not None:
        return final, None
    if campaign.status == "finished":
        return None, "final_version_missing"
    if await audit.current_context_key(db, "team", campaign.preset_id) != campaign.team_context_key:
        return None, "team_lifetime_ended"
    fingerprint, reason = await _live_identity(db, campaign, client)
    if fingerprint is None:
        return None, reason
    return await _ask_row(db, campaign.id, fingerprint), None


_STAMP = r"\d{4}-\d\d-\d\dT[0-9:.]+"
# The two texts that ``_coverage`` writes with the calculation cutoff as the end of the window.
_GENERATED_REASON = re.compile(rf"^({_STAMP})/({_STAMP}): (no forward-coverage marker is installed|before instrumentation start)$")
_CUTOFF = "<cutoff>"


def _same_instant(text: str, other: str | None) -> bool:
    try:
        return other is not None and datetime.fromisoformat(text) == datetime.fromisoformat(other)
    except ValueError:
        return False


def _comparable(value, as_of: str | None, later_marker: bool, path: tuple = ()):
    """The payload as the comparison reads it: without the times that the calculation generates from its cutoff
    (V1 review 130). Those are every ``as_of`` key, the end of a coverage window in a generated unknown-reason text
    when it is the cutoff, and the start of the rolling four-hour bottleneck window (the cutoff minus four hours).
    Every time that was recorded is kept, also when it equals the cutoff."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "as_of" or (path == ("bottlenecks", "last_four_hours") and key == "started_at"):
                continue
            if key == "unknown_reasons" and isinstance(item, list):
                out[key] = [_generated_reason(text, as_of, later_marker) for text in item]
            else:
                out[key] = _comparable(item, as_of, later_marker, path + (key,))
        return out
    if isinstance(value, list):
        return [_comparable(item, as_of, later_marker, path) for item in value]
    return value


def _generated_reason(text, as_of: str | None, later_marker: bool):
    found = _GENERATED_REASON.match(text) if isinstance(text, str) else None
    if found is None or not _same_instant(found[2], as_of):
        return text
    # "before instrumentation start" has this form only when the recording marker is later than the cutoff. When a
    # marker equals the cutoff, the end is that recorded marker: it stays.
    if found[3] == "no forward-coverage marker is installed" or later_marker:
        return f"{found[1]}/{_CUTOFF}: {found[3]}"
    return text


def metrics_digest(payload: dict) -> str:
    """Whether two versions say the same: every reported measurement, with its counts and coverage. That is the
    metrics, the repository counts and the item time, time went, bottleneck and flow sections, and the calculation.
    Version metadata (ids) and the times generated from the cutoff are not compared; recorded times are."""
    as_of = payload.get("as_of")
    # The generated coverage-window text comes from the rate-limit recorder alone (``_rate_limited``), so only its
    # start decides how the text reads. Any other recorder's start is a recorded fact and is compared as it is
    # (V1 review 131).
    started = next((entry.get("started_at") for entry in payload.get("recorded_coverage") or []
                    if entry.get("measure") == "rate_limit_windows"), None)
    try:
        cutoff = datetime.fromisoformat(as_of) if as_of else None
        later_marker = cutoff is not None and started is not None and datetime.fromisoformat(started) > cutoff
    except ValueError:
        later_marker = False
    body = _comparable(payload, as_of, later_marker)
    body.pop("campaign_id", None)
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
