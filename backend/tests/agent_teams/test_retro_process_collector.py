"""#1418 (#1245 part 3): the process collectors, required checks per check and review per verifier.

Process time comes from recorded sources only. A required check runs from its queue start (Actions' job creation, else
its start) to its completion, per repository and check name, on the attempt's watched heads. Review runs from the
review notice a watch delivered to a verifier to that verifier's first verdict on the head. The notice is the later of
the reviewer's binding and the checks' success; no assignment time is recorded, so none is invented.
"""
from __future__ import annotations

from itertools import count

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.database import (
    Base,
    GithubCheckRunHistory,
    GithubHeadReview,
    GithubRequiredCheckRecipient,
    GithubWorkItem,
    MailMessage,
    TeamGithubScope,
)
from app.services.retro_stage_engine import breakdown
from tests.agent_teams.test_retro_attempt_collector import (
    _at,
    _collect,
    _dispatch,
    _lost,
    _revision,
    _revision_verdict,
    _spans,
    _team,
    _watch,
)

pytestmark = pytest.mark.asyncio
RUNS = count(10_000)
V1, V2, LEADER = 57, 58, 60


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'process.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()


async def _watched(db, item, sha, at, *, nonce="nonce-a", reviewer=V1, completion="success", pr=5):
    watch = await _watch(db, item, nonce, sha, at, pr=pr)
    watch.reviewer_member_id, watch.completion_kind = reviewer, completion
    await db.flush()
    return watch


async def _run(db, item, sha, name, queued, started, done, *, repo="example/repo", pr=5, required=True):
    db.add(GithubCheckRunHistory(
        repository=repo, check_run_id=next(RUNS), head_sha=sha, scope_id=item.scope_id, pr_number=pr,
        check_name=name, required=required, job_created_at=None if queued is None else _at(queued),
        started_at=None if started is None else _at(started), completed_at=None if done is None else _at(done),
        observed_at=_at(0)))
    await db.flush()


async def _notice(db, watch, member, at, *, message=True, recipient=None, key=None):
    """The "Required checks success" Mail that the watch delivered to one member, under its delivery key."""
    message_id = None
    if message:
        mail = MailMessage(recipient_member_id=member if recipient is None else recipient, body_markdown="checks passed",
                           created_at=_at(at), delivery_key=f"required-checks:{watch.id}:member:{member}" if key is None
                           else key or None)
        db.add(mail)
        await db.flush()
        message_id = mail.id if message is True else mail.id + 1000  # a deleted message
    db.add(GithubRequiredCheckRecipient(watch_id=watch.id, member_id=member, message_id=message_id))
    await db.flush()


async def _verdict(db, item, sha, member, at, *, nonce="nonce-a", pr=5, revision=0):
    db.add(GithubHeadReview(work_item_id=item.id, dispatch_nonce=nonce, pr_number=pr, head_sha=sha, scope_revision=revision,
                            reviewer_member_id=member, verdict="accepted", summary="s", evidence_url="u",
                            created_at=_at(at)))
    await db.flush()


def _process(attempt):
    return [s for s in _spans(attempt) if s[0] == "process"]


async def _one(db):
    """One attempt (nonce-a, launch 1) dispatched at 0 and still open."""
    team, (item,) = await _team(db)
    await _dispatch(db, item, 1, _at(0), current_nonce="nonce-a")
    return team, item


async def _reviews(db, team, as_of=60):
    (attempt,) = (await _collect(db, team, as_of=as_of)).attempts
    return [s for s in _process(attempt) if s[1] == "review"]


# Required checks per check
async def test_required_checks_split_per_check_across_two_repositories(db):
    team, (first,) = await _team(db)
    scope = TeamGithubScope(preset_id=team.id, repo_owner="o", repo_name="b1", repo_path="/b")
    db.add(scope)
    await db.flush()
    second = GithubWorkItem(scope_id=scope.id, issue_number=9, issue_title="Item", issue_url="u/9",
                            github_updated_at=_at(0), dispatch_status="dispatched", owner_slot_id=first.owner_slot_id,
                            created_at=_at(-30))
    db.add(second)
    await db.flush()
    for item, nonce, sha, repo, name, times in ((first, "nonce-a", "h1", "example/repo", "e2e", (5, 6, 15)),
                                                (second, "nonce-b", "h2", "o/b1", "ci/build", (10, 10, 20))):
        await _dispatch(db, item, 1, _at(0), current_nonce=nonce)
        await _watched(db, item, sha, _at(1), nonce=nonce, completion=None)
        await _run(db, item, sha, name, *times, repo=repo)
    one, two = (await _collect(db, team)).attempts
    assert _process(one) == [("process", "required_check", "example/repo:e2e", 5, 15)]
    assert _process(two) == [("process", "required_check", "o/b1:ci/build", 10, 20)]


