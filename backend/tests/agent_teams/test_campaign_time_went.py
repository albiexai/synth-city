"""#1419 (#1245 part 4): "where the time went" in the campaign snapshot.

Item time is Q1's (recorded on #1245): per attempt, from waiting for dispatch to the merge, or ``as_of`` while open;
before the first attempt, from campaign membership. One item's attempt windows are contiguous, so their sum is the
item's campaign time. The stage engine splits each window into waiting on someone, process, active and an explicit
unaccounted remainder, so the stages plus unaccounted equal item time. A member with no recorded attempt is unknown,
never zero time.
"""
from __future__ import annotations

from datetime import timedelta
from itertools import count

import pytest
from sqlalchemy import text

from app.models.database import (
    AgentTeamLaunch,
    CampaignSnapshot,
    CampaignUpgradeCoverage,
    GithubApprovalRequest,
    GithubCheckRunHistory,
    GithubCheckRunHistoryCoverage,
    GithubHeadReview,
    GithubRequiredCheckRecipient,
    GithubRequiredCheckWatch,
    GithubWorkItem,
    MailMessage,
    MailTeamMember,
    TeamGithubScope,
)
from app.services import campaign_record_service as campaigns
from app.services import campaign_snapshot_service as snapshots
from app.services import factory_audit_service as audit
from tests.agent_teams.test_campaign_item_time import (
    CREATED,
    MARKER,
    _dispatch,
    _m,
    _marker,
)
from tests.agent_teams.test_campaign_record import (  # noqa: F401  (fixtures)
    FakeGithub,
    _issue,
    db,
    world,
)
from tests.agent_teams.test_campaign_snapshot import OPERATOR, T0

pytestmark = pytest.mark.asyncio
RUNS = count(50_000)
AS_OF = _m(180)
MIN = 60


@pytest.fixture(autouse=True)
def fresh_cache():
    snapshots.clear_cache()
    yield
    snapshots.clear_cache()


async def _launch(db, world, number):
    """A team launch row: the item row's ``launch_id`` names one (evidence E1 for its current nonce)."""
    launch = AgentTeamLaunch(preset_id=world.team, plan_hash=f"plan-{number}")
    db.add(launch)
    await db.flush()
    return launch.id


async def _item(db, world, number, *, nonce=None, launch=None):
    launch = await _launch(db, world, number) if launch else None
    item = GithubWorkItem(scope_id=world.scopes["a1"], issue_number=number, issue_title="I", issue_url=f"u/{number}",
                          github_updated_at=T0, dispatch_status="dispatched", created_at=CREATED,
                          dispatch_nonce=nonce, launch_id=launch)
    db.add(item)
    await db.flush()
    return item


async def _merged(db, item, minutes, launch):
    """The merge fact, bound to the launch whose window it ends."""
    await audit.record_delivery_fact(
        db, item_id=item.id, delivery_outcome="delivered", completion_kind="merged_code",
        fact_source="github_pull_request_merged", fact_time=_m(minutes), artifact=f"o/a1/pull/{item.issue_number}",
        attempt=f"{audit.launch_attempt_key(item.id, launch)}:revision:None",
        launch_attempt=audit.launch_attempt_key(item.id, launch))


async def _effort(db, item, start, end):
    for kind, at in (("effort_wait_started", start), ("effort_wait_ended", end)):
        await audit.record_event(
            db, event_kind=kind, source="test", occurred_at=_m(at), fact_time=_m(at),
            actor=audit.derive_actor(actor_kind="system"), item_id=item.id, after_values={"awaited_role": "leader"},
            operation_id=f"{kind}:message:{item.id}", action_outcome="applied")


async def _watch(db, item, nonce, sha, *, completion=None, reviewer=None):
    watch = GithubRequiredCheckWatch(work_item_id=item.id, dispatch_nonce=nonce, scope_revision=0, pr_number=5,
                                     head_sha=sha, owner_slot_id=1, policy_digest="p", observed_at=_m(1),
                                     changed_at=_m(1), completion_kind=completion, reviewer_member_id=reviewer)
    db.add(watch)
    await db.flush()
    return watch


