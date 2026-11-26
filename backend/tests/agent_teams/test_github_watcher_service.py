"""GitHub client + watcher service tests."""
import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models.database  # noqa: F401
from app.database import Base
from app.models.database import (
    AgentTeamPreset,
    AgentTeamSlot,
    FactoryAuditEvent,
    GithubApprovalRequest,
    GithubMilestoneWait,
    GithubWorkItem,
    GithubWorkspace,
    MailExternalActor,
    MailMessage,
    MailTeamMember,
    TeamGithubScope,
)
from app.services.github_client import GithubClient, GithubClientResponseError
from app.services.github_watcher_service import GithubWatcherService, defer_milestone_exit, github_watcher_service


class _RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return self.handler(request)


@pytest.mark.asyncio
async def test_campaign_client_reads_all_milestone_pages_and_closed_issues():
    requests = []
    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.params["state"] == "all"
        endpoint = request.url.path
        if endpoint.endswith("milestones"):
            data = [{"number": 7, "title": "Campaign"}]
        elif request.url.params.get("page") == "2":
            data = [{"number": 2, "title": "Closed", "state": "closed", "labels": [],
                     "html_url": "https://github.com/o/r/issues/2", "milestone": {"number": 7}}]
        else:
            data = [{"number": 1, "title": "Open", "state": "open", "labels": [],
                     "html_url": "https://github.com/o/r/issues/1", "milestone": {"number": 7}},
                    {"number": 5, "pull_request": {}}]
        headers = {}
        if endpoint.endswith("issues") and not request.url.params.get("page"):
            headers["Link"] = '<https://api.github.com/repos/o/r/issues?milestone=7&state=all&per_page=100&page=2>; rel="next"'
        return httpx.Response(200, json=data, headers=headers)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.com") as http:
        client = GithubClient(http=http, token="synthetic-test-key")
        assert await client.list_milestones("o", "r") == [{"number": 7, "title": "Campaign"}]
        issues = await client.list_milestone_issues("o", "r", 7)
    assert [issue["number"] for issue in issues] == [1, 2]
    assert len(requests) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("link", [
    "https://elsewhere.example/repos/o/r/milestones?state=all&per_page=100&page=2",
    "https://api.github.com/repos/other/r/milestones?state=all&per_page=100&page=2",
    "https://api.github.com/repos/o/r/milestones?state=open&per_page=100&page=2",
    "https://api.github.com/repos/o/r/milestones?state=all&per_page=100&page=0",
])
async def test_campaign_client_rejects_unsafe_pagination_before_request(link):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[], headers={"Link": f'<{link}>; rel="next"'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.com") as http:
        with pytest.raises(GithubClientResponseError):
            await GithubClient(http=http).list_milestones("o", "r")
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_campaign_client_rejects_wrong_milestone_issue():
    data = [{"number": 1, "title": "Wrong campaign", "state": "open", "labels": [],
             "html_url": "https://github.com/o/r/issues/1", "milestone": {"number": 8}}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data)),
                                 base_url="https://api.github.com") as http:
        with pytest.raises(GithubClientResponseError):
            await GithubClient(http=http).list_milestone_issues("o", "r", 7)


@pytest.mark.asyncio
async def test_campaign_client_pagination_bound_never_returns_partial_backlog():
    requests = []
    def handler(request):
        requests.append(request)
        page = int(request.url.params.get("page", "1"))
        link = f'https://api.github.com/repos/o/r/milestones?state=all&per_page=100&page={page + 1}'
        return httpx.Response(200, json=[{"number": page, "title": f"Campaign {page}"}],
                              headers={"Link": f'<{link}>; rel="next"'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.com") as http:
        with pytest.raises(GithubClientResponseError, match="bound exceeded"):
            await GithubClient(http=http).list_milestones("o", "r")
    assert len(requests) == 10


@pytest.mark.asyncio
async def test_list_issues_with_label_builds_request():
    def handler(request):
        return httpx.Response(
            200,
            json=[
                {
                    "number": 42,
                    "title": "bug",
                    "html_url": "u",
                    "updated_at": "2026-07-04T00:00:00Z",
                    "labels": [{"name": "claude-deck-ready"}],
                }
            ],
        )

    transport = _RecordingTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://api.github.com"
    ) as http:
        client = GithubClient(http=http, token="tok")
        issues = await client.list_issues_with_label("o", "r", "claude-deck-ready")

    req = transport.requests[0]
    assert req.url.path == "/repos/o/r/issues"
    assert req.url.params["labels"] == "claude-deck-ready"
    assert req.url.params["state"] == "open"
    assert req.headers["Authorization"] == "Bearer tok"
    assert issues[0]["number"] == 42


@pytest.mark.asyncio
async def test_github_client_pr_check_and_merge_requests():
    sha = "a" * 40

    def handler(request):
        if request.url.path == "/repos/o/r/pulls/5" and request.method == "GET":
            return httpx.Response(
                200,
                json={"number": 5, "node_id": "PR_node", "head": {"sha": sha}, "merged": False},
            )
        if request.url.path == f"/repos/o/r/commits/{sha}/check-runs":
            return httpx.Response(200, json={"total_count": 1, "check_runs": [{
                "id": 1, "head_sha": sha, "name": "ci", "status": "completed",
                "conclusion": "success", "app": {"id": 2, "slug": "external-ci"},
            }]})
        if request.url.path == "/repos/o/r/actions/runs":
            assert request.url.params["head_sha"] == sha
            return httpx.Response(200, json={"total_count": 0, "workflow_runs": []})
        if request.url.path == f"/repos/o/r/commits/{sha}/status":
            return httpx.Response(200, json={"state": "success", "statuses": [{"context": "ci"}]})
        if request.url.path == "/graphql":
            return httpx.Response(200, json={"data": {"markPullRequestReadyForReview": {"pullRequest": {"id": "PR_node"}}}})
        if request.url.path == "/repos/o/r/pulls/5/merge":
            return httpx.Response(200, json={"merged": True})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    transport = _RecordingTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://api.github.com"
    ) as http:
        client = GithubClient(http=http, token="tok")
        pull = await client.get_pull("o", "r", 5)
        checks = await client.list_check_runs_for_ref("o", "r", sha)
        status = await client.get_combined_status_for_ref("o", "r", sha)
        ready = await client.mark_pull_ready_for_review("PR_node")
        merged = await client.merge_pull("o", "r", 5, expected_head_sha=sha)

    assert pull["head"]["sha"] == sha
    assert checks[0]["name"] == "ci"
    assert status["state"] == "success"
    assert ready["data"]["markPullRequestReadyForReview"]["pullRequest"]["id"] == "PR_node"
    assert merged["merged"] is True
    assert json.loads(transport.requests[-1].content) == {"sha": sha}
    check_reads = [request for request in transport.requests if request.url.path.endswith("/check-runs")]
    assert [request.url.params["filter"] for request in check_reads] == ["all", "latest", "all", "latest"]
    assert all(request.headers["Authorization"] == "Bearer tok" for request in transport.requests)


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'watcher.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


class _FakeClient:
    def __init__(self, labeled=None, by_number=None):
        self._labeled = labeled or []
        self._by_number = by_number or {}
        self.by_number_calls = []

    async def list_issues_with_label(self, owner, repo, label):
        return list(self._labeled)

    async def get_open_issues_by_number(self, owner, repo, numbers):
        return {
            number: self._by_number[number]
            for number in numbers
            if number in self._by_number and self._by_number[number].get("state", "open") == "open"
        }

    async def get_issues_by_number(self, owner, repo, numbers):
        self.by_number_calls.append(list(numbers))
        return {number: self._by_number[number] for number in numbers if number in self._by_number}

    async def list_repo_labels(self, owner, repo):
        return []


