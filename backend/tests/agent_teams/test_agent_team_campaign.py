"""Campaign data counts stored work and a fixed GitHub backlog."""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.models.database import AgentTeamSlot, GithubWorkItem
from app.services.agent_team_campaign_service import AgentTeamCampaignService
from app.services.github_client import GithubClient
from tests.agent_teams.test_agent_team_summary import Attention, seed, service


START = datetime(2026, 10, 12, 12, 0)


class Backlog:
    def __init__(self, issues, *, unavailable=False):
        self.issues = issues
        self.unavailable = unavailable
        self.calls = []

    async def get_issue(self, owner, repo, number):
        self.calls.append(("issue", owner, repo, number))
        if self.unavailable:
            raise OSError("GitHub unavailable")
        return {"number": number, "milestone": {"number": 7, "title": "Campaign"}}

    async def list_milestone_issues(self, owner, repo, milestone):
        self.calls.append(("issues", owner, repo, milestone))
        if self.unavailable:
            raise OSError("GitHub unavailable")
        return self.issues

    async def list_milestones(self, owner, repo):
        self.calls.append(("milestones", owner, repo))
        return [{"number": 7, "title": "Campaign"}]


def issue(number, title, *labels):
    return {"number": number, "title": title, "state": "open",
            "labels": [{"name": label} for label in labels]}


async def fixed_campaign(db, monkeypatch, statuses=("merged", "dispatched", "pending")):
    team, scope, items = await seed(db, statuses)
    scope.base_ref = "origin/release/v3.1"
    slot = await db.scalar(select(AgentTeamSlot).where(AgentTeamSlot.preset_id == team.id))
    slot.display_name = "B1 Backend implementer"
    slot.area_labels = ["area:backend"]
    for index, item in enumerate(items, 1):
        item.issue_title = f"Work {index}"
        item.dispatch_base_ref = scope.base_ref
        if item.dispatch_status == "merged":
            item.auto_merged_at = START + timedelta(minutes=index)
    await db.commit()
    summary, _ = service(monkeypatch)
    return team, scope, items, summary


async def observe_backlog(db, scope, summary, client):
    # The old summary has no campaign reader. Its response assertions fail first.
    campaign = getattr(summary, "campaign", None)
    if campaign is not None:
        await campaign.refresh_scope(db, scope, client)


@pytest.mark.asyncio
async def test_campaign_counts_open_merged_issues_and_excludes_parent(db, monkeypatch):
    team, scope, items, summary = await fixed_campaign(db, monkeypatch)
    client = Backlog([issue(1, "Done but open"), issue(2, "Active"), issue(3, "Admitted"),
                      issue(4, "Backend work", "area:backend"), issue(5, "Other work", "area:unknown"),
                      issue(506, "Parent campaign", "area:backend")])
    await observe_backlog(db, scope, summary, client)
    result = await summary.summary(db, team.id)
    campaign = result.campaign
    assert campaign.backlog_state == "available"
    assert campaign.done_count == 1
    assert campaign.latest_done[0].title == "Work 1"
    assert campaign.latest_done[0].merged_at == (START + timedelta(minutes=1)).replace(tzinfo=timezone.utc)
    assert campaign.under_way_count == 1
    assert campaign.under_way[0].owner_short_name == "B1"
    assert campaign.under_way[0].state_phrase == "building it"
    assert campaign.queued_count == 1
    assert campaign.not_started_count == 2
    assert {group.area: group.count for group in campaign.not_started_groups} == {"area:backend": 1, "Other": 1}
    payload = campaign.model_dump_json()
    assert "2026-10-12T12:01:00Z" in payload
    assert "Parent campaign" not in payload
    assert "private-" not in payload and "work_item_id" not in payload
    states = (await db.scalars(select(GithubWorkItem.dispatch_status).order_by(GithubWorkItem.id))).all()
    assert states == ["merged", "dispatched", "pending"]
    assert await db.scalar(select(GithubWorkItem.auto_merged_at).where(
        GithubWorkItem.issue_number == 1)) == START + timedelta(minutes=1)


