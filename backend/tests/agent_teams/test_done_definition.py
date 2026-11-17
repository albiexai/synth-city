"""#1106 (U9): one definition of done, merged plus completed, in every view that counts done items.

Campaign Progress, the Finished category behind the tile and Work, work-item progress and the team summary's
remaining work all read the same item set the same way. The totals still differ by population (the campaign counts
its backlog, the tile every item), which is disclosed, not a second definition.
"""
import pytest
from sqlalchemy import select

from app.models.database import GithubWorkItem
from app.services import factory_projection_service as projection
from app.services.github_work_progress_service import _next_step
from app.services.work_item_states import ATTENTION_STATUSES, DONE_STATUSES, is_done, needs_attention
from tests.agent_teams.test_agent_team_campaign import Backlog, fixed_campaign, issue, observe_backlog
from tests.agent_teams.test_agent_team_summary import db  # noqa: F401  (the summary's database fixture)


def test_done_is_merged_or_completed():
    assert DONE_STATUSES == ("merged", "completed")
    assert is_done("merged") and is_done("completed")
    assert not any(is_done(status) for status in ("dispatched", "verifying", "escalated", "failed", "pending", None))


@pytest.mark.asyncio
async def test_every_view_counts_the_same_done_items(db, monkeypatch):
    statuses = ("merged", "merged", "completed", "dispatched")
    team, scope, items, summary = await fixed_campaign(db, monkeypatch, statuses)
    items[1].dispatch_base_ref = "origin/another-branch"  # merged, but into another branch
    await db.commit()
    await observe_backlog(db, scope, summary, Backlog([issue(n, f"Issue {n}") for n in range(1, 5)]))
    done = {item.id for item in items if item.dispatch_status in ("merged", "completed")}
    assert len(done) == 3

    result = await summary.summary(db, team.id)
    # Campaign Progress: every done item, with the other-branch merge disclosed.
    assert result.campaign.done_count == 3 and result.campaign.done_on_other_branch_count == 1
    # Team summary: everything that is not done is remaining.
    assert result.remaining_work.unfinished_items == len(items) - len(done) == 1
    # The Finished tile and Work.
    counts = await projection.counts(db, select(GithubWorkItem).where(GithubWorkItem.scope_id == scope.id))
    assert (counts.finished, counts.total) == (3, 4)
    # Work-item progress: exactly the done items are complete. Read fresh rows: the summary's commit expired the seeds.
    rows = (await db.scalars(select(GithubWorkItem).where(GithubWorkItem.scope_id == scope.id))).all()
    await db.refresh(scope)
    await db.refresh(team)
    complete = {row.id for row in rows if _next_step(row, scope, team, None, None)[0] == "complete"}
    assert complete == done


def test_needing_attention_is_escalated_or_failed():
    assert ATTENTION_STATUSES == ("escalated", "failed")
    assert projection.CATEGORY_STATUSES["attention"] == ATTENTION_STATUSES
    assert needs_attention("escalated") and needs_attention("failed") and not needs_attention("dispatched")


@pytest.mark.asyncio
async def test_the_campaign_and_the_tiles_count_the_same_items_as_needing_attention(db, monkeypatch):
    # #1129 (U27): the same item set, read by Campaign Progress and by the tiles.
    statuses = ("merged", "escalated", "failed", "dispatched")
    team, scope, items, summary = await fixed_campaign(db, monkeypatch, statuses)
    await observe_backlog(db, scope, summary, Backlog([issue(n, f"Issue {n}") for n in range(1, 5)]))
    campaign = (await summary.summary(db, team.id)).campaign
    counts = await projection.counts(db, select(GithubWorkItem).where(GithubWorkItem.scope_id == scope.id))
    assert campaign.attention_count == counts.attention == 2
    assert campaign.under_way_count == counts.active == 1
    assert campaign.done_count == counts.finished == 1