async def _run(db, world, sha, name, queued, done):
    db.add(GithubCheckRunHistory(repository="o/a1", check_run_id=next(RUNS), head_sha=sha,
                                 scope_id=world.scopes["a1"], pr_number=5, check_name=name, required=True,
                                 job_created_at=_m(queued), started_at=_m(queued), completed_at=_m(done),
                                 observed_at=_m(done)))
    await db.flush()


async def _review(db, item, nonce, watch, noticed, verdict):
    if await db.get(MailTeamMember, 57) is None:  # the verifier's Mail identity, which a notice is addressed to
        db.add(MailTeamMember(id=57, identity_key="v1", repo_id="r", repo_path="/r", repo_name="r", display_name="V1"))
        await db.flush()
    mail = MailMessage(recipient_member_id=57, body_markdown="checks passed", created_at=_m(noticed),
                       delivery_key=f"required-checks:{watch.id}:member:57")
    db.add(mail)
    await db.flush()
    db.add(GithubRequiredCheckRecipient(watch_id=watch.id, member_id=57, message_id=mail.id))
    db.add(GithubHeadReview(work_item_id=item.id, dispatch_nonce=nonce, pr_number=5, head_sha=watch.head_sha,
                            scope_revision=0, reviewer_member_id=57, verdict="accepted", summary="s",
                            evidence_url="u", created_at=_m(verdict)))
    await db.flush()


async def _recording(db, *, checks=True, marker=MARKER):
    await _marker(db, marker)
    db.add(CampaignUpgradeCoverage(id=1, started_at=T0 - timedelta(days=3)))
    if checks:
        db.add(GithubCheckRunHistoryCoverage(id=1, started_at=T0 - timedelta(days=3)))
    await db.flush()


async def _build(db, world, numbers, *, cached=False):
    labelled = [_issue("a1", number, state="closed") for number in numbers]
    campaign = await campaigns.current(db, world.team)
    if campaign is None:
        await db.commit()
        campaign = await campaigns.start(db, world.team, "label:v4.4", started_at=T0, actor=OPERATOR)
    await db.commit()
    return await snapshots.build_snapshot(db, campaign, FakeGithub(labelled={"a1": labelled}), as_of=AS_OF,
                                          cached=cached)


async def _two_items(db, world):
    """A: attempt 1 from 5 (an effort wait 10-20), attempt 2 from 60 (a check 70-80, review 85-95), merged at 120.
    B: dispatched at 30, open to the cutoff at 180. C: a member with no attempt."""
    await _recording(db)
    a = await _item(db, world, 10, nonce="nonce-a2", launch=True)
    await _dispatch(db, a.id, 5, launch=0)  # attempt 1: an earlier launch
    await _effort(db, a, 10, 20)
    await _dispatch(db, a.id, 60, launch=a.launch_id)
    watch = await _watch(db, a, "nonce-a2", "h1", completion="success", reviewer=57)
    await _run(db, world, "h1", "e2e", 70, 80)
    await _review(db, a, "nonce-a2", watch, 85, 95)
    await _merged(db, a, 120, launch=a.launch_id)
    b = await _item(db, world, 11, nonce="nonce-b", launch=True)
    await _dispatch(db, b.id, 30, launch=b.launch_id)
    await _item(db, world, 12)
    return a, b


# The acceptance: stages plus unaccounted equal item time
async def test_stages_and_unaccounted_reconcile_with_item_time_across_attempts(db, world):
    await _two_items(db, world)
    went = (await _build(db, world, [10, 11, 12])).time_went
    # A: 0-60 and 60-120, contiguous; B: 0-180. Item time 300 minutes.
    assert (went.items, went.attempts, went.total_seconds) == (2, 3, 300 * MIN)
    assert went.groups == {"waiting": 45 * MIN, "process": 20 * MIN, "active": 0}
    assert went.unaccounted_seconds == 235 * MIN
    assert sum(went.groups.values()) + went.unaccounted_seconds == went.total_seconds
    assert sum(row.share_seconds for row in went.stages) == sum(went.groups.values())
    assert {(row.group, row.stage, row.actor): row.share_seconds for row in went.stages} == {
        ("waiting", "dispatch", "dispatch"): 35 * MIN, ("waiting", "effort", "leader"): 10 * MIN,
        ("process", "required_check", "o/a1:e2e"): 10 * MIN, ("process", "review", "member:57"): 10 * MIN}


