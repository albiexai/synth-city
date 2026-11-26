"""#1310 (#1238 part 2): each Synth City merge attempt and its outcome, and the classification of every merged PR.

The attempt is committed before the merge call's existing fences, which stay the last checks before the call. Its
outcome is `merged`, `refused` (the call failed, or a fence stopped it after the record) or `uncertain` (a timeout or
a crash). A merged PR is `automatic` with a confirmed attempt, `outside` with none or only refused ones, and `unknown`
with an uncertain one or before the recording started.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from app import database
from app.models.database import (
    AgentTeamPreset, GithubMergeAttempt, GithubMergeAttemptCoverage, GithubWorkItem, TeamGithubScope,
)
from app.services import github_coordination_service as coordination
from app.services import github_merge_attempt_service as attempts
from app.services.github_client import GithubClientResponseError
from app.services.github_rate_limit_service import GithubRateLimited
from app.services.github_verification_service import github_verification_service as service
from tests.agent_teams.test_github_verification_service import _HoldClient, _item, _scope, db  # noqa: F401
from tests.agent_teams.test_head_acceptance import twin  # noqa: F401
from tests.agent_teams.test_superseded_revision_promotion import HEAD, superseded_case
from tests.factory.test_factory_audit_metrics import _install_marker, _seed_team, _seed_work, store  # noqa: F401
from tests.factory.test_pull_request_facts import _Client as _FactsClient
from tests.factory.test_pull_request_facts import _facts, _merge, _poll, _pull

pytestmark = pytest.mark.asyncio
GREEN = [{"name": "ci", "status": "completed", "conclusion": "success"}]


async def _attempts(session):
    return (await session.scalars(select(GithubMergeAttempt).order_by(GithubMergeAttempt.id))).all()


@pytest.fixture
def ordinary(monkeypatch):
    """As test_github_verification_service runs the other merge gates: the reviewer-acceptance gate open (it is
    covered by test_head_acceptance)."""
    from unittest.mock import AsyncMock

    from app.config import settings
    from app.services import github_head_acceptance_service as acceptance

    monkeypatch.setattr(settings, "mail_capability_tokens_required", True)
    monkeypatch.setattr(acceptance, "gate_note", AsyncMock(return_value=None))


async def _ordinary(db, error=None):
    """A green, unheld PR on the ordinary automatic-merge path."""
    scope = await _scope(db, merge_policy="auto", review_hold_label="review-hold")
    item = await _item(db, scope, dispatch_status="ready_for_review", pr_number=5, last_verified_sha="sha")
    client = _HoldClient(check_runs=GREEN)
    client.merge_error = error
    return scope, item, client


# Outcomes on the ordinary path
async def test_a_confirmed_merge_records_one_merged_attempt(db, ordinary):
    scope, item, client = await _ordinary(db)
    await service.process_scope(db, scope, client=client)
    assert item.dispatch_status == "merged" and client.merge_calls == 1
    (attempt,) = await _attempts(db)
    assert (attempt.status, attempt.scope_id, attempt.work_item_id, attempt.pr_number, attempt.head_sha) == (
        "merged", scope.id, item.id, 5, "sha")
    assert attempt.finished_at is not None and attempt.scope_context_key is not None


def _status_error(code):
    request = httpx.Request("PUT", "https://api.github.com/repos/o/r/pulls/5/merge")
    return httpx.HTTPStatusError("refused", request=request, response=httpx.Response(code, request=request))


@pytest.mark.parametrize("error,status,reason", [
    (_status_error(409), "refused", "github_409"),
    (_status_error(403), "refused", "github_403"),
    (_status_error(502), "uncertain", "github_502"),
])
async def test_a_failed_call_records_its_outcome_and_keeps_its_handling(db, ordinary, error, status, reason):
    scope, item, client = await _ordinary(db, error)
    await service.process_scope(db, scope, client=client)
    assert client.merge_calls == 1 and item.dispatch_status != "merged"
    (attempt,) = await _attempts(db)
    assert (attempt.status, attempt.reason) == (status, reason) and attempt.finished_at is not None


@pytest.mark.parametrize("error,status,reason", [
    (httpx.ReadTimeout("slow"), "uncertain", "ReadTimeout"),
    (httpx.RemoteProtocolError("dropped"), "uncertain", "RemoteProtocolError"),
    (httpx.ConnectError("no route"), "refused", "ConnectError"),
    (httpx.ConnectTimeout("no route"), "refused", "ConnectTimeout"),
])
async def test_a_transport_failure_is_uncertain_once_sent_and_refused_before(db, ordinary, error, status, reason):
    scope, _item_row, client = await _ordinary(db, error)
    await service.process_scope(db, scope, client=client)
    (attempt,) = await _attempts(db)
    assert (attempt.status, attempt.reason) == (status, reason)


@pytest.mark.parametrize("error,status", [
    (GithubRateLimited(datetime.utcnow() + timedelta(minutes=5),
                       httpx.Request("PUT", "https://api.github.com/repos/o/r/pulls/5/merge")), "refused"),
    (GithubClientResponseError("GitHub merge response was not valid JSON"), "uncertain"),
])
async def test_a_propagating_failure_is_recorded_before_it_propagates(db, ordinary, error, status):
    scope, _item_row, client = await _ordinary(db, error)
    with pytest.raises(type(error)):
        await service.process_scope(db, scope, client=client)
    await db.rollback()
    (attempt,) = await _attempts(db)
    assert attempt.status == status


async def test_a_crash_during_the_call_leaves_an_unfinished_attempt(db, ordinary):
    scope, _item_row, client = await _ordinary(db)

    async def crash(*_args, **_kwargs):
        raise SystemExit("the process stopped during the call")

    client.merge_pull = crash
    with pytest.raises(SystemExit):
        await service.process_scope(db, scope, client=client)
    await db.rollback()
    (attempt,) = await _attempts(db)
    assert (attempt.status, attempt.finished_at) == ("attempted", None)


# The fences stay last (the superseded-revision path, which has them)
@pytest.fixture
def superseded(monkeypatch):
    """The superseded-revision path's own enforcement settings (test_superseded_revision_promotion)."""
    from unittest.mock import AsyncMock

    from app.config import settings
    from app.services.github_approval_service import github_approval_service

    monkeypatch.setattr(settings, "mail_capability_tokens_required", True)
    monkeypatch.setattr(github_approval_service, "github_read_token", AsyncMock(return_value=None))


