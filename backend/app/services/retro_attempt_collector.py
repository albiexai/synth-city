"""#1417 (#1245 part 2): attempt windows and the waiting-on-someone and active collectors.

Read-only. Each dispatch attempt of a team's items becomes an ``AttemptWindow`` and ``StageInterval``s for
``retro_stage_engine.breakdown``. The process collectors are #1418, and the snapshot section is #1419.

The rules are the Supervisor's design answers on #1245 and the approved plan (2026-11-27):
- An attempt is a ``work_lifecycle`` dispatched event, named by its launch key. Its window runs from the previous
  attempt's end (the first attempt's from campaign membership, labelled) to its merge fact, its failed event, the next
  dispatch or ``as_of``.
- The ledger never holds a nonce. A nonce maps to a launch only on persisted evidence: the item row's current nonce
  and launch, or an audit event whose revision row holds the nonce and whose attempt key names that revision and
  launch. Missing or conflicting evidence leaves every row of the nonce unattributed; nothing is placed by its time.
- Effort and pause waits carry neither a nonce nor a launch key, so they fall in the attempt containing their start.
- Mail and input waits (Q4) go to the one open attempt whose owner sent the request; otherwise they are unattributed.
- Every interval is clipped to its attempt's window, and the part outside it is unattributed.
- Everything, attributed or not, is clipped to the campaign period ``[campaign_start, as_of]``; what lies wholly
  outside it is left out, and an attempt that ended before the campaign is not shown (V1 review 431).
- A head is a (PR, SHA) pair; a changes-required verdict, on the PR or on a revision, ends at a different commit.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select

from app.models.database import (
    FactoryAuditEvent,
    GithubApprovalRequest,
    GithubAttemptScopeRevision,
    GithubCheckRunHistory,
    GithubHeadReview,
    GithubRequiredCheckWatch,
    GithubRevisionReview,
    GithubWorkItem,
    TeamGithubScope,
)
from app.services import factory_audit_service as audit
from app.services.retro_stage_engine import AttemptWindow, StageInterval

FIRST_LABEL = "from campaign membership; ready time not recorded"
ITEM_WAITS = ("decision", "effort", "ceiling", "hold", "pause")
TEAM_WAITS = ("mail", "input")


@dataclass(frozen=True)
class Unattributed:
    source: str  # a stage, or the table of a nonce-linked row
    item_id: int | None
    start: datetime | None
    end: datetime | None
    # attempt_unproven, attempt_conflict, attempt_not_recorded, request_not_recorded, outside_attempts,
    # before_attempt_start, after_attempt_end, no_open_attempt, several_open_attempts
    reason: str


@dataclass
class CollectedAttempt:
    item_id: int
    launch_attempt: str
    dispatched_at: datetime
    window: AttemptWindow
    intervals: list[StageInterval] = field(default_factory=list)
    owner_member_id: int | None = None


@dataclass
class TeamAttempts:
    attempts: list[CollectedAttempt] = field(default_factory=list)
    unattributed: list[Unattributed] = field(default_factory=list)


def _naive(value: datetime | None) -> datetime | None:
    return value.astimezone(UTC).replace(tzinfo=None) if value is not None and value.tzinfo else value


def _when(event: FactoryAuditEvent) -> datetime:
    return _naive(event.fact_time or event.occurred_at)


def _lose(out: TeamAttempts, period, source: str, item_id: int | None, start: datetime | None, end: datetime | None,
          reason: str) -> None:
    """An unattributed span, clipped to the campaign period; one wholly outside it is left out."""
    low, high = period
    start, end = max(start or low, low), high if end is None else min(end, high)
    if start < end:
        out.unattributed.append(Unattributed(source, item_id, start, end, reason))


def _place(out: TeamAttempts, period, attempt: CollectedAttempt, group: str, stage: str, actor: str,
           start: datetime, end: datetime | None) -> None:
    """Clip to the period and the attempt's window; an unended interval runs to ``as_of``, the rest is unattributed."""
    end = period[1] if end is None else min(end, period[1])  # what precedes the campaign is dropped by _lose
    if end <= start:
        return
    window = attempt.window
    if start < window.start:
        _lose(out, period, stage, attempt.item_id, start, min(end, window.start), "before_attempt_start")
    if end > window.end:
        _lose(out, period, stage, attempt.item_id, max(start, window.end), end, "after_attempt_end")
    if max(start, window.start) < min(end, window.end):
        attempt.intervals.append(StageInterval(group, stage, actor, max(start, window.start), min(end, window.end)))