async def test_overlapping_checks_share_their_time_and_keep_their_own_wall_time(db):
    team, item = await _one(db)
    await _watched(db, item, "h1", _at(1), completion=None)
    for name in ("e2e", "backend"):
        await _run(db, item, "h1", name, 10, 11, 20)
    (attempt,) = (await _collect(db, team, as_of=30)).attempts
    stages = breakdown(attempt.window, attempt.intervals)
    checks = {s.actor: (s.share_seconds, s.wall_seconds) for s in stages.stages if s.stage == "required_check"}
    assert checks == {"example/repo:e2e": (300, 600), "example/repo:backend": (300, 600)}
    assert stages.groups["process"] == 600
    assert sum(stages.groups.values()) + stages.unaccounted_seconds == stages.total_seconds


async def test_the_queue_start_falls_back_to_the_runs_start(db):
    team, item = await _one(db)
    await _watched(db, item, "h1", _at(1), completion=None)
    await _run(db, item, "h1", "e2e", None, 8, 12)
    (attempt,) = (await _collect(db, team)).attempts
    assert _process(attempt) == [("process", "required_check", "example/repo:e2e", 8, 12)]


async def test_optional_unwatched_and_untimed_runs_are_left_out(db):
    team, item = await _one(db)
    await _watched(db, item, "h1", _at(1), completion=None)
    await _run(db, item, "h1", "lint", 5, 5, 9, required=False)
    await _run(db, item, "h9", "e2e", 5, 5, 9)  # a head the attempt never watched
    await _run(db, item, "h1", "e2e", 5, 5, 9, pr=6)  # the watched commit on another PR
    await _run(db, item, "h1", "backend", 5, 5, None)  # no completion recorded
    await _run(db, item, "h1", "docs", None, None, 9)  # no queue or start recorded
    (attempt,) = (await _collect(db, team)).attempts
    assert _process(attempt) == []


async def test_history_of_another_repository_under_a_reused_scope_id_is_left_out(db):
    """V1 review 443: SQLite reuses a deleted scope's id; its history keeps the old repository's name."""
    team, item = await _one(db)
    await _watched(db, item, "h1", _at(1), completion=None)
    await _run(db, item, "h1", "e2e", 5, 5, 15, repo="old/repository")
    await _run(db, item, "h1", "e2e", 20, 20, 25, repo="Example/Repo".lower())
    (attempt,) = (await _collect(db, team)).attempts
    assert _process(attempt) == [("process", "required_check", "example/repo:e2e", 20, 25)]


async def test_another_repositorys_history_does_not_end_building(db):
    """The same rule for the head times that end building (#1417)."""
    team, item = await _one(db)
    await _watched(db, item, "h2", _at(1), completion=None)
    await _verdict(db, item, "h1", V1, 10)
    review = (await db.scalars(select(GithubHeadReview))).one()
    review.verdict = "changes_required"
    await _run(db, item, "h2", "e2e", 15, 15, 16, repo="old/repository")
    await _run(db, item, "h2", "e2e", 25, 25, 26)
    (attempt,) = (await _collect(db, team)).attempts
    assert [s for s in _spans(attempt) if s[1] == "building"] == [("active", "building", "owner", 10, 25)]


# Review per verifier
async def test_two_verifiers_on_two_heads_split_per_verifier(db):
    team, item = await _one(db)
    first = await _watched(db, item, "h1", _at(1))
    await _notice(db, first, V1, 20)
    await _verdict(db, item, "h1", V1, 30)
    second = await _watched(db, item, "h2", _at(31), reviewer=V2)
    await _notice(db, second, V2, 40)
    await _verdict(db, item, "h2", V2, 45)
    assert await _reviews(db, team) == [("process", "review", "member:57", 20, 30),
                                        ("process", "review", "member:58", 40, 45)]


async def test_a_verdict_on_another_head_does_not_end_this_heads_review(db):
    team, item = await _one(db)
    await _notice(db, await _watched(db, item, "h1", _at(1)), V1, 20)
    await _notice(db, await _watched(db, item, "h2", _at(2)), V1, 25)
    await _verdict(db, item, "h2", V1, 30)
    assert await _reviews(db, team) == [("process", "review", "member:57", 20, 60),
                                        ("process", "review", "member:57", 25, 30)]


@pytest.mark.parametrize("other", ["pr", "scope_revision"])
async def test_a_verdict_in_another_review_context_does_not_end_this_review(db, other):
    """V1 review 443: the same member, dispatch and commit, but another PR or scope revision."""
    team, item = await _one(db)
    await _notice(db, await _watched(db, item, "h1", _at(1)), V1, 20)
    await _verdict(db, item, "h1", V1, 30, **({"pr": 6} if other == "pr" else {"revision": 1}))
    assert await _reviews(db, team) == [("process", "review", "member:57", 20, 60)]


@pytest.mark.parametrize("unrelated", ["another_recipient", "no_delivery_key", "another_watchs_key"])
async def test_a_message_that_is_not_this_watchs_notice_starts_no_review(db, unrelated):
    """V1 review 443: a reused message id can point at an ordinary message."""
    team, item = await _one(db)
    watch = await _watched(db, item, "h1", _at(1))
    if unrelated == "another_recipient":
        await _notice(db, watch, V1, 20, recipient=V2)
    else:
        await _notice(db, watch, V1, 20, key="" if unrelated == "no_delivery_key" else
                      f"required-checks:{watch.id + 1}:member:{V1}")
    await _verdict(db, item, "h1", V1, 45)
    assert await _reviews(db, team) == []


