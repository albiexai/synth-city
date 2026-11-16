"""Stuck states and silent background failures, found by the factory itself (#941).

Four detectors open a finding when a condition lasts long enough, keep it while it lasts, and resolve it when it
clears. Each opened finding is shown through the API and in ``/health`` (as ``health: degraded`` with reason codes),
and sent once to the operator: the Supervisor bound to the team. The finding and its notice are written in one
transaction, so a failure leaves neither and the next poll tries again.

Detection never breaks the work it watches: every public method catches its own failure, logs it and returns.
Findings hold identifiers and counts only; no prompt, token or raw error text.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.database import AgentTeamPreset, GithubWorkItem, MailMessage, OperationsFinding, TeamGithubScope

logger = logging.getLogger(__name__)

VERIFYING_AFTER_MERGE = "verifying_after_merge"
ITEM_SKIPPED = "item_skipped"
ADMISSION_MISSING = "admission_missing"
POLL_ROLLED_BACK = "poll_rolled_back"
KINDS = (ADMISSION_MISSING, ITEM_SKIPPED, POLL_ROLLED_BACK, VERIFYING_AFTER_MERGE)

# Fixed thresholds. They are code constants, not settings: a finding that depends on an operator remembering to
# tune it is another silent failure.
VERIFYING_AFTER_MERGE_MINUTES = 15  # an item still 'verifying' this long after GitHub reports its PR merged
SKIPPED_POLLS = 5                   # verification skipped the same item this many polls in a row
MISSING_ADMISSION_POLLS = 3         # a labeled ready issue had no work item for this many polls in a row
SWEEP_LIMIT = 8                     # PR reads per poll for the merged check
NOTICE_SUBJECT = "Operations finding"


class OperationsHealthService:
    def __init__(self) -> None:
        # Consecutive-poll counters live in this process: a restart starts counting again, which can only delay a
        # finding, never invent one.
        self._skips: dict[int, int] = {}
        self._missing: dict[tuple[int, int], int] = {}
        # Every write uses a session of its own, never the caller's: detection cannot discard the poll's pending
        # writes, and it cannot hold SQLite's single write lock against them. Tests replace this factory.
        self._session = AsyncSessionLocal

    # ---- the finding lifecycle -------------------------------------------------------------------------

    async def _open(
        self, db: AsyncSession, *, kind: str, subject: str, detail: str,
        scope_id: int | None = None, work_item_id: int | None = None,
    ) -> bool:
        """Open the finding, or refresh it while it stays open. Returns True when a new one was opened."""
        now = datetime.utcnow()
        existing = await db.scalar(select(OperationsFinding).where(
            OperationsFinding.kind == kind, OperationsFinding.subject == subject,
            OperationsFinding.status == "open"))
        if existing is not None:
            existing.last_seen_at = now
            existing.occurrences = existing.occurrences + 1
            existing.detail = detail
            await db.commit()
            return False
        try:
            finding = OperationsFinding(
                kind=kind, subject=subject, detail=detail, scope_id=scope_id, work_item_id=work_item_id,
                first_seen_at=now, last_seen_at=now)
            db.add(finding)
            await db.flush()
            await self._add_operator_notice(db, finding, scope_id, now)
            await db.commit()  # the finding and its notice persist together or not at all
        except BaseException:
            await db.rollback()
            raise
        return True

    async def _add_operator_notice(
        self, db: AsyncSession, finding: OperationsFinding, scope_id: int | None, now: datetime,
    ) -> None:
        """Add (never commit) the Supervisor's notice: a system row, no member receipts, one per finding."""
        preset_id = await self._preset_id(db, finding, scope_id)
        if preset_id is None:
            return
        preset = await db.get(AgentTeamPreset, preset_id)
        if preset is None or preset.supervisor_actor_id is None:
            return
        db.add(MailMessage(
            kind="message",
            sender_type="system",
            audience_type="team_preset",
            audience_id=str(preset.id),
            subject=NOTICE_SUBJECT,
            body_markdown=(
                f"{finding.detail} Reason code: {finding.kind}. The finding stays open and shows in /health "
                "until the condition clears. This notice grants no authority."
            ),
            payload={
                "notice": "operations_finding", "finding_id": finding.id, "kind": finding.kind,
                "subject": finding.subject, "detail": finding.detail,
                "supervisor_actor_id": preset.supervisor_actor_id,
            },
            delivery_key=f"operations-finding:{finding.id}",
        ))
        finding.notified_at = now

    @staticmethod
    async def _preset_id(db: AsyncSession, finding: OperationsFinding, scope_id: int | None) -> int | None:
        if scope_id is not None:
            return await db.scalar(select(TeamGithubScope.preset_id).where(TeamGithubScope.id == scope_id))
        if finding.kind == POLL_ROLLED_BACK and finding.subject.startswith("repo:"):
            owner, _, repo = finding.subject.removeprefix("repo:").partition("/")
            return await db.scalar(
                select(TeamGithubScope.preset_id).where(
                    TeamGithubScope.repo_owner == owner, TeamGithubScope.repo_name == repo,
                ).order_by(TeamGithubScope.id).limit(1))
        return None

    async def _resolve(self, db: AsyncSession, findings: list[OperationsFinding]) -> None:
        if not findings:
            return
        now = datetime.utcnow()
        await db.execute(update(OperationsFinding).where(
            OperationsFinding.id.in_([finding.id for finding in findings]),
            OperationsFinding.status == "open",
        ).values(status="resolved", resolved_at=now).execution_options(synchronize_session=False))
        await db.commit()

    async def _open_findings(
        self, db: AsyncSession, kind: str, *, scope_id: int | None = None, subject: str | None = None,
    ) -> list[OperationsFinding]:
        query = select(OperationsFinding).where(OperationsFinding.kind == kind, OperationsFinding.status == "open")
        if scope_id is not None:
            query = query.where(OperationsFinding.scope_id == scope_id)
        if subject is not None:
            query = query.where(OperationsFinding.subject == subject)
        return list((await db.scalars(query.execution_options(populate_existing=True))).all())

    # ---- detector 1: still verifying after the PR merged -------------------------------------------------

    async def sweep_verifying_after_merge(self, scope_id: int, owner: str, repo: str, client) -> None:
        try:
            async with self._session() as db:
                cutoff = datetime.utcnow() - timedelta(minutes=VERIFYING_AFTER_MERGE_MINUTES)
                stale = (await db.execute(
                    select(GithubWorkItem.id, GithubWorkItem.pr_number).where(
                        GithubWorkItem.scope_id == scope_id, GithubWorkItem.dispatch_status == "verifying",
                        GithubWorkItem.pr_number.is_not(None), GithubWorkItem.updated_at < cutoff,
                    ).order_by(GithubWorkItem.id).limit(SWEEP_LIMIT))).all()
                for item_id, pr_number in stale:
                    try:
                        pull = await client.get_pull(owner, repo, pr_number)
                    except Exception:
                        continue  # an unreadable PR is not a finding
                    if not isinstance(pull, dict) or pull.get("merged") is not True:
                        continue
                    await self._open(
                        db, kind=VERIFYING_AFTER_MERGE, subject=f"work_item:{item_id}", scope_id=scope_id,
                        work_item_id=item_id,
                        detail=(f"Work item {item_id} (PR #{pr_number}) is still verifying more than "
                                f"{VERIFYING_AFTER_MERGE_MINUTES} minutes after GitHub reports the PR merged."))
                # A finding closes when its item is no longer verifying.
                for finding in await self._open_findings(db, VERIFYING_AFTER_MERGE, scope_id=scope_id):
                    status = await db.scalar(select(GithubWorkItem.dispatch_status).where(
                        GithubWorkItem.id == finding.work_item_id))
                    if status != "verifying":
                        await self._resolve(db, [finding])
        except Exception:
            self._failed("verifying_after_merge sweep")

    # ---- detector 2: skipped by verification, poll after poll ------------------------------------------

    async def record_verification_skips(self, scope_id: int, skipped: set[int]) -> None:
        """Called after the poll's commit with the items verification skipped in this poll."""
        try:
            async with self._session() as db:
                for item_id in sorted(skipped):
                    self._skips[item_id] = self._skips.get(item_id, 0) + 1
                    if self._skips[item_id] >= SKIPPED_POLLS:
                        await self._open(
                            db, kind=ITEM_SKIPPED, subject=f"work_item:{item_id}", scope_id=scope_id,
                            work_item_id=item_id,
                            detail=(f"Verification skipped work item {item_id} on "
                                    f"{self._skips[item_id]} polls in a row."))
                for item_id in [item_id for item_id in self._skips if item_id not in skipped]:
                    del self._skips[item_id]
                for finding in await self._open_findings(db, ITEM_SKIPPED, scope_id=scope_id):
                    if finding.work_item_id not in skipped:
                        await self._resolve(db, [finding])
        except Exception:
            self._failed("verification skip record")

    # ---- detector 3: a ready issue with no work item ---------------------------------------------------

    async def record_admissions(self, scope_id: int, labeled_numbers: set[int]) -> None:
        """Called right after the poll committed its admissions, with the issue numbers GitHub listed."""
        try:
            async with self._session() as db:
                present = set((await db.scalars(select(GithubWorkItem.issue_number).where(
                    GithubWorkItem.scope_id == scope_id, GithubWorkItem.issue_number.in_(labeled_numbers)))).all()
                ) if labeled_numbers else set()
                missing = labeled_numbers - present
                for number in sorted(missing):
                    key = (scope_id, number)
                    self._missing[key] = self._missing.get(key, 0) + 1
                    if self._missing[key] >= MISSING_ADMISSION_POLLS:
                        await self._open(
                            db, kind=ADMISSION_MISSING, subject=f"issue:{number}", scope_id=scope_id,
                            detail=(f"Issue #{number} carries the dispatch label but has had no work item for "
                                    f"{self._missing[key]} polls in a row."))
                for key in [key for key in self._missing if key[0] == scope_id and key[1] not in missing]:
                    del self._missing[key]
                for finding in await self._open_findings(db, ADMISSION_MISSING, scope_id=scope_id):
                    if int(finding.subject.removeprefix("issue:")) not in missing:
                        await self._resolve(db, [finding])
        except Exception:
            self._failed("admission record")

    # ---- detector 4: a poll whose writes were rolled back -----------------------------------------------

    async def record_poll_failure(self, owner: str, repo: str, error: BaseException) -> None:
        """A repo run raised, so its session was discarded with everything it had not committed."""
        try:
            async with self._session() as db:
                await self._open(
                    db, kind=POLL_ROLLED_BACK, subject=f"repo:{owner}/{repo}",
                    detail=(f"A poll of {owner}/{repo} failed ({type(error).__name__}) and its uncommitted "
                            "writes were discarded."))
        except Exception:
            self._failed(f"poll failure record for {owner}/{repo}")

    async def record_poll_success(self, owner: str, repo: str) -> None:
        try:
            async with self._session() as db:
                await self._resolve(db, await self._open_findings(
                    db, POLL_ROLLED_BACK, subject=f"repo:{owner}/{repo}"))
        except Exception:
            self._failed("poll success record")

    # ---- reading ---------------------------------------------------------------------------------------

    async def health(self) -> dict:
        """health, reasons and open_findings for /health. Unreadable findings are themselves degraded."""
        try:
            async with self._session() as db:
                rows = (await db.execute(
                    select(OperationsFinding.kind, func.count(OperationsFinding.id))
                    .where(OperationsFinding.status == "open").group_by(OperationsFinding.kind))).all()
        except Exception:
            logger.warning("operations findings could not be read for /health", exc_info=True)
            return {"health": "degraded", "reasons": ["findings_unreadable"], "open_findings": None}
        reasons = sorted(kind for kind, _count in rows)
        return {
            "health": "degraded" if reasons else "ok",
            "reasons": reasons,
            "open_findings": sum(count for _kind, count in rows),
        }

    async def findings(self, db: AsyncSession, *, limit: int = 50) -> list[OperationsFinding]:
        """Open findings first, then the most recently resolved."""
        open_rows = list((await db.scalars(select(OperationsFinding).where(OperationsFinding.status == "open")
            .order_by(OperationsFinding.first_seen_at.desc(), OperationsFinding.id.desc()).limit(limit))).all())
        room = max(0, limit - len(open_rows))
        resolved = list((await db.scalars(select(OperationsFinding).where(OperationsFinding.status == "resolved")
            .order_by(OperationsFinding.resolved_at.desc(), OperationsFinding.id.desc()).limit(room))).all()) if room else []
        return open_rows + resolved

    @staticmethod
    def _failed(what: str) -> None:
        logger.warning("operations detection failed (%s); the watched work is unaffected", what, exc_info=True)


operations_health_service = OperationsHealthService()
