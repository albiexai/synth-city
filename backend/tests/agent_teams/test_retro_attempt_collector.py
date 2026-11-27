"""#1417 (#1245 part 2): attempt windows and the waiting-on-someone and active collectors.

Each dispatch attempt of an item becomes a window and typed intervals for the stage engine. Every interval comes from
a recorded source. The ledger never holds a nonce, so a nonce maps to an attempt only on persisted evidence: the item
row (current nonce and launch) or a revision-bound audit event. Anything without evidence, or with conflicting
evidence, is unattributed with its reason, never placed by time. Mail and input waits follow answer Q4.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import count

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.database import (
    AgentTeamPreset,
    AgentTeamSlot,
    Base,
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
from app.services.decision_wait_service import record_wait_ended, record_wait_started
from app.services.github_dispatch_service import observe_work_lifecycle
from app.services.retro_attempt_collector import collect_team_attempts
from app.services.retro_stage_engine import breakdown

T0 = datetime(2026, 11, 20, 9, 0)
LABEL = "from campaign membership; ready time not recorded"
OWNER, OTHER = 61, 62
RUNS = count(1)


pytestmark = pytest.mark.asyncio


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'retro.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()


async def _team(db, items=1):
    team = AgentTeamPreset(name="Retro", description="Ship.")
    db.add(team)
    await db.flush()
    scope = TeamGithubScope(preset_id=team.id, repo_owner="example", repo_name="repo", repo_path="/r")
    slot = AgentTeamSlot(preset_id=team.id, display_name="B3", provider="claude-code", repo_path="/r",
                         repo_id="r", repo_name="repo")
    db.add_all([scope, slot])
    await db.flush()
    rows = [GithubWorkItem(scope_id=scope.id, issue_number=n, issue_title="Item", issue_url=f"u/{n}",
                           github_updated_at=T0, dispatch_status="dispatched", owner_slot_id=slot.id,
                           created_at=_at(-30)) for n in range(1, items + 1)]
    db.add_all(rows)
    await db.flush()
    return team, rows


def _key(item, launch):
    return audit.launch_attempt_key(item.id, launch)


async def _ev(db, kind, at, *, item=None, team=None, op=None, request_id=None, revision_id=None, after=None,
              snapshot=None, outcome=None, fact_source=None):
    await audit.record_event(
        db, event_kind=kind, source="test", occurred_at=at, fact_time=at, fact_source=fact_source,
        actor=audit.derive_actor(actor_kind="system"), item_id=item.id if item else None, team_preset_id=team,
        request_id=request_id, revision_id=revision_id, after_values=after, context_snapshot=snapshot,
        delivery_outcome=outcome, action_outcome="applied", operation_id=op, correlation_id=op)


async def _dispatch(db, item, launch, at, *, current_nonce=None):
    """The real lifecycle event at launch; the item row holds the current nonce and launch (evidence E1)."""
    item.launch_id, item.dispatch_nonce = launch, current_nonce
    await _ev(db, "work_lifecycle", at, item=item, after={"dispatch_status": "dispatched"},
              snapshot={"launch_attempt": _key(item, launch)})


async def _revision(db, item, nonce, owner=OWNER):
    row = GithubAttemptScopeRevision(
        work_item_id=item.id, dispatch_nonce=nonce, revision=0, owner_slot_id=item.owner_slot_id,
        owner_member_id=owner, phase="initial", execution_target="workspace", summary="s", allowed_paths=[],
        allowed_actions=[], allowed_commands=[], prohibited_actions=[], tool_fallbacks=[], baseline_head_sha="b",
        baseline_tree_sha="t", originating_escalation_reason="none", expected_workspace_id="w",
        expected_lease_token_hash="h", max_failed_heads=3, created_at=T0)
    db.add(row)
    await db.flush()
    return row


async def _bound(db, item, launch, revision, at, *, kind="delivery_evidence", merged=False):
    """A revision-bound audit event (evidence E2): its revision row holds the nonce, its context the launch."""
    key = _key(item, launch)
    await _ev(db, kind, at, item=item, revision_id=revision.id, outcome="delivered" if merged else None,
              fact_source="github_pull_request_merged" if merged else "test",
              snapshot={"attempt": f"{key}:revision:{revision.id}", "launch_attempt": key})


async def _request(db, item, nonce, created, decided=None, *, status="pending", kind="initial_plan", owner=OWNER):
    row = GithubApprovalRequest(
        work_item_id=item.id, request_kind=kind, dispatch_nonce=nonce, approval_round=1, owner_member_id=owner,
        leader_member_id=60, request_fingerprint=f"fp-{nonce}-{created}", status=status, created_at=created,
        decided_at=decided)
    db.add(row)
    await db.flush()
    return row


async def _wait(db, family, suffix, start, end=None, *, item=None, team=None, request_id=None, after=None,
                snapshot=None):
    for kind, at in ((f"{family}_wait_started", start), (f"{family}_wait_ended", end)):
        if at is not None:
            await _ev(db, kind, at, item=item, team=team, op=f"{kind}:{suffix}", request_id=request_id,
                      after=after, snapshot=snapshot)


async def _head(db, item, nonce, sha, pushed, *, verdict=None, reviewed=None, pr=5):
    """A head of the attempt's PR: its first check run, and optionally its review verdict."""
    db.add(GithubCheckRunHistory(repository="example/repo", check_run_id=next(RUNS),
                                 head_sha=sha, scope_id=item.scope_id, pr_number=pr, required=True,
                                 job_created_at=pushed, observed_at=pushed))
    if verdict:
        db.add(GithubHeadReview(work_item_id=item.id, dispatch_nonce=nonce, pr_number=pr, head_sha=sha,
                                reviewer_member_id=57, verdict=verdict, summary="s", evidence_url="u",
                                created_at=reviewed))
    await db.flush()


