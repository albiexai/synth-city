"""#1164: an escalated item whose current revision was superseded settles when its PR merges at an accepted head.

Live: WI233 (#1055, PR #1152) was escalated for owner_idle_timeout and its revision 3 was superseded (cancelled through
the Leader's tool); the PR then merged at V1's accepted head and the item stayed escalated, because the escalated-merge
settlement considered only active, submitted or exhausted revisions. A superseded revision has no submitted head, so
only an active acceptance of the exact merged head (same nonce and PR) settles it, and the revision stays superseded.
"""
from __future__ import annotations

import pytest
from sqlalchemy import update

from app.models.database import GithubHeadReview
from tests.agent_teams.test_github_verification_service import db  # noqa: F401
from tests.agent_teams.test_merged_item_settlement import HEAD, _escalated_with, _row_values, _settlements, _tick

OTHER = "c" * 40


@pytest.mark.asyncio
async def test_a_superseded_revision_settles_when_its_pr_merged_at_an_accepted_head(db):
    scope, item, revision, client = await _escalated_with(db, "superseded", submitted=None, accepted=HEAD)
    item.escalation_reason = "owner_idle_timeout"
    await db.commit()
    await _tick(db, scope, client, item, revision)
    assert item.dispatch_status == "merged" and item.escalation_reason is None
    assert revision.status == "superseded" and revision.completed_at is None  # a cancelled revision stays cancelled
    assert [(e.item_id, e.revision_id, e.sanitized_reason) for e in await _settlements(db)] == [
        (item.id, revision.id, "pr_merged_while_escalated_with_superseded_revision")]
    await _tick(db, scope, client, item, revision)
    assert len(await _settlements(db)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "no_acceptance", "submitted_head_only", "other_head", "inactive", "other_nonce", "other_pr", "rejected",
    "closed_unmerged", "not_implementation",
])
async def test_a_superseded_revision_needs_an_active_exact_acceptance_of_a_merged_head(db, case):
    submitted = HEAD if case == "submitted_head_only" else None
    accepted = None if case in {"no_acceptance", "submitted_head_only"} else (OTHER if case == "other_head" else HEAD)
    scope, item, revision, client = await _escalated_with(db, "superseded", submitted=submitted, accepted=accepted)
    if case == "inactive":
        await db.execute(update(GithubHeadReview).values(status="superseded"))
    elif case == "other_nonce":
        await db.execute(update(GithubHeadReview).values(dispatch_nonce="another-attempt"))
    elif case == "other_pr":
        await db.execute(update(GithubHeadReview).values(pr_number=6))
    elif case == "rejected":
        await db.execute(update(GithubHeadReview).values(verdict="changes_required"))
    elif case == "closed_unmerged":
        client.pull.update(merged=False, merged_at=None, state="closed")
    elif case == "not_implementation":
        revision.phase = "diagnostic"
    await db.commit()
    item_before, revision_before = _row_values(item), _row_values(revision)
    await _tick(db, scope, client, item, revision)
    assert _row_values(item) == item_before
    assert _row_values(revision) == revision_before
    assert await _settlements(db) == []
