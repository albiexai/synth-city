"""#1007: a check run GitHub reports inconsistently never becomes an HTTP 500.

The observation stays strict: a row that is neither pending without a conclusion nor completed with a known one is
refused with ``invalid_execution_state``, because reading it as green could pass a bad head. What changes is that
settlement answers it with a 409 that carries a fixed reason code, as the poll loop already answered it with a
pending note. No GitHub text reaches a response.
"""
from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from app.models.database import MailMessage
from app.services.github_approval_service import GithubApprovalError
from app.services.github_check_observation import GithubCheckObservationError, observe_checks
from app.services.github_client import github_client
from app.services.github_verification_service import github_verification_service
from tests.agent_teams import test_github_check_observation as observation_tests
from tests.agent_teams.test_github_verification_service import _Client, _item, _scope, db as _db  # noqa: F401
from tests.agent_teams.test_github_verification_service import db as db_gate  # noqa: F401
from tests.agent_teams.test_head_acceptance import enforcement  # noqa: F401  (autouse fixture)
from tests.agent_teams.test_scope_amendments import (  # noqa: F401  (fixtures and helpers shared)
    attempt_facts, db, healthy_attempt, record_accepted, require_jobs, settle_accepted, submitted_attempt,
)

SHAPES = {
    "in_progress_with_a_conclusion": ("in_progress", "success"),
    "completed_without_a_conclusion": ("completed", None),
}
HEAD = "a" * 40


def _api(monkeypatch, head, shape, *, consistent=False):
    """The GitHub API as the observation reads it, with one check run in the given state."""
    monkeypatch.setattr(observation_tests, "SHA", head)
    status, conclusion = ("completed", "success") if consistent else SHAPES[shape]
    return observation_tests.API(
        [observation_tests.check(status=status, conclusion=conclusion)], [observation_tests.run()],
        {10: [observation_tests.job(status=status, conclusion=conclusion)]})


def _serve(api):
    async def served(owner, repo, ref, token=None):
        async with httpx.AsyncClient(transport=httpx.MockTransport(api), base_url="https://api.github.com") as http:
            # The scripted API describes owner/repo; the caller's owner and repository names do not matter here.
            return await observe_checks(http, {"Authorization": "Bearer polling-token"}, "owner", "repo", ref)
    return served


# --- the observation stays strict -----------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(SHAPES))
async def test_an_inconsistent_run_is_refused_by_the_observation_with_a_fixed_reason(monkeypatch, shape):
    served = _serve(_api(monkeypatch, HEAD, shape))
    with pytest.raises(GithubCheckObservationError) as caught:
        await served("o", "r", HEAD)
    assert str(caught.value) == "invalid_execution_state"


@pytest.mark.asyncio
async def test_a_consistent_run_is_still_observed(monkeypatch):
    rows = await _serve(_api(monkeypatch, HEAD, "x", consistent=True))("o", "r", HEAD)
    assert rows and all(row["status"] == "completed" and row["conclusion"] == "success" for row in rows)


# --- settlement answers it with a 409 and a fixed reason ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(SHAPES))
async def test_settlement_refuses_an_inconsistent_run_with_a_409_and_changes_nothing(
        db, submitted_attempt, monkeypatch, shape):
    a = submitted_attempt
    await require_jobs(db, a, monkeypatch)
    monkeypatch.setattr(github_client, "list_check_runs_for_ref", _serve(_api(monkeypatch, a.head, shape)))
    fact = await record_accepted(db, a)
    before = await attempt_facts(db, a)
    with pytest.raises(GithubApprovalError) as caught:
        await settle_accepted(db, a, fact)
    assert caught.value.status_code == 409
    assert caught.value.detail == "checks_not_observable:invalid_execution_state"
    await db.refresh(fact)
    assert fact.status == "active" and a.revision.status == "submitted" and a.revision.completed_at is None
    assert await attempt_facts(db, a) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["observation_limit", "github_checks_unavailable", "unsafe_observation_location"])