async def test_a_member_without_an_attempt_is_unknown_not_zero_time(db, world):
    await _two_items(db, world)
    went = (await _build(db, world, [10, 11, 12])).time_went
    assert went.unknown_items == 1
    assert "no_attempt_recorded" in went.unknown_reasons
    assert went.coverage == "partial"


async def test_an_item_outside_the_campaign_adds_no_time(db, world):
    await _two_items(db, world)
    outside = await _item(db, world, 13, nonce="nonce-x", launch=True)  # tracked, but not a campaign member
    await _dispatch(db, outside.id, 0, launch=outside.launch_id)
    went = (await _build(db, world, [10, 11, 12])).time_went
    assert (went.items, went.attempts, went.total_seconds) == (2, 3, 300 * MIN)


async def test_each_checks_wall_time_is_kept_beside_its_share(db, world):
    await _recording(db)
    a = await _item(db, world, 10, nonce="nonce-a", launch=True)
    await _dispatch(db, a.id, 0, launch=a.launch_id)
    await _watch(db, a, "nonce-a", "h1")
    await _run(db, world, "h1", "e2e", 10, 20)
    await _run(db, world, "h1", "backend", 10, 20)
    went = (await _build(db, world, [10])).time_went
    checks = {row.actor: (row.share_seconds, row.wall_seconds) for row in went.stages if row.stage == "required_check"}
    assert checks == {"o/a1:e2e": (5 * MIN, 10 * MIN), "o/a1:backend": (5 * MIN, 10 * MIN)}
    assert went.overlap_seconds == 10 * MIN


# Unknown, never zero
async def test_with_no_attempt_every_value_is_unknown(db, world):
    await _recording(db)
    await _item(db, world, 12)
    went = (await _build(db, world, [12])).time_went
    assert (went.total_seconds, went.unaccounted_seconds, went.groups, went.coverage) == (
        None, None, {"waiting": None, "process": None, "active": None}, "unavailable")
    assert (went.unknown_items, went.attempts) == (1, 0)


async def test_unattributed_evidence_is_unknown_with_its_reason(db, world):
    await _recording(db)
    a = await _item(db, world, 10, nonce="nonce-a", launch=True)
    await _dispatch(db, a.id, 0, launch=a.launch_id)
    await audit.record_event(
        db, event_kind="mail_wait_started", source="test", occurred_at=_m(10), fact_time=_m(10),
        actor=audit.derive_actor(actor_kind="system"), team_preset_id=world.team,
        after_values={"owner_member_id": 999, "awaited_member_id": 60}, operation_id="mail_wait_started:message:1",
        action_outcome="applied")
    went = (await _build(db, world, [10])).time_went
    assert (went.unattributed, went.coverage) == (1, "partial")
    assert "no_open_attempt" in went.unknown_reasons
    # V1 review 119: the wait it couldn't place makes waiting a lower bound, not 0 with full coverage
    assert went.group_coverage["waiting"] == "partial"
    assert "waiting: unattributed_evidence" in went.unknown_reasons


async def test_check_runs_recorded_too_late_make_the_section_partial(db, world):
    await _recording(db, checks=False)
    a = await _item(db, world, 10, nonce="nonce-a", launch=True)
    await _dispatch(db, a.id, 0, launch=a.launch_id)
    went = (await _build(db, world, [10])).time_went
    assert went.coverage == "partial"
    assert "check_runs: recording_not_installed" in went.unknown_reasons