async def _make_scope(db, **kw):
    preset = AgentTeamPreset(name=kw.pop("preset_name", "T"), description="", created_by="t",
                             backlog_milestone=kw.pop("milestone", None))
    db.add(preset)
    await db.flush()
    leader_slot = AgentTeamSlot(
        preset_id=preset.id, position=0, display_name="Leader", role="Leader",
        provider="codex-cli", repo_id="r", repo_path="/tmp/r", repo_name="r",
    )
    db.add(leader_slot)
    await db.flush()
    preset.leader_slot_id = leader_slot.id
    leader = MailTeamMember(
        identity_key=f"slot:{leader_slot.id}", repo_id="r", repo_path="/tmp/r", repo_name="r",
        display_name="Leader", participant_kind="team_slot", team_preset_id=preset.id,
        team_slot_id=leader_slot.id,
    )
    db.add(leader)
    scope = TeamGithubScope(
        preset_id=preset.id, repo_owner="o", repo_name="r", repo_path="/tmp/r",
        **{"dispatch_label": "claude-deck-ready", "design_label": "claude-deck-design", **kw}
    )
    db.add(scope)
    await db.commit()
    await db.refresh(scope)
    return scope


def _issue(number, labels, updated="2026-07-04T00:00:00Z"):
    return {
        "number": number,
        "title": f"issue {number}",
        "html_url": f"https://github.com/o/r/issues/{number}",
        "updated_at": updated,
        "state": "open",
        "labels": [{"name": name} for name in labels],
    }