async def _watch(db, item, nonce, sha, at, *, pr=5):
    row = GithubRequiredCheckWatch(work_item_id=item.id, dispatch_nonce=nonce, scope_revision=0, pr_number=pr,
                                   head_sha=sha, owner_slot_id=item.owner_slot_id, policy_digest="p", observed_at=at,
                                   changed_at=at)
    db.add(row)
    await db.flush()
    return row


async def _revision_verdict(db, item, revision, watch, at, *, nonce="nonce-a", verdict="changes_required"):
    db.add(GithubRevisionReview(work_item_id=item.id, revision_id=revision.id, watch_id=watch.id, dispatch_nonce=nonce,
                                head_sha=watch.head_sha, reviewer_member_id=57, expected_lease_token_hash="h",
                                verdict=verdict, summary="s", evidence_url="u", created_at=at))
    await db.flush()


async def _collect(db, team, as_of=120, start=-10):
    await db.commit()
    return await collect_team_attempts(db, team.id, campaign_start=_at(start), as_of=_at(as_of))


def _spans(attempt):
    return sorted((i.group, i.stage, i.actor, (i.start - T0) / timedelta(minutes=1), (i.end - T0) / timedelta(
        minutes=1)) for i in attempt.intervals)


def _lost(result):
    return sorted((u.source, u.reason) for u in result.unattributed)


# One attempt: the window and each collector
async def test_one_attempt_waits_for_dispatch_plans_waits_on_the_leader_and_builds(db):
    team, (item,) = await _team(db)
    await _dispatch(db, item, 1, _at(0), current_nonce="nonce-a")
    plan = await _request(db, item, "nonce-a", _at(10), _at(20), status="approved")
    await _wait(db, "decision", f"request:{plan.id}", _at(10), _at(20), item=item, request_id=plan.id,
                after={"awaited_role": "leader"})
    await _head(db, item, "nonce-a", "h1", _at(35), verdict="accepted", reviewed=_at(40))
    rev = await _revision(db, item, "nonce-a")
    await _bound(db, item, 1, rev, _at(50), merged=True)
    result = await _collect(db, team)
    (attempt,) = result.attempts
    assert (attempt.window.start, attempt.window.end, attempt.window.start_label) == (_at(-10), _at(50), LABEL)
    assert _spans(attempt) == [
        ("active", "building", "owner", 20, 35), ("active", "planning", "owner", 0, 10),
        ("waiting", "decision", "leader", 10, 20), ("waiting", "dispatch", "dispatch", -10, 0)]
    stages = breakdown(attempt.window, attempt.intervals)
    assert stages.groups == {"waiting": 1200, "process": 0, "active": 1500}
    assert sum(stages.groups.values()) + stages.unaccounted_seconds == stages.total_seconds == 3600
    assert result.unattributed == []