# V1 review 118
async def test_a_member_tracked_by_two_rows_with_overlapping_attempts_is_ambiguous_not_doubled(db, world):
    await _recording(db)
    twin = TeamGithubScope(preset_id=world.team, repo_owner="O", repo_name="A1", repo_path="/twin", enabled=True)
    db.add(twin)
    await db.flush()
    first = await _item(db, world, 10)
    second = GithubWorkItem(scope_id=twin.id, issue_number=10, issue_title="I", issue_url="u/twin",
                            github_updated_at=T0, dispatch_status="dispatched", created_at=CREATED)
    db.add(second)
    await db.flush()
    for item in (first, second):
        await _dispatch(db, item.id, 0, launch=0)
    went = (await _build(db, world, [10])).time_went
    assert (went.items, went.attempts, went.unknown_items, went.total_seconds) == (0, 0, 1, None)
    assert "ambiguous_tracking" in went.unknown_reasons
    assert "no_attempt_recorded" not in went.unknown_reasons  # both rows recorded attempts (V1 review 119)


async def test_a_zero_length_second_row_does_not_erase_the_members_time(db, world):
    """V1 review 119: a row created, dispatched and failed at minute 30 shares no positive time with the first."""
    await _recording(db)
    twin = TeamGithubScope(preset_id=world.team, repo_owner="O", repo_name="A1", repo_path="/twin", enabled=True)
    db.add(twin)
    await db.flush()
    first = await _item(db, world, 10)
    await _dispatch(db, first.id, 0, launch=0)
    second = GithubWorkItem(scope_id=twin.id, issue_number=10, issue_title="I", issue_url="u/twin",
                            github_updated_at=T0, dispatch_status="failed", created_at=_m(30))
    db.add(second)
    await db.flush()
    await _dispatch(db, second.id, 30, launch=0)
    await audit.record_event(
        db, event_kind="work_lifecycle", source="test", occurred_at=_m(30), actor=audit.derive_actor(actor_kind="system"),
        item_id=second.id, after_values={"dispatch_status": "failed"},
        context_snapshot={"launch_attempt": audit.launch_attempt_key(second.id, 0)}, action_outcome="applied")
    went = (await _build(db, world, [10])).time_went
    assert (went.items, went.attempts, went.total_seconds) == (1, 2, 180 * MIN)
    assert "ambiguous_tracking" not in went.unknown_reasons


@pytest.mark.parametrize("issue", [999, 10], ids=["another_issue", "the_same_issue"])
async def test_an_item_id_reused_between_the_snapshots_reads_adds_nothing(db, world, monkeypatch, issue):
    """V1 review 119: the team retires and a replacement issue takes the numeric item id between two reads. Even the
    same issue number, tracked by the replacement team, belongs to another team lifetime."""
    from app.services import retro_attempt_collector as collector

    await _recording(db)
    old = await _item(db, world, 10)
    original = collector.collect_team_attempts

    async def racing(session, preset_id, **kwargs):
        campaign = await campaigns.current(session, world.team)
        await session.execute(text("UPDATE factory_context_keys SET retired_at = :t WHERE key_kind IN ('team', 'item')"),
                              {"t": _m(1)})
        await session.execute(text("DELETE FROM github_work_items WHERE id = :i"), {"i": old.id})
        session.add(GithubWorkItem(id=old.id, scope_id=world.scopes["a1"], issue_number=issue, issue_title="R",
                                   issue_url=f"u/{issue}", github_updated_at=T0, dispatch_status="dispatched",
                                   created_at=CREATED))
        await session.flush()
        await _dispatch(session, old.id, 0, launch=0)
        await session.commit()
        assert campaign is not None
        return await original(session, preset_id, **kwargs)

    await _build(db, world, [10])  # start the campaign before the race
    snapshots.clear_cache()
    monkeypatch.setattr(collector, "collect_team_attempts", racing)
    went = (await _build(db, world, [10])).time_went
    assert (went.attempts, went.items, went.total_seconds) == (0, 0, None)