async def test_settlement_carries_only_the_observations_fixed_reason(db, submitted_attempt, monkeypatch, error):
    a = submitted_attempt
    await require_jobs(db, a, monkeypatch)

    async def unavailable(owner, repo, ref, token=None):
        raise GithubCheckObservationError(error)

    monkeypatch.setattr(github_client, "list_check_runs_for_ref", unavailable)
    fact = await record_accepted(db, a)
    with pytest.raises(GithubApprovalError) as caught:
        await settle_accepted(db, a, fact)
    assert (caught.value.status_code, caught.value.detail) == (409, f"checks_not_observable:{error}")


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(SHAPES))
async def test_the_settlement_route_returns_a_409_with_the_reason_not_a_500(db, submitted_attempt, monkeypatch, shape):
    from fastapi import HTTPException

    from app.api.v1 import agent_teams
    from app.models.database import MailAgentSession
    from app.models.schemas import GithubSubmittedRevisionEnd
    a = submitted_attempt
    await require_jobs(db, a, monkeypatch)
    monkeypatch.setattr(github_client, "list_check_runs_for_ref", _serve(_api(monkeypatch, a.head, shape)))
    fact = await record_accepted(db, a)
    body = GithubSubmittedRevisionEnd(review_id=fact.id, dispatch_nonce=a.item.dispatch_nonce, reason="Settle")
    item_id, leader_id = a.item.id, a.leader.id
    leader_session = await db.scalar(select(MailAgentSession).where(MailAgentSession.member_id == leader_id))
    with pytest.raises(HTTPException) as refused:
        await agent_teams.settle_github_accepted_revision(item_id, body, leader_session, db)
    assert refused.value.status_code == 409
    assert refused.value.detail == "checks_not_observable:invalid_execution_state"


@pytest.mark.asyncio
async def test_a_consistent_pending_run_still_gives_the_old_required_checks_refusal(db, submitted_attempt, monkeypatch):
    """Not every unsettled case is an observation failure: a pending job keeps its own refusal."""
    a = submitted_attempt
    runs = [{"name": "Full backend regression tests", "status": "in_progress", "conclusion": None,
             "app": {"slug": "github-actions"}, "head_sha": a.head},
            {"name": "e2e", "status": "completed", "conclusion": "success", "app": {"slug": "github-actions"},
             "head_sha": a.head}]
    await require_jobs(db, a, monkeypatch, runs=runs)
    fact = await record_accepted(db, a)
    with pytest.raises(GithubApprovalError, match="required_checks_not_successful"):
        await settle_accepted(db, a, fact)


# --- verification and the auto-merge gate keep the item pending, charge nothing and record the reason ----------------


class _ObservedClient(_Client):
    """A client whose check runs come through the real observation of a scripted GitHub API."""

    def __init__(self, api, **kwargs):
        super().__init__(**kwargs)
        self.pull["head"] = {**self.pull["head"], "sha": HEAD}
        self._serve = _serve(api)

    async def list_check_runs_for_ref(self, owner, repo, ref, *, token=None):
        return await self._serve(owner, repo, ref)


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(SHAPES))
async def test_the_poll_loop_keeps_a_verifying_item_pending_and_records_the_reason(_db, monkeypatch, shape):
    session = _db
    scope = await _scope(session, merge_policy="auto", max_verification_retries=2)
    item = await _item(session, scope, dispatch_status="verifying", pr_number=5, retry_count=1, last_verified_sha=HEAD)
    client = _ObservedClient(_api(monkeypatch, HEAD, shape))
    await github_verification_service.process_scope(session, scope, client=client)  # no exception
    await session.refresh(item)
    assert item.dispatch_status == "verifying" and item.retry_count == 1 and item.diagnostic_retry_count == 0
    assert item.escalation_reason is None and item.auto_merged_at is None
    assert client.merge_calls == client.ready_calls == 0
    assert "checks are unavailable" in item.status_note and "invalid_execution_state" in item.status_note
    assert not (await session.execute(select(MailMessage))).scalars().all()
    # The next poll reads the run again: once GitHub reports it consistently, the note is no longer the reason.
    consistent = _ObservedClient(_api(monkeypatch, HEAD, shape, consistent=True))
    await github_verification_service.process_scope(session, scope, client=consistent)
    await session.refresh(item)
    assert "invalid_execution_state" not in (item.status_note or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(SHAPES))
async def test_the_auto_merge_gate_does_not_merge_on_an_inconsistent_run_and_charges_nothing(db_gate, monkeypatch, shape):
    """An accepted, ready item at the merge gate: the observation fails, nothing merges, no retry is charged."""
    from tests.agent_teams.test_head_acceptance import HEAD_A, make_case
    assert HEAD_A == HEAD
    case = await make_case(db_gate, monkeypatch)
    await case.record(HEAD_A)
    retries = case.item.retry_count
    case.client.list_check_runs_for_ref = _serve(_api(monkeypatch, HEAD_A, shape))
    await case.tick()  # no exception
    assert case.client.merge_calls == 0 and case.item.dispatch_status == "ready_for_review"
    assert case.item.retry_count == retries and case.item.escalation_reason is None and case.item.auto_merged_at is None
    assert "checks are unavailable" in case.item.status_note and "invalid_execution_state" in case.item.status_note
    case.client.list_check_runs_for_ref = _serve(_api(monkeypatch, HEAD_A, shape, consistent=True))
    await case.tick()
    assert case.item.dispatch_status == "merged" or "invalid_execution_state" not in (case.item.status_note or "")