@pytest.mark.asyncio
async def test_unreadable_backlog_keeps_deck_counts_and_unknown_not_started(db, monkeypatch):
    team, scope, _, summary = await fixed_campaign(db, monkeypatch)
    await observe_backlog(db, scope, summary, Backlog([], unavailable=True))
    result = await summary.summary(db, team.id)
    assert result.campaign.backlog_state == "backlog unavailable"
    assert result.campaign.done_count == 1
    assert result.campaign.under_way_count == 1
    assert result.campaign.queued_count == 1
    assert result.campaign.not_started_count is None
    assert result.campaign.not_started_groups == []


@pytest.mark.asyncio
async def test_campaign_returns_latest_three_merges(db, monkeypatch):
    team, scope, _, summary = await fixed_campaign(db, monkeypatch, ("merged",) * 4)
    await observe_backlog(db, scope, summary, Backlog([issue(n, f"Work {n}") for n in range(1, 5)]))
    result = await summary.summary(db, team.id)
    assert result.campaign.done_count == 4
    assert [item.title for item in result.campaign.latest_done] == ["Work 4", "Work 3", "Work 2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("title,available", [("Campaign", True), ("No match", False)])
async def test_explicit_milestone_uses_exact_title(db, monkeypatch, title, available):
    team, scope, _, summary = await fixed_campaign(db, monkeypatch)
    team.backlog_milestone = title
    await db.commit()
    client = Backlog([issue(4, "Not started")])
    await observe_backlog(db, scope, summary, client)
    result = await summary.summary(db, team.id)
    assert result.campaign.backlog_state == ("available" if available else "backlog unavailable")
    assert ("milestones", "example", "repo") in client.calls
    assert not any(call[0] == "issue" for call in client.calls)
    assert result.campaign.not_started_count == (1 if available else None)


@pytest.mark.asyncio
async def test_backlog_configuration_change_invalidates_retained_counts(db, monkeypatch):
    team, scope, _, summary = await fixed_campaign(db, monkeypatch)
    clock = [0.0]
    summary.campaign = AgentTeamCampaignService(interval_seconds=60, clock=lambda: clock[0])
    client = Backlog([issue(4, "Not started")])
    await summary.campaign.refresh_scope(db, scope, client)
    await summary.campaign.refresh_scope(db, scope, client)
    assert len(client.calls) == 2
    clock[0] = 61
    team_id, scope_id = team.id, scope.id
    result = await summary.summary(db, team_id)
    assert result.campaign.not_started_count == 1
    team = await db.get(type(team), team_id)
    team.backlog_milestone = "No match"
    await db.commit()
    scope = await db.get(type(scope), scope_id)
    await summary.campaign.refresh_scope(db, scope, client)
    result = await summary.summary(db, team_id)
    assert result.campaign.backlog_state == "backlog unavailable"


def controlled_campaign(summary):
    clock = [0.0]
    start = START.replace(tzinfo=timezone.utc)
    summary.campaign = AgentTeamCampaignService(interval_seconds=120, clock=lambda: clock[0],
        wall_clock=lambda: start + timedelta(seconds=clock[0]))
    return clock, start


@pytest.mark.asyncio
async def test_campaign_backlog_stays_available_between_late_polls(db, monkeypatch):
    team, scope, _, summary = await fixed_campaign(db, monkeypatch)
    clock, start = controlled_campaign(summary)
    client = Backlog([issue(3, "Queued"), issue(4, "Not started"), issue(5, "More work")])
    await summary.campaign.refresh_scope(db, scope, client)

    for delay in (25, 45, 60):
        read_at = clock[0]
        for age in (119, 120, 120 + delay):
            clock[0] = read_at + age
            campaign = (await summary.summary(db, team.id)).campaign
            assert campaign.backlog_state == "available"
            assert (campaign.queued_count, campaign.not_started_count) == (1, 2)
            assert campaign.backlog_observed_at == start + timedelta(seconds=read_at)
            assert campaign.backlog_stale is (age > 120)
        await summary.campaign.refresh_scope(db, scope, client)
        refreshed = (await summary.summary(db, team.id)).campaign
        assert refreshed.backlog_observed_at == start + timedelta(seconds=clock[0])
        assert not refreshed.backlog_stale
    assert len(client.calls) == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["network", "incomplete"])