async def test_two_rows_tracking_one_issue_one_after_the_other_still_sum(db, world):
    """A replaced tracking row whose window follows the first one's is not ambiguous: the windows don't overlap."""
    await _recording(db)
    twin = TeamGithubScope(preset_id=world.team, repo_owner="O", repo_name="A1", repo_path="/twin", enabled=True)
    db.add(twin)
    await db.flush()
    first = await _item(db, world, 10)
    await _dispatch(db, first.id, 0, launch=0)
    await audit.record_event(
        db, event_kind="work_lifecycle", source="test", occurred_at=_m(50), actor=audit.derive_actor(actor_kind="system"),
        item_id=first.id, after_values={"dispatch_status": "failed"},
        context_snapshot={"launch_attempt": audit.launch_attempt_key(first.id, 0)}, action_outcome="applied")
    second = GithubWorkItem(scope_id=twin.id, issue_number=10, issue_title="I", issue_url="u/twin",
                            github_updated_at=T0, dispatch_status="dispatched", created_at=_m(55))
    db.add(second)
    await db.flush()
    await _dispatch(db, second.id, 60, launch=0)
    went = (await _build(db, world, [10])).time_went
    assert (went.items, went.attempts, went.total_seconds) == (1, 2, (50 + 125) * MIN)
    assert "ambiguous_tracking" not in went.unknown_reasons


async def test_a_replacement_teams_wait_stays_out_of_the_retired_campaign(db, world):
    await _recording(db)
    a = await _item(db, world, 10, nonce="nonce-a", launch=True)
    await _dispatch(db, a.id, 0, launch=a.launch_id)
    snapshot = await _build(db, world, [10])
    campaign = await campaigns.current(db, world.team)
    await db.execute(text("UPDATE factory_context_keys SET retired_at = :t WHERE key_kind = 'team' "
                          "AND context_key = :k"), {"t": _m(30), "k": campaign.team_context_key})
    await audit.record_event(  # the replacement team reuses the preset id under a new lifetime key
        db, event_kind="mail_wait_started", source="test", occurred_at=_m(40), fact_time=_m(40),
        actor=audit.derive_actor(actor_kind="system"), team_preset_id=world.team,
        after_values={"owner_member_id": 999, "awaited_member_id": 60}, operation_id="mail_wait_started:message:9",
        action_outcome="applied")
    await db.commit()
    snapshots.clear_cache()
    went = (await snapshots.build_snapshot(db, campaign, FakeGithub(labelled={"a1": [_issue("a1", 10, "closed")]}),
                                           as_of=AS_OF, cached=False)).time_went
    assert snapshot.time_went.unattributed == 0
    assert went.unattributed == 0
    assert "no_open_attempt" not in went.unknown_reasons


async def test_lifecycle_recording_that_starts_inside_the_window_is_partial(db, world):
    await _recording(db, marker=_m(45))
    a = await _item(db, world, 10, nonce="nonce-a", launch=True)
    await _dispatch(db, a.id, 60, launch=a.launch_id)
    went = (await _build(db, world, [10])).time_went
    assert went.coverage == "partial"
    assert "dispatch_before_instrumentation" in went.unknown_reasons
    assert went.total_seconds == 180 * MIN  # the known contribution is kept


async def test_without_a_check_run_recorder_process_time_is_unknown_not_zero(db, world):
    await _recording(db, checks=False)
    a = await _item(db, world, 10, nonce="nonce-a", launch=True)
    await _dispatch(db, a.id, 30, launch=a.launch_id)
    went = (await _build(db, world, [10])).time_went
    assert (went.groups["process"], went.group_coverage["process"]) == (None, "unavailable")
    assert (went.groups["waiting"], went.group_coverage["waiting"]) == (30 * MIN, "full")


async def test_check_runs_recorded_late_make_process_time_a_lower_bound(db, world):
    await _recording(db, checks=False)
    db.add(GithubCheckRunHistoryCoverage(id=1, started_at=_m(5)))
    a = await _item(db, world, 10, nonce="nonce-a", launch=True)
    await _dispatch(db, a.id, 0, launch=a.launch_id)
    await _watch(db, a, "nonce-a", "h1")
    await _run(db, world, "h1", "e2e", 10, 20)
    went = (await _build(db, world, [10])).time_went
    assert (went.groups["process"], went.group_coverage["process"]) == (10 * MIN, "partial")  # a lower bound
    assert "check_runs: recording_started_late" in went.unknown_reasons
    (check,) = [row for row in went.stages if row.stage == "required_check"]
    assert check.coverage == "partial"


