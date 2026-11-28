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

import math
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import groupby
from statistics import median

from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.engine import Row
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
from app.services.github_check_observation import ACTIONS_APP_ID
from app.services.github_rate_limit_history import campaign_rate_limit_time
from app.services.retro_bottleneck_service import (
    CampaignBottleneckResponse, CampaignBottlenecks, RankingAttempt, build_bottlenecks,
)

# v5 (#1257): bottleneck windows; v6 (#1247): CI and friction
CALCULATION_VERSION = "campaign-snapshot-v6"
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


class CheckIdentity(BaseModel):
    repository: str
    app_id: int | None
    app_slug: str | None
    check_name: str


class CheckMeasureRow(CheckIdentity):
    median_seconds: FactoryMetricSample
    p85_seconds: FactoryMetricSample
    queue_seconds: FactoryMetricSample
    runner_seconds: FactoryMetricSample


class CheckOutcomeRow(CheckIdentity):
    reruns: FactoryMetricSample
    flaky_heads: FactoryMetricSample


class QueueLoadSection(BaseModel):
    quiet_queue_seconds: FactoryMetricSample
    loaded_queue_seconds: FactoryMetricSample


class CiSection(BaseModel):
    observed_checks: FactoryMetricSample
    required_checks: list[CheckMeasureRow]
    non_required_checks: list[CheckMeasureRow]
    check_outcomes: list[CheckOutcomeRow]
    slowest_required_check: CheckMeasureRow | None
    item_time_share: FactoryMetricSample
    queue_under_load: QueueLoadSection


class OutsideMerge(BaseModel):
    artifact: str
    merged_by: str | None
    merged_by_type: str | None


class FrictionSection(BaseModel):
    hand_merges: FactoryMetricSample
    outside_merges: list[OutsideMerge]
    decision_waits: FactoryMetricSample
    decision_wait_seconds: FactoryMetricSample
    rate_limited_time: FactoryMetricSample
    agent_errors: FactoryMetricSample


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
    ci: CiSection | None = None  # additive (#1247)
    friction: FrictionSection | None = None


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
    scopes = tuple((await db.execute(select(TeamGithubScope.id, TeamGithubScope.repo_owner, TeamGithubScope.repo_name,
                                            TeamGithubScope.created_at)
                                     .join(FactoryContextKey, _lifetime(campaign.team_context_key))
                                     .order_by(TeamGithubScope.id))).all())
    items = tuple((await db.execute(select(GithubWorkItem.id, GithubWorkItem.dispatch_status, GithubWorkItem.updated_at,
                                           GithubWorkItem.auto_merged_at, GithubWorkItem.pr_number,
                                           GithubWorkItem.created_at)
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
               GithubCheckRunHistory.required, GithubCheckRunHistory.scope_id, GithubCheckRunHistory.app_id,
               GithubCheckRunHistory.conclusion, GithubCheckRunHistory.observed_at, GithubCheckRunHistory.job_created_at,
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
               GithubRequiredCheckWatch.scope_revision, GithubRequiredCheckWatch.observed_at)
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
                         cached: bool = True) -> Snapshot:
    """The campaign's snapshot up to ``as_of``; served from the cache while no record it reads has changed.

    Omit ``as_of`` for a current read that can reuse a fresh, matching live cutoff.
    ``observed_at`` is when the caller reads GitHub, ``as_of`` by default. GitHub reports the membership as it is
    then, so a read after the cutoff leaves the historical membership unknown (V1 review 6079984028).
    """
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
    friction = snapshot.friction.model_copy(update={"rate_limited_time": fresh}) if snapshot.friction else None
    refreshed = snapshot.model_copy(update={"metrics": metrics, "friction": friction})
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
    rate_limit = await _rate_limited(db, coverage, started, as_of)
    metrics.append(rate_limit)
    # Missing delivery history the counts already know of makes item time partial too (V1 review 448).
    item_time = await _item_time(db, campaign, list(members), facts, coverage, as_of,
                                 gaps + ([reason] if unknown else []), label)  # both kept (V1 review 115)
    time_went, bottlenecks, wait_durations, required_spans = await _time_went(
        db, campaign, list(members), coverage, as_of, gaps)
    ci = await _ci_measures(db, campaign, members, facts, coverage, as_of, gaps, time_went, required_spans)
    friction = await _friction_measures(db, campaign, members, facts, coverage, as_of, gaps,
                                        [reason] if unknown else [], time_went, wait_durations, rate_limit)
    return Snapshot(
        campaign_id=campaign.id, selector=campaign.selector, started_at=started, as_of=as_of,
        calculation_version=CALCULATION_VERSION, metrics=sorted(metrics, key=lambda sample: sample.name),
        recorded_coverage=[MeasureCoverageEntry(measure=c.measure, started_at=c.started_at, covered=c.covered,
                                                reason=c.reason) for c in coverage], item_time=item_time,
        time_went=time_went, bottlenecks=bottlenecks, ci=ci, friction=friction,
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
                     gaps: list[str]) -> tuple[TimeWentSection, CampaignBottlenecks, list[float], int]:
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
    wait_durations: list[float] = []
    required_spans = 0
    for attempt in attempts:
        wait_durations.extend((span.end - span.start).total_seconds() for span in attempt.intervals
                              if span.group == "waiting" and span.stage != "dispatch")
        required_spans += sum(span.stage == "required_check" for span in attempt.intervals)
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
    return section, rankings, wait_durations, required_spans


def _p85(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.85 * len(ordered)) - 1]