def _ends(events) -> dict[str, datetime]:
    ends: dict[str, datetime] = {}
    for event in events:
        if event.event_kind.endswith("_wait_ended") and event.operation_id:
            ends[event.operation_id] = min(ends.get(event.operation_id, _when(event)), _when(event))
    return ends


def _end_of(start: FactoryAuditEvent, family: str, ends: dict[str, datetime]) -> datetime | None:
    op = start.operation_id or ""
    return ends.get(op.replace(f"{family}_wait_started", f"{family}_wait_ended", 1)) if op else None


async def collect_team_attempts(db, preset_id: int, *, campaign_start: datetime, as_of: datetime) -> TeamAttempts:
    """Every recorded attempt of the team's items, with its window and intervals, and what could not be attributed."""
    campaign_start, as_of = _naive(campaign_start), _naive(as_of)
    out, period = TeamAttempts(), (campaign_start, as_of)
    items = (await db.scalars(select(GithubWorkItem).join(TeamGithubScope, TeamGithubScope.id == GithubWorkItem.scope_id)
                              .where(TeamGithubScope.preset_id == preset_id).order_by(GithubWorkItem.id))).all()
    for item in items:
        await _collect_item(db, item, out, period)
    team_key = await audit.current_context_key(db, "team", preset_id)
    events = (await db.scalars(select(FactoryAuditEvent).where(
        FactoryAuditEvent.team_context_key == team_key, FactoryAuditEvent.team_context_key.is_not(None),
        FactoryAuditEvent.event_kind.in_([f"{f}_wait_{s}" for f in TEAM_WAITS for s in ("started", "ended")]))
        .order_by(FactoryAuditEvent.occurred_at, FactoryAuditEvent.id))).all()
    ends = _ends(events)
    for start in (e for e in events if e.event_kind.endswith("_wait_started")):
        family, values, at = start.event_kind.removesuffix("_wait_started"), start.after_values or {}, _when(start)
        owner = values.get("owner_member_id")
        open_ = [a for a in out.attempts
                 if owner is not None and a.owner_member_id == owner and a.dispatched_at <= at < a.window.end]
        if len(open_) != 1:
            _lose(out, period, family, None, at, _end_of(start, family, ends),
                  "several_open_attempts" if open_ else "no_open_attempt")
            continue
        actor = (f"member:{values['awaited_member_id']}" if values.get("awaited_member_id") is not None
                 else f"actor:{values['awaited_actor_id']}" if values.get("awaited_actor_id") is not None else "unknown")
        _place(out, period, open_[0], "waiting", family, actor, at, _end_of(start, family, ends))
    return out