async def test_a_cached_read_after_a_check_name_change_matches_a_fresh_read(db, world):
    await _two_items(db, world)
    await _build(db, world, [10, 11, 12], cached=True)
    await db.execute(text("UPDATE github_check_run_history SET check_name = 'renamed'"))
    await db.commit()
    cached = (await _build(db, world, [10, 11, 12], cached=True)).time_went
    snapshots.clear_cache()
    fresh = (await _build(db, world, [10, 11, 12])).time_went
    assert cached == fresh
    assert any(row.actor == "o/a1:renamed" for row in fresh.stages)


async def test_stage_rows_carry_their_attempts_coverage_and_the_dispatch_basis(db, world):
    await _two_items(db, world)
    went = (await _build(db, world, [10, 11, 12])).time_went
    rows = {(row.group, row.stage): row for row in went.stages}
    dispatch = rows[("waiting", "dispatch")]
    assert (dispatch.attempts, dispatch.basis, dispatch.coverage) == (
        2, "from campaign membership; ready time not recorded", "full")
    assert (rows[("process", "required_check")].attempts, rows[("process", "review")].attempts) == (1, 1)
    assert went.group_coverage == {"waiting": "full", "process": "full", "active": "full"}


# V1 review 121: a lifetime replaced after the item read and before the evidence read
async def _race(db, world, monkeypatch, replace, *, keyed=False, again=None):
    """Issue 10 (no attempt) is read; then, before its evidence is read, ``replace`` runs and commits. With ``keyed``
    the row already has a lifetime key (an earlier event); ``again`` runs before its second, first-key read."""
    from app.services import retro_attempt_collector as collector

    await _recording(db)
    old = await _item(db, world, 10)
    if keyed:
        await _effort(db, old, -50, -45)
    await _build(db, world, [10])  # the campaign starts before the race
    snapshots.clear_cache()
    original, raced = collector._collect_item, []

    async def interleaved(session, item, *args, **kwargs):
        if item.id == old.id and len(raced) < (2 if again else 1):  # issue 10's own row, after the item read
            raced.append(True)
            await (replace if len(raced) == 1 else again)(session, item)
            await session.commit()
        return await original(session, item, *args, **kwargs)

    monkeypatch.setattr(collector, "_collect_item", interleaved)
    return old, (await _build(db, world, [10])).time_went


async def _replaced_by_999(session, item, *, dispatch=True, request=False):
    item_id = item.id
    session.expunge(item)
    await session.execute(text("DELETE FROM github_work_items WHERE id = :i"), {"i": item_id})
    session.add(GithubWorkItem(id=item_id, scope_id=item.scope_id, issue_number=999, issue_title="R", issue_url="u/999",
                               github_updated_at=T0, dispatch_status="dispatched", created_at=CREATED,
                               dispatch_nonce="nonce-999"))
    await session.flush()
    await _dispatch(session, item_id, 0, launch=0)  # the replacement's own lifetime key
    if dispatch is False:
        await session.execute(text("DELETE FROM factory_audit_events WHERE event_kind = 'work_lifecycle' "
                                   "AND item_id = :i"), {"i": item_id})
    if request:
        for member in (60, 61):  # the request's owner and leader Mail identities
            if await session.get(MailTeamMember, member) is None:
                session.add(MailTeamMember(id=member, identity_key=f"m{member}", repo_id="r", repo_path="/r",
                                           repo_name="r", display_name=f"M{member}"))
        await session.flush()
        session.add(GithubApprovalRequest(work_item_id=item_id, request_kind="initial_plan", dispatch_nonce="nonce-999",
                                          approval_round=1, owner_member_id=61, leader_member_id=60,
                                          request_fingerprint="fp-999", created_at=_m(10)))
    await session.flush()


async def test_a_replacement_committed_after_the_item_read_adds_no_time(db, world, monkeypatch):
    _, went = await _race(db, world, monkeypatch, _replaced_by_999)
    assert (went.attempts, went.items, went.unknown_items, went.total_seconds) == (0, 0, 1, None)
    assert "item_lifetime_changed_during_read" in went.unknown_reasons
    assert "no_attempt_recorded" not in went.unknown_reasons  # it is unknown for the race, not for a missing attempt