async def test_the_first_window_starts_at_the_items_creation_when_it_joined_later(db):
    team, (item,) = await _team(db)
    item.created_at = _at(-5)
    await _dispatch(db, item, 1, _at(0), current_nonce="nonce-a")
    (attempt,) = (await _collect(db, team, as_of=30)).attempts
    assert (attempt.window.start, attempt.window.end) == (_at(-5), _at(30))


async def test_waits_are_split_by_who_had_to_answer(db):
    team, (item,) = await _team(db)
    await _dispatch(db, item, 1, _at(0), current_nonce="nonce-a")
    cont = await _request(db, item, "nonce-a", _at(1), kind="continuation")
    await _wait(db, "effort", "message:1", _at(5), _at(10), item=item, after={"awaited_role": "leader"})
    await _wait(db, "ceiling", "message:2", _at(10), _at(20), item=item, after={"awaited_role": "supervisor"},
                snapshot={"launch_attempt": _key(item, 1)})
    await _wait(db, "hold", f"request:{cont.id}:merge", _at(20), _at(25), item=item, request_id=cont.id,
                after={"awaited_role": "operator"})
    await _wait(db, "pause", "pause:3", _at(25), _at(30), item=item, after={"awaited_role": "operator"})
    (attempt,) = (await _collect(db, team, as_of=40)).attempts
    assert [s for s in _spans(attempt) if s[1] != "dispatch"] == [
        ("waiting", "ceiling", "supervisor", 10, 20), ("waiting", "effort", "leader", 5, 10),
        ("waiting", "hold", "operator", 20, 25), ("waiting", "pause", "operator", 25, 30)]


async def test_an_unended_wait_runs_to_the_cutoff(db):
    team, (item,) = await _team(db)
    await _dispatch(db, item, 1, _at(0), current_nonce="nonce-a")
    plan = await _request(db, item, "nonce-a", _at(10))
    await _wait(db, "decision", f"request:{plan.id}", _at(10), item=item, request_id=plan.id,
                after={"awaited_role": "leader"})
    (attempt,) = (await _collect(db, team, as_of=30)).attempts
    assert ("waiting", "decision", "leader", 10, 30) in _spans(attempt)


async def test_a_review_verdict_to_the_next_head_is_building(db):
    team, (item,) = await _team(db)
    await _dispatch(db, item, 1, _at(0), current_nonce="nonce-a")
    await _request(db, item, "nonce-a", _at(5), _at(10), status="approved")
    await _head(db, item, "nonce-a", "h1", _at(20), verdict="changes_required", reviewed=_at(30))
    await _head(db, item, "nonce-a", "h2", _at(45), verdict="accepted", reviewed=_at(50))
    await _head(db, item, "nonce-a", "h2", _at(35), pr=6)  # the same head on another PR is not this attempt's
    (attempt,) = (await _collect(db, team, as_of=60)).attempts
    assert [s for s in _spans(attempt) if s[1] == "building"] == [
        ("active", "building", "owner", 10, 20), ("active", "building", "owner", 30, 45)]


# Several attempts: windows and evidence-only attribution
async def _redispatched(db, *, evidence=True):
    """Attempt 1 (nonce-a, launch 1) from 0 to 60, attempt 2 (nonce-b, current, launch 2) from 60."""
    team, (item,) = await _team(db)
    await _dispatch(db, item, 1, _at(0))
    if evidence:
        await _bound(db, item, 1, await _revision(db, item, "nonce-a"), _at(30))
    await _dispatch(db, item, 2, _at(60), current_nonce="nonce-b")
    await _request(db, item, "nonce-b", _at(70))
    return team, item