async def test_the_attempt_is_committed_before_the_call(twin, superseded):
    db, maker = twin
    case = await superseded_case(db)
    original, seen = case.client.merge_pull, []

    async def merge_pull(*args, **kwargs):
        async with maker() as other:  # a second connection sees only committed rows
            seen.extend((row.status, row.head_sha) for row in await _attempts(other))
        return await original(*args, **kwargs)

    case.client.merge_pull = merge_pull
    await case.tick()
    assert seen == [("attempted", HEAD)]
    assert case.item.dispatch_status == "merged"
    assert [row.status for row in await _attempts(db)] == ["merged"]


async def test_a_hold_at_the_last_read_refuses_the_recorded_attempt(twin, superseded, monkeypatch):
    """The HOLD read follows the record: a HOLD there leaves a refused attempt and no merge."""
    db, _maker = twin
    case = await superseded_case(db)
    monkeypatch.setattr(coordination, "hold_code", lambda: "hold")
    await case.tick()
    assert case.client.merge_calls == 0
    assert [(row.status, row.reason) for row in await _attempts(db)] == [("refused", "hold")]


async def test_withdrawn_authority_at_the_fence_refuses_the_recorded_attempt(twin, superseded, monkeypatch):
    """The authority fence follows the record: a change committed after the last awaited check refuses it."""
    db, maker = twin
    case = await superseded_case(db)
    original = service._settled_context_note

    async def boundary(*args, **kwargs):
        note = await original(*args, **kwargs)
        async with maker() as other:
            await other.execute(update(GithubWorkItem).where(GithubWorkItem.id == case.ids["item"])
                                .values(handoff_state="requested"))
            await other.commit()
        return note

    monkeypatch.setattr(service, "_settled_context_note", boundary)
    await case.tick()
    assert case.client.merge_calls == 0
    assert [(row.status, row.reason) for row in await _attempts(db)] == [("refused", "authority_changed")]


async def test_nothing_is_recorded_when_an_earlier_gate_stops_the_merge(db, ordinary):
    scope, _item_row, client = await _ordinary(db)
    client.pull["labels"] = [{"name": "review-hold"}]
    await service.process_scope(db, scope, client=client)
    assert client.merge_calls == 0 and await _attempts(db) == []