async def test_failed_refresh_keeps_success_until_the_hard_limit(db, monkeypatch, failure):
    team, scope, _, summary = await fixed_campaign(db, monkeypatch)
    clock, start = controlled_campaign(summary)
    await summary.campaign.refresh_scope(db, scope, Backlog([issue(4, "Not started")]))
    client = Backlog([{"number": 4}]) if failure == "incomplete" else Backlog([], unavailable=True)
    for elapsed in (150, 270, 359):
        clock[0] = elapsed
        await summary.campaign.refresh_scope(db, scope, client)
        retained = (await summary.summary(db, team.id)).campaign
        assert retained.backlog_state == "available" and retained.not_started_count == 1
        assert retained.backlog_observed_at == start and retained.backlog_stale
    assert len(client.calls) == (6 if failure == "incomplete" else 3)

    clock[0] = 360
    expired = (await summary.summary(db, team.id)).campaign
    assert expired.backlog_state == "backlog unavailable"
    assert expired.not_started_count is None and expired.backlog_observed_at is None
    assert not expired.backlog_stale

    clock[0] = 390
    await summary.campaign.refresh_scope(db, scope, Backlog([]))
    recovered = (await summary.summary(db, team.id)).campaign
    assert recovered.backlog_state == "available" and recovered.not_started_count == 0
    assert recovered.backlog_observed_at == start + timedelta(seconds=390)