async def test_a_second_attempt_starts_where_the_first_ended(db):
    team, item = await _redispatched(db)
    await _request(db, item, "nonce-a", _at(10), status="superseded")
    first, second = (await _collect(db, team, as_of=90)).attempts
    assert (first.window.end, second.window.start, second.window.end, second.window.start_label) == (
        _at(60), _at(60), _at(90), None)
    assert ("active", "planning", "owner", 0, 10) in _spans(first)
    assert ("active", "planning", "owner", 60, 70) in _spans(second)


@pytest.mark.parametrize(("evidence", "expected"), [(True, []), (False, [("head_review", "attempt_unproven")] * 2)])
async def test_an_old_nonce_whose_plan_request_is_gone_maps_only_on_evidence(db, evidence, expected):
    """A missing first record: only a delayed verdict of attempt 1 survives."""
    team, item = await _redispatched(db, evidence=evidence)
    await _request(db, item, "nonce-a", _at(5), _at(8), status="approved")
    await db.execute(GithubApprovalRequest.__table__.delete().where(GithubApprovalRequest.dispatch_nonce == "nonce-a"))
    await _head(db, item, "nonce-a", "h1", _at(20), verdict="changes_required", reviewed=_at(40))
    await _head(db, item, "nonce-a", "h2", _at(50), verdict="accepted", reviewed=_at(55))
    result = await _collect(db, team, as_of=90)
    assert _lost(result) == expected
    first, second = result.attempts
    assert [s for s in _spans(first) if s[1] == "building"] == ([("active", "building", "owner", 40, 50)]
                                                                 if evidence else [])
    assert all(s[1] != "building" for s in _spans(second))


async def test_an_old_nonce_first_seen_after_the_redispatch_stays_out_of_the_new_attempt(db):
    """A late first record: attempt 1's only surviving rows are dated inside attempt 2."""
    team, item = await _redispatched(db)
    await _head(db, item, "nonce-a", "h1", _at(20), verdict="changes_required", reviewed=_at(65))
    await _head(db, item, "nonce-a", "h3", _at(80), verdict="accepted", reviewed=_at(85))
    result = await _collect(db, team, as_of=90)
    first, second = result.attempts
    assert all(s[1] != "building" for a in (first, second) for s in _spans(a))
    assert [(u.source, u.reason, u.start, u.end) for u in result.unattributed] == [
        ("building", "after_attempt_end", _at(65), _at(80))]


async def test_a_late_record_without_evidence_is_unproven_not_the_new_attempt(db):
    team, item = await _redispatched(db, evidence=False)
    plan = await _request(db, item, "nonce-a", _at(65), status="superseded")
    await _wait(db, "decision", f"request:{plan.id}", _at(65), _at(75), item=item, request_id=plan.id,
                after={"awaited_role": "leader"})
    result = await _collect(db, team, as_of=90)
    first, second = result.attempts
    assert all(s[1] not in ("decision", "planning") for s in _spans(first))
    assert [s for s in _spans(second) if s[1] in ("decision", "planning")] == [("active", "planning", "owner", 60, 70)]
    assert _lost(result) == [("approval_request", "attempt_unproven"), ("decision", "attempt_unproven")]


async def test_a_delayed_decision_with_the_old_nonce_stays_in_its_attempt_and_is_clipped(db):
    team, item = await _redispatched(db)
    plan = await _request(db, item, "nonce-a", _at(40), _at(75), status="rejected")
    await _wait(db, "decision", f"request:{plan.id}", _at(40), _at(75), item=item, request_id=plan.id,
                after={"awaited_role": "leader"})
    await _wait(db, "ceiling", "message:2", _at(62), _at(70), item=item, after={"awaited_role": "supervisor"},
                snapshot={"launch_attempt": _key(item, 1)})
    current = (await db.scalars(select(GithubApprovalRequest).where(GithubApprovalRequest.dispatch_nonce == "nonce-b"))).one()
    await _wait(db, "hold", f"request:{current.id}:merge", _at(55), _at(65), item=item, request_id=current.id,
                after={"awaited_role": "operator"})
    result = await _collect(db, team, as_of=90)
    first, second = result.attempts
    assert [s for s in _spans(first) if s[0] == "waiting" and s[1] != "dispatch"] == [
        ("waiting", "decision", "leader", 40, 60)]
    assert [s for s in _spans(second) if s[0] == "waiting"] == [("waiting", "hold", "operator", 60, 65)]
    assert sorted((u.source, u.reason, u.start, u.end) for u in result.unattributed) == [
        ("ceiling", "after_attempt_end", _at(62), _at(70)), ("decision", "after_attempt_end", _at(60), _at(75)),
        ("hold", "before_attempt_start", _at(55), _at(60))]