def _band_of(lines: int) -> str:
    return next(name for name, upper in BANDS if upper is None or lines <= upper)


async def _item_time(db, campaign: Campaign, members: list[tuple[str, int]], facts: _Deliveries, coverage,
                     as_of: datetime, gaps: list[str], membership: str) -> ItemTimeSection:
    """Each member delivered by the cutoff, from its evidenced first dispatch to its earliest merge, by size band."""
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


# CI and friction (#1247)
def _retro_population_sample(name: str, unit: str, value: float, count: int, source: str, gaps: list[str],
                             *, missing: int = 0) -> FactoryMetricSample:
    # A positive observed population may be a lower bound. Zero requires complete coverage.
    known = value > 0 or not gaps
    return _sample(name, unit, value if known else None, count, source=source, reasons=gaps,
                   coverage=("partial" if gaps else "full") if known else "unavailable", unknown=missing or None)


def _retro_stat(name: str, unit: str, values: list[float], source: str, gaps: list[str],
                *, reduce: Callable[[list[float]], float] = median, missing: int = 0) -> FactoryMetricSample:
    return _sample(name, unit, reduce(values) if values else None, len(values), source=source,
                   reasons=gaps or ([] if values else ["no_observations"]),
                   coverage=("partial" if gaps else "full") if values else "unavailable", unknown=missing or None)


def _recording_gaps(coverage: tuple[upgrade.MeasureCoverage, ...], measure: str) -> list[str]:
    return [c.reason for c in coverage if c.measure == measure and c.reason]


def _pull_identity(artifact: str | None) -> tuple[str, int] | None:
    repo, _, number = (artifact or "").lower().rpartition("/pull/")
    return (repo, int(number)) if repo and number.isascii() and number.isdigit() else None