async def test_a_review_ends_at_a_revision_verdict_on_the_watch(db):
    team, item = await _one(db)
    watch = await _watched(db, item, "h1", _at(1))
    await _notice(db, watch, V1, 20)
    await _revision_verdict(db, item, await _revision(db, item, "nonce-a"), watch, _at(28), verdict="accepted")
    assert await _reviews(db, team) == [("process", "review", "member:57", 20, 28)]


async def test_the_bound_reviewers_unended_review_runs_to_the_cutoff(db):
    team, item = await _one(db)
    await _notice(db, await _watched(db, item, "h1", _at(1)), V1, 20)
    assert await _reviews(db, team, as_of=50) == [("process", "review", "member:57", 20, 50)]


@pytest.mark.parametrize("notice", ["no_row", "no_message", "deleted_message"])
async def test_a_missing_review_notice_adds_no_review(db, notice):
    team, item = await _one(db)
    watch = await _watched(db, item, "h1", _at(1))
    if notice != "no_row":
        await _notice(db, watch, V1, 20, message=False if notice == "no_message" else "deleted")
    await _verdict(db, item, "h1", V1, 30)
    assert await _reviews(db, team) == []


async def test_a_verdict_before_the_notice_adds_nothing_and_leaves_nothing_open(db):
    """The verifier reviewed while checks still ran: no recorded start, so the time stays unaccounted."""
    team, item = await _one(db)
    watch = await _watched(db, item, "h1", _at(1))
    await _verdict(db, item, "h1", V1, 15)
    await _notice(db, watch, V1, 20)
    assert await _reviews(db, team) == []


@pytest.mark.parametrize("previous_verdict", [True, False])
async def test_a_replaced_reviewer_counts_only_with_their_own_verdict(db, previous_verdict):
    team, item = await _one(db)
    watch = await _watched(db, item, "h1", _at(1), reviewer=V2)  # rebound from V1 to V2
    await _notice(db, watch, V1, 20)
    await _notice(db, watch, V2, 25)
    if previous_verdict:
        await _verdict(db, item, "h1", V1, 30)
    expected = [("process", "review", "member:58", 25, 60)]
    if previous_verdict:
        expected.insert(0, ("process", "review", "member:57", 20, 30))
    assert await _reviews(db, team) == expected


@pytest.mark.parametrize("own_notice", [True, False])
async def test_a_verdict_by_another_member_counts_only_from_their_own_notice(db, own_notice):
    team, item = await _one(db)
    watch = await _watched(db, item, "h1", _at(1))
    await _notice(db, watch, V1, 20)
    if own_notice:
        await _notice(db, watch, V2, 22)
    await _verdict(db, item, "h1", V2, 30)
    expected = [("process", "review", "member:57", 20, 60)]  # V2's verdict never ends V1's review
    if own_notice:
        expected.append(("process", "review", "member:58", 22, 30))
    assert await _reviews(db, team) == expected


@pytest.mark.parametrize("completion", ["success", "failure"])
async def test_a_leader_or_owner_notice_alone_adds_no_review(db, completion):
    team, item = await _one(db)
    watch = await _watched(db, item, "h1", _at(1), reviewer=None, completion=completion)
    await _notice(db, watch, LEADER, 20)
    await _notice(db, watch, 61, 20)  # the owner, on a failure
    assert await _reviews(db, team) == []


async def test_a_failed_watch_gives_its_reviewer_no_review(db):
    team, item = await _one(db)
    await _notice(db, await _watched(db, item, "h1", _at(1), completion="failure"), V1, 20)
    assert await _reviews(db, team) == []


# Attribution stays #1417's
async def test_a_watch_of_an_unproven_nonce_adds_no_process_time(db):
    team, (item,) = await _team(db)
    await _dispatch(db, item, 1, _at(0), current_nonce="nonce-z")
    watch = await _watched(db, item, "h1", _at(1))
    await _run(db, item, "h1", "e2e", 5, 5, 9)
    await _notice(db, watch, V1, 20)
    result = await _collect(db, team)
    assert _process(result.attempts[0]) == []
    assert ("check_watch", "attempt_unproven") in _lost(result)


async def test_checks_and_review_are_clipped_to_the_campaign_and_the_attempt(db):
    team, item = await _one(db)
    watch = await _watched(db, item, "h1", _at(1))
    await _run(db, item, "h1", "e2e", -20, -15, 10)  # queued before the campaign start at -10
    await _notice(db, watch, V1, 20)
    result = await _collect(db, team, as_of=30)
    assert _process(result.attempts[0]) == [("process", "required_check", "example/repo:e2e", -10, 10),
                                            ("process", "review", "member:57", 20, 30)]
    assert result.unattributed == []