async def test_a_nonce_with_conflicting_launch_evidence_is_unattributed(db):
    """One revision row claimed for two launches: both claims on launch 2 are ambiguous, so neither maps."""
    team, item = await _redispatched(db)
    revision = (await db.scalars(select(GithubAttemptScopeRevision))).one()
    await _bound(db, item, 2, revision, _at(65))
    await _request(db, item, "nonce-a", _at(10), status="superseded")
    result = await _collect(db, team, as_of=90)
    assert _lost(result) == [("approval_request", "attempt_conflict")] * 2
    assert all(s[1] != "planning" for a in result.attempts for s in _spans(a))


async def test_a_nonce_bound_to_two_launches_maps_to_neither(db):
    """No other nonce claims either launch: the nonce's own two claims are the conflict."""
    team, (item,) = await _team(db)
    await _dispatch(db, item, 1, _at(0))
    revision = await _revision(db, item, "nonce-a")
    await _bound(db, item, 1, revision, _at(30))
    await _dispatch(db, item, 2, _at(60))
    await _bound(db, item, 2, revision, _at(65))
    await _request(db, item, "nonce-a", _at(10))
    result = await _collect(db, team, as_of=90)
    assert _lost(result) == [("approval_request", "attempt_conflict")]
    assert all(s[1] != "planning" for a in result.attempts for s in _spans(a))


async def test_an_effort_wait_past_its_attempts_end_is_split(db):
    team, item = await _redispatched(db)
    await _wait(db, "effort", "message:1", _at(50), _at(70), item=item, after={"awaited_role": "leader"})
    result = await _collect(db, team, as_of=90)
    first, second = result.attempts
    assert ("waiting", "effort", "leader", 50, 60) in _spans(first)
    assert all(s[1] != "effort" for s in _spans(second))
    assert [(u.source, u.reason, u.start, u.end) for u in result.unattributed] == [
        ("effort", "after_attempt_end", _at(60), _at(70))]


async def test_a_wait_outside_every_attempt_is_unattributed(db):
    team, (item,) = await _team(db)
    item.created_at = _at(-5)  # the item joined after the campaign started: its first window starts then
    await _dispatch(db, item, 1, _at(0), current_nonce="nonce-a")
    await _wait(db, "pause", "pause:1", _at(-9), _at(-7), item=item, after={"awaited_role": "operator"})
    assert _lost(await _collect(db, team, as_of=30)) == [("pause", "outside_attempts")]


async def test_a_revision_event_whose_attempt_key_names_another_revision_is_no_evidence(db):
    team, item = await _redispatched(db, evidence=False)
    revision = await _revision(db, item, "nonce-a")
    key = _key(item, 1)
    await _ev(db, "delivery_evidence", _at(30), item=item, revision_id=revision.id,
              snapshot={"attempt": f"{key}:revision:{revision.id + 1}", "launch_attempt": key})
    await _request(db, item, "nonce-a", _at(10), status="superseded")
    assert _lost(await _collect(db, team, as_of=90)) == [("approval_request", "attempt_unproven")]


async def test_a_failed_event_of_the_launch_ends_its_attempt(db):
    team, (item,) = await _team(db)
    await _dispatch(db, item, 1, _at(0), current_nonce="nonce-a")
    await _ev(db, "work_lifecycle", _at(20), item=item, after={"dispatch_status": "failed"},
              snapshot={"launch_attempt": _key(item, 1)})
    (attempt,) = (await _collect(db, team, as_of=60)).attempts
    assert attempt.window.end == _at(20)


