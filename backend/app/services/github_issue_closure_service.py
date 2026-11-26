"""Close the issue for a committed auto-merge delivery."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import AgentTeamPreset, GithubIssueClosure, GithubWorkItem, TeamGithubScope
from app.services.github_client import GithubClient, GithubClientResponseError
from app.services.github_coordination_service import hold_code
from app.services.github_rate_limit_service import GithubRateLimited

logger = logging.getLogger(__name__)
_POLL_LIMIT = 4
_ATTEMPT_TIMEOUT_SECONDS = 20
_RETRY_DELAY = timedelta(minutes=5)
_COMMENT_SCAN_LIMIT = 500  # The existing GitHub client reads at most five pages.


async def queue_issue_closure(db: AsyncSession, item: GithubWorkItem) -> None:
    """Record one job in the caller's terminal transaction. No historic backfill."""
    if item.issue_type != "code" or item.pr_number is None:
        return
    scope = await db.get(TeamGithubScope, item.scope_id, populate_existing=True)
    if scope is None or scope.merge_policy != "auto":
        return
    if await db.get(GithubIssueClosure, item.id) is None:
        db.add(GithubIssueClosure(work_item_id=item.id, repo_owner=scope.repo_owner, repo_name=scope.repo_name,
            issue_number=item.issue_number, pr_number=item.pr_number))


def _eligible_jobs(scope_id: int):
    return select(GithubIssueClosure).join(GithubWorkItem, GithubWorkItem.id == GithubIssueClosure.work_item_id).join(
        TeamGithubScope, TeamGithubScope.id == GithubWorkItem.scope_id).join(
        AgentTeamPreset, AgentTeamPreset.id == TeamGithubScope.preset_id).where(
        TeamGithubScope.id == scope_id, TeamGithubScope.enabled.is_(True),
        AgentTeamPreset.autonomy_enabled.is_(True), TeamGithubScope.merge_policy == "auto",
        TeamGithubScope.repo_owner == GithubIssueClosure.repo_owner,
        TeamGithubScope.repo_name == GithubIssueClosure.repo_name,
        GithubWorkItem.dispatch_status == "merged", GithubWorkItem.issue_type == "code",
        GithubWorkItem.issue_number == GithubIssueClosure.issue_number,
        GithubWorkItem.pr_number == GithubIssueClosure.pr_number, GithubIssueClosure.completed_at.is_(None))


async def _issue_is_open(client: GithubClient, owner: str, repo: str, issue_number: int) -> bool:
    issue = await client.get_issue(owner, repo, issue_number)
    if issue is None or issue.get("state") not in {"open", "closed"}:
        raise GithubClientResponseError("GitHub issue state was unavailable for closure")
    return issue["state"] == "open"


async def _deliver(db: AsyncSession, scope_id: int, item_id: int, retry_at: datetime, client: GithubClient) -> bool:
    claim = _eligible_jobs(scope_id).where(GithubIssueClosure.work_item_id == item_id,
        GithubIssueClosure.next_attempt_at == retry_at)
    target = (await db.execute(claim.with_only_columns(GithubIssueClosure.repo_owner, GithubIssueClosure.repo_name,
        GithubIssueClosure.issue_number, GithubIssueClosure.pr_number))).one_or_none()
    if target is None or hold_code():
        return False
    owner, repo, issue_number, pr_number = target
    if not await _issue_is_open(client, owner, repo, issue_number):
        return True

    pull = await client.get_pull(owner, repo, pr_number)
    base = pull.get("base")
    base_repo = base.get("repo") if isinstance(base, dict) else None
    merge_sha = pull.get("merge_commit_sha")
    merge_time = pull.get("merged_at")
    if (type(pull.get("number")) is not int or pull["number"] != pr_number
            or pull.get("state") != "closed" or pull.get("merged") is not True or not isinstance(merge_time, str)
            or not isinstance(base_repo, dict) or str(base_repo.get("full_name")).lower() != f"{owner}/{repo}".lower()
            or not isinstance(merge_sha, str) or re.fullmatch(r"[a-f0-9]{40}", merge_sha) is None):
        raise GithubClientResponseError("GitHub merge evidence was unavailable for issue closure")
    try:
        merged_at = datetime.fromisoformat(merge_time)
    except ValueError as error:
        raise GithubClientResponseError("GitHub merge time was invalid for issue closure") from error
    if merged_at.tzinfo is None:
        raise GithubClientResponseError("GitHub merge time was missing its time zone")

    marker = f"<!-- deck:issue-closure:{pr_number}:{merge_sha} -->"
    # GitHub's filter is exclusive. Keep comments posted in the same second as the merge.
    since = (merged_at.astimezone(timezone.utc) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    comments = await client.list_issue_comments(owner, repo, issue_number, since=since)
    if not any(isinstance(body := comment.get("body"), str) and body.endswith(marker) for comment in comments):
        # A truncated list cannot prove the completion comment is absent.
        if len(comments) >= _COMMENT_SCAN_LIMIT:
            raise GithubClientResponseError("GitHub issue comments were incomplete for closure")
        if not await _issue_is_open(client, owner, repo, issue_number):
            return True
        if await db.scalar(claim.with_only_columns(GithubIssueClosure.work_item_id)) is None or hold_code():
            return False
        body = f"Delivered in PR #{pr_number} at merge commit `{merge_sha}`.\n\n{marker}"
        await client.create_issue_comment(owner, repo, issue_number, body)

    if not await _issue_is_open(client, owner, repo, issue_number):
        return True
    if await db.scalar(claim.with_only_columns(GithubIssueClosure.work_item_id)) is None or hold_code():
        return False
    await client.close_issue(owner, repo, issue_number)
    return True


async def process_issue_closures(
    db: AsyncSession, scope_id: int, client: GithubClient, *, due_before: datetime | None = None,
) -> None:
    """Claim due jobs before writing to GitHub. A lost reply is reconciled on the next attempt."""
    if hold_code():
        return
    due = _eligible_jobs(scope_id).with_only_columns(GithubIssueClosure.work_item_id).where(
        GithubIssueClosure.next_attempt_at < (due_before or datetime.utcnow()))
    item_ids = (await db.scalars(due.order_by(
        GithubIssueClosure.next_attempt_at, GithubIssueClosure.work_item_id).limit(_POLL_LIMIT))).all()
    for item_id in item_ids:
        if hold_code():
            return
        retry_at = datetime.utcnow() + _RETRY_DELAY
        claim = await db.execute(update(GithubIssueClosure).where(GithubIssueClosure.work_item_id.in_(
            due.where(GithubIssueClosure.work_item_id == item_id))).values(next_attempt_at=retry_at)
            .returning(GithubIssueClosure.work_item_id).execution_options(synchronize_session=False))
        claimed = claim.scalar_one_or_none()
        await db.commit()
        if claimed is None:
            continue
        try:
            async with asyncio.timeout(_ATTEMPT_TIMEOUT_SECONDS):
                completed = await _deliver(db, scope_id, item_id, retry_at, client)
        except GithubRateLimited:
            await db.rollback()
            raise
        except (TimeoutError, httpx.HTTPError, GithubClientResponseError) as error:
            await db.rollback()
            logger.warning("GitHub issue closure for work item %s will retry (%s)", item_id, type(error).__name__)
            continue
        if completed:
            await db.execute(update(GithubIssueClosure).where(GithubIssueClosure.work_item_id == item_id,
                GithubIssueClosure.next_attempt_at == retry_at, GithubIssueClosure.completed_at.is_(None))
                .values(completed_at=datetime.utcnow()).execution_options(synchronize_session=False))
            await db.commit()
