"""Post-merge issue closure uses only committed and still-eligible work."""
import asyncio
from datetime import datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.database import AgentTeamPreset, GithubIssueClosure, GithubWorkItem, TeamGithubScope
from app.services.github_client import GithubClientResponseError
from app.services.github_verification_service import github_verification_service


@pytest_asyncio.fixture
async def job(tmp_path, monkeypatch):
    from app.services import github_issue_closure_service

    monkeypatch.setattr(github_issue_closure_service, "hold_code", lambda: None)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'issue-closure.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions.begin() as db:
        preset = AgentTeamPreset(name="Closure", autonomy_enabled=True)
        db.add(preset)
        await db.flush()
        scope = TeamGithubScope(preset_id=preset.id, repo_owner="o", repo_name="r", repo_path="/fixture",
            enabled=True, merge_policy="auto", github_auth_mode="ambient")
        db.add(scope)
        await db.flush()
        item = GithubWorkItem(scope_id=scope.id, issue_number=94, issue_title="Closure fixture",
            issue_url="https://github.com/o/r/issues/94", github_updated_at=datetime.utcnow(),
            dispatch_status="verifying", issue_type="code", pr_number=5)
        db.add(item)
        await db.flush()
        await github_verification_service._mark_merged_with_fact(db, item, {"number": 5}, source="closure-test")
        scope_id, item_id = scope.id, item.id
    try:
        yield sessions, scope_id, item_id
    finally:
        await engine.dispose()


class Github:
    def __init__(self, *, lose=None):
        self.issue_state = "open"
        self.comments = []
        self.closed = 0
        self.lose = lose
        self.post_started = None
        self.allow_post = None

    async def get_issue(self, owner, repo, number):
        assert (owner, repo, number) == ("o", "r", 94)
        return {"number": number, "state": self.issue_state}

    async def get_pull(self, owner, repo, number):
        assert (owner, repo, number) == ("o", "r", 5)
        return {"number": 5, "state": "closed", "merged": True, "merged_at": "2026-11-26T07:00:00Z",
            "merge_commit_sha": "a" * 40, "base": {"repo": {"full_name": "o/r"}}}

    async def list_issue_comments(self, owner, repo, number, *, since):
        assert since == "2026-11-26T06:59:59Z"
        return list(self.comments)

    async def create_issue_comment(self, owner, repo, number, body):
        if self.post_started is not None:
            self.post_started.set()
            await self.allow_post.wait()
        self.comments.append({"body": body})
        if self.lose == "comment":
            raise httpx.ReadTimeout("Fixture lost the comment response")
        return {"id": 1, "body": body}

    async def close_issue(self, owner, repo, number):
        self.closed += 1
        self.issue_state = "closed"
        if self.lose == "close":
            raise httpx.ReadTimeout("Fixture lost the close response")


async def poll(sessions, scope_id, client):
    from app.services.github_issue_closure_service import process_issue_closures

    async with sessions() as db:
        await process_issue_closures(db, scope_id, client)


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_response", ["comment", "close"])
async def test_lost_response_and_next_poll_cannot_duplicate_remote_writes(job, lost_response):
    sessions, scope_id, item_id = job
    remote = Github(lose=lost_response)
    await poll(sessions, scope_id, remote)
    assert len(remote.comments) == 1
    assert remote.closed == int(lost_response == "close")
    # One immediate poll leaves the claim's retry delay in place.
    remote.lose = None
    await poll(sessions, scope_id, remote)
    assert remote.closed == int(lost_response == "close")
    async with sessions.begin() as db:
        await db.execute(update(GithubIssueClosure).where(GithubIssueClosure.work_item_id == item_id)
            .values(next_attempt_at=datetime.utcnow() - timedelta(seconds=1)))
    await poll(sessions, scope_id, remote)
    await poll(sessions, scope_id, remote)
    assert len(remote.comments) == remote.closed == 1
    async with sessions() as db:
        assert (await db.get(GithubIssueClosure, item_id)).completed_at is not None


@pytest.mark.asyncio
async def test_repeated_merge_record_preserves_completed_closure(job):
    sessions, scope_id, item_id = job
    remote = Github()
    await poll(sessions, scope_id, remote)

    async with sessions.begin() as db:
        closure = await db.get(GithubIssueClosure, item_id)
        assert closure.completed_at is not None
        completed_at, next_attempt_at = closure.completed_at, closure.next_attempt_at
        item = await db.get(GithubWorkItem, item_id)
        await github_verification_service._mark_merged_with_fact(db, item, {"number": 5}, source="closure-test")
        await db.flush()
        assert (closure.completed_at, closure.next_attempt_at) == (completed_at, next_attempt_at)

    await poll(sessions, scope_id, remote)
    assert len(remote.comments) == remote.closed == 1