async def _collect_item(db, item: GithubWorkItem, out: TeamAttempts, period):
    campaign_start, as_of = period
    item_key = await audit.current_context_key(db, "item", item.id)
    if item_key is None:
        return
    events = (await db.scalars(select(FactoryAuditEvent).where(FactoryAuditEvent.item_context_key == item_key)
                               .order_by(FactoryAuditEvent.occurred_at, FactoryAuditEvent.id))).all()

    def lifecycle(event, status):
        return event.event_kind == "work_lifecycle" and (event.after_values or {}).get("dispatch_status") == status

    # The attempts and their windows (Q1)
    boundary: dict[str, datetime] = {}
    for event in events:
        launch = (event.context_snapshot or {}).get("launch_attempt")
        merged = (event.event_kind == "delivery_evidence" and event.delivery_outcome == "delivered"
                  and event.fact_source == "github_pull_request_merged" and event.fact_time is not None)
        if launch and (merged or lifecycle(event, "failed")):
            boundary[launch] = min(boundary.get(launch, _when(event)), _when(event))
    launches = [e for e in events if lifecycle(e, "dispatched") and _naive(e.occurred_at) <= as_of]
    attempts: list[CollectedAttempt] = []
    start = min(max(campaign_start, _naive(item.created_at)), _naive(launches[0].occurred_at)) if launches else None
    for n, event in enumerate(launches):
        key, dispatched = event.context_snapshot["launch_attempt"], _naive(event.occurred_at)
        following = _naive(launches[n + 1].occurred_at) if n + 1 < len(launches) else as_of
        end = max(dispatched, min(following, as_of, boundary.get(key, as_of)))
        shown = max(start, campaign_start)  # an attempt that ended before the campaign keeps its own window, unshown
        window = AttemptWindow(shown, end, FIRST_LABEL if n == 0 or start < campaign_start else None) if (
            end > campaign_start or dispatched >= campaign_start) else AttemptWindow(start, end)
        attempt = CollectedAttempt(item.id, key, dispatched, window)
        if shown < dispatched:
            attempt.intervals.append(StageInterval("waiting", "dispatch", "dispatch", window.start, dispatched))
        attempts.append(attempt)
        start = end
    out.attempts.extend(a for a in attempts if a.window.end > campaign_start or a.dispatched_at >= campaign_start)
    by_key = {a.launch_attempt: a for a in attempts}

    # Nonce to launch, on persisted evidence only
    revisions = (await db.scalars(select(GithubAttemptScopeRevision)
                                  .where(GithubAttemptScopeRevision.work_item_id == item.id))).all()
    revision_nonce = {r.id: r.dispatch_nonce for r in revisions}
    claims: dict[str, set[str]] = defaultdict(set)
    if item.dispatch_nonce and item.launch_id is not None:
        claims[item.dispatch_nonce].add(audit.launch_attempt_key(item.id, item.launch_id))
    for event in events:
        snapshot = event.context_snapshot or {}
        launch = snapshot.get("launch_attempt")
        if (event.revision_id in revision_nonce and launch
                and snapshot.get("attempt") == f"{launch}:revision:{event.revision_id}"):
            claims[revision_nonce[event.revision_id]].add(launch)
    claimants: dict[str, set[str]] = defaultdict(set)
    for nonce, launches_claimed in claims.items():
        for launch in launches_claimed:
            claimants[launch].add(nonce)

    def mapped(nonce: str) -> tuple[CollectedAttempt | None, str]:
        found = claims.get(nonce, set())
        if not found:
            return None, "attempt_unproven"
        if len(found) > 1 or len(claimants[next(iter(found))]) > 1:
            return None, "attempt_conflict"
        attempt = by_key.get(next(iter(found)))
        return attempt, "attempt_not_recorded"

    requests = (await db.scalars(select(GithubApprovalRequest).where(GithubApprovalRequest.work_item_id == item.id)
                                 .order_by(GithubApprovalRequest.created_at, GithubApprovalRequest.id))).all()
    reviews = (await db.scalars(select(GithubHeadReview).where(GithubHeadReview.work_item_id == item.id)
                                .order_by(GithubHeadReview.created_at, GithubHeadReview.id))).all()
    watches = (await db.scalars(select(GithubRequiredCheckWatch)
                                .where(GithubRequiredCheckWatch.work_item_id == item.id))).all()
    verdicts = (await db.scalars(select(GithubRevisionReview).where(GithubRevisionReview.work_item_id == item.id)
                                 .order_by(GithubRevisionReview.created_at, GithubRevisionReview.id))).all()
    rows: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for source, table, span in (("approval_request", requests, lambda r: (r.created_at, r.decided_at)),
                                ("head_review", reviews, lambda r: (r.created_at, r.ended_at)),
                                ("check_watch", watches, lambda r: (r.changed_at, r.completed_at))):
        for row in table:
            attempt, reason = mapped(row.dispatch_nonce)
            if attempt is None:
                _lose(out, period, source, item.id, *map(_naive, span(row)), reason)
            else:
                rows[attempt.launch_attempt][source].append(row)
    # A revision verdict counts through its bound revision and watch, which must name the same nonce and head.
    watch_by_id = {w.id: w for w in watches}
    for verdict in verdicts:
        attempt, reason = mapped(verdict.dispatch_nonce)
        watch = watch_by_id.get(verdict.watch_id)
        if attempt is not None and (watch is None or verdict.revision_id not in revision_nonce):
            attempt, reason = None, "attempt_unproven"
        elif attempt is not None and (revision_nonce[verdict.revision_id] != verdict.dispatch_nonce
                                      or watch.dispatch_nonce != verdict.dispatch_nonce
                                      or watch.head_sha != verdict.head_sha):
            attempt, reason = None, "attempt_conflict"
        if attempt is None:
            _lose(out, period, "revision_review", item.id, _naive(verdict.created_at), _naive(verdict.ended_at), reason)
        else:
            rows[attempt.launch_attempt]["revision_review"].append((watch.pr_number, verdict))

    # Active: planning and building, from recorded times only
    for attempt in attempts:
        mine = rows[attempt.launch_attempt]
        owners = ({r.owner_member_id for r in mine["approval_request"]}
                  | {r.owner_member_id for r in revisions if mapped(r.dispatch_nonce)[0] is attempt})
        attempt.owner_member_id = owners.pop() if len(owners) == 1 else None
        plans = [r for r in mine["approval_request"] if r.request_kind == "initial_plan"]
        if plans:
            _place(out, period, attempt, "active", "planning", "owner", attempt.dispatched_at,
                   _naive(plans[0].created_at))
        heads = await _head_times(db, item, {(r.pr_number, r.head_sha) for r in mine["head_review"] + mine["check_watch"]}
                                  | {(pr, v.head_sha) for pr, v in mine["revision_review"]})
        approved = next((r for r in plans if r.status == "approved" and r.decided_at), None)
        starts = ([(_naive(approved.decided_at), None)] if approved else []) + [
            (_naive(r.created_at), r.head_sha) for r in mine["head_review"] + [v for _, v in mine["revision_review"]]
            if r.verdict == "changes_required"]
        for begun, rejected in starts:  # a verdict's successor is another commit
            following = [t for (_, sha), t in heads.items() if t > begun and sha != rejected]
            if following:
                _place(out, period, attempt, "active", "building", "owner", begun, min(following))

    # Waiting on someone, by who had to answer
    by_request = {r.id: r for r in requests}
    ends = _ends(events)
    for start in (e for e in events if e.event_kind in {f"{f}_wait_started" for f in ITEM_WAITS}):
        family, at = start.event_kind.removesuffix("_wait_started"), _when(start)
        if family in ("decision", "hold"):
            request = by_request.get(start.request_id)
            attempt, reason = mapped(request.dispatch_nonce) if request else (None, "request_not_recorded")
        elif family == "ceiling":
            attempt, reason = by_key.get((start.context_snapshot or {}).get("launch_attempt")), "attempt_not_recorded"
        else:
            attempt = next((a for a in attempts if a.window.start <= at < a.window.end), None)
            reason = "outside_attempts"
        if attempt is None:
            _lose(out, period, family, item.id, at, _end_of(start, family, ends), reason)
            continue
        actor = (start.after_values or {}).get("awaited_role") or "unknown"
        _place(out, period, attempt, "waiting", family, actor, at, _end_of(start, family, ends))


async def _head_times(db, item: GithubWorkItem, pairs: set[tuple[int, str]]) -> dict[tuple[int, str], datetime]:
    """Each observed (PR, SHA) pair's first check run; a run of the same commit on another PR is not that head."""
    if not pairs:
        return {}
    firsts: dict[tuple[int, str], datetime] = {}
    for run in (await db.scalars(select(GithubCheckRunHistory).where(
            GithubCheckRunHistory.scope_id == item.scope_id,
            GithubCheckRunHistory.head_sha.in_({sha for _, sha in pairs})))).all():
        pair = (run.pr_number, run.head_sha)
        for at in (run.job_created_at, run.started_at):
            if pair in pairs and at is not None:
                firsts[pair] = min(firsts.get(pair, _naive(at)), _naive(at))
    return firsts
