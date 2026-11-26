"""#1310 (#1238 part 2): Synth City's automatic-merge attempts and the classification of merged pull requests.

GitHub can prove whether Synth City made a merge, not whether a person did: the factory merges with one shared
account. So a merge is ``automatic`` only with a confirmed attempt, ``outside`` (merged outside Synth City's automatic
merge) with none or only refused ones, and ``unknown`` with an uncertain or unfinished one, or before the recording
started.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import httpx
from sqlalchemy import Table, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.models.database import GithubMergeAttempt, GithubMergeAttemptCoverage
from app.services import factory_audit_service as audit
from app.services.github_rate_limit_service import GithubRateLimited

ATTEMPTED, MERGED, REFUSED, UNCERTAIN = "attempted", "merged", "refused", "uncertain"
# Transport failures raised before the request leaves: GitHub can't have merged.
_UNSENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


async def install_merge_attempt_coverage(conn: AsyncConnection) -> None:
    """Record once, at installation, when attempt recording began."""
    table = cast(Table, GithubMergeAttemptCoverage.__table__)
    insert = postgres_insert if conn.dialect.name == "postgresql" else sqlite_insert
    await conn.execute(insert(table).on_conflict_do_nothing(index_elements=["id"]),
                       {"id": 1, "started_at": _now()})


async def begin(db: AsyncSession, *, scope_id: int, item_id: int, pr_number: int, head_sha: str,
                repository: str) -> int:
    """Add (never commit) an attempt; the caller commits it before the merge call's final checks."""
    key = await audit.current_context_key(db, "scope", scope_id) or await audit.context_key_for(db, "scope", scope_id)
    attempt = GithubMergeAttempt(scope_id=scope_id, scope_context_key=key, work_item_id=item_id,
                                 repository=repository.lower(), pr_number=pr_number, head_sha=head_sha,
                                 status=ATTEMPTED)
    db.add(attempt)
    await db.flush()
    return attempt.id


async def finish(db: AsyncSession, attempt_id: int, status: str, reason: str | None) -> None:
    """Record an attempt's outcome once (never commit); a finished attempt keeps its first outcome."""
    await db.execute(update(GithubMergeAttempt).where(
        GithubMergeAttempt.id == attempt_id, GithubMergeAttempt.status == ATTEMPTED,
    ).values(status=status, reason=reason, finished_at=_now()).execution_options(synchronize_session=False))


def outcome(error: BaseException) -> tuple[str, str]:
    """What a failed merge call proves: ``refused`` when GitHub can't have merged, else ``uncertain``."""
    if isinstance(error, GithubRateLimited):
        return REFUSED, "rate_limited"  # unsent during a cooldown, or refused by quota
    if isinstance(error, httpx.HTTPStatusError):
        code = error.response.status_code
        # A gateway 5xx can follow a merge GitHub completed; a 4xx is GitHub's refusal.
        return (UNCERTAIN if code >= 500 else REFUSED), f"github_{code}"
    if isinstance(error, _UNSENT):
        return REFUSED, type(error).__name__
    return UNCERTAIN, type(error).__name__  # sent, and the answer was lost or unreadable


async def merge_path(db: AsyncSession, *, repository: str | None, pr_number: int | None,
                     merged_at: datetime | None) -> str:
    """``automatic``, ``outside`` or ``unknown`` for one merged pull request, whichever scope observes it.

    Matched by the repository and pull request recorded at attempt time (V1 review 6074761059): a scope that moved to
    another repository can't lend its old attempts, and a second team watching the repository sees the first's.
    """
    if repository is None or pr_number is None:
        return "unknown"
    statuses = set((await db.scalars(select(GithubMergeAttempt.status).where(
        GithubMergeAttempt.repository == repository.lower(),
        GithubMergeAttempt.pr_number == pr_number))).all())
    if MERGED in statuses:
        return "automatic"
    if statuses & {ATTEMPTED, UNCERTAIN}:
        return "unknown"
    started = await db.scalar(select(GithubMergeAttemptCoverage.started_at))
    if started is None or merged_at is None or _naive(merged_at) < _naive(started):
        return "unknown"  # merged before the recording started: no attempt proves nothing
    return "outside"


def _naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo is not None else value


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)  # stored naive, in UTC, like the other ledger times