# Coverage
async def test_installation_sets_the_coverage_start_once(db, monkeypatch):
    monkeypatch.setattr(database, "engine", db.bind)
    await database.init_db()
    started = await db.scalar(select(GithubMergeAttemptCoverage.started_at))
    assert started is not None
    await db.commit()
    await database.init_db()
    assert (await db.scalars(select(GithubMergeAttemptCoverage.started_at))).all() == [started]


# The classification in the pull request facts (#1238)
async def _coverage(store, started_at):
    async with store() as session:
        session.add(GithubMergeAttemptCoverage(id=1, started_at=started_at))
        await session.commit()


async def _attempt(store, item_id, number, status):
    async with store() as session:
        item = await session.get(GithubWorkItem, item_id)
        scope = await session.get(TeamGithubScope, item.scope_id)
        attempt_id = await attempts.begin(session, scope_id=item.scope_id, item_id=item.id, pr_number=number,
                                          head_sha="d" * 40, repository=f"{scope.repo_owner}/{scope.repo_name}")
        if status != "attempted":
            await attempts.finish(session, attempt_id, status, "test")
        await session.commit()


@pytest.mark.parametrize("recorded,path", [
    (["merged"], "automatic"),
    (["refused", "merged"], "automatic"),
    (["refused"], "outside"),
    ([], "outside"),
    (["uncertain"], "unknown"),
    (["refused", "attempted"], "unknown"),
])
async def test_each_merged_pull_is_classified_by_its_attempts(store, recorded, path):
    await _install_marker(store)
    await _seed_team(store)
    await _coverage(store, datetime(2026, 10, 1))  # before the pull's merge (2026-10-07)
    await _seed_work(store, item_id=301, status="merged", pr_number=90)
    for status in recorded:
        await _attempt(store, 301, 90, status)
    await _merge(store, 301, 90, seed=False)
    await _poll(store, _FactsClient())
    (fact,) = await _facts(store)
    assert (fact["after"]["merge_path"], fact["after"]["merged_by"]) == (path, "shared-account")


@pytest.mark.parametrize("coverage", [None, datetime(2026, 10, 8)])
async def test_a_pull_merged_before_the_recording_started_stays_unknown(store, coverage):
    await _install_marker(store)
    await _seed_team(store)
    if coverage is not None:
        await _coverage(store, coverage)  # after the pull's merge
    await _merge(store, 302, 91)
    await _poll(store, _FactsClient())
    (fact,) = await _facts(store)
    assert fact["after"]["merge_path"] == "unknown"


async def test_another_pulls_attempt_classifies_nothing(store):
    await _install_marker(store)
    await _seed_team(store)
    await _coverage(store, datetime(2026, 10, 1))
    await _seed_work(store, item_id=303, status="merged", pr_number=92)
    await _attempt(store, 303, 93, "merged")  # pull 93, not 92
    await _merge(store, 303, 92, seed=False)
    await _poll(store, _FactsClient())
    (fact,) = await _facts(store)
    assert fact["after"]["merge_path"] == "outside"



async def test_an_attempt_keeps_its_first_outcome(db, ordinary):
    scope, item, _client = await _ordinary(db)
    attempt_id = await attempts.begin(db, scope_id=scope.id, item_id=item.id, pr_number=5, head_sha="sha",
                                      repository="o/r")
    await attempts.finish(db, attempt_id, attempts.REFUSED, "hold")
    await attempts.finish(db, attempt_id, attempts.MERGED, None)  # a late writer can't rewrite it
    await db.commit()
    (attempt,) = await _attempts(db)
    assert (attempt.status, attempt.reason) == ("refused", "hold")


# V1 review 6074761059: an attempt belongs to its repository and pull request, whichever scope observes the merge
async def test_a_scope_moved_to_another_repository_does_not_borrow_its_old_attempts(store):
    await _install_marker(store)
    await _seed_team(store)
    await _coverage(store, datetime(2026, 10, 1))
    await _seed_work(store, item_id=304, status="merged", pr_number=90)
    await _attempt(store, 304, 90, "merged")  # the old repository's pull 90
    async with store() as session:
        scope = await session.get(TeamGithubScope, 5)
        scope.repo_owner, scope.repo_name = "moved-owner", "moved-repo"
        await session.commit()
    await _seed_work(store, item_id=305, status="merged", pr_number=90)
    await _merge(store, 305, 90, seed=False)  # the new repository's pull 90
    await _poll(store, _FactsClient({90: _pull(90, repo="moved-owner/moved-repo")}))
    (fact,) = await _facts(store)
    assert fact["after"]["merge_path"] == "outside"