# V1 review 431: the campaign period, revision verdicts and head pairs
async def test_the_period_runs_from_the_campaign_start_and_drops_what_lies_wholly_before_it(db):
    """An attempt that ended before the campaign is not shown; its nonce's later wait is not the next attempt's."""
    team, (item,) = await _team(db)
    await _dispatch(db, item, 1, _at(-40))
    await _bound(db, item, 1, await _revision(db, item, "nonce-a"), _at(-35))
    old = await _request(db, item, "nonce-a", _at(-30), _at(20), status="rejected")
    await _wait(db, "decision", f"request:{old.id}", _at(-30), _at(20), item=item, request_id=old.id,
                after={"awaited_role": "leader"})
    await _dispatch(db, item, 2, _at(0), current_nonce="nonce-b")
    await _request(db, item, "nonce-b", _at(5))
    result = await _collect(db, team, as_of=60, start=10)
    (attempt,) = result.attempts
    assert (attempt.window.start, attempt.window.end, attempt.window.start_label) == (_at(10), _at(60), LABEL)
    assert _spans(attempt) == []  # dispatch at 0 and planning to 5 both precede the campaign
    assert [(u.source, u.reason, u.start, u.end) for u in result.unattributed] == [
        ("decision", "after_attempt_end", _at(10), _at(20))]


async def test_unattributed_spans_are_clipped_to_the_cutoff_and_later_ones_left_out(db):
    team, (item,) = await _team(db)
    await _dispatch(db, item, 1, _at(0), current_nonce="nonce-a")
    for message, start, end in ((9, 25, 40), (10, 40, 45), (11, -30, -20)):
        await _wait(db, "mail", f"message:{message}", _at(start), _at(end), team=team.id,
                    after={"owner_member_id": OTHER, "awaited_member_id": 60})
    result = await _collect(db, team, as_of=30)
    assert [(u.source, u.reason, u.start, u.end) for u in result.unattributed] == [
        ("mail", "no_open_attempt", _at(25), _at(30))]


async def test_a_proven_revision_alone_names_the_attempts_owner(db):
    team, (item,) = await _team(db)
    await _dispatch(db, item, 1, _at(0), current_nonce="nonce-a")
    await _bound(db, item, 1, await _revision(db, item, "nonce-a"), _at(2))
    await _wait(db, "mail", "message:9", _at(10), _at(15), team=team.id,
                after={"owner_member_id": OWNER, "awaited_member_id": 60})
    result = await _collect(db, team, as_of=30)
    assert ("waiting", "mail", "member:60", 10, 15) in _spans(result.attempts[0])
    assert result.unattributed == []


@pytest.mark.parametrize("proof", ["proven", "unproven", "conflicting"])
async def test_a_revision_verdict_to_the_next_head_is_building_only_with_proof(db, proof):
    team, (item,) = await _team(db)
    await _dispatch(db, item, 1, _at(0), current_nonce="nonce-a" if proof != "unproven" else "nonce-z")
    revision = await _revision(db, item, "nonce-a" if proof != "conflicting" else "nonce-x")
    rejected = await _watch(db, item, "nonce-a", "h1", _at(5))
    await _head(db, item, "nonce-a", "h1", _at(5))
    await _revision_verdict(db, item, revision, rejected, _at(10))
    await _watch(db, item, "nonce-a", "h2", _at(25))
    await _head(db, item, "nonce-a", "h2", _at(25))
    result = await _collect(db, team, as_of=60)
    building = [s for s in _spans(result.attempts[0]) if s[1] == "building"]
    if proof == "proven":
        assert (building, result.unattributed) == ([("active", "building", "owner", 10, 25)], [])
    else:
        assert building == []
        reason = "attempt_unproven" if proof == "unproven" else "attempt_conflict"
        assert ("revision_review", reason) in _lost(result)


async def test_overlapping_head_and_revision_verdicts_count_their_building_once(db):
    team, (item,) = await _team(db)
    await _dispatch(db, item, 1, _at(0), current_nonce="nonce-a")
    revision = await _revision(db, item, "nonce-a")
    rejected = await _watch(db, item, "nonce-a", "h1", _at(5))
    await _head(db, item, "nonce-a", "h1", _at(5), verdict="changes_required", reviewed=_at(12))
    await _revision_verdict(db, item, revision, rejected, _at(10))
    await _watch(db, item, "nonce-a", "h2", _at(25))
    await _head(db, item, "nonce-a", "h2", _at(25))
    (attempt,) = (await _collect(db, team, as_of=30)).attempts
    assert breakdown(attempt.window, attempt.intervals).groups["active"] == 900  # 10 to 25, once