async def _campaign_checks(db, campaign: Campaign, members, facts: _Deliveries,
                           began: datetime, as_of: datetime) -> tuple[list[Row], int, bool]:
    """Use the tracked PR, earlier watched PRs and durable deliveries. A run has one recorded scope and PR."""
    items = (await db.execute(
        select(TeamGithubScope.id.label("scope_id"), TeamGithubScope.repo_owner, TeamGithubScope.repo_name,
               TeamGithubScope.created_at,
               GithubWorkItem.issue_number, GithubWorkItem.pr_number, GithubRequiredCheckWatch.pr_number.label("watched"))
        .join(FactoryContextKey, _lifetime(campaign.team_context_key))
        .outerjoin(GithubWorkItem, (GithubWorkItem.scope_id == TeamGithubScope.id) & (GithubWorkItem.created_at <= as_of))
        .outerjoin(GithubRequiredCheckWatch, (GithubRequiredCheckWatch.work_item_id == GithubWorkItem.id)
                   & (GithubRequiredCheckWatch.observed_at <= as_of)))).all()
    scopes: dict[int, tuple[str, datetime]] = {}
    pulls: dict[tuple[int, int], set[tuple[str, int]]] = defaultdict(set)
    for row in items:
        repo = f"{row.repo_owner}/{row.repo_name}".lower()
        scopes[row.scope_id] = (repo, row.created_at)
        for pr in {row.pr_number, row.watched} - {None}:
            pulls[row.scope_id, pr].add((repo, row.issue_number))
    # The artifact supplies the PR number even if its tracked row or watch was removed.
    scopes_by_repo: dict[str, list[int]] = defaultdict(list)
    for scope_id, (repo, _) in scopes.items():
        scopes_by_repo[repo].append(scope_id)
    for member in members:
        for at, artifact, _ in facts.merges.get(member, ()):
            pull = _pull_identity(artifact)
            if pull and at <= as_of and pull[0] == member[0]:
                scope_ids = scopes_by_repo.get(pull[0], ())
                if len(scope_ids) == 1:
                    pulls[scope_ids[0], pull[1]].add(member)
    boundary = func.coalesce(GithubCheckRunHistory.completed_at, GithubCheckRunHistory.observed_at)
    first_time = func.coalesce(GithubCheckRunHistory.job_created_at, GithubCheckRunHistory.started_at,
                               GithubCheckRunHistory.observed_at)
    # Column reads see committed replacements without refreshing objects the caller may hold.
    rows = (await db.execute(select(*GithubCheckRunHistory.__table__.c)
                             .where(GithubCheckRunHistory.scope_id.in_(scopes), boundary >= began,
                                    first_time <= as_of)
                             .order_by(GithubCheckRunHistory.repository, GithubCheckRunHistory.check_run_id))).all()
    runs, unknown, future_process_evidence = [], 0, False
    for run in rows:
        repo, scope_started = scopes[run.scope_id]
        if repo != run.repository.lower() or run.observed_at < scope_started:
            continue  # history retains numeric scope ids after deletion
        candidates = pulls.get((run.scope_id, run.pr_number), set()) if run.pr_number is not None else set()
        matching = not candidates.isdisjoint(members)
        if run.observed_at > as_of or (run.completed_at is not None and run.completed_at > as_of):
            future_process_evidence |= matching and run.required
        elif len(candidates) == 1 and matching and run.check_name and (run.app_id is not None or run.app_slug):
            runs.append(run)
        elif matching or run.pr_number is None:
            unknown += 1
    return runs, unknown, future_process_evidence


def _elapsed(start: datetime | None, end: datetime | None) -> float | None:
    return (_naive_utc(end) - _naive_utc(start)).total_seconds() if start is not None and end is not None else None


def _is_actions(run: Row) -> bool:
    return run.app_id == ACTIONS_APP_ID and run.app_slug == "github-actions"


def _check_queue(run: Row) -> float | None:
    return _elapsed(run.job_created_at, run.started_at) if _is_actions(run) else None