def _milestone_issue(scope, title, updated="2026-07-04T00:00:00Z"):
    return dict(_issue(1, [scope.dispatch_label], updated=updated), milestone={"title": title} if title else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("prepared", [False, True])
async def test_milestone_move_waits_for_original_owner_in_either_poll_order(db, reverse, prepared):
    old = await _make_scope(db, preset_name="Old", milestone="V1")
    new = await _make_scope(db, preset_name="New", milestone="V2")
    fallback = await _make_scope(db, preset_name="Fallback")
    issue = _milestone_issue(old, "V1")
    await github_watcher_service.poll_scope(db, fallback, _FakeClient(labeled=[issue]))
    assert await db.scalar(select(GithubWorkItem)) is None
    await github_watcher_service.poll_scope(db, old, _FakeClient(labeled=[issue]))
    original = await db.scalar(select(GithubWorkItem))
    assert original.scope_id == old.id
    if prepared:
        original.dispatch_nonce = "prepared"
        original.dispatch_base_ref = "release/v1"
        original.dispatch_head_ref = "work/issue-1"
        original.dispatched_at = original.github_updated_at
        original.pending_reason = "queued_no_workspace"
        await db.commit()

    moved = dict(_milestone_issue(old, "V2"), updated_at="2026-07-05T00:00:00Z")
    for scope in ([new, old] if reverse else [old, new]):
        await github_watcher_service.poll_scope(db, scope, _FakeClient(labeled=[moved]))
    assert original.dispatch_status == "cancelled"
    event = await db.scalar(select(FactoryAuditEvent).where(
        FactoryAuditEvent.event_kind == "stopped_work_item_ended"))
    assert event.after_values["outcome"] == "deferred"
    await github_watcher_service.poll_scope(db, new, _FakeClient(labeled=[moved]))
    assert (await db.scalars(select(GithubWorkItem.scope_id).order_by(GithubWorkItem.id))).all() == [
        old.id, new.id]

    await github_watcher_service.poll_scope(db, old, _FakeClient(labeled=[issue]))
    assert original.dispatch_status == "cancelled"
    returned = dict(_milestone_issue(old, "V1"), updated_at="2026-07-06T00:00:00Z")
    for scope in ([new, old] if reverse else [old, new]):
        await github_watcher_service.poll_scope(db, scope, _FakeClient(labeled=[returned]))
    await github_watcher_service.poll_scope(db, old, _FakeClient(labeled=[returned]))
    await github_watcher_service.poll_scope(db, old, _FakeClient(labeled=[returned]))
    history, departure, current = (await db.scalars(
        select(GithubWorkItem).order_by(GithubWorkItem.id))).all()
    assert history.id == original.id
    assert history.dispatch_status == departure.dispatch_status == "cancelled"
    assert history.dispatch_head_ref == ("work/issue-1" if prepared else None)
    assert history.dispatch_nonce == ("prepared" if prepared else None)
    assert (current.scope_id, current.dispatch_status, current.active_scope_revision) == (old.id, "pending", 0)
    assert current.dispatch_nonce is None and current.pr_number is None
    assert (await db.get(FactoryAuditEvent, event.id)).item_id == history.id


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_admission", [False, True])
async def test_repository_milestone_never_readmits_a_merged_issue(db, previous_admission):
    old = await _make_scope(db, preset_name="Old", milestone="V1")
    new = await _make_scope(db, preset_name="New", milestone="V2")
    await github_watcher_service.poll_scope(db, old, _FakeClient(labeled=[_milestone_issue(old, "V1")]))
    original = await db.scalar(select(GithubWorkItem))
    original.dispatch_status = "merged"
    original.milestone_replaced_at = datetime.utcnow() if previous_admission else None
    await db.commit()
    await github_watcher_service.poll_scope(db, new, _FakeClient(labeled=[_milestone_issue(new, "V2")]))
    await github_watcher_service.poll_scope(db, old, _FakeClient(labeled=[_milestone_issue(old, "V1")]))
    unmatched = _milestone_issue(new, "V9", "2026-07-05T00:00:00Z")
    await github_watcher_service.poll_scope(db, new, _FakeClient(labeled=[unmatched]))
    assert (await db.scalars(select(GithubWorkItem.id))).all() == [original.id]
    assert await _milestone_wait_titles(db) == {}


@pytest.mark.asyncio
async def test_same_second_milestone_return_uses_current_issue(db):
    scope = await _make_scope(db, milestone="V1")
    home = _milestone_issue(scope, "V1")
    away = _milestone_issue(scope, "V2")
    await github_watcher_service.poll_scope(db, scope, _FakeClient(labeled=[home]))
    await github_watcher_service.poll_scope(db, scope, _FakeClient(labeled=[away]))
    original = await db.scalar(select(GithubWorkItem))
    assert original.dispatch_status == "cancelled"
    await github_watcher_service.poll_scope(db, scope, _FakeClient(labeled=[home], by_number={1: away}))
    assert (await db.scalars(select(GithubWorkItem.id))).all() == [original.id]
    await github_watcher_service.poll_scope(db, scope, _FakeClient(labeled=[home], by_number={1: home}))
    assert (await db.scalars(select(GithubWorkItem.dispatch_status).order_by(GithubWorkItem.id))).all() == [
        "cancelled", "pending",
    ]


async def _prior_route(db, scope, *, status="pending"):
    item = GithubWorkItem(
        scope_id=scope.id, issue_number=1, issue_title="Previous route", issue_url="u",
        github_updated_at=datetime(2026, 7, 4), dispatch_status=status,
    )
    db.add(item)
    await db.commit()
    return item


@pytest.mark.asyncio
async def test_terminal_source_with_lease_blocks_destination_admission(db):
    source = await _make_scope(db, preset_name="Source", milestone="V1")
    destination = await _make_scope(db, preset_name="Destination", milestone="V2")
    prior = await _prior_route(db, source, status="cancelled")
    lease = GithubWorkspace(scope_id=source.id, path="/tmp/leased-source", leased_item_id=prior.id)
    db.add(lease)
    await db.commit()
    await github_watcher_service.poll_scope(db, destination, _FakeClient(
        labeled=[_milestone_issue(destination, "V2", "2026-07-06T00:00:00Z")],
    ))
    assert (await db.scalars(select(GithubWorkItem.id))).all() == [prior.id]
    assert lease.leased_item_id == prior.id


@pytest.mark.asyncio
async def test_stale_foreign_poll_cannot_admit_after_deferral(db):
    source = await _make_scope(db, preset_name="Source", milestone="V1")
    destination = await _make_scope(db, preset_name="Destination", milestone="V2")
    prior = await _prior_route(db, source)
    await defer_milestone_exit(db, source, prior, "V2", "2026-07-06T00:00:00Z")
    await github_watcher_service.poll_scope(db, destination, _FakeClient(
        labeled=[_milestone_issue(destination, "V2", "2026-07-05T00:00:00Z")],
    ))
    assert (await db.scalars(select(GithubWorkItem.id))).all() == [prior.id]


@pytest.mark.asyncio
async def test_older_same_scope_return_cannot_replace_newer_departure(db):
    home = await _make_scope(db, preset_name="Home", milestone="V1")
    other = await _make_scope(db, preset_name="Other", milestone="V2")
    previous_home = await _prior_route(db, home)
    await defer_milestone_exit(db, home, previous_home, "V2", "2026-07-07T00:00:00Z")
    previous_other = await _prior_route(db, other)
    # The last deferral fact was recorded later, from an older issue observation.
    await defer_milestone_exit(db, other, previous_other, "V1", "2026-07-05T00:00:00Z")
    await github_watcher_service.poll_scope(db, home, _FakeClient(
        labeled=[_milestone_issue(home, "V1", "2026-07-06T00:00:00Z")],
    ))
    assert previous_home.milestone_replaced_at is None
    assert (await db.scalars(select(GithubWorkItem.id).order_by(GithubWorkItem.id))).all() == [
        previous_home.id, previous_other.id,
    ]


@pytest.mark.asyncio
async def test_cancelled_item_without_milestone_deferral_stays_closed(db):
    scope = await _make_scope(db, milestone="V1")
    prior = await _prior_route(db, scope, status="cancelled")
    await github_watcher_service.poll_scope(db, scope, _FakeClient(
        labeled=[_milestone_issue(scope, "V1", "2026-07-06T00:00:00Z")],
    ))
    assert (await db.scalars(select(GithubWorkItem.id))).all() == [prior.id]
    assert prior.milestone_replaced_at is None


@pytest.mark.asyncio
async def test_leased_cancelled_deferral_cannot_be_replaced_in_its_scope(db):
    scope = await _make_scope(db, milestone="V1")
    prior = await _prior_route(db, scope)
    await defer_milestone_exit(db, scope, prior, "V2", "2026-07-05T00:00:00Z")
    lease = GithubWorkspace(scope_id=scope.id, path="/tmp/leased-return", leased_item_id=prior.id)
    db.add(lease)
    await db.commit()
    await github_watcher_service.poll_scope(db, scope, _FakeClient(
        labeled=[_milestone_issue(scope, "V1", "2026-07-06T00:00:00Z")],
    ))
    assert (await db.scalars(select(GithubWorkItem.id))).all() == [prior.id]
    assert prior.milestone_replaced_at is None and lease.leased_item_id == prior.id


@pytest.mark.asyncio
async def test_same_second_return_requires_ready_label_on_confirmation(db):
    scope = await _make_scope(db, milestone="V1")
    prior = await _prior_route(db, scope)
    await defer_milestone_exit(db, scope, prior, "V2", "2026-07-04T00:00:00Z")
    returned = _milestone_issue(scope, "V1")
    await github_watcher_service.poll_scope(db, scope, _FakeClient(
        labeled=[returned], by_number={1: dict(returned, labels=[])},
    ))
    assert (await db.scalars(select(GithubWorkItem.id))).all() == [prior.id]
    assert prior.milestone_replaced_at is None


@pytest.mark.asyncio
async def test_milestone_deferral_records_observed_departure_time(db):
    scope = await _make_scope(db, milestone="V1")
    prior = await _prior_route(db, scope)
    await defer_milestone_exit(db, scope, prior, "V2", "2026-07-06T00:00:00Z")
    await db.refresh(prior)
    assert (prior.dispatch_status, prior.github_updated_at) == ("cancelled", datetime(2026, 7, 6))


@pytest.mark.asyncio
async def test_milestone_deferral_rejects_older_terminal_observation(db):
    scope = await _make_scope(db, milestone="V1")
    prior = await _prior_route(db, scope)
    with pytest.raises(ValueError, match="end_stopped_item_context_changed"):
        await defer_milestone_exit(db, scope, prior, "V2", "2026-07-03T00:00:00Z")
    assert (prior.dispatch_status, prior.github_updated_at) == ("pending", datetime(2026, 7, 4))
    assert await db.scalar(select(FactoryAuditEvent.id).where(
        FactoryAuditEvent.event_kind == "stopped_work_item_ended",
    )) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("title", [None, "v1", "V1"])
async def test_no_exact_milestone_or_conflicting_teams(db, title):
    scopes = [await _make_scope(db, milestone="V1")]
    if title == "V1":
        scopes.append(await _make_scope(db, preset_name="Other", milestone="V1"))
    for scope in scopes:
        await github_watcher_service.poll_scope(db, scope, _FakeClient(labeled=[_milestone_issue(scope, title)]))
    assert await db.scalar(select(GithubWorkItem)) is None
    expected = {scopes[0].id: "v1"} if title == "v1" else {}
    assert await _milestone_wait_titles(db) == expected


async def _milestone_wait_titles(db):
    return dict((await db.execute(
        select(GithubMilestoneWait.scope_id, GithubMilestoneWait.milestone_title)
        .where(GithubMilestoneWait.milestone_title.is_not(None))
    )).all())


@pytest.mark.asyncio
async def test_new_issue_milestone_wait_survives_restart_without_duplicates(db):
    home = await _make_scope(db, milestone="V1")
    sibling = await _make_scope(db, preset_name="Sibling", milestone="V2", dispatch_label="sibling-ready")
    other_repo = await _make_scope(db, preset_name="Other repo", milestone="V9")
    other_repo.repo_name = "another"
    await db.commit()
    issue = _milestone_issue(home, "V9")
    maker = async_sessionmaker(db.bind, expire_on_commit=False)

    async def poll():
        async with maker() as session:
            scope = await session.get(TeamGithubScope, home.id)
            await github_watcher_service.poll_scope(session, scope, _FakeClient(labeled=[issue]))

    await asyncio.gather(poll(), poll())
    expected = {home.id: "V9", sibling.id: "V9"}
    assert await _milestone_wait_titles(db) == expected
    assert await db.scalar(select(GithubWorkItem)) is None
    await db.commit()

    restarted_engine = create_async_engine(db.bind.url)
    maker = async_sessionmaker(restarted_engine, expire_on_commit=False)
    try:
        async with maker() as restarted:
            watcher = GithubWatcherService()
            assert await _milestone_wait_titles(restarted) == expected
            sibling_scope = await restarted.get(TeamGithubScope, sibling.id)
            sibling_client = _FakeClient(by_number={issue["number"]: issue})
            await watcher.poll_scope(restarted, sibling_scope, sibling_client)
            assert sibling_client.by_number_calls == [[issue["number"]]]
            home_scope = await restarted.get(TeamGithubScope, home.id)
            await watcher.poll_scope(restarted, home_scope, _FakeClient(labeled=[issue]))
            assert await _milestone_wait_titles(restarted) == expected
            assert len((await restarted.scalars(select(GithubMilestoneWait))).all()) == 2
    finally:
        await restarted_engine.dispose()


@pytest.mark.asyncio
async def test_milestone_wait_for_moved_issue_belongs_only_to_departed_scope(db):
    home = await _make_scope(db, milestone="V1")
    sibling = await _make_scope(db, preset_name="Sibling", milestone="V2")
    assigned = _milestone_issue(home, "V1", "2026-07-05T00:00:00Z")
    await github_watcher_service.poll_scope(db, home, _FakeClient(labeled=[assigned]))
    await github_watcher_service.poll_scope(db, sibling, _FakeClient(labeled=[_milestone_issue(home, "V9")]))
    assert await _milestone_wait_titles(db) == {}
    moved = _milestone_issue(home, "V9", "2026-07-06T00:00:00Z")
    for scope in (sibling, home, sibling):
        await github_watcher_service.poll_scope(db, scope, _FakeClient(labeled=[moved]))
        assert await _milestone_wait_titles(db) == {home.id: "V9"}
    items = (await db.scalars(select(GithubWorkItem))).all()
    assert len(items) == 1 and items[0].scope_id == home.id


@pytest.mark.asyncio
@pytest.mark.parametrize("destination_milestone", ["V9", None], ids=["milestone-route", "label-only"])
async def test_milestone_wait_clears_when_team_admits_issue(db, monkeypatch, destination_milestone):
    home = await _make_scope(db, milestone="V1")
    destination = await _make_scope(db, preset_name="Destination", milestone="V2")
    issue = _milestone_issue(home, "V9")
    client = _FakeClient(labeled=[issue], by_number={issue["number"]: issue})
    await github_watcher_service.poll_scope(db, home, client)
    assert await _milestone_wait_titles(db) == {home.id: "V9", destination.id: "V9"}

    preset = await db.get(AgentTeamPreset, destination.preset_id)
    preset.backlog_milestone = destination_milestone
    await db.commit()
    issue = dict(issue, updated_at="2026-07-05T00:00:00Z")
    client = _FakeClient(labeled=[issue], by_number={issue["number"]: issue})
    await github_watcher_service.poll_scope(db, home, client)
    assert await _milestone_wait_titles(db) == {home.id: "V9", destination.id: "V9"}
    skipped = GithubWatcherService()
    refused_upsert = AsyncMock(return_value=False)
    monkeypatch.setattr(skipped, "_upsert_item", refused_upsert)
    await skipped.poll_scope(db, destination, client)
    refused_upsert.assert_awaited_once_with(db, destination, issue)
    assert await db.scalar(select(GithubWorkItem)) is None
    assert await _milestone_wait_titles(db) == {home.id: "V9", destination.id: "V9"}

    await github_watcher_service.poll_scope(db, destination, client)
    assert (await db.scalar(select(GithubWorkItem))).scope_id == destination.id
    assert await _milestone_wait_titles(db) == {}
    refreshed = dict(issue, updated_at="2026-07-06T00:00:00Z")
    await github_watcher_service.poll_scope(db, home, _FakeClient(labeled=[refreshed]))
    assert await _milestone_wait_titles(db) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("new_title", ["V2", "V10", None], ids=["routed", "unmatched", "removed"])
async def test_milestone_wait_clears_for_old_title_when_milestone_changes(db, new_title):
    home = await _make_scope(db, milestone="V1")
    sibling = await _make_scope(db, preset_name="Sibling", milestone="V2")
    waiting = _milestone_issue(home, "V9")
    await github_watcher_service.poll_scope(db, home, _FakeClient(labeled=[waiting]))
    changed = _milestone_issue(home, new_title, "2026-07-05T00:00:00Z")
    await github_watcher_service.poll_scope(db, home, _FakeClient(labeled=[changed]))
    expected = {home.id: "V10", sibling.id: "V10"} if new_title == "V10" else {}
    assert await _milestone_wait_titles(db) == expected
    assert await db.scalar(select(GithubWorkItem)) is None


@pytest.mark.asyncio
async def test_milestone_wait_rechecks_conflicting_milestone_at_same_second(db):
    home = await _make_scope(db, milestone="V1")
    sibling = await _make_scope(db, preset_name="Sibling", milestone="V2")
    waiting = _milestone_issue(home, "V9")
    await github_watcher_service.poll_scope(db, home, _FakeClient(labeled=[waiting]))

    listed = _milestone_issue(home, "V10", waiting["updated_at"])
    current = _milestone_issue(home, "V11", waiting["updated_at"])
    client = _FakeClient(labeled=[listed], by_number={waiting["number"]: current})
    await github_watcher_service.poll_scope(db, home, client)

    assert client.by_number_calls == [[waiting["number"]]]
    assert await _milestone_wait_titles(db) == {home.id: "V11", sibling.id: "V11"}


@pytest.mark.asyncio
async def test_milestone_wait_clears_when_issue_is_confirmed_closed(db):
    home = await _make_scope(db, milestone="V1")
    sibling = await _make_scope(db, preset_name="Sibling", milestone="V2")
    waiting = _milestone_issue(home, "V9")
    await github_watcher_service.poll_scope(db, home, _FakeClient(labeled=[waiting]))
    assert await _milestone_wait_titles(db) == {home.id: "V9", sibling.id: "V9"}

    closed = dict(waiting, state="closed", updated_at="2026-07-05T00:00:00Z")
    client = _FakeClient(by_number={closed["number"]: closed})
    await github_watcher_service.poll_scope(db, home, client)

    assert client.by_number_calls == [[closed["number"]]]
    assert await _milestone_wait_titles(db) == {}
    assert await db.scalar(select(GithubWorkItem)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("same_second", [False, True], ids=["older-snapshot", "same-second"])
async def test_milestone_wait_clears_on_confirmed_ready_label_removal(db, same_second):
    home = await _make_scope(db, milestone="V1")
    sibling = await _make_scope(db, preset_name="Sibling", milestone="V2")
    issue = _milestone_issue(home, "V9")
    number = issue["number"]
    await github_watcher_service.poll_scope(db, home, _FakeClient(labeled=[issue]))
    await github_watcher_service.poll_scope(db, home, _FakeClient())
    assert await _milestone_wait_titles(db) == {home.id: "V9", sibling.id: "V9"}

    updated_at = issue["updated_at"] if same_second else "2026-07-05T00:00:00Z"
    unlabeled = dict(issue, labels=[], updated_at=updated_at)
    removal = _FakeClient(by_number={number: unlabeled})
    await github_watcher_service.poll_scope(db, home, removal)
    assert removal.by_number_calls == [[number]]
    assert await _milestone_wait_titles(db) == {}

    stale = _FakeClient(labeled=[issue], by_number={number: unlabeled})
    await github_watcher_service.poll_scope(db, sibling, stale)
    assert await _milestone_wait_titles(db) == {}


@pytest.mark.asyncio
async def test_milestone_wait_confirmation_failure_preserves_wait_and_admissions(db):
    class FailingConfirmation(_FakeClient):
        async def get_issues_by_number(self, owner, repo, numbers):
            raise httpx.ReadTimeout("confirmation failed")

    scope = await _make_scope(db, milestone="V1")
    waiting = _milestone_issue(scope, "V9")
    await github_watcher_service.poll_scope(db, scope, _FakeClient(labeled=[waiting]))
    ready = dict(_milestone_issue(scope, "V1"), number=2)

    await github_watcher_service.poll_scope(db, scope, FailingConfirmation(labeled=[ready]))

    assert await _milestone_wait_titles(db) == {scope.id: "V9"}
    assert (await db.scalar(select(GithubWorkItem))).issue_number == ready["number"]


@pytest.mark.asyncio
async def test_concurrent_label_only_polls_admit_at_most_one_owner(db):
    scopes = [await _make_scope(db, preset_name=name) for name in ("One", "Two")]
    maker = async_sessionmaker(db.bind, expire_on_commit=False)

    async def poll(scope_id):
        async with maker() as session:
            scope = await session.get(TeamGithubScope, scope_id)
            await github_watcher_service.poll_scope(session, scope, _FakeClient(labeled=[_milestone_issue(scope, None)]))

    await asyncio.gather(*(poll(scope.id) for scope in scopes))
    assert len((await db.scalars(select(GithubWorkItem))).all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["escalated", "failed", "pending", "dispatched", "verifying", "awaiting_human_review"])
async def test_milestone_exit_defers_or_notifies_once(db, status, caplog):
    from app.services.agent_team_supervisor_service import supervisor_inbox

    scope = await _make_scope(db, milestone="V1")
    preset = await db.get(AgentTeamPreset, scope.preset_id)
    actor = MailExternalActor(actor_key="supervisor", display_name="Supervisor", token_hash="test")
    owner = AgentTeamSlot(preset_id=preset.id, position=1, display_name="Owner", role="Builder",
                          provider="codex-cli", repo_id="r", repo_path="/tmp/r", repo_name="r")
    db.add_all([actor, owner])
    await db.flush()
    preset.supervisor_actor_id = actor.id
    db.add(MailTeamMember(identity_key=f"slot:{owner.id}", repo_id="r", repo_path="/tmp/r", repo_name="r",
                          display_name="Owner", participant_kind="team_slot", team_preset_id=preset.id,
                          team_slot_id=owner.id))
    item = GithubWorkItem(scope_id=scope.id, issue_number=1, issue_title="Issue", issue_url="u",
                          github_updated_at=datetime(2026, 7, 1), dispatch_status=status, owner_slot_id=owner.id)
    db.add(item)
    if status == "pending":
        item.dispatch_nonce = "prepared"
        await db.flush()
        workspace = GithubWorkspace(scope_id=scope.id, path="/tmp/r-held", leased_item_id=item.id)
        db.add(workspace)
    await db.commit()
    destination = await _make_scope(db, preset_name="Destination", milestone="V2")
    moved = dict(_milestone_issue(scope, "V2"), labels=[])
    client = _FakeClient(by_number={1: moved})
    for _ in range(2):
        await github_watcher_service.poll_scope(db, scope, client)
    assert "Completion failed" not in caplog.text
    if status == "pending":
        await db.refresh(workspace)
        assert workspace.leased_item_id == item.id
    if status in {"escalated", "failed"}:
        assert item.dispatch_status == "cancelled"
        assert await db.scalar(select(FactoryAuditEvent.id).where(
            FactoryAuditEvent.event_kind == "stopped_work_item_ended"))
    else:
        await github_watcher_service.poll_scope(db, destination, _FakeClient(labeled=[_milestone_issue(destination, "V2")]))
        assert (await db.scalars(select(GithubWorkItem.id))).all() == [item.id]
        assert item.dispatch_status == status
        recipients = (await db.scalars(select(MailMessage.recipient_member_id).where(
            MailMessage.subject == "Issue milestone changed",
            MailMessage.recipient_member_id.is_not(None)))).all()
        assert len(recipients) == len(set(recipients)) == 2
        assert len(await supervisor_inbox(db, actor)) == 1


@pytest.mark.asyncio
async def test_complete_and_notify_does_not_release_workspace(db):
    scope = await _make_scope(db)
    item = GithubWorkItem(
        scope_id=scope.id,
        issue_number=99,
        issue_title="closed",
        issue_url="u",
        github_updated_at=datetime.utcnow(),
        dispatch_status="dispatched",
    )
    db.add(item)
    await db.flush()
    workspace = GithubWorkspace(
        scope_id=scope.id,
        path="/tmp/r-ws-1",
        leased_item_id=item.id,
    )
    db.add(workspace)
    await db.commit()

    await github_watcher_service._complete_and_notify(db, scope, item)

    assert item.dispatch_status == "completed"
    assert workspace.leased_item_id == item.id


@pytest.mark.asyncio
async def test_poll_creates_pending_code_item(db):
    scope = await _make_scope(db)
    client = _FakeClient(labeled=[_issue(1, ["claude-deck-ready"])])
    await github_watcher_service.poll_scope(db, scope, client)
    items = (await db.execute(select(GithubWorkItem))).scalars().all()
    assert len(items) == 1
    assert items[0].issue_number == 1
    assert items[0].issue_type == "code"
    assert items[0].dispatch_status == "pending"


@pytest.mark.asyncio
async def test_campaign_read_failure_cannot_discard_poll_results(db, monkeypatch):
    scope = await _make_scope(db)
    scope_id = scope.id
    async def unavailable(*args):
        raise OSError("synthetic cache failure")
    monkeypatch.setattr("app.services.github_watcher_service.agent_team_campaign_service.refresh_scope", unavailable)
    await github_watcher_service.poll_scope(db, scope, _FakeClient(labeled=[_issue(1, ["claude-deck-ready"])]))
    item = await db.scalar(select(GithubWorkItem))
    assert item.dispatch_status == "pending"
    fresh = await db.get(TeamGithubScope, scope_id)
    assert fresh.last_polled_at is not None


@pytest.mark.asyncio
async def test_poll_detects_design_type(db):
    scope = await _make_scope(db)
    client = _FakeClient(labeled=[_issue(2, ["claude-deck-ready", "claude-deck-design"])])
    await github_watcher_service.poll_scope(db, scope, client)
    item = (await db.execute(select(GithubWorkItem))).scalars().one()
    assert item.issue_type == "design"


@pytest.mark.asyncio
async def test_poll_is_idempotent(db):
    scope = await _make_scope(db)
    client = _FakeClient(labeled=[_issue(1, ["claude-deck-ready"])])
    await github_watcher_service.poll_scope(db, scope, client)
    await github_watcher_service.poll_scope(db, scope, client)
    items = (await db.execute(select(GithubWorkItem))).scalars().all()
    assert len(items) == 1


@pytest.mark.asyncio
async def test_escalated_item_recovers_on_updated_timestamp(db):
    scope = await _make_scope(db)
    item = GithubWorkItem(
        scope_id=scope.id,
        issue_number=5,
        issue_title="x",
        issue_url="u",
        github_updated_at=datetime(2026, 7, 1),
        dispatch_status="escalated",
        escalation_reason="retry_count_exhausted",
        pending_reason="queued_slot_busy",
        handoff_state="pending",
        handoff_target_slot_id=123,
        retry_count=2,
        approval_round_count=1,
    )
    db.add(item)
    await db.commit()
    client = _FakeClient(
        labeled=[_issue(5, ["claude-deck-ready"], updated="2026-07-04T00:00:00Z")]
    )
    await github_watcher_service.poll_scope(db, scope, client)
    await db.refresh(item)
    assert item.dispatch_status == "pending"
    assert item.escalation_reason is None
    assert item.pending_reason is None
    assert item.handoff_state is None
    assert item.handoff_target_slot_id is None
    assert item.retry_count == 0
    assert item.approval_round_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["pr", "active_revision", "pending_approval", "deferred"])
async def test_issue_updates_preserve_pr_and_continuation_attempts(db, guard):
    scope = await _make_scope(db)
    item = GithubWorkItem(
        scope_id=scope.id,
        issue_number=55,
        issue_title="old title",
        issue_url="u",
        github_updated_at=datetime(2026, 7, 1),
        issue_type="code",
        dispatch_status="escalated",
        escalation_reason="retry_count_exhausted",
        dispatch_nonce="preserved-nonce",
        retry_count=4,
        approval_round_count=2,
        status_note="preserved context",
        pr_number=42 if guard == "pr" else None,
        active_scope_revision=1 if guard == "active_revision" else 0,
        retry_requested_at=(
            datetime(2026, 7, 2) if guard == "deferred" else None
        ),
    )
    db.add(item)
    await db.flush()
    if guard == "pending_approval":
        owner = MailTeamMember(
            identity_key="watcher-owner",
            repo_id="r",
            repo_path="/tmp/r",
            repo_name="r",
            display_name="Owner",
        )
        leader = MailTeamMember(
            identity_key="watcher-leader",
            repo_id="r",
            repo_path="/tmp/r",
            repo_name="r",
            display_name="Leader",
        )
        db.add_all([owner, leader])
        await db.flush()
        db.add(
            GithubApprovalRequest(
                work_item_id=item.id,
                request_kind="continuation",
                dispatch_nonce=item.dispatch_nonce,
                approval_round=item.approval_round_count,
                owner_member_id=owner.id,
                leader_member_id=leader.id,
                request_fingerprint="a" * 64,
                status="pending",
            )
        )
    if guard == "deferred":
        db.add(
            GithubWorkspace(
                scope_id=scope.id,
                path="/tmp/watcher-preserved-workspace",
                leased_item_id=item.id,
                lease_token="lease",
            )
        )
    await db.commit()
    retry_requested_at = item.retry_requested_at
    client = _FakeClient(
        labeled=[
            {
                **_issue(
                    55,
                    ["claude-deck-ready", "claude-deck-design"],
                    updated="2026-07-04T00:00:00Z",
                ),
                "title": "new title",
            }
        ]
    )

    await github_watcher_service.poll_scope(db, scope, client)
    await db.refresh(item)

    assert item.dispatch_status == "escalated"
    assert item.issue_title == "new title"
    assert item.issue_type == "code"
    assert item.dispatch_nonce == "preserved-nonce"
    assert item.retry_count == 4
    assert item.approval_round_count == 2
    assert item.status_note == "preserved context"
    if guard == "deferred":
        assert item.retry_requested_at == retry_requested_at


@pytest.mark.asyncio
async def test_watcher_update_defers_retry_while_workspace_is_leased(db):
    scope = await _make_scope(db)
    item = GithubWorkItem(
        scope_id=scope.id,
        issue_number=6,
        issue_title="x",
        issue_url="u",
        github_updated_at=datetime(2026, 7, 1),
        dispatch_status="escalated",
        escalation_reason="plan_blocked",
    )
    db.add(item)
    await db.flush()
    workspace = GithubWorkspace(
        scope_id=scope.id,
        path="/tmp/r-ws-retry",
        leased_item_id=item.id,
        lease_token="watcher-token",
    )
    db.add(workspace)
    await db.commit()
    client = _FakeClient(
        labeled=[_issue(6, ["claude-deck-ready"], updated="2026-07-04T00:00:00Z")]
    )

    await github_watcher_service.poll_scope(db, scope, client)

    await db.refresh(item)
    assert item.dispatch_status == "escalated"
    assert item.escalation_reason == "plan_blocked"
    assert item.retry_requested_at is not None
    assert workspace.leased_item_id == item.id


@pytest.mark.asyncio
async def test_active_item_escalates_when_label_removed(db):
    scope = await _make_scope(db)
    item = GithubWorkItem(
        scope_id=scope.id,
        issue_number=7,
        issue_title="x",
        issue_url="u",
        github_updated_at=datetime(2026, 7, 1),
        dispatch_status="dispatched",
    )
    db.add(item)
    await db.commit()
    client = _FakeClient(
        labeled=[],
        by_number={7: _issue(7, ["some-other-label"])},
    )
    await github_watcher_service.poll_scope(db, scope, client)
    await db.refresh(item)
    assert item.dispatch_status == "escalated"
    assert item.escalation_reason == "dispatch_label_removed"


@pytest.mark.asyncio
async def test_label_removed_sends_broadcast_and_owner_direct_message(db):
    scope = await _make_scope(db)
    slot = AgentTeamSlot(
        preset_id=scope.preset_id,
        position=0,
        display_name="Owner",
        provider="codex-cli",
        repo_id="r",
        repo_path="/tmp/r",
        repo_name="r",
    )
    db.add(slot)
    await db.flush()
    member = MailTeamMember(
        identity_key="slot:owner",
        repo_id="r",
        repo_path="/tmp/r",
        repo_name="r",
        display_name="Owner",
        participant_kind="team_slot",
        team_preset_id=scope.preset_id,
        team_slot_id=slot.id,
    )
    db.add(member)
    item = GithubWorkItem(
        scope_id=scope.id,
        issue_number=9,
        issue_title="x",
        issue_url="u",
        github_updated_at=datetime(2026, 7, 1),
        dispatch_status="dispatched",
        owner_slot_id=slot.id,
    )
    db.add(item)
    await db.commit()
    client = _FakeClient(labeled=[], by_number={9: _issue(9, ["some-other-label"])})

    await github_watcher_service.poll_scope(db, scope, client)

    messages = (await db.execute(select(MailMessage))).scalars().all()
    assert any(message.kind == "broadcast" for message in messages)
    assert any(
        message.kind == "message" and message.recipient_member_id == member.id
        for message in messages
    )


@pytest.mark.asyncio
async def test_active_item_closed_does_not_escalate_as_label_removed(db):
    scope = await _make_scope(db)
    item = GithubWorkItem(
        scope_id=scope.id,
        issue_number=8,
        issue_title="x",
        issue_url="u",
        github_updated_at=datetime(2026, 7, 1),
        dispatch_status="dispatched",
    )
    db.add(item)
    await db.commit()
    closed = _issue(8, ["some-other-label"])
    closed["state"] = "closed"
    client = _FakeClient(labeled=[], by_number={8: closed})
    await github_watcher_service.poll_scope(db, scope, client)
    await db.refresh(item)
    assert item.dispatch_status == "completed"
    assert item.escalation_reason is None


@pytest.mark.asyncio
async def test_watcher_completed_fires_blocker_merged_notification(db):
    scope = await _make_scope(db)
    leader = AgentTeamSlot(
        preset_id=scope.preset_id,
        position=0,
        display_name="Leader",
        provider="codex-cli",
        repo_id="r",
        repo_path="/tmp/r",
        repo_name="r",
    )
    db.add(leader)
    await db.flush()
    preset = await db.get(AgentTeamPreset, scope.preset_id)
    preset.leader_slot_id = leader.id
    member = MailTeamMember(
        identity_key="slot:leader",
        repo_id="r",
        repo_path="/tmp/r",
        repo_name="r",
        display_name="Leader",
        participant_kind="team_slot",
        team_preset_id=scope.preset_id,
        team_slot_id=leader.id,
    )
    db.add(member)
    item = GithubWorkItem(
        scope_id=scope.id,
        issue_number=858,
        issue_title="design",
        issue_url="u",
        github_updated_at=datetime.utcnow(),
        dispatch_status="awaiting_human_review",
    )
    db.add(item)
    await db.commit()
    closed = _issue(858, ["claude-deck-ready"])
    closed["state"] = "closed"

    await github_watcher_service.poll_scope(
        db,
        scope,
        _FakeClient(labeled=[], by_number={858: closed}),
    )

    await db.refresh(item)
    assert item.dispatch_status == "completed"
    messages = (await db.execute(select(MailMessage))).scalars().all()
    notifications = [
        message
        for message in messages
        if (message.payload or {}).get("kind") == "github_dispatch_blocker_merged"
    ]
    assert len(notifications) == 1
    assert notifications[0].recipient_member_id == member.id


@pytest.mark.asyncio
async def test_closed_issue_reconciles_escalated_item(db):
    scope = await _make_scope(db)
    item = GithubWorkItem(
        scope_id=scope.id,
        issue_number=818,
        issue_title="blocked work",
        issue_url="u",
        github_updated_at=datetime(2026, 7, 4),
        dispatch_status="escalated",
        escalation_reason="plan_blocked",
    )
    db.add(item)
    await db.commit()
    closed = _issue(818, ["claude-deck-ready"])
    closed["state"] = "closed"

    await github_watcher_service.poll_scope(
        db,
        scope,
        _FakeClient(labeled=[], by_number={818: closed}),
    )

    await db.refresh(item)
    assert item.dispatch_status == "completed"
    assert item.escalation_reason is None


@pytest.mark.asyncio
async def test_closed_issue_reconciliation_fires_blocker_merged_notification(db):
    scope = await _make_scope(db)
    leader = AgentTeamSlot(
        preset_id=scope.preset_id,
        position=0,
        display_name="Leader",
        provider="codex-cli",
        repo_id="r",
        repo_path="/tmp/r",
        repo_name="r",
    )
    db.add(leader)
    await db.flush()
    preset = await db.get(AgentTeamPreset, scope.preset_id)
    preset.leader_slot_id = leader.id
    member = MailTeamMember(
        identity_key="slot:closed-reconciliation-leader",
        repo_id="r",
        repo_path="/tmp/r",
        repo_name="r",
        display_name="Leader",
        participant_kind="team_slot",
        team_preset_id=scope.preset_id,
        team_slot_id=leader.id,
    )
    db.add(member)
    item = GithubWorkItem(
        scope_id=scope.id,
        issue_number=819,
        issue_title="blocked work",
        issue_url="u",
        github_updated_at=datetime(2026, 7, 4),
        dispatch_status="escalated",
        escalation_reason="plan_blocked",
    )
    db.add(item)
    await db.commit()
    closed = _issue(819, ["claude-deck-ready"])
    closed["state"] = "closed"

    await github_watcher_service.poll_scope(
        db,
        scope,
        _FakeClient(labeled=[], by_number={819: closed}),
    )

    messages = (await db.execute(select(MailMessage))).scalars().all()
    notifications = [
        message
        for message in messages
        if (message.payload or {}).get("kind") == "github_dispatch_blocker_merged"
    ]
    assert len(notifications) == 1
    assert notifications[0].recipient_member_id == member.id


@pytest.mark.asyncio
async def test_closed_issue_reconciles_failed_item(db):
    scope = await _make_scope(db)
    item = GithubWorkItem(
        scope_id=scope.id,
        issue_number=820,
        issue_title="failed work",
        issue_url="u",
        github_updated_at=datetime(2026, 7, 4),
        dispatch_status="failed",
        escalation_reason="launch_failed",
    )
    db.add(item)
    await db.commit()
    closed = _issue(820, ["claude-deck-ready"])
    closed["state"] = "closed"

    await github_watcher_service.poll_scope(
        db,
        scope,
        _FakeClient(labeled=[], by_number={820: closed}),
    )

    await db.refresh(item)
    assert item.dispatch_status == "completed"
    assert item.escalation_reason is None


@pytest.mark.asyncio
async def test_closed_issue_skips_item_with_open_pr(db, caplog):
    scope = await _make_scope(db)
    item = GithubWorkItem(
        scope_id=scope.id,
        issue_number=821,
        issue_title="work with open PR",
        issue_url="u",
        github_updated_at=datetime(2026, 7, 4),
        dispatch_status="escalated",
        escalation_reason="dispatch_label_removed",
        pr_number=865,
    )
    db.add(item)
    await db.commit()
    closed = _issue(821, ["claude-deck-ready"])
    closed["state"] = "closed"
    caplog.set_level("INFO", logger="app.services.github_watcher_service")

    await github_watcher_service.poll_scope(
        db,
        scope,
        _FakeClient(labeled=[], by_number={821: closed}),
    )

    await db.refresh(item)
    assert item.dispatch_status == "escalated"
    assert item.escalation_reason == "dispatch_label_removed"
    assert item.pr_number == 865
    messages = (await db.execute(select(MailMessage))).scalars().all()
    assert not any(
        (message.payload or {}).get("kind") == "github_dispatch_blocker_merged"
        for message in messages
    )
    # A30: without fresh scoped closed-unmerged proof the item stays escalated.
    assert "PR #865 has no current closed-unmerged proof" in caplog.text


@pytest.mark.asyncio
async def test_failed_item_with_label_removed_is_not_laundered_into_escalated(db):
    scope = await _make_scope(db)
    item = GithubWorkItem(
        scope_id=scope.id,
        issue_number=822,
        issue_title="failed work",
        issue_url="u",
        github_updated_at=datetime(2026, 7, 4),
        dispatch_status="failed",
        escalation_reason="launch_failed",
    )
    db.add(item)
    await db.commit()
    open_issue = _issue(822, [])

    await github_watcher_service.poll_scope(
        db,
        scope,
        _FakeClient(labeled=[], by_number={822: open_issue}),
    )

    await db.refresh(item)
    assert item.dispatch_status == "failed"
    assert item.escalation_reason == "launch_failed"


@pytest.mark.asyncio
async def test_escalated_item_with_open_labeled_issue_is_untouched(db):
    scope = await _make_scope(db)
    item = GithubWorkItem(
        scope_id=scope.id,
        issue_number=823,
        issue_title="blocked work",
        issue_url="u",
        github_updated_at=datetime(2026, 7, 4),
        dispatch_status="escalated",
        escalation_reason="plan_blocked",
    )
    db.add(item)
    await db.commit()
    open_issue = _issue(823, ["claude-deck-ready"])

    await github_watcher_service.poll_scope(
        db,
        scope,
        _FakeClient(labeled=[open_issue], by_number={823: open_issue}),
    )

    await db.refresh(item)
    assert item.dispatch_status == "escalated"
    assert item.escalation_reason == "plan_blocked"
    messages = (await db.execute(select(MailMessage))).scalars().all()
    assert not any(
        (message.payload or {}).get("kind") == "github_dispatch_blocker_merged"
        for message in messages
    )


@pytest.mark.asyncio
async def test_sweep_skips_issues_already_in_labeled_list(db):
    scope = await _make_scope(db)
    item = GithubWorkItem(
        scope_id=scope.id,
        issue_number=824,
        issue_title="open labeled work",
        issue_url="u",
        github_updated_at=datetime(2026, 7, 4),
        dispatch_status="escalated",
        escalation_reason="plan_blocked",
    )
    db.add(item)
    await db.commit()
    open_issue = _issue(824, ["claude-deck-ready"])
    client = _FakeClient(labeled=[open_issue], by_number={824: open_issue})

    await github_watcher_service.poll_scope(db, scope, client)

    requested = [number for call in client.by_number_calls for number in call]
    assert 824 not in requested
    await db.refresh(item)
    assert item.dispatch_status == "escalated"


@pytest.mark.asyncio
async def test_sweep_fetches_only_issues_absent_from_labeled_list(db):
    scope = await _make_scope(db)
    labeled_item = GithubWorkItem(
        scope_id=scope.id,
        issue_number=825,
        issue_title="open labeled work",
        issue_url="u",
        github_updated_at=datetime(2026, 7, 4),
        dispatch_status="escalated",
        escalation_reason="plan_blocked",
    )
    absent_item = GithubWorkItem(
        scope_id=scope.id,
        issue_number=826,
        issue_title="closed absent work",
        issue_url="u",
        github_updated_at=datetime(2026, 7, 4),
        dispatch_status="escalated",
        escalation_reason="plan_blocked",
    )
    db.add_all([labeled_item, absent_item])
    await db.commit()
    open_issue = _issue(825, ["claude-deck-ready"])
    closed_issue = _issue(826, ["claude-deck-ready"])
    closed_issue["state"] = "closed"
    client = _FakeClient(
        labeled=[open_issue],
        by_number={825: open_issue, 826: closed_issue},
    )

    await github_watcher_service.poll_scope(db, scope, client)

    requested = [number for call in client.by_number_calls for number in call]
    assert requested == [826]
    await db.refresh(labeled_item)
    await db.refresh(absent_item)
    assert labeled_item.dispatch_status == "escalated"
    assert absent_item.dispatch_status == "completed"


@pytest.mark.asyncio
async def test_sweep_does_not_treat_absent_open_issue_as_closed(db):
    scope = await _make_scope(db)
    item = GithubWorkItem(
        scope_id=scope.id,
        issue_number=858,
        issue_title="open unlabeled work",
        issue_url="u",
        github_updated_at=datetime(2026, 7, 4),
        dispatch_status="escalated",
        escalation_reason="dispatch_label_removed",
        pr_number=860,
    )
    db.add(item)
    await db.commit()
    open_issue = _issue(858, [])
    client = _FakeClient(labeled=[], by_number={858: open_issue})

    await github_watcher_service.poll_scope(db, scope, client)

    requested = [number for call in client.by_number_calls for number in call]
    assert requested == [858]
    await db.refresh(item)
    assert item.dispatch_status == "escalated"
    assert item.escalation_reason == "dispatch_label_removed"
    messages = (await db.execute(select(MailMessage))).scalars().all()
    assert not any(
        (message.payload or {}).get("kind") == "github_dispatch_blocker_merged"
        for message in messages
    )


@pytest.mark.asyncio
async def test_sweep_makes_no_request_when_all_stalled_items_are_labeled(db):
    scope = await _make_scope(db)
    escalated = GithubWorkItem(
        scope_id=scope.id,
        issue_number=827,
        issue_title="open escalated work",
        issue_url="u",
        github_updated_at=datetime(2026, 7, 4),
        dispatch_status="escalated",
        escalation_reason="plan_blocked",
    )
    failed = GithubWorkItem(
        scope_id=scope.id,
        issue_number=828,
        issue_title="open failed work",
        issue_url="u",
        github_updated_at=datetime(2026, 7, 4),
        dispatch_status="failed",
        escalation_reason="launch_failed",
    )
    db.add_all([escalated, failed])
    await db.commit()
    escalated_issue = _issue(827, ["claude-deck-ready"])
    failed_issue = _issue(828, ["claude-deck-ready"])
    client = _FakeClient(
        labeled=[escalated_issue, failed_issue],
        by_number={827: escalated_issue, 828: failed_issue},
    )

    await github_watcher_service.poll_scope(db, scope, client)

    assert client.by_number_calls == []
    await db.refresh(escalated)
    await db.refresh(failed)
    assert escalated.dispatch_status == "escalated"
    assert failed.dispatch_status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("labels,expected", [
    (["priority:1"], 1), (["priority:9999"], 9999),
    (["priority:8", "priority:2", "priority:01"], 2),
    ([], None), (["Priority:1", "priority:0", "priority:-1", "priority:01", "priority:10000", "priority:1x"], None),
])
async def test_priority_watcher_observes_labels_and_clears_removed_values(db, labels, expected):
    scope = await _make_scope(db)
    await github_watcher_service._upsert_item(db, scope, _issue(795, [scope.dispatch_label, *labels]))
    await db.commit()
    item = await db.scalar(select(GithubWorkItem).where(GithubWorkItem.issue_number == 795))
    assert hasattr(type(item), "dispatch_priority")
    assert item.dispatch_priority == expected
    await github_watcher_service._upsert_item(db, scope,
        _issue(795, [scope.dispatch_label, "priority:7"], updated="2026-07-05T00:00:00Z"))
    await db.commit()
    await db.refresh(item)
    assert item.dispatch_priority == 7
    await github_watcher_service._upsert_item(db, scope,
        _issue(795, [scope.dispatch_label], updated="2026-07-06T00:00:00Z"))
    await db.commit()
    await db.refresh(item)
    assert item.dispatch_priority is None and item.dispatch_status == "pending"
    assert item.dispatch_nonce is None and item.retry_count == 0


# #1337, review 6074331482: a poll that began before its scope changed repository must not write for the scope.
@pytest_asyncio.fixture
async def maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'paused.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class _PausedClient(_FakeClient):
    """Holds the remote read until the test releases it, as a slow GitHub answer does."""

    def __init__(self, labeled):
        super().__init__(labeled=labeled)
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def list_issues_with_label(self, owner, repo, label):
        self.entered.set()
        await self.release.wait()
        return list(self._labeled)


async def _poll_while(maker, change, *, milestone="V1"):
    """Poll a scope; run `change` on the scope row while the remote read waits. Return the stored state."""
    async with maker() as db, maker() as other:
        old = await _make_scope(db, preset_name="Old", milestone=milestone)
        twin = await _make_scope(db, preset_name="Twin", milestone="V9")  # another scope of the old repository
        old_id, twin_id = old.id, twin.id
        client = _PausedClient([_milestone_issue(old, "V1")])
        task = asyncio.create_task(github_watcher_service.poll_scope(db, old, client))
        await client.entered.wait()
        await change(other, old_id)
        client.release.set()
        await task
    async with maker() as check:
        stored = await check.get(TeamGithubScope, old_id)
        return SimpleNamespace(
            items=(await check.scalars(select(GithubWorkItem))).all(),
            waits=await _milestone_wait_titles(check),
            polled=stored.last_polled_at if stored else None,
            twin_polled=(await check.get(TeamGithubScope, twin_id)).last_polled_at,
        )


async def _move(other, scope_id):
    await other.execute(update(TeamGithubScope).where(TeamGithubScope.id == scope_id).values(repo_name="moved"))
    await other.commit()


async def _remove(other, scope_id):
    await other.execute(delete(TeamGithubScope).where(TeamGithubScope.id == scope_id))
    await other.commit()


async def _same_repository_other_case(other, scope_id):
    await other.execute(update(TeamGithubScope).where(TeamGithubScope.id == scope_id).values(repo_owner="O"))
    await other.commit()


@pytest.mark.asyncio
async def test_a_poll_that_began_before_the_scope_moved_writes_nothing(maker):
    state = await _poll_while(maker, _move)
    assert state.items == [] and state.waits == {}
    assert state.polled is None and state.twin_polled is None


@pytest.mark.asyncio
async def test_a_poll_that_began_before_the_scope_was_removed_writes_nothing(maker):
    state = await _poll_while(maker, _remove)
    assert state.items == [] and state.waits == {}


@pytest.mark.asyncio
async def test_a_poll_goes_on_when_only_the_case_of_the_repository_changed(maker):
    state = await _poll_while(maker, _same_repository_other_case)
    assert [item.issue_number for item in state.items] == [1]
    assert state.polled is not None