async def test_a_late_first_check_on_the_rejected_commit_is_not_its_successor(db):
    team, (item,) = await _team(db)
    await _dispatch(db, item, 1, _at(0), current_nonce="nonce-a")
    await _head(db, item, "nonce-a", "h1", _at(15), verdict="changes_required", reviewed=_at(10))
    (attempt,) = (await _collect(db, team, as_of=60)).attempts
    assert all(s[1] != "building" for s in _spans(attempt))


async def test_a_check_of_the_same_commit_on_another_pr_is_not_this_pairs_head(db):
    team, (item,) = await _team(db)
    await _dispatch(db, item, 1, _at(0), current_nonce="nonce-a")
    await _head(db, item, "nonce-a", "h1", _at(5), verdict="changes_required", reviewed=_at(10))
    await _head(db, item, "nonce-a", "h2", _at(35), verdict="accepted", reviewed=_at(40))
    await _watch(db, item, "nonce-a", "h9", _at(1), pr=6)  # the attempt also watched PR 6
    await _head(db, item, "nonce-a", "h2", _at(15), pr=6)  # the same commit, checked earlier on PR 6
    (attempt,) = (await _collect(db, team, as_of=60)).attempts
    assert [s for s in _spans(attempt) if s[1] == "building"] == [("active", "building", "owner", 10, 35)]


@pytest.mark.parametrize("family", ["effort", "pause", "ceiling"])
async def test_a_wait_begun_before_the_campaign_keeps_its_attempt_and_is_clipped(db, family):
    """V1 review 434: the attempt is chosen at the wait's own start, then the span is clipped to the campaign."""
    team, (item,) = await _team(db)
    await _dispatch(db, item, 1, _at(-40), current_nonce="nonce-a")
    await _wait(db, family, "x:1", _at(-30), _at(-5), item=item, after={"awaited_role": "leader"},
                snapshot={"launch_attempt": _key(item, 1)})
    result = await _collect(db, team, as_of=30, start=-20)
    (attempt,) = result.attempts
    assert [s for s in _spans(attempt) if s[1] == family] == [("waiting", family, "leader", -20, -5)]
    assert result.unattributed == []


# Mail and input waits (Q4)
async def test_mail_and_input_waits_follow_the_owner_of_the_one_open_attempt(db):
    team, (mine, theirs) = await _team(db, items=2)
    await _dispatch(db, mine, 1, _at(0), current_nonce="nonce-a")
    await _request(db, mine, "nonce-a", _at(5))
    await _dispatch(db, theirs, 1, _at(0), current_nonce="nonce-b")
    await _request(db, theirs, "nonce-b", _at(5), owner=OTHER)
    await _revision(db, theirs, "nonce-b", owner=OWNER)  # two recorded owners: the attempt has no owner
    await _wait(db, "mail", "message:9", _at(10), _at(20), team=team.id,
                after={"owner_member_id": OWNER, "awaited_member_id": 60})
    await _wait(db, "input", "binding:4:x", _at(20), _at(25), team=team.id,
                after={"owner_member_id": OWNER, "awaited_actor_id": 6})
    await _wait(db, "mail", "message:8", _at(-5), _at(-2), team=team.id,
                after={"owner_member_id": OWNER, "awaited_member_id": 60})  # before the owner's dispatch
    result = await _collect(db, team, as_of=30)
    first, second = result.attempts
    assert _lost(result) == [("mail", "no_open_attempt")]
    assert [s for s in _spans(first) if s[1] in ("mail", "input")] == [
        ("waiting", "input", "actor:6", 20, 25), ("waiting", "mail", "member:60", 10, 20)]
    assert all(s[1] not in ("mail", "input") for s in _spans(second))