@pytest.mark.asyncio
async def test_pagination_refusal_logs_one_sanitized_reason_per_scope(db, monkeypatch, caplog):
    _, scope, _, summary = await fixed_campaign(db, monkeypatch)
    clock, _ = controlled_campaign(summary)
    pagination_reads = []

    def handler(request):
        if request.url.path == "/repos/example/repo/issues/506":
            return httpx.Response(200, json={
                "number": 506, "html_url": "https://github.com/example/repo/issues/506",
                "milestone": {"number": 7},
            })
        assert request.url.path == "/repos/example/repo/issues"
        pagination_reads.append(request.url.path)
        link = ("https://api.github.com/repos/example/repo/issues?milestone=7&state=all"
                "&per_page=100&page=2&unexpected=private-cursor")
        return httpx.Response(200, json=[], headers={"Link": f'<{link}>; rel="next"'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="https://api.github.com") as http:
        client = GithubClient(http=http, token="private-test-token")
        for elapsed in (0, 120, 240):
            clock[0] = elapsed
            await summary.campaign.refresh_scope(db, scope, client)

    messages = [r.getMessage() for r in caplog.records if r.name == "app.services.agent_team_campaign_service"]
    assert len(pagination_reads) == 3
    assert messages == [f"GitHub campaign pagination refused for scope {scope.id}: query changed"]


@pytest.mark.asyncio
async def test_late_backlog_response_does_not_replace_newer_success(db, monkeypatch):
    team, scope, _, summary = await fixed_campaign(db, monkeypatch)
    clock, start = controlled_campaign(summary)
    entered, resume = asyncio.Event(), asyncio.Event()

    class SlowBacklog(Backlog):
        async def list_milestone_issues(self, owner, repo, milestone):
            entered.set()
            await resume.wait()
            return self.issues

    older = asyncio.create_task(summary.campaign.refresh_scope(db, scope, SlowBacklog([])))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        clock[0] = 1
        await summary.campaign.refresh_scope(db, scope, Backlog([issue(4, "Newer result")]))
    finally:
        resume.set()
        await older

    current = (await summary.summary(db, team.id)).campaign
    assert current.not_started_count == 1
    assert current.backlog_observed_at == start + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_other_branch_merge_is_done_and_disclosed_and_parent_is_never_counted(db, monkeypatch):
    # #1106: one definition of done in every view. A merge into another branch is done, and it stays visible.
    team, scope, items, summary = await fixed_campaign(db, monkeypatch, ("merged", "merged", "pending"))
    items[0].dispatch_base_ref = "origin/another-branch"
    items[2].issue_number = 506
    await db.commit()
    await observe_backlog(db, scope, summary, Backlog([issue(1, "Other branch"), issue(2, "Done"), issue(506, "Parent")]))
    result = await summary.summary(db, team.id)
    assert result.campaign.done_count == 2
    assert result.campaign.done_on_other_branch_count == 1
    assert result.campaign.queued_count == 0
    assert result.campaign.not_started_count == 0


@pytest.mark.asyncio
async def test_a_merge_without_a_recorded_base_is_done_but_not_claimed_as_another_branch(db, monkeypatch):
    # dispatch_base_ref was added without a backfill, so historical merged rows can have none. Such a row is done,
    # and only a recorded base that differs is evidence of another branch (V1 review 5945503487).
    team, scope, items, summary = await fixed_campaign(db, monkeypatch, ("merged", "completed", "merged"))
    items[0].dispatch_base_ref = None
    items[2].dispatch_base_ref = "origin/known-other"
    await db.commit()
    await observe_backlog(db, scope, summary, Backlog([issue(1, "No base"), issue(2, "Completed"), issue(3, "Other")]))
    result = await summary.summary(db, team.id)
    assert result.campaign.done_count == 3
    assert result.campaign.done_on_other_branch_count == 1


@pytest.mark.asyncio
async def test_a_completed_item_counts_as_done(db, monkeypatch):
    # #1106 (U9): deck-website-plan's completed item was in no campaign bucket.
    team, scope, items, summary = await fixed_campaign(db, monkeypatch, ("completed", "merged", "pending"))
    await observe_backlog(db, scope, summary, Backlog([issue(1, "Completed"), issue(2, "Merged"), issue(3, "Queued")]))
    result = await summary.summary(db, team.id)
    assert result.campaign.done_count == 2 and result.campaign.done_on_other_branch_count == 0
    assert {row.title for row in result.campaign.latest_done} == {"Work 1", "Work 2"}
    assert result.campaign.queued_count == 1


@pytest.mark.asyncio
async def test_under_way_phrases_and_planned_decisions_are_plain(db, monkeypatch):
    team, scope, _, summary = await fixed_campaign(db, monkeypatch,
        ("dispatched", "verifying", "ready_for_review", "awaiting_human_review", "escalated"))
    team.planned_decisions = [{"text": "Decide promotion.", "state": "later"},
                              {"text": "Choose the trial.", "state": "now"},
                              {"text": "Approve the plan.", "state": "done"}]
    await db.commit()
    await observe_backlog(db, scope, summary, Backlog([issue(n, f"Work {n}") for n in range(1, 6)]))
    result = await summary.summary(db, team.id)
    # #1129 (U27): the escalated item needs attention; it is not under way.
    assert (result.campaign.under_way_count, result.campaign.attention_count) == (4, 1)
    assert [item.state_phrase for item in result.campaign.under_way] == [
        "building it", "waiting for checks", "reviewing it", "waiting for you"]
    assert [(item.text, item.state) for item in result.campaign.decisions_ahead] == [
        ("Decide promotion.", "later"), ("Choose the trial.", "now"), ("Approve the plan.", "done")]
    assert "dispatched" not in result.campaign.model_dump_json()


@pytest.mark.asyncio
async def test_escalated_and_failed_items_need_attention_and_every_item_has_one_bucket(db, monkeypatch):
    # #1129 (U27): escalated was counted as under way and failed in no bucket at all.
    statuses = ("merged", "dispatched", "escalated", "failed", "pending")
    team, scope, _, summary = await fixed_campaign(db, monkeypatch, statuses)
    await observe_backlog(db, scope, summary, Backlog([issue(n, f"Work {n}") for n in range(1, 6)]))
    c = (await summary.summary(db, team.id)).campaign
    assert (c.done_count, c.under_way_count, c.attention_count, c.queued_count) == (1, 1, 2, 1)
    assert c.done_count + c.under_way_count + c.attention_count + c.queued_count == len(statuses)
    assert [item.state_phrase for item in c.under_way] == ["building it"]


@pytest.mark.asyncio
async def test_current_human_decisions_preserve_missing_action_details(db, monkeypatch):
    team, _, _ = await seed(db, ())
    class HumanActions(Attention):
        async def summary(self, db, preset_id):
            value = await super().summary(db, preset_id)
            value["actions"] = [
                {"scope_id": 1, "issue_number": 1, "kind": "scope_clarification",
                 "state": state, "source": "leader", "assessment_current": current,
                 "private_prompt": "private-human-action"}
                for state, current in [("requested", True), ("context_pending", True),
                                       ("historical", False), ("resolved", True)]]
            return value
    summary, _ = service(monkeypatch, attention=HumanActions())
    result = await summary.summary(db, team.id)
    assert [(decision.text, decision.state) for decision in result.campaign.decisions_ahead] == [
        ("Clarify the work scope.", "now"), ("Complete the action details.", "now")]
    assert "private-" not in result.campaign.model_dump_json()


@pytest.mark.asyncio
async def test_empty_milestone_field_uses_parent_milestone(db, monkeypatch):
    team, scope, _, summary = await fixed_campaign(db, monkeypatch)
    from app.models.schemas import AgentTeamPresetUpdate
    team.backlog_milestone = AgentTeamPresetUpdate(backlog_milestone="  ").backlog_milestone
    await db.commit()
    client = Backlog([issue(4, "Not started")])
    await observe_backlog(db, scope, summary, client)
    result = await summary.summary(db, team.id)
    assert result.campaign.not_started_count == 1
    assert client.calls == [("issue", "example", "repo", 506), ("issues", "example", "repo", 7)]


@pytest.mark.asyncio
async def test_priority_queue_order_is_visible_without_milestone_backlog(db, monkeypatch):
    team, scope, items, summary = await fixed_campaign(db, monkeypatch, ("pending",) * 3)
    for item, number, priority in zip(items, (790, 789, 788), (2, 1, None)):
        item.issue_number, item.issue_title = number, f"Work {number}"
        item.dispatch_priority = priority
    await db.commit()
    result = await summary.summary(db, team.id)
    assert result.campaign.backlog_state == "backlog unavailable"
    assert [item.title for item in result.campaign.queued] == ["Work 790", "Work 789", "Work 788"]
    queued = result.campaign.dispatch_order
    assert [item.title for item in queued] == ["Work 789", "Work 790", "Work 788"]
    assert [(item.issue_number, item.priority, item.dispatch_position) for item in queued] == [
        (789, 1, 1), (790, 2, 2), (788, None, 3)]
    assert all(item.scope_id == scope.id and item.repository == "example/repo" for item in queued)
    public = result.campaign.model_dump_json()
    for private in ("private-nonce", "lease_token", "/private", "launch_options"):
        assert private not in public


@pytest.mark.asyncio
async def test_priority_order_includes_ready_work_outside_campaign_milestone(db, monkeypatch):
    team, scope, items, summary = await fixed_campaign(db, monkeypatch, ("pending", "pending"))
    items[0].dispatch_priority = 3
    items[1].dispatch_priority = 1
    await db.commit()
    await observe_backlog(db, scope, summary, Backlog([issue(1, "In milestone")]))
    result = await summary.summary(db, team.id)
    assert result.campaign.backlog_state == "available"
    assert result.campaign.queued_count == 1
    assert [item.title for item in result.campaign.queued] == ["Work 1"]
    assert [(item.issue_number, item.dispatch_position) for item in result.campaign.dispatch_order] == [(2, 1), (1, 2)]


@pytest.mark.asyncio
async def test_priority_ranks_are_per_scope_with_oldest_issue_ties(db, monkeypatch):
    from app.models.database import TeamGithubScope
    team, scope, items, summary = await fixed_campaign(db, monkeypatch, ("pending", "pending"))
    other = TeamGithubScope(preset_id=team.id, repo_owner="other", repo_name="repo", repo_path="/private/other")
    db.add(other)
    await db.flush()
    items[0].issue_number = 8
    items[1].issue_number = 7
    items[0].dispatch_priority = items[1].dispatch_priority = 2
    db.add(GithubWorkItem(scope_id=other.id, issue_number=1, issue_title="Other scope", issue_url="url",
        github_updated_at=datetime.utcnow(), dispatch_status="pending", dispatch_priority=1))
    await db.commit()
    result = await summary.summary(db, team.id)
    assert [(item.repository, item.issue_number, item.dispatch_position) for item in result.campaign.dispatch_order] == [
        ("example/repo", 7, 1), ("example/repo", 8, 2), ("other/repo", 1, 1)]