def _check_row(identity: CheckIdentity, runs: list[Row], gaps: list[str]) -> CheckMeasureRow:
    source = "github_check_run_history"
    durations = [v for run in runs if (v := _elapsed(run.started_at, run.completed_at)) is not None and v >= 0]
    queues = [v for run in runs if (v := _check_queue(run)) is not None and v >= 0]
    missing_runtime = len(runs) - len(durations)
    runtime_gaps = gaps + (["runtime_not_recorded"] if missing_runtime else [])
    queue_gaps = gaps + (["ci_does_not_report_queue_time"] if identity.app_slug and not any(_is_actions(r) for r in runs)
                         else ["queue_time_not_recorded"] if len(queues) < len(runs) else [])
    return CheckMeasureRow(
        **identity.model_dump(),
        median_seconds=_retro_stat("median_seconds", "second", durations, source, runtime_gaps, missing=missing_runtime),
        p85_seconds=_retro_stat("p85_seconds", "second", durations, source, runtime_gaps, reduce=_p85,
                                missing=missing_runtime),
        runner_seconds=_retro_stat("runner_seconds", "second", durations, source, runtime_gaps, reduce=sum,
                                   missing=missing_runtime),
        queue_seconds=_retro_stat("median_queue_seconds", "second", queues, source, queue_gaps,
                                  missing=len(runs) - len(queues)))


def _check_outcomes(identity: CheckIdentity, runs: list[Row], gaps: list[str]) -> CheckOutcomeRow:
    """One sequence per PR head, across observed policy changes and confined to one check app."""
    heads: dict[tuple[int | None, str], list[Row]] = defaultdict(list)
    for run in runs:
        heads[run.pr_number, run.head_sha].append(run)
    reruns = sum(len(values) - 1 for values in heads.values())
    flaky = incomplete = 0
    for values in heads.values():
        failures = [r.completed_at for r in values if r.conclusion in ("failure", "timed_out") and r.completed_at]
        successes = [r.completed_at for r in values if r.conclusion == "success" and r.completed_at]
        if failures and successes and min(failures) < max(successes):
            flaky += 1
        elif (any(not r.conclusion or r.completed_at is None for r in values)
              or (failures and successes and min(failures) == max(successes))):
            incomplete += 1
    reasons = gaps + (["conclusion_or_order_not_recorded"] if incomplete else [])
    source = "github_check_run_history"
    return CheckOutcomeRow(
        **identity.model_dump(),
        reruns=_retro_population_sample("reruns", "check_run", reruns, len(runs), source, gaps),
        flaky_heads=_retro_population_sample("flaky_heads", "pull_request_head", flaky, len(heads) - incomplete,
                                             source, reasons, missing=incomplete))


def _queue_under_load(runs: list[Row], gaps: list[str]) -> QueueLoadSection:
    """Compare Actions queues with one versus at least three recorded overlapping PRs in the same repository."""
    events: dict[str, list[tuple[datetime, bool, int, float]]] = defaultdict(list)
    missing = 0
    for run in runs:
        if not _is_actions(run):
            continue
        queue = _check_queue(run)
        queued, started, completed = run.job_created_at, run.started_at, run.completed_at
        if (queue is None or queue < 0 or queued is None or started is None or completed is None
                or started > completed or queued >= completed or run.pr_number is None):
            missing += 1
            continue
        events[run.repository.lower()].extend(((queued, True, run.pr_number, queue),
                                              (completed, False, run.pr_number, queue)))
    quiet, loaded = [], []
    for repo_events in events.values():
        active: Counter[int] = Counter()
        # Remove completed jobs and add all new arrivals before sampling load at this timestamp.
        for _, at_time in groupby(sorted(repo_events), key=lambda event: event[0]):
            arrivals = []
            for _, arriving, pr, seconds in at_time:
                if arriving:
                    active[pr] += 1
                    arrivals.append(seconds)
                else:
                    active[pr] -= 1
                    if not active[pr]:
                        del active[pr]
            if len(active) == 1:
                quiet.extend(arrivals)
            elif len(active) >= 3:
                loaded.extend(arrivals)
    load_gaps = gaps + (["queue_or_runtime_not_recorded"] if missing else [])
    source = "inferred_from_github_actions_pr_overlap"
    return QueueLoadSection(
        quiet_queue_seconds=_retro_stat("quiet_queue_seconds", "second", quiet, source, load_gaps, missing=missing),
        loaded_queue_seconds=_retro_stat("loaded_queue_seconds", "second", loaded, source, load_gaps, missing=missing))