async def test_a_replacements_item_owned_rows_add_no_unattributed_evidence(db, world, monkeypatch):
    async def rows_only(session, item):
        await _replaced_by_999(session, item, dispatch=False, request=True)

    _, went = await _race(db, world, monkeypatch, rows_only, keyed=True)  # its numeric-id rows are read
    assert (went.unattributed, went.attempts) == (0, 0)
    assert "item_lifetime_changed_during_read" in went.unknown_reasons


async def test_a_dispatch_on_the_original_lifetime_during_the_same_interleaving_still_counts(db, world, monkeypatch):
    async def own_dispatch(session, item):
        await _dispatch(session, item.id, 0, launch=0)

    _, went = await _race(db, world, monkeypatch, own_dispatch)
    assert (went.attempts, went.items, went.total_seconds) == (1, 1, 180 * MIN)
    assert "item_lifetime_changed_during_read" not in went.unknown_reasons


async def test_a_row_replaced_during_the_first_key_read_is_dropped(db, world, monkeypatch):
    """The original lifetime's first dispatch arrives, then the row is replaced before that read completes."""
    async def own_dispatch(session, item):
        await _dispatch(session, item.id, 0, launch=0)

    _, went = await _race(db, world, monkeypatch, own_dispatch, again=_replaced_by_999)
    assert (went.attempts, went.total_seconds) == (0, None)
    assert "item_lifetime_changed_during_read" in went.unknown_reasons


# The cache: inserts, updates and deletions with no audit event
async def _key(db, world):
    return await snapshots._fingerprint(db, await campaigns.current(db, world.team))


@pytest.mark.parametrize("change", ["insert_check_run", "complete_check_run", "complete_watch", "review_status",
                                    "delete_review", "delete_watch"])
async def test_each_record_change_misses_the_cache(db, world, change):
    await _two_items(db, world)
    await _build(db, world, [10, 11, 12])
    before = await _key(db, world)
    if change == "insert_check_run":
        await _run(db, world, "h1", "lint", 71, 72)
    elif change == "complete_check_run":
        await db.execute(text("UPDATE github_check_run_history SET completed_at = :t"), {"t": _m(79)})
    elif change == "complete_watch":
        await db.execute(text("UPDATE github_required_check_watches SET completed_at = :t"), {"t": _m(90)})
    elif change == "review_status":
        await db.execute(text("UPDATE github_head_reviews SET status = 'superseded'"))
    elif change == "delete_review":
        await db.execute(text("DELETE FROM github_head_reviews"))
    else:
        await db.execute(text("DELETE FROM github_required_check_recipients"))
        await db.execute(text("DELETE FROM github_required_check_watches"))
    await db.commit()
    assert await _key(db, world) != before


# Frozen snapshots
async def test_a_stored_v3_snapshot_without_the_section_reads_back_unchanged(db, world):
    await _two_items(db, world)
    snapshot = await _build(db, world, [10, 11, 12])
    old = snapshot.model_dump(mode="json")
    old.pop("time_went")
    old["calculation_version"] = "campaign-snapshot-v3"
    campaign = await campaigns.current(db, world.team)
    db.add(CampaignSnapshot(campaign_id=campaign.id, version=1, calculation_version="campaign-snapshot-v3",
                            payload=old, created_by="operator"))
    await db.commit()
    frozen = await snapshots.freeze(db, campaign, FakeGithub(), actor=OPERATOR)
    assert (frozen.time_went, frozen.calculation_version) == (None, "campaign-snapshot-v3")
    assert (await snapshots.latest(db, campaign.id)).payload == old


async def test_a_new_freeze_stores_v4_with_the_section(db, world):
    await _two_items(db, world)
    await _build(db, world, [10, 11, 12])
    campaign = await campaigns.current(db, world.team)
    labelled = [_issue("a1", number, state="closed") for number in (10, 11, 12)]
    frozen = await snapshots.freeze(db, campaign, FakeGithub(labelled={"a1": labelled}), actor=OPERATOR, as_of=AS_OF)
    stored = await snapshots.latest(db, campaign.id)
    assert (stored.calculation_version, frozen.calculation_version) == ("campaign-snapshot-v4", "campaign-snapshot-v4")
    assert stored.payload["time_went"]["total_seconds"] == 300 * MIN