async def test_a_mail_wait_with_no_or_several_open_attempts_is_shown_once_unattributed(db):
    team, (one, two) = await _team(db, items=2)
    await _dispatch(db, one, 1, _at(0), current_nonce="nonce-a")
    await _request(db, one, "nonce-a", _at(5))
    await _dispatch(db, two, 1, _at(0), current_nonce="nonce-b")
    await _request(db, two, "nonce-b", _at(5))
    await _wait(db, "mail", "message:9", _at(10), _at(20), team=team.id,
                after={"owner_member_id": OWNER, "awaited_member_id": 60})
    await _wait(db, "mail", "message:10", _at(10), _at(20), team=team.id,
                after={"owner_member_id": OTHER, "awaited_member_id": 60})
    result = await _collect(db, team, as_of=30)
    assert _lost(result) == [("mail", "no_open_attempt"), ("mail", "several_open_attempts")]
    assert all(s[1] != "mail" for a in result.attempts for s in _spans(a))


async def test_a_mail_wait_ambiguous_when_sent_stays_ambiguous_after_one_attempt_ends_before_the_campaign(db):
    """V1 review 434: ambiguity is decided among the attempts open at the send, not the ones shown."""
    team, (one, two) = await _team(db, items=2)
    await _dispatch(db, one, 1, _at(-40), current_nonce="nonce-a")
    await _request(db, one, "nonce-a", _at(-38))
    await _bound(db, one, 1, await _revision(db, one, "nonce-a"), _at(-15), merged=True)
    await _dispatch(db, two, 1, _at(-40), current_nonce="nonce-b")
    await _request(db, two, "nonce-b", _at(-38))
    await _wait(db, "mail", "message:9", _at(-20), _at(5), team=team.id,
                after={"owner_member_id": OWNER, "awaited_member_id": 60})
    result = await _collect(db, team, as_of=30, start=-10)
    assert [a.item_id for a in result.attempts] == [two.id]
    assert [(u.source, u.reason, u.start, u.end) for u in result.unattributed] == [
        ("mail", "several_open_attempts", _at(-10), _at(5))]
    assert all(s[1] != "mail" for s in _spans(result.attempts[0]))


# The real producers, and reading only
async def test_the_real_lifecycle_and_decision_wait_events_are_read(db):
    team, (item,) = await _team(db)
    item.launch_id, item.dispatch_nonce = 1, "nonce-a"
    await observe_work_lifecycle(db, item=item, from_status=None, to_status="dispatched", source="test")
    started = datetime.now(UTC).replace(tzinfo=None)
    plan = await _request(db, item, "nonce-a", started + timedelta(minutes=1))
    await record_wait_started(db, plan, source="test")
    plan.decided_at = started + timedelta(minutes=4)
    await record_wait_ended(db, [plan.id], outcome="approved", source="test", ended_at=plan.decided_at)
    await db.commit()
    result = await collect_team_attempts(db, team.id, campaign_start=started - timedelta(minutes=5),
                                         as_of=started + timedelta(minutes=10))
    (attempt,) = result.attempts
    assert {(i.group, i.stage, i.actor) for i in attempt.intervals} == {
        ("waiting", "dispatch", "dispatch"), ("active", "planning", "owner"), ("waiting", "decision", "leader")}


async def test_collecting_writes_nothing(db):
    team, item = await _redispatched(db)
    await _wait(db, "effort", "message:1", _at(50), _at(70), item=item, after={"awaited_role": "leader"})
    await db.commit()
    before = await db.scalar(select(func.count()).select_from(FactoryAuditEvent))
    await collect_team_attempts(db, team.id, campaign_start=_at(-10), as_of=_at(90))
    assert not (db.new or db.dirty or db.deleted)
    assert await db.scalar(select(func.count()).select_from(FactoryAuditEvent)) == before


@pytest.mark.parametrize("as_of", [-20, 0])
async def test_no_attempt_before_its_dispatch(db, as_of):
    team, (item,) = await _team(db)
    await _dispatch(db, item, 1, _at(0), current_nonce="nonce-a")
    assert len((await _collect(db, team, as_of=as_of)).attempts) == (0 if as_of < 0 else 1)
