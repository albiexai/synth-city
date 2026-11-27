"""#1417 (#1245 part 2): attempt windows and the waiting-on-someone and active collectors.

Read-only. Each dispatch attempt of a team's items becomes an ``AttemptWindow`` and ``StageInterval``s for
``retro_stage_engine.breakdown``: waiting on someone and active (#1417), and process (#1418). The snapshot section
is #1419.

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
- Process (#1418): each required check run on a watched head of the scope's own repository (a reused scope id keeps
  another repository's history), per repository and check name, from its queue start
  (Actions' job creation, else its start) to its completion. Review per verifier runs from the review notice the
  watch delivered to that member (its own message, under its delivery key) to their first verdict on the head in the
  watch's review context (its PR and scope revision). The notice is the later of the reviewer's
  binding and the checks' success, not an assignment time, which is not recorded. A missing notice, a verdict before
  it, or a replaced reviewer without a verdict adds nothing; only the bound reviewer's unended review stays open.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import aliased

from app.models.database import (
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
    opened_at: datetime | None = None  # the window's start before the campaign clip, for lookups at a wait's start


@dataclass
class TeamAttempts:
    attempts: list[CollectedAttempt] = field(default_factory=list)
    unattributed: list[Unattributed] = field(default_factory=list)
    # #1419: each collected item's (repository, issue), read in the same query as the items themselves
    item_identity: dict[int, tuple[str, int]] = field(default_factory=dict)
    # items whose lifetime changed while they were read (V1 review 121): dropped, never another lifetime's evidence
    unstable_items: set[int] = field(default_factory=set)


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


async def collect_team_attempts(db, preset_id: int, *, campaign_start: datetime, as_of: datetime,
                                team_context_key: str | None = None) -> TeamAttempts:
    """Every recorded attempt of the team's items, with its window and intervals, and what could not be attributed.

    Team-wide waits are read from ``team_context_key``'s lifetime, the current team's by default: a campaign passes
    its own, so a replacement team reusing the preset id never adds its waits to a retired campaign (#1419)."""
    campaign_start, as_of = _naive(campaign_start), _naive(as_of)
    out, period = TeamAttempts(), (campaign_start, as_of)
    item_keys = aliased(FactoryContextKey)  # each item's lifetime key, bound in the same read as its identity
    # Scalar columns, never the entity: a session already holding the item would get its cached attributes back,
    # pairing an old identity with a reused id's lifetime, and refreshing it would discard a caller's pending edit
    # (V1 review 123). Columns are read as the row is now, and the caller's entities are left untouched.
    query = (select(GithubWorkItem.id, GithubWorkItem.scope_id, GithubWorkItem.issue_number, GithubWorkItem.created_at,
                    GithubWorkItem.dispatch_nonce, GithubWorkItem.launch_id, TeamGithubScope.repo_owner,
                    TeamGithubScope.repo_name, item_keys.context_key)
             .join(TeamGithubScope, TeamGithubScope.id == GithubWorkItem.scope_id)
             .outerjoin(item_keys, (item_keys.key_kind == "item") & (item_keys.numeric_id == GithubWorkItem.id)
                        & item_keys.retired_at.is_(None)))
    if team_context_key:  # the team lifetime's own scopes, never a replacement team reusing the preset id
        query = query.join(FactoryContextKey, (FactoryContextKey.key_kind == "team")
                           & (FactoryContextKey.numeric_id == TeamGithubScope.preset_id)
                           & (FactoryContextKey.context_key == team_context_key)
                           & FactoryContextKey.retired_at.is_(None))
    else:
        query = query.where(TeamGithubScope.preset_id == preset_id)
    rows = [(_ItemRow(r.id, r.scope_id, r.issue_number, r.created_at, r.dispatch_nonce, r.launch_id),
             r.repo_owner, r.repo_name, r.context_key) for r in (await db.execute(query.order_by(GithubWorkItem.id))).all()]
    out.item_identity = {row[0].id: (f"{row[1]}/{row[2]}".lower(), row[0].issue_number) for row in rows}
    keys = {row[0].id: row[3] for row in rows}
    seen = {row[0].id: (row[0].scope_id, row[0].issue_number, row[0].created_at) for row in rows}
    every: list[CollectedAttempt] = []  # shown or not: who was open when a wait began (V1 review 434)
    for row in rows:
        every += await _collect_item(db, row[0], out, period, keys[row[0].id])

    async def same_row(item_id: int) -> bool:
        now = (await db.execute(select(GithubWorkItem.scope_id, GithubWorkItem.issue_number, GithubWorkItem.created_at)
                                .where(GithubWorkItem.id == item_id))).first()
        return now is not None and tuple(now) == seen[item_id]

    # Rows read by numeric id carry no lifetime: an item whose key changed meanwhile is dropped entirely. A first key
    # on the same row is the same lifetime's first event, so it is read under that key and checked again.
    for row in rows:
        item_id, key = row[0].id, keys[row[0].id]
        current = await audit.current_context_key(db, "item", item_id)
        if current == key:
            continue
        if key is None and current is not None and await same_row(item_id):
            every += await _collect_item(db, row[0], out, period, current)
            if await audit.current_context_key(db, "item", item_id) == current and await same_row(item_id):
                continue
        out.unstable_items.add(item_id)
    if out.unstable_items:
        out.attempts = [a for a in out.attempts if a.item_id not in out.unstable_items]
        out.unattributed = [u for u in out.unattributed if u.item_id not in out.unstable_items]
        every = [a for a in every if a.item_id not in out.unstable_items]
    team_key = team_context_key or await audit.current_context_key(db, "team", preset_id)
    events = (await db.scalars(select(FactoryAuditEvent).where(
        FactoryAuditEvent.team_context_key == team_key, FactoryAuditEvent.team_context_key.is_not(None),
        FactoryAuditEvent.event_kind.in_([f"{f}_wait_{s}" for f in TEAM_WAITS for s in ("started", "ended")]))
        .order_by(FactoryAuditEvent.occurred_at, FactoryAuditEvent.id))).all()
    ends = _ends(events)
    for start in (e for e in events if e.event_kind.endswith("_wait_started")):
        family, values, at = start.event_kind.removesuffix("_wait_started"), start.after_values or {}, _when(start)
        owner = values.get("owner_member_id")
        open_ = [a for a in every
                 if owner is not None and a.owner_member_id == owner and a.dispatched_at <= at < a.window.end]
        if len(open_) != 1:
            _lose(out, period, family, None, at, _end_of(start, family, ends),
                  "several_open_attempts" if open_ else "no_open_attempt")
            continue
        actor = (f"member:{values['awaited_member_id']}" if values.get("awaited_member_id") is not None
                 else f"actor:{values['awaited_actor_id']}" if values.get("awaited_actor_id") is not None else "unknown")
        _place(out, period, open_[0], "waiting", family, actor, at, _end_of(start, family, ends))
    return out


@dataclass(frozen=True)
class _ItemRow:
    """The item fields the collector reads, as one row read them: no ORM entity is loaded or refreshed."""
    id: int
    scope_id: int
    issue_number: int
    created_at: datetime
    dispatch_nonce: str | None
    launch_id: int | None


async def _collect_item(db, item: _ItemRow, out: TeamAttempts, period, item_key: str | None = None):
    campaign_start, as_of = period
    if item_key is None:
        return []
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
        attempt = CollectedAttempt(item.id, key, dispatched, window, opened_at=start)
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
        repository = await _repository(db, item)
        heads = await _head_times(db, item, repository, {(r.pr_number, r.head_sha) for r in mine["head_review"] + mine["check_watch"]}
                                  | {(pr, v.head_sha) for pr, v in mine["revision_review"]})
        approved = next((r for r in plans if r.status == "approved" and r.decided_at), None)
        starts = ([(_naive(approved.decided_at), None)] if approved else []) + [
            (_naive(r.created_at), r.head_sha) for r in mine["head_review"] + [v for _, v in mine["revision_review"]]
            if r.verdict == "changes_required"]
        for begun, rejected in starts:  # a verdict's successor is another commit
            following = [t for (_, sha), t in heads.items() if t > begun and sha != rejected]
            if following:
                _place(out, period, attempt, "active", "building", "owner", begun, min(following))
        # Process: required checks per check, and review per verifier (#1418)
        for run in await _required_runs(db, item, repository, {(w.pr_number, w.head_sha) for w in mine["check_watch"]}):
            queued = run.job_created_at or run.started_at
            if queued is not None and run.completed_at is not None:
                _place(out, period, attempt, "process", "required_check", f"{run.repository}:{run.check_name}",
                       _naive(queued), _naive(run.completed_at))
        for watch in mine["check_watch"]:
            for member, noticed, verdict in await _reviews(db, watch, mine["head_review"], mine["revision_review"]):
                _place(out, period, attempt, "process", "review", f"member:{member}", noticed, verdict)

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
            attempt = next((a for a in attempts if a.opened_at <= at < a.window.end), None)
            reason = "outside_attempts"
        if attempt is None:
            _lose(out, period, family, item.id, at, _end_of(start, family, ends), reason)
            continue
        actor = (start.after_values or {}).get("awaited_role") or "unknown"
        _place(out, period, attempt, "waiting", family, actor, at, _end_of(start, family, ends))
    return attempts


async def _repository(db, item: _ItemRow) -> str | None:
    """The scope's repository as the check history records it; history outlives its scope, whose id can be reused."""
    scope = await db.get(TeamGithubScope, item.scope_id)
    return f"{scope.repo_owner}/{scope.repo_name}".lower() if scope is not None else None


async def _required_runs(db, item: _ItemRow, repository: str | None,
                         pairs: set[tuple[int, str]]) -> list[GithubCheckRunHistory]:
    """The required check runs of the watched (PR, SHA) pairs in the scope's own repository."""
    if not pairs or repository is None:
        return []
    runs = (await db.scalars(select(GithubCheckRunHistory).where(
        GithubCheckRunHistory.scope_id == item.scope_id, GithubCheckRunHistory.repository == repository,
        GithubCheckRunHistory.required.is_(True),
        GithubCheckRunHistory.head_sha.in_({sha for _, sha in pairs})))).all()
    return [run for run in runs if (run.pr_number, run.head_sha) in pairs]


async def _reviews(db, watch: GithubRequiredCheckWatch, head_reviews, revision_verdicts):
    """Each verifier's (member, notice, first verdict at or after it) on a watch whose checks succeeded."""
    if watch.completion_kind != "success":
        return []
    # The notice must be this watch's own delivery to that member: a message id can be reused by an ordinary message.
    notices = [(member, at) for member, at, recipient, key in (await db.execute(
        select(GithubRequiredCheckRecipient.member_id, MailMessage.created_at, MailMessage.recipient_member_id,
               MailMessage.delivery_key)
        .join(MailMessage, MailMessage.id == GithubRequiredCheckRecipient.message_id)
        .where(GithubRequiredCheckRecipient.watch_id == watch.id))).all()
        if recipient == member and key == f"required-checks:{watch.id}:member:{member}"]
    verdicts: dict[int, list[datetime]] = defaultdict(list)
    for review in head_reviews:  # a verdict in the watch's review context: its dispatch, PR, commit and scope revision
        if (review.dispatch_nonce, review.pr_number, review.head_sha, review.scope_revision or 0) == (
                watch.dispatch_nonce, watch.pr_number, watch.head_sha, watch.scope_revision):
            verdicts[review.reviewer_member_id].append(_naive(review.created_at))
    for _, review in revision_verdicts:
        if review.watch_id == watch.id:
            verdicts[review.reviewer_member_id].append(_naive(review.created_at))
    found = []
    for member, noticed in notices:
        noticed, own = _naive(noticed), verdicts.get(member, [])
        if own:  # a verdict before the notice has no recorded start and leaves nothing open
            if after := [t for t in own if t >= noticed]:
                found.append((member, noticed, min(after)))
        elif member == watch.reviewer_member_id:  # only the bound reviewer's unended review stays open
            found.append((member, noticed, None))
    return sorted(found, key=lambda review: (review[1], review[0]))


async def _head_times(db, item: _ItemRow, repository: str | None,
                      pairs: set[tuple[int, str]]) -> dict[tuple[int, str], datetime]:
    """Each observed (PR, SHA) pair's first check run in the scope's repository; another PR's run is not that head."""
    if not pairs or repository is None:
        return {}
    firsts: dict[tuple[int, str], datetime] = {}
    for run in (await db.scalars(select(GithubCheckRunHistory).where(
            GithubCheckRunHistory.scope_id == item.scope_id, GithubCheckRunHistory.repository == repository,
            GithubCheckRunHistory.head_sha.in_({sha for _, sha in pairs})))).all():
        pair = (run.pr_number, run.head_sha)
        for at in (run.job_created_at, run.started_at):
            if pair in pairs and at is not None:
                firsts[pair] = min(firsts.get(pair, _naive(at)), _naive(at))
    return firsts