async def test_a_second_team_watching_the_repository_sees_the_first_teams_confirmed_merge(store):
    await _install_marker(store)
    await _seed_team(store)
    await _coverage(store, datetime(2026, 10, 1))
    async with store() as session:
        first = await session.get(TeamGithubScope, 5)
        other = AgentTeamPreset(name="Second team")
        session.add(other)
        await session.flush()
        session.add(TeamGithubScope(id=7, preset_id=other.id, repo_owner=first.repo_owner, repo_name=first.repo_name,
                                    repo_path="/tmp/second-team", base_ref="origin/main", github_auth_mode="ambient",
                                    merge_policy="human"))
        await session.commit()
    await _seed_work(store, item_id=306, status="merged", pr_number=94)
    await _attempt(store, 306, 94, "merged")  # the first team's automatic merge
    await _merge(store, 307, 94, scope_id=7)  # the second team observes the same pull request
    await _poll(store, _FactsClient(), scope_id=7)
    (fact,) = await _facts(store)
    assert fact["after"]["merge_path"] == "automatic"


# V1 review 6074761059: the ordinary path's final authority checks follow the durable attempt
@pytest.mark.parametrize("kind,reason", [("settlement", "settlement_changed"), ("acceptance", "acceptance_changed")])
async def test_authority_withdrawn_after_the_attempt_refuses_it_on_the_ordinary_path(twin, superseded, monkeypatch,
                                                                                    kind, reason):
    from app.models.database import GithubHeadReview
    from tests.agent_teams.test_settled_revision_promotion import _withdraw, settled_case

    db, maker = twin
    case = await settled_case(db)
    statement = _withdraw(case, "revision_superseded") if kind == "settlement" else (
        update(GithubHeadReview).where(GithubHeadReview.work_item_id == case.item.id)
        .values(status="superseded", ended_at=datetime.utcnow()))
    original_begin, original_note, state = attempts.begin, service._acceptance_note, {"begun": False, "fired": False}

    async def begin(*args, **kwargs):
        state["begun"] = True
        return await original_begin(*args, **kwargs)

    async def acceptance_note(*args, **kwargs):
        if state["begun"] and not state["fired"]:  # the attempt is committed; withdraw before the final checks
            state["fired"] = True
            async with maker() as other:
                await other.execute(statement)
                await other.commit()
        return await original_note(*args, **kwargs)

    monkeypatch.setattr(attempts, "begin", begin)
    monkeypatch.setattr(service, "_acceptance_note", acceptance_note)
    await case.tick()
    assert state["fired"] and case.client.merge_calls == 0
    assert [(row.status, row.reason) for row in await _attempts(db)] == [("refused", reason)]


async def test_with_nothing_withdrawn_the_ordinary_settled_path_still_merges(twin, superseded):
    from tests.agent_teams.test_settled_revision_promotion import settled_case

    db, _maker = twin
    case = await settled_case(db)
    await case.tick()
    assert case.client.merge_calls == 1 and case.item.dispatch_status == "merged"
    assert [(row.status, row.repository) for row in await _attempts(db)] == [("merged", "o/r")]


async def test_a_repository_spelled_with_capitals_still_matches_its_attempt(store):
    """GitHub owner and repository names are case-insensitive, as #1238's facts treat them."""
    await _install_marker(store)
    await _seed_team(store)
    await _coverage(store, datetime(2026, 10, 1))
    async with store() as session:
        scope = await session.get(TeamGithubScope, 5)
        scope.repo_owner, scope.repo_name = "Matrix-Owner", "Matrix-Repo"
        await session.commit()
    await _seed_work(store, item_id=308, status="merged", pr_number=95)
    await _attempt(store, 308, 95, "merged")
    await _merge(store, 308, 95, seed=False)
    await _poll(store, _FactsClient({95: _pull(95, repo="Matrix-Owner/Matrix-Repo")}))
    (fact,) = await _facts(store)
    assert fact["after"]["merge_path"] == "automatic"
