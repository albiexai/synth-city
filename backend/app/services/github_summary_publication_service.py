"""Generated review and remaining-work summaries (#940, phase 1).

The two blocks are a pure function of recorded facts. They are published as ONE system-owned pull request
comment, created once and then edited by its recorded id. The system never edits a PR or issue body: GitHub offers
no conditional body update, so a write could overwrite a concurrent human edit and no operator record or
hand-written block is ever touched. The comment grants no approval, review or merge authority, and nothing in
approval, review, acceptance, readiness or merge reads it.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.database import GithubApprovalRequest, GithubHeadReview, GithubWorkItem, TeamGithubScope
from app.services.factory_delivery_policy import effective_policy, scope_policy

logger = logging.getLogger(__name__)

MARKER = "<!-- deck:summary-comment -->"
_CLAIM_STALE = timedelta(minutes=5)
_MERGED_WINDOW = timedelta(hours=24)
_REREAD = timedelta(minutes=5)
_PUBLISH_STATUSES = ("ready_for_review", "awaiting_human_review", "merged")
_SHA = re.compile(r"\b[0-9a-f]{40}\b")
_PRIVATE = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]|github_pat_|sk-(?:or-v1-)?[A-Za-z0-9]|bearer\s+\S|"
    r"(?<![A-Za-z])(?:token|password|secret|credential|lease|nonce|api[_-]?key)\w*\s*[:=]|\b[a-f0-9]{16,}\b)", re.I)
_VERDICT = re.compile(r"[a-z_]{1,32}")
_BASE = re.compile(r"[A-Za-z0-9._/-]{1,100}")
_locks: dict[tuple[str, int], asyncio.Lock] = {}


class SummaryRenderError(ValueError):
    """A fact holds text that must not be published."""


class SummaryUnavailable(RuntimeError):
    """The publisher cannot act now (for example the scope's GitHub token could not be minted)."""


@dataclass(frozen=True)
class SummaryFacts:
    repo: str = ""
    pull_number: int = 0
    pull_url: str = ""
    base: str = ""
    head_sha: str = ""
    state: str = "open"  # open, merged or closed
    draft: bool = False
    merge_sha: str | None = None
    hold_label: str | None = None
    hold_present: bool | None = None
    checks: tuple[tuple[str, str], ...] = ()
    review: tuple[str, str | None] | None = None  # verdict and canonical evidence link (None: withheld)
    tracked: bool = False
    issue_number: int | None = None
    plan: tuple[int, int] | None = None  # approved request id and round
    effort: tuple[int, int, int] | None = None  # recorded limit, extension, advisory ledger minutes
    merge_policy: str = "human"
    item_status: str | None = None


def _guard(text: str) -> str:
    if _PRIVATE.search(_SHA.sub("", text)):
        raise SummaryRenderError("a fact holds text that must not be published")
    return text


def _pull_line(facts: SummaryFacts) -> str:
    if facts.state == "merged":
        phrase = f"merged as `{facts.merge_sha}`" if facts.merge_sha else "merged"
    elif facts.state == "closed":
        phrase = "closed without merging"
    elif facts.draft:
        phrase = "a draft"
    else:
        phrase = "ready for review"
    return f"- **Pull request:** {facts.pull_url} into `{facts.base}`, head `{facts.head_sha}`, {phrase}."


def _checks_line(facts: SummaryFacts) -> str:
    if not facts.checks:
        return "- **Required checks:** none configured."
    return "- **Required checks:** " + "; ".join(f"{name}: {state}" for name, state in facts.checks) + "."


def _review_line(facts: SummaryFacts) -> str:
    if facts.review is not None:
        verdict, evidence = facts.review
        shown = evidence if evidence else "evidence link withheld"
        return f"- **Head review:** {verdict} at this exact head ({shown})."
    return ("- **Head review:** none recorded for this head." if facts.tracked
            else "- **Head review:** not recorded.")


def _hold_line(facts: SummaryFacts) -> str:
    if facts.hold_label is None:
        return "- **Review hold label:** not configured."
    state = {True: "present", False: "absent", None: "not known"}[facts.hold_present]
    return f"- **Review hold label:** {state}."


def _plan_line(facts: SummaryFacts) -> str:
    if facts.plan is None:
        return "- **Approved plan:** not recorded."
    return f"- **Approved plan:** request {facts.plan[0]}, round {facts.plan[1]}."


def _effort_line(facts: SummaryFacts) -> str:
    label = "- **Recorded effort limit (advisory ledger; not an estimate of remaining work):** "
    if facts.effort is None:
        return label + "not recorded."
    limit, extension, elapsed = facts.effort
    shown = f"{limit} minutes" + (f" (+{extension} extension)" if extension else "")
    return label + f"{shown}; ledger {elapsed} minutes."


def _phase(facts: SummaryFacts) -> tuple[str, str, str]:
    """Requested action, Remaining and Next, derived only from the recorded state."""
    if facts.state == "merged":
        return "none.", "Closeout.", "The owner closes out."
    if facts.state == "closed":
        return ("none until the Leader decides the next step.",
                "Decide the next step; the pull request is closed without merging.", "The Leader decides.")
    if facts.draft:
        return ("none from a human; the owner marks the pull request ready when review and checks are complete.",
                "Complete review, required checks and readiness.",
                "The owner and the reviewer finish review; then the owner marks it ready.")
    if facts.merge_policy == "auto":
        return ("none from a human; the configured auto merge applies when every gate is met.",
                "Complete the configured merge and closeout.",
                "The configured merge applies; then the owner closes out.")
    return ("a human reviews the pull request and decides the merge; the configured policy never merges for a human.",
            "Complete the human merge decision and closeout.", "A human reviews and merges; then the owner closes out.")


def render_comment(facts: SummaryFacts) -> str:
    action, remaining, next_step = _phase(facts)
    lines = [
        MARKER,
        "Generated and owned by Synth City from recorded facts. It is rewritten when the facts change, so an edit "
        "here is overwritten. It does not replace the owner's summary in the PR and issue bodies and grants no "
        "approval, review or merge authority.",
        "",
        "<!-- deck:human-review-summary:start -->",
        "## Human review summary (generated)",
        "",
        _pull_line(facts), _checks_line(facts), _review_line(facts), _hold_line(facts), _plan_line(facts),
        _effort_line(facts),
        f"- **Requested action:** {action}",
        "<!-- deck:human-review-summary:end -->",
        "",
        "<!-- deck:work-remaining:start -->",
        "## Work remaining",
        "",
        f"- **Remaining:** {remaining}",
        "- **Estimate:** Unknown.",
        f"- **Next:** {next_step}",
        "<!-- deck:work-remaining:end -->",
    ]
    return _guard("\n".join(lines) + "\n")


def render_digest(facts: SummaryFacts) -> str:
    return hashlib.sha256(render_comment(facts).encode()).hexdigest()


def _state(pull: dict) -> str:
    if pull.get("merged"):
        return "merged"
    return "closed" if pull.get("state") == "closed" else "open"


def _check_states(policy, checks: list[dict], head: str) -> tuple[tuple[str, str], ...]:
    states = []
    for required in policy.required_checks:
        rows = [row for row in checks if row.get("name") == required.name
                and (row.get("app") or {}).get("slug") == required.app_slug and row.get("head_sha") == head]
        if not rows:
            state = "missing"
        elif any(row.get("status") != "completed" for row in rows):
            state = "pending"
        elif any(row.get("conclusion") != "success" for row in rows):
            state = "failure"
        else:
            state = "success"
        states.append((required.name, state))
    return tuple(states)


def _base_matches(pull: dict, item: GithubWorkItem) -> bool:
    expected = (item.dispatch_base_ref or "").removeprefix("origin/")
    return not expected or (pull.get("base") or {}).get("ref") == expected


class GithubSummaryPublicationService:
    async def _facts(self, db: AsyncSession, scope: TeamGithubScope, item: GithubWorkItem | None, pull: dict,
                     checks: list[dict], number: int) -> SummaryFacts:
        head = str((pull.get("head") or {}).get("sha") or "")
        labels = {str(row.get("name")) for row in pull.get("labels") or [] if isinstance(row, dict)}
        hold = scope.review_hold_label
        policy = effective_policy(item, scope) if item is not None else scope_policy(scope)
        review = plan = effort = None
        if item is not None:
            row = (await db.execute(select(GithubHeadReview).where(
                GithubHeadReview.work_item_id == item.id, GithubHeadReview.head_sha == head,
                GithubHeadReview.status == "active").order_by(GithubHeadReview.id.desc()).limit(1))).scalar_one_or_none()
            review = self._review_fact(row, scope, number)
            approval = (await db.execute(select(GithubApprovalRequest).where(
                GithubApprovalRequest.work_item_id == item.id, GithubApprovalRequest.request_kind == "initial_plan",
                GithubApprovalRequest.dispatch_nonce == item.dispatch_nonce,
                GithubApprovalRequest.status == "approved").order_by(GithubApprovalRequest.id.desc()).limit(1)
            )).scalar_one_or_none()
            plan = (approval.id, approval.approval_round) if approval is not None else None
            if item.effort_limit_minutes is not None:
                effort = (item.effort_limit_minutes, item.effort_extension_minutes or 0,
                          (item.effort_elapsed_seconds or 0) // 60)
        base = str((pull.get("base") or {}).get("ref") or "")
        merge_sha = str(pull.get("merge_commit_sha") or "") if pull.get("merged") else ""
        return SummaryFacts(
            repo=f"{scope.repo_owner}/{scope.repo_name}", pull_number=number,
            # Built from the repository and the number, never copied from a response field.
            pull_url=f"https://github.com/{scope.repo_owner}/{scope.repo_name}/pull/{number}",
            base=base if _BASE.fullmatch(base) else "", head_sha=head if _SHA.fullmatch(head) else "",
            state=_state(pull), draft=bool(pull.get("draft")),
            merge_sha=merge_sha if _SHA.fullmatch(merge_sha) else None, hold_label=hold,
            hold_present=(hold in labels) if hold else None, checks=_check_states(policy, checks, head),
            review=review, tracked=item is not None, issue_number=item.issue_number if item is not None else None,
            plan=plan, effort=effort, merge_policy=scope.merge_policy,
            item_status=item.dispatch_status if item is not None else None)

    @staticmethod
    def _review_fact(row, scope: TeamGithubScope, number: int) -> tuple[str, str | None] | None:
        """The head review for this exact head. Only a canonical GitHub review link of THIS pull request is shown."""
        if row is None:
            return None
        evidence = str(row.evidence_url or "")
        prefix = f"https://github.com/{scope.repo_owner}/{scope.repo_name}/pull/{number}#pullrequestreview-"
        # Plain string checks, no pattern built from stored values: only this pull request's own review link passes.
        canonical = evidence if (evidence.startswith(prefix) and evidence[len(prefix):].isascii()
                                 and evidence[len(prefix):].isdigit() and 0 < len(evidence[len(prefix):]) <= 20) else None
        verdict = row.verdict if _VERDICT.fullmatch(str(row.verdict or "")) else "unknown"
        return verdict, canonical

    @staticmethod
    async def _token(scope: TeamGithubScope):
        """The scope's repository token with the approved pull-request purpose; no other source, no fallback."""
        from app.services.github_approval_service import github_approval_service

        try:
            return await github_approval_service.github_read_token(scope)
        except Exception as exc:
            raise SummaryUnavailable(type(exc).__name__) from exc

    async def _tracked_item(self, db: AsyncSession, scope: TeamGithubScope, pull_number: int):
        return (await db.execute(select(GithubWorkItem).where(
            GithubWorkItem.scope_id == scope.id, GithubWorkItem.pr_number == pull_number)
            .order_by(GithubWorkItem.id.desc()).limit(1))).scalar_one_or_none()

    async def render(self, db: AsyncSession, scope: TeamGithubScope, pull_number: int, client) -> dict:
        """Read only: facts from the records and GitHub, rendered. Nothing is written anywhere."""
        token = await self._token(scope)
        item = await self._tracked_item(db, scope, pull_number)
        pull = await client.get_pull(scope.repo_owner, scope.repo_name, pull_number, token=token)
        head = str((pull.get("head") or {}).get("sha") or "")
        checks = (await client.list_check_runs_for_ref(scope.repo_owner, scope.repo_name, head, token=token)
                  if head else [])
        facts = await self._facts(db, scope, item, pull, checks, pull_number)
        text = render_comment(facts)
        return {"tracked": item is not None, "pull_number": pull_number, "head_sha": head, "state": facts.state,
                "digest": hashlib.sha256(text.encode()).hexdigest(), "comment": text}

    # -- writing: one system-owned comment --------------------------------------------------------------------------

    @staticmethod
    def _ours(comment: dict | None, author: str | None, scope: TeamGithubScope, number: int) -> bool:
        """Marker, bot author AND this repository's current pull request must all match."""
        return (isinstance(comment, dict) and MARKER in str(comment.get("body") or "") and bool(author)
                and (comment.get("user") or {}).get("login") == author
                and str(comment.get("issue_url") or "").endswith(
                    f"/repos/{scope.repo_owner}/{scope.repo_name}/issues/{number}"))

    async def _locate(self, scope, client, number: int, recorded_id, recorded_author, token):
        """The comment this system owns for THIS pull request, or ``None``."""
        if recorded_id is not None:
            found = await client.get_issue_comment(scope.repo_owner, scope.repo_name, int(recorded_id), token=token)
            if self._ours(found, recorded_author, scope, number):
                return found
        bot = settings.github_app_bot_login
        if bot:
            for comment in await client.list_issue_comments(scope.repo_owner, scope.repo_name, number, token=token):
                if self._ours(comment, bot, scope, number):
                    return comment
        return None

    async def _write(self, scope, client, number: int, existing: dict | None, text: str, token):
        if existing is None:
            return "created", await client.create_issue_comment(
                scope.repo_owner, scope.repo_name, number, text, token=token)
        if str(existing.get("body") or "") == text:
            return "unchanged", existing
        return "updated", await client.update_issue_comment(
            scope.repo_owner, scope.repo_name, int(existing["id"]), text, token=token)

    async def _still_current(self, scope, client, number: int, head: str, token) -> bool:
        """A fresh read, as close to the write as possible: the head and state are still the rendered ones."""
        again = await client.get_pull(scope.repo_owner, scope.repo_name, number, token=token)
        return str((again.get("head") or {}).get("sha") or "") == head and _state(again) != "closed"

    async def publish_item(self, db: AsyncSession, scope: TeamGithubScope, item: GithubWorkItem, client) -> str:
        """Publish for a tracked pull request. Returns created, updated, unchanged, skipped or unavailable."""
        item_id, number = item.id, int(item.pr_number)
        try:
            token = await self._token(scope)
            pull = await client.get_pull(scope.repo_owner, scope.repo_name, number, token=token)
            state = _state(pull)
            if state == "closed" or not _base_matches(pull, item):
                return "skipped"
            head = str((pull.get("head") or {}).get("sha") or "")
            checks = (await client.list_check_runs_for_ref(scope.repo_owner, scope.repo_name, head, token=token)
                      if head else [])
            fresh = (await db.execute(select(GithubWorkItem).where(GithubWorkItem.id == item_id)
                                      .execution_options(populate_existing=True))).scalar_one()
            facts = await self._facts(db, scope, fresh, pull, checks, number)
            text = render_comment(facts)
            digest = hashlib.sha256(text.encode()).hexdigest()
            now = datetime.utcnow()
            if fresh.summary_published_digest == digest and fresh.summary_comment_id is not None:
                # Nothing changed: restart the throttle so the scope pass does not read GitHub again at once.
                await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == item_id).values(
                    summary_published_at=now).execution_options(synchronize_session=False))
                await db.commit()
                return "unchanged"
            claim = await db.execute(update(GithubWorkItem).where(
                GithubWorkItem.id == item_id,
                or_(GithubWorkItem.summary_publish_claim_at.is_(None),
                    GithubWorkItem.summary_publish_claim_at < now - _CLAIM_STALE),
            ).values(summary_publish_claim_at=now).execution_options(synchronize_session=False))
            await db.commit()
            if claim.rowcount != 1:
                return "skipped"
            try:
                existing = await self._locate(scope, client, number, fresh.summary_comment_id,
                                              fresh.summary_comment_author, token)
                if not await self._still_current(scope, client, number, head, token):
                    return "skipped"
                outcome, written = await self._write(scope, client, number, existing, text, token)
                author = str((written.get("user") or {}).get("login") or settings.github_app_bot_login or "") or None
                await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == item_id).values(
                    summary_comment_id=int(written["id"]), summary_comment_author=author,
                    summary_published_digest=digest, summary_published_head_sha=head,
                    summary_published_status=fresh.dispatch_status, summary_published_at=now)
                    .execution_options(synchronize_session=False))
                await db.commit()
                return outcome
            finally:
                await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == item_id).values(
                    summary_publish_claim_at=None).execution_options(synchronize_session=False))
                await db.commit()
        except Exception as exc:  # publication never blocks or changes any gate
            await db.rollback()
            logger.warning("summary_publication_unavailable work_item_id=%s error_type=%s", item_id,
                           type(exc).__name__)
            return "unavailable"

    async def publish_pull(self, db: AsyncSession, scope: TeamGithubScope, pull_number: int, client) -> str:
        """Publish for any pull request: tracked ones through the item, standalone ones by marker and author."""
        item = await self._tracked_item(db, scope, pull_number)
        if item is not None:
            return await self.publish_item(db, scope, item, client)
        if not settings.github_app_bot_login:
            return "unavailable"  # without the author login an owned comment cannot be told from a forged one
        lock = _locks.setdefault((f"{scope.repo_owner}/{scope.repo_name}", pull_number), asyncio.Lock())
        async with lock:
            try:
                token = await self._token(scope)
                pull = await client.get_pull(scope.repo_owner, scope.repo_name, pull_number, token=token)
                if _state(pull) == "closed":
                    return "skipped"
                head = str((pull.get("head") or {}).get("sha") or "")
                checks = (await client.list_check_runs_for_ref(scope.repo_owner, scope.repo_name, head, token=token)
                          if head else [])
                text = render_comment(await self._facts(db, scope, None, pull, checks, pull_number))
                existing = await self._locate(scope, client, pull_number, None, None, token)
                if not await self._still_current(scope, client, pull_number, head, token):
                    return "skipped"
                return (await self._write(scope, client, pull_number, existing, text, token))[0]
            except Exception as exc:
                logger.warning("summary_publication_unavailable pull=%s error_type=%s", pull_number,
                               type(exc).__name__)
                return "unavailable"

    async def publish_scope(self, db: AsyncSession, scope: TeamGithubScope, client) -> None:
        """The poll's tail: at ready, at merge and on changed facts. Runs after every decision has been committed.

        A published item is re-read at most once per five minutes, sooner when its head or status changed; the
        rendered digest decides whether anything is written, so a withdrawn review, a hold label or a check change
        on the same head refreshes the comment and unchanged facts write nothing.
        """
        if not settings.github_publish_summaries_enabled:
            return
        rows = (await db.execute(select(GithubWorkItem).where(
            GithubWorkItem.scope_id == scope.id, GithubWorkItem.pr_number.is_not(None),
            GithubWorkItem.dispatch_status.in_(_PUBLISH_STATUSES)))).scalars().all()
        now = datetime.utcnow()
        for item in rows:
            if item.dispatch_status == "merged" and (
                    item.summary_published_status == "merged" or item.updated_at < now - _MERGED_WINDOW):
                continue
            if (item.summary_comment_id is not None and item.summary_published_status == item.dispatch_status
                    and item.summary_published_head_sha == item.last_verified_sha
                    and item.summary_published_at is not None and item.summary_published_at > now - _REREAD):
                continue
            await self.publish_item(db, scope, item, client)


github_summary_publication_service = GithubSummaryPublicationService()