async def _ci_measures(db, campaign, members, facts, coverage, as_of, gaps, time_went: TimeWentSection,
                       required_spans: int) -> CiSection:
    runs, unknown, future_process_evidence = await _campaign_checks(
        db, campaign, members, facts, _naive_utc(campaign.started_at), as_of)
    check_gaps = gaps + _recording_gaps(coverage, "check_runs") + (["check_attribution_missing"] if unknown else [])
    groups: dict[tuple[str, str, int | None, str | None], list[Row]] = defaultdict(list)
    for run in runs:
        if run.check_name:
            app_slug = run.app_slug if run.app_id is None else None
            groups[run.repository.lower(), run.check_name, run.app_id, app_slug].append(run)
    required: list[CheckMeasureRow] = []
    optional: list[CheckMeasureRow] = []
    outcomes = []
    for (repo, name, app_id, _), checks in sorted(groups.items(), key=lambda pair: str(pair[0])):
        slug = next((run.app_slug for run in checks if run.app_slug), None)
        identity = CheckIdentity(repository=repo, check_name=name, app_id=app_id, app_slug=slug)
        for is_required, output in ((True, required), (False, optional)):
            cohort = [run for run in checks if run.required == is_required]
            if cohort:
                output.append(_check_row(identity, cohort, check_gaps))
        outcomes.append(_check_outcomes(identity, checks, check_gaps))
    timed = [(row.median_seconds.value, row) for row in required if row.median_seconds.value is not None]
    slowest = max(timed, key=lambda pair: pair[0])[1] if timed else None
    share_gaps = set(check_gaps) | set(time_went.unknown_reasons)
    if not required:
        share_gaps.add("no_required_check_observations")
    if future_process_evidence:
        # The stage collector clips intervals to the attempt. Later observations must not inform an earlier share.
        share_gaps.add("required_check_evidence_after_cutoff")
    if any(row.median_seconds.unknown_count for row in required):
        share_gaps.add("required_check_runtime_not_recorded")
    if required_spans != sum(run.required for run in runs):
        share_gaps.add("required_checks_not_reconciled_with_attempt_intervals")
    total = time_went.total_seconds
    share = None
    if total and not share_gaps and time_went.coverage == time_went.group_coverage["process"] == "full":
        share = sum(row.share_seconds for row in time_went.stages if row.stage == "required_check") / total
    return CiSection(
        observed_checks=_retro_population_sample("observed_checks", "check_run", len(runs), len(runs),
                                                 "github_check_run_history", check_gaps, missing=unknown),
        required_checks=required, non_required_checks=optional, check_outcomes=outcomes, slowest_required_check=slowest,
        item_time_share=_sample("ci_item_time_share", "fraction", share, time_went.items, source="campaign_time_went",
                                reasons=sorted(share_gaps) if share_gaps else
                                (["item_time_unavailable"] if share is None else []),
                                coverage=("full" if share is not None else
                                          "partial" if time_went.coverage == "partial" else "unavailable"),
                                unknown=time_went.unknown_items or None),
        queue_under_load=_queue_under_load(runs, check_gaps))


_BLOCKING_AUDIT_OUTCOMES = ("rejected", "refused", "blocked", "error", "failed")