@pytest.mark.asyncio
async def test_another_poll_cannot_write_while_first_poll_owns_the_claim(job):
    sessions, scope_id, _ = job
    remote = Github()
    remote.post_started, remote.allow_post = asyncio.Event(), asyncio.Event()
    first = asyncio.create_task(poll(sessions, scope_id, remote))
    try:
        await asyncio.wait_for(remote.post_started.wait(), timeout=3)
        await asyncio.wait_for(poll(sessions, scope_id, remote), timeout=3)
        assert remote.comments == []
    finally:
        remote.allow_post.set()
        await first
    assert len(remote.comments) == remote.closed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["item", "scope", "hold", "disabled", "different_pr", "different_repository"])
async def test_a_change_before_the_comment_blocks_writes(job, monkeypatch, changed):
    from app.services import github_issue_closure_service

    sessions, scope_id, item_id = job

    class StateChange(Github):
        async def list_issue_comments(self, owner, repo, number, *, since):
            if changed == "hold":
                monkeypatch.setattr(github_issue_closure_service, "hold_code", lambda: "hold")
            else:
                async with sessions.begin() as db:
                    if changed in {"item", "different_pr"}:
                        item = await db.get(GithubWorkItem, item_id)
                        if changed == "item":
                            item.dispatch_status = "verifying"
                        else:
                            item.pr_number = 6
                    else:
                        scope = await db.get(TeamGithubScope, scope_id)
                        if changed == "disabled":
                            scope.enabled = False
                        elif changed == "different_repository":
                            scope.repo_name = "other"
                        else:
                            scope.merge_policy = "human"
            return []

    remote = StateChange()
    await poll(sessions, scope_id, remote)
    assert remote.comments == []
    assert remote.closed == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["replacement", "completion"])
async def test_existing_comment_does_not_allow_closing_after_claim_changes(job, changed):
    sessions, scope_id, item_id = job
    changed_claim = False

    class ClaimChanged(Github):
        async def list_issue_comments(self, owner, repo, number, *, since):
            nonlocal changed_claim
            comments = await super().list_issue_comments(owner, repo, number, since=since)
            now = datetime.utcnow()
            values = {"next_attempt_at": now + timedelta(hours=1)} if changed == "replacement" else {"completed_at": now}
            async with sessions.begin() as db:
                await db.execute(update(GithubIssueClosure).where(GithubIssueClosure.work_item_id == item_id)
                    .values(**values))
            changed_claim = True
            return comments

    remote = ClaimChanged()
    merge_sha = "a" * 40
    note = f"Delivered in PR #5 at merge commit `{merge_sha}`.\n\n<!-- deck:issue-closure:5:{merge_sha} -->"
    remote.comments = [{"body": note}]

    await poll(sessions, scope_id, remote)

    assert changed_claim
    assert remote.comments == [{"body": note}]
    assert remote.closed == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", [
    {"number": 6},
    {"base": {"repo": {"full_name": "o/other"}}},
    {"state": "open"},
    {"merged": False},
    {"merged_at": None},
    {"merged_at": "not-a-date"},
    {"merged_at": "2026-11-26T07:00:00"},
    {"merge_commit_sha": "not-a-commit"},
], ids=["different-pr", "wrong-repository", "open", "unmerged", "no-merge-time",
    "invalid-merge-time", "no-timezone", "invalid-merge-sha"])
async def test_invalid_github_merge_evidence_prevents_remote_writes(job, evidence):
    from app.services.github_issue_closure_service import _deliver

    sessions, scope_id, item_id = job

    class InvalidMerge(Github):
        async def get_pull(self, owner, repo, number):
            return {**(await super().get_pull(owner, repo, number)), **evidence}

    remote = InvalidMerge()
    async with sessions() as db:
        closure = await db.get(GithubIssueClosure, item_id)
        with pytest.raises(GithubClientResponseError, match="GitHub merge"):
            await _deliver(db, scope_id, item_id, closure.next_attempt_at, remote)
        assert closure.completed_at is None
    assert remote.comments == []
    assert remote.closed == 0


@pytest.mark.asyncio
async def test_incomplete_comment_history_cannot_prove_the_note_is_absent(job):
    sessions, scope_id, _ = job
    remote = Github()
    remote.comments = [{"body": "An unrelated comment"}] * 500

    await poll(sessions, scope_id, remote)

    assert len(remote.comments) == 500
    assert remote.closed == 0