async def _friction_measures(db, campaign, members, facts, coverage, as_of, gaps, delivery_gaps,
                             time_went: TimeWentSection, wait_durations: list[float],
                             rate_limit: FactoryMetricSample) -> FrictionSection:
    """Count recorded member failures and outside merges, and use the attributed decision-wait intervals."""
    began = _naive_utc(campaign.started_at)
    ledger = FactoryAuditEvent
    at = func.coalesce(ledger.fact_time, ledger.occurred_at)
    rows = (await db.execute(select(ledger.event_kind, ledger.context_snapshot, ledger.scope_context_key,
                                    ledger.after_values, ledger.action_outcome)
                             .where(ledger.team_context_key == campaign.team_context_key, ledger.occurred_at <= as_of,
                                    at >= began, at <= as_of,
                                    or_(ledger.event_kind.in_((audit.PULL_FACTS_KIND, audit.MERGE_PATH_KIND)),
                                        (ledger.actor_kind == "member")
                                        & ledger.action_outcome.in_(_BLOCKING_AUDIT_OUTCOMES)))
                             .order_by(ledger.id))).all()
    observations: dict[tuple[str, int], dict[str | None, dict]] = defaultdict(dict)
    settlements = {}
    error_count = error_unknown = 0
    for row in rows:
        context = row.context_snapshot or {}
        identity = _pull_identity(context.get("artifact"))
        values = row.after_values or {}
        if row.event_kind == audit.PULL_FACTS_KIND and identity is not None:
            observations[identity][row.scope_context_key] = values
        elif row.event_kind == audit.MERGE_PATH_KIND and identity is not None:
            settlements[identity, row.scope_context_key] = values.get("merge_path")
        elif row.action_outcome in _BLOCKING_AUDIT_OUTCOMES:
            owner, name, issue = context.get("repo_owner"), context.get("repo_name"), context.get("issue_number")
            if owner and name and isinstance(issue, int):
                error_count += _identity(f"{owner}/{name}", issue) in members
            else:
                error_unknown += 1
    merges = {}
    unknown = int(facts.unidentified)
    for member in members:
        unknown += sum(began <= at <= as_of for at in facts.untimed.get(member, ()))
        for when, artifact, _ in facts.merges.get(member, ()):
            if began <= when <= as_of:
                identity = _pull_identity(artifact)
                if identity is not None and identity[0] == member[0]:
                    merges[identity] = artifact
                else:
                    unknown += 1
    outside = []
    classified = 0
    for identity, artifact in sorted(merges.items()):
        paths = {}
        for scope, values in observations[identity].items():
            path = settlements.get((identity, scope), values.get("merge_path"))
            if path in ("automatic", "outside"):
                paths[path] = values
        if len(paths) != 1:
            unknown += 1
            continue
        classified += 1
        if "outside" in paths:
            values = paths["outside"]
            outside.append(OutsideMerge(artifact=artifact, merged_by=values.get("merged_by"),
                                        merged_by_type=values.get("merged_by_type")))
    merge_gaps = [*gaps, *delivery_gaps, *_recording_gaps(coverage, "merge_path"),
                  *_recording_gaps(coverage, "pull_request_facts")]
    if unknown:
        merge_gaps.append("merge_path_unknown")
    wait_gaps = gaps + _recording_gaps(coverage, "decision_waits")
    if members and (time_went.unknown_items or time_went.group_coverage["waiting"] != "full"):
        wait_gaps.append("wait_attribution_incomplete")
    wait_gaps.extend(reason for reason in time_went.unknown_reasons
                     if reason in ("no_forward_coverage_marker", "dispatch_before_instrumentation"))
    marker = await audit.instrumentation_start(db)
    _, _, error_gaps = _coverage(marker and _naive_utc(marker), began, as_of)
    error_gaps = gaps + error_gaps + (["blocking_audit_event_unattributed"] if error_unknown else [])
    return FrictionSection(
        hand_merges=_retro_population_sample("hand_merges", "pull_request", len(outside), classified,
                                             "pull_request_facts", merge_gaps, missing=unknown),
        outside_merges=outside,
        decision_waits=_retro_population_sample("decision_waits", "wait", len(wait_durations), len(wait_durations),
                                                "campaign_attempt_waits", wait_gaps),
        decision_wait_seconds=_retro_population_sample("decision_wait_seconds", "second", sum(wait_durations),
                                                       len(wait_durations), "campaign_attempt_waits", wait_gaps),
        rate_limited_time=rate_limit,
        agent_errors=_retro_population_sample("agent_errors", "audit_event", error_count, error_count,
                                              "factory_audit_events", error_gaps, missing=error_unknown))


# Frozen snapshots
async def latest(db, campaign_id: int) -> CampaignSnapshot | None:
    return await db.scalar(select(CampaignSnapshot).where(CampaignSnapshot.campaign_id == campaign_id)
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
