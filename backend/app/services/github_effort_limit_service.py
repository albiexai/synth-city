"""Effort limit of a dispatched work item (#938, option C).

An approved plan's estimate becomes the item's effort limit, bounded by the scope's ``max_item_effort_minutes``.
A ledger of elapsed *working* time is advanced by the scope monitor; reaching the limit sends one notice to the
Leader and the owner; the Leader may extend the limit once. The ledger is advisory: it never ends, expires or
escalates work. The idle monitor stays the only fail-safe, and no expiry is added or changed here.
"""
from __future__ import annotations

import logging
import math
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from sqlalchemy import exists, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.database import GithubOwnerObservationPause, GithubWorkItem, GithubWorkspace, TeamGithubScope
from app.models.schemas import (
    DEFAULT_EFFORT_EXTENSION_CAP_MINUTES, DEFAULT_EFFORT_EXTENSION_CAP_PERCENT, DEFAULT_PLAN_ESTIMATE_MINUTES,
)
from app.services import decision_wait_service as decision_waits
from app.services.github_terminal_revision_service import TERMINAL_ITEMS

logger = logging.getLogger(__name__)

# A single tick adds at most this much, so a missed monitor never books a long gap as work.
MAX_TICK_SECONDS = 300
_REASON_LIMIT = 500
# An extension serves the working attempt and its review corrections: a PR under review is "verifying" (#1059).
_EXTENDABLE_STATUSES = ("dispatched", "verifying")
_UNSET = object()


class EffortLimitError(ValueError):
    def __init__(self, code: str, *, status_code: int = 409, message: str | None = None):
        self.code = code
        self.status_code = status_code
        super().__init__(message or code)


@asynccontextmanager
async def _keep_loaded(db: AsyncSession):
    """Commit without expiring the caller's loaded rows: the monitor holds scope and slot objects across a tick."""
    session = db.sync_session
    previous, session.expire_on_commit = session.expire_on_commit, False
    try:
        yield
    finally:
        session.expire_on_commit = previous


def _minutes_text(value) -> str:
    from app.services.request_preflight import _minutes_text as text

    return text(value)


def plan_split_suggestion(limit_minutes: int) -> str:
    return f"Split the issue into parts estimated at {limit_minutes} active minutes or less."


class GithubEffortLimitService:
    @staticmethod
    def plan_estimate_limit(scope) -> int:
        configured = getattr(scope, "max_plan_estimate_minutes", None)
        default_or_override = DEFAULT_PLAN_ESTIMATE_MINUTES if configured is None else configured
        return min(default_or_override, scope.max_item_effort_minutes)

    def initial_plan_limit(self, plan_metadata, scope) -> int | None:
        """Validate a new plan against both the item ceiling and its plan limit."""
        effort_limit = self.limit_for_plan(plan_metadata, scope)
        if effort_limit is None:
            return None
        plan_limit = self.plan_estimate_limit(scope)
        if effort_limit > plan_limit:
            raise EffortLimitError(
                "plan_estimate_exceeds_limit", status_code=400,
                message=(f"The estimate exceeds this scope's {plan_limit}-minute plan limit "
                         f"(max_plan_estimate_minutes). {plan_split_suggestion(plan_limit)}"),
            )
        return effort_limit

    def limit_for_plan(self, plan_metadata, scope) -> int | None:
        """The effort limit a plan's estimate asks for, or ``None`` when it states no estimate.

        An estimate above the scope ceiling is refused; one that cannot be read is refused rather than guessed.
        """
        if not isinstance(plan_metadata, dict) or plan_metadata.get("estimate_active_minutes") is None:
            return None
        from app.services.request_preflight import estimate_upper_bound

        upper = estimate_upper_bound(plan_metadata["estimate_active_minutes"])
        if upper is None:
            raise EffortLimitError("effort_estimate_invalid", status_code=400,
                                   message="The estimate must be a positive number of minutes or a low and high pair.")
        ceiling = scope.max_item_effort_minutes
        if upper > ceiling:
            raise EffortLimitError(
                "effort_estimate_exceeds_ceiling", status_code=400,
                message=(f"The estimate's upper bound is {_minutes_text(upper)} minutes; this scope allows at most "
                         f"{ceiling} minutes of effort per item (max_item_effort_minutes). Narrow or split the "
                         "issue, or ask an operator to raise the scope setting."))
        return math.ceil(upper)

    @staticmethod
    def extension_cap(limit_minutes: int, scope=None) -> int:
        """Bound one Leader extension by minutes and a percentage of the original limit."""
        minutes = getattr(scope, "effort_extension_cap_minutes", DEFAULT_EFFORT_EXTENSION_CAP_MINUTES)
        percent = getattr(scope, "effort_extension_cap_percent", DEFAULT_EFFORT_EXTENSION_CAP_PERCENT)
        return max(0, min(minutes, limit_minutes * percent // 100))

    @staticmethod
    def ceiling_room(limit_minutes: int, extension_minutes: int, ceiling_minutes) -> int | None:
        """The minutes the limit and its extensions leave under ``max_item_effort_minutes``, or ``None`` unread."""
        if isinstance(ceiling_minutes, int) and not isinstance(ceiling_minutes, bool):
            return max(0, ceiling_minutes - limit_minutes - extension_minutes)
        return None

    @classmethod
    def extension_cap_for(
        cls, limit_minutes: int, ceiling_minutes, extension_minutes: int = 0, *, scope=None,
    ) -> int:
        """The most the next extension may add (#968, #1101): the per-step cap, clamped to the room under the ceiling.

        The per-step cap comes from the original limit, so repeated steps never grow it. A ceiling that cannot be
        read leaves the per-step cap.
        """
        cap = cls.extension_cap(limit_minutes, scope)
        room = cls.ceiling_room(limit_minutes, extension_minutes, ceiling_minutes)
        return cap if room is None else min(cap, room)

    @classmethod
    def effort_view(cls, item, scope) -> dict | None:
        """What an item's recorded estimate, limit and extension look like in a view, or ``None`` without a limit.

        Plain numbers only: no ledger time, no nonce, no lease. ``extension_cap_minutes`` is the most the Leader may
        add with the next extension: the per-step cap within the room left under the ceiling (#1101).
        """
        limit = getattr(item, "effort_limit_minutes", None)
        if limit is None:
            return None
        extension = item.effort_extension_minutes or 0
        ceiling = getattr(scope, "max_item_effort_minutes", None)
        return {
            "estimate_minutes": item.effort_estimate_minutes,
            "limit_minutes": limit,
            "extension_minutes": extension,
            "effective_limit_minutes": limit + extension,
            "ceiling_minutes": ceiling,
            "extension_cap_minutes": cls.extension_cap_for(limit, ceiling, extension, scope=scope),
        }

    # -- ledger -----------------------------------------------------------------------------------------------------

    async def tick_scope(self, db: AsyncSession, scope, *, now: datetime | None = None) -> None:
        """Advance the ledger of every limited item of a scope. One failing item never stops the others."""
        ids = (await db.execute(select(GithubWorkItem.id).where(
            GithubWorkItem.scope_id == scope.id, GithubWorkItem.effort_limit_minutes.is_not(None),
        ))).scalars().all()
        for item_id in ids:
            try:
                await self.tick_item(db, item_id, now=now)
            except Exception as exc:  # advisory accounting must never break the monitor
                logger.warning("effort_ledger_tick_failed work_item_id=%s error_type=%s", item_id, type(exc).__name__)

    async def tick_item(self, db: AsyncSession, item_id: int, *, now: datetime | None = None,
                        seen_last_tick=_UNSET) -> str:
        """Advance one item's ledger; the result is skipped, reset, started, counted or lost.

        The update is a compare-and-swap on the last tick the caller read, so two ticks can never both count the
        same span. Time is booked only between two ticks that both found the item working.
        """
        now = now or datetime.utcnow()
        item = (await db.execute(select(GithubWorkItem).where(GithubWorkItem.id == item_id)
                                 .execution_options(populate_existing=True))).scalar_one_or_none()
        if item is not None:
            ended = await self._end_ceiling_waits(db, item, now=now)
            if item.dispatch_status in TERMINAL_ITEMS:
                await self._record_effort_waits(db, item.id, now=now)
            if ended or item.dispatch_status in TERMINAL_ITEMS:
                await db.commit()
        if item is None or item.effort_limit_minutes is None:
            return "skipped"
        last = item.effort_last_tick_at if seen_last_tick is _UNSET else seen_last_tick
        paused = exists(select(GithubOwnerObservationPause.id).where(
            GithubOwnerObservationPause.work_item_id == item.id,
            GithubOwnerObservationPause.status.in_(("waiting", "paused"))))
        working = (item.dispatch_status == "dispatched" and item.ack_received_at is not None
                   and not await db.scalar(select(paused)))
        guards = [GithubWorkItem.id == item.id, GithubWorkItem.dispatch_nonce == item.dispatch_nonce]
        try:
            async with _keep_loaded(db):
                if not working:
                    if last is not None:
                        async with db.begin_nested():
                            await db.execute(update(GithubWorkItem).where(
                                *guards, GithubWorkItem.effort_last_tick_at == last,
                            ).values(effort_last_tick_at=None).execution_options(synchronize_session=False))
                        await db.commit()
                    return "reset"
                if last is None:
                    async with db.begin_nested():
                        started = await db.execute(update(GithubWorkItem).where(
                            *guards, GithubWorkItem.dispatch_status == "dispatched",
                            GithubWorkItem.effort_last_tick_at.is_(None),
                        ).values(effort_last_tick_at=now).execution_options(synchronize_session=False))
                    await db.commit()
                    return "started" if started.rowcount == 1 else "lost"
                if item.ack_received_at is not None and item.ack_received_at > last:
                    # Re-acknowledged since the last tick: the item left work and came back (an escalation and a
                    # continuation, say), so the span between the two working ticks is not one working span. Restart
                    # the clock and book nothing. A round trip that leaves no trace on the row stays an
                    # approximation, bounded by one tick gap (at most MAX_TICK_SECONDS).
                    async with db.begin_nested():
                        restarted = await db.execute(update(GithubWorkItem).where(
                            *guards, GithubWorkItem.dispatch_status == "dispatched",
                            GithubWorkItem.effort_last_tick_at == last,
                        ).values(effort_last_tick_at=now).execution_options(synchronize_session=False))
                    await db.commit()
                    return "started" if restarted.rowcount == 1 else "lost"
                gap = max(0, min(int((now - last).total_seconds()), MAX_TICK_SECONDS))
                async with db.begin_nested():
                    counted = await db.execute(update(GithubWorkItem).where(
                        *guards, GithubWorkItem.dispatch_status == "dispatched",
                        GithubWorkItem.effort_last_tick_at == last,
                    ).values(effort_elapsed_seconds=GithubWorkItem.effort_elapsed_seconds + gap,
                             effort_last_tick_at=now).execution_options(synchronize_session=False))
                await db.commit()
        except OperationalError:
            return "lost"  # another writer won the race: this tick counts nothing
        if counted.rowcount != 1:
            return "lost"
        async with _keep_loaded(db):
            await db.refresh(item)
            await self._notify_at_limit(db, item, now=now)
        return "counted"

    async def _notify_at_limit(self, db: AsyncSession, item: GithubWorkItem, *, now: datetime) -> None:
        limit = item.effort_limit_minutes + item.effort_extension_minutes
        if item.effort_elapsed_seconds < limit * 60:
            return
        from app.services.agent_mail_service import agent_mail_service

        owner, leader = await agent_mail_service._dispatch_participants(db, item)
        # The body is fixed for one delivery key (the Mail layer rejects a changed body), so it carries the limit,
        # never the moving elapsed time. A key per extension state sends the notice once, and once more after an
        # extension; a rerun after a crash sends each notice exactly once.
        body = (f"Issue #{item.issue_number}: the advisory elapsed working time from the system ledger reached the "
                f"effort limit of {limit} minutes (limit {item.effort_limit_minutes}, extension "
                f"{item.effort_extension_minutes}). This is not the owner's reported active effort. Nothing was "
                "stopped or expired. The ledger is an approximation: a round trip between two ticks that leaves no "
                "trace on the row can add at most one tick gap, capped at 300 seconds. The Leader may extend the "
                "limit with synth_city_extend_effort_limit, and extend again up to the scope ceiling, or stop the "
                "work; the owner reports the active effort and says if the work needs more.")
        for role, member in (("leader", leader), ("owner", owner)):
            if member is None:
                continue
            await agent_mail_service.send_direct_message(
                db, recipient_member_id=member.id, subject=f"Effort limit reached: issue #{item.issue_number}",
                body_markdown=body,
                payload={"kind": "github_effort_limit_reached", "work_item_id": item.id,
                         "limit_minutes": item.effort_limit_minutes, "extension_minutes": item.effort_extension_minutes},
                delivery_key=f"github-effort:{item.id}:{item.dispatch_nonce}:{item.effort_extension_minutes}:{role}")
        await self._record_effort_waits(db, item.id, now=now)
        await db.commit()

    async def _record_effort_waits(self, db: AsyncSession, item_id: int, *, now: datetime,
                                   extended_by: int | None = None, extended_from: int | None = None) -> None:
        """#1299: rebuild the attempt's effort waits from durable evidence (V1 review 6067843784).

        The Mail layer commits a notice on its own, so an interruption or a concurrent extension can fall between a
        Leader notice and its wait start. Each Leader notice therefore starts one wait whenever this runs: after the
        notices, inside each extension and when the item ends. A higher extension total ends it: answered by the
        Leader at the extension that raised the notice's own total, otherwise unattributed when found, which can be
        later than the real end. The item's end ends every open one.
        """
        from app.models.database import MailMessage

        row = (await db.execute(select(
            GithubWorkItem.dispatch_nonce, GithubWorkItem.dispatch_status, GithubWorkItem.effort_extension_minutes,
            GithubWorkItem.effort_extension_decided_at, GithubWorkItem.created_at,
        ).where(GithubWorkItem.id == item_id))).one_or_none()
        if row is None:
            return
        nonce, status, total, extended_at, created_at = row
        # Every attempt's notices in this item lifetime (V1 review 6068009562): a reused item id's are older.
        prefix = f"github-effort:{item_id}:"
        sent = (await db.scalars(select(MailMessage).where(
            MailMessage.delivery_key.startswith(prefix, autoescape=True),
            MailMessage.created_at >= created_at).order_by(MailMessage.id))).all()
        owners = {m.delivery_key.rsplit(":", 1)[0]: m.recipient_member_id
                  for m in sent if m.delivery_key.endswith(":owner")}
        notices = [m for m in sent if m.delivery_key.endswith(":leader")]
        source = "github_effort_limit_service._record_effort_waits"
        for notice in notices:  # the owner notified with it, never the current assignment; else unknown
            await decision_waits.record_effort_wait_started(
                db, item_id=item_id, notice_id=notice.id, noticed_at=notice.created_at,
                owner_member_id=owners.get(notice.delivery_key.rsplit(":", 1)[0]),
                leader_member_id=notice.recipient_member_id, source=source)
        for notice in notices:
            noticed_total = (notice.payload or {}).get("extension_minutes", total)
            # Only this attempt's extensions end its notices; an earlier attempt's close on the item's end.
            if notice.delivery_key.startswith(f"{prefix}{nonce}:") and noticed_total < total:
                answered = noticed_total == extended_from
                await decision_waits.record_effort_wait_ended(
                    db, item_id=item_id, notice_ids=[notice.id], outcome="extended",
                    ended_at=(extended_at or now) if answered or extended_from is None else now,
                    answered_by_member_id=extended_by if answered else None, source=source)
        if status in TERMINAL_ITEMS:
            await decision_waits.record_effort_wait_ended(
                db, item_id=item_id, outcome="item_ended", ended_at=now, source=source)

    async def _end_ceiling_waits(self, db: AsyncSession, item: GithubWorkItem, *, now: datetime) -> bool:
        """#1300: close a Supervisor ceiling wait the item's end or a new attempt resolved, unattributed.

        The notice belongs to one attempt (its delivery key names the nonce); a new attempt asks anew. Whether
        anything was written.
        """
        from app.services import factory_audit_service as audit

        status, nonce, wrote = item.dispatch_status, item.dispatch_nonce, False
        for start in await decision_waits.open_ceiling_waits(db, item.id):
            notice_id = (start.after_values or {}).get("wait_message_id")
            snapshot, detail = None, None
            if status in TERMINAL_ITEMS:
                outcome, evidence = "item_terminal", {"dispatch_status": status}
            else:
                noticed = await self._notice_attempt(db, notice_id, item.id)
                # A missing or reused Mail row is no evidence: the wait stays open.
                if noticed is None or noticed == nonce:
                    continue
                # The replacement by its non-secret launch identity: the ledger keeps no nonce or digest of one. A
                # retry clears the nonce but keeps the old launch until the new one is published (V1 review
                # 6070048705), so a launch the notice already named is no evidence of the replacement.
                outcome, evidence = "attempt_ended", {}
                replacement = audit.launch_attempt_key(item.id, item.launch_id)
                if item.launch_id is not None and replacement != (start.context_snapshot or {}).get("launch_attempt"):
                    snapshot = {"launch_attempt": replacement}
                else:
                    detail = "the replacement attempt's launch is not yet known"
            await decision_waits.record_ceiling_wait_ended(
                db, item_id=item.id, notice_ids=[notice_id], outcome=outcome, ended_at=now, evidence=evidence,
                context_snapshot=snapshot, detail=detail, source="github_effort_limit_service.tick_item")
            wrote = True
        return wrote

    @staticmethod
    async def _notice_attempt(db: AsyncSession, notice_id, item_id: int) -> str | None:
        """The attempt nonce a ceiling notice's delivery key names, or ``None`` for a missing or reused Mail row."""
        from app.models.database import MailMessage

        key = await db.scalar(select(MailMessage.delivery_key).where(MailMessage.id == notice_id))
        parts = (key or "").split(":")
        if len(parts) != 4 or parts[:2] != ["github-effort-ceiling", str(item_id)] or parts[3] != "operator":
            return None
        return parts[2]

    async def end_resolved_ceiling_waits(self, db: AsyncSession, *, scope_id: int, ceiling_before, ceiling_after,
                                         actor: dict, correlation_id: str, now: datetime) -> None:
        """#1300: a ceiling change ends a Supervisor ceiling wait only when the change itself makes the asked step fit.

        The room is the one ``extend`` computes, on the item's current limit and extensions: too small before the
        change and enough after it. An item that already ended, or a notice of a replaced attempt, is left for the
        tick, which records that end instead (V1 review 6069720545). Called in the scope change's own transaction.
        """
        from app.models.database import FactoryAuditEvent, MailMessage

        items = (await db.execute(select(
            GithubWorkItem.id, GithubWorkItem.effort_limit_minutes, GithubWorkItem.effort_extension_minutes,
            GithubWorkItem.dispatch_status, GithubWorkItem.dispatch_nonce,
        ).where(GithubWorkItem.scope_id == scope_id, GithubWorkItem.id.in_(select(FactoryAuditEvent.item_id).where(
            FactoryAuditEvent.event_kind == decision_waits.CEILING_STARTED))))).all()
        for item_id, limit, used, status, nonce in items:
            if status in TERMINAL_ITEMS:
                continue
            before = self.ceiling_room(limit or 0, used or 0, ceiling_before)
            after = self.ceiling_room(limit or 0, used or 0, ceiling_after)
            for start in await decision_waits.open_ceiling_waits(db, item_id):
                notice_id = (start.after_values or {}).get("wait_message_id")
                # Only a readable notice of the item's current attempt; a retry reset leaves no current nonce.
                noticed = await self._notice_attempt(db, notice_id, item_id)
                notice = await db.get(MailMessage, notice_id) if noticed is not None and noticed == nonce else None
                if notice is None:
                    continue
                requested = (notice.payload or {}).get("requested_minutes")
                if (not isinstance(requested, int) or before is None or before >= requested
                        or (after is not None and after < requested)):
                    continue
                room = "no ceiling" if after is None else f"{after} minutes of room"
                await decision_waits.record_ceiling_wait_ended(
                    db, item_id=item_id, notice_ids=[notice_id], outcome="ceiling_raised", ended_at=now,
                    actor=actor, source="agent_teams.update_github_scope", correlation_id=correlation_id,
                    before_values={"max_item_effort_minutes": ceiling_before},
                    evidence={"max_item_effort_minutes": ceiling_after},
                    detail=f"the asked {requested} minutes now fit: {room}, {before} before")

    # -- Leader extensions ----------------------------------------------------------------------------------------

    async def _read_activity(self, db: AsyncSession, item: GithubWorkItem, scope, now: datetime):
        """Why an extension must be refused for want of recent owner activity, as ``(code, contact)``.

        ``code`` is ``None`` when the attempt is acknowledged, no observation pause is waiting or paused, and the
        leased workspace shows owner contact within the owner idle window (the window the idle monitor uses: the
        attempt delivery policy's, then the team's, then the installation's). ``contact`` is the contact time read.
        """
        if item.ack_received_at is None:
            return "effort_extension_not_acknowledged", None
        if await db.scalar(select(self._pause_exists(item.id))):
            return "effort_extension_paused", None
        contact = await db.scalar(select(GithubWorkspace.lease_last_owner_contact_at).where(
            GithubWorkspace.leased_item_id == item.id))
        from app.models.database import AgentTeamPreset
        from app.services.factory_delivery_policy import effective_policy, owner_contact_windows

        preset = await db.get(AgentTeamPreset, scope.preset_id, populate_existing=True)
        idle, _ = owner_contact_windows(preset, effective_policy(item, scope))
        window = timedelta(seconds=idle)
        if contact is None or now - contact > window:
            return "effort_extension_no_recent_activity", contact
        return None, contact

    @staticmethod
    def _pause_exists(item_id: int):
        return exists(select(GithubOwnerObservationPause.id).where(
            GithubOwnerObservationPause.work_item_id == item_id,
            GithubOwnerObservationPause.status.in_(("waiting", "paused"))))

    async def extend(self, db: AsyncSession, *, work_item_id: int, dispatch_nonce: str, leader_member_id: int,
                     minutes, reason, expected_extension_minutes=None, evidence=None,
                     now: datetime | None = None) -> dict:
        """Grant one Leader extension step (#1101). A single guarded update decides; a loser changes nothing.

        The Leader may extend repeatedly: each step is within the per-step cap of the original limit, and the limit
        plus every extension stays within the scope ceiling.

        The caller states the extension total it saw (#1100). A call whose total is no longer the recorded one is
        refused, so a replayed or concurrent call never grants twice.
        """
        now = now or datetime.utcnow()
        # Take the writer first so two concurrent calls serialize instead of upgrading a read lock.
        await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == work_item_id)
                         .values(updated_at=GithubWorkItem.updated_at).execution_options(synchronize_session=False))
        item = (await db.execute(select(GithubWorkItem).where(GithubWorkItem.id == work_item_id)
                                 .execution_options(populate_existing=True))).scalar_one_or_none()
        if item is None:
            await db.rollback()
            raise EffortLimitError("work_item_not_found", status_code=404)
        if item.dispatch_nonce != dispatch_nonce:
            await db.rollback()
            raise EffortLimitError("effort_extension_stale")
        from app.services.agent_mail_service import agent_mail_service

        _owner, leader = await agent_mail_service._dispatch_participants(db, item)
        if leader is None or leader.id != leader_member_id:
            await db.rollback()
            raise EffortLimitError("not_item_leader", status_code=403)
        reason = (reason or "").strip()[:_REASON_LIMIT] if isinstance(reason, str) else ""
        if not reason:
            await db.rollback()
            raise EffortLimitError("extension_reason_required", status_code=400)
        evidence = (evidence or "").strip()[:_REASON_LIMIT] if isinstance(evidence, str) else ""
        if not evidence:
            await db.rollback()
            raise EffortLimitError("extension_evidence_required", status_code=400)
        expected = expected_extension_minutes
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            await db.rollback()
            raise EffortLimitError("expected_extension_minutes_required", status_code=400)
        if item.effort_limit_minutes is None:
            await db.rollback()
            raise EffortLimitError("effort_limit_not_set")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 1:
            await db.rollback()
            raise EffortLimitError("extension_exceeds_cap", status_code=400)
        # Read everything needed before any rollback: a rollback expires the loaded row.
        limit, used, scope_id = item.effort_limit_minutes, item.effort_extension_minutes, item.scope_id
        status = item.dispatch_status
        # Read as it is now: the session may hold an older copy of the scope row (V1 review 5925693104).
        scope = (await db.execute(select(TeamGithubScope).where(TeamGithubScope.id == item.scope_id)
                                  .execution_options(populate_existing=True))).scalar_one_or_none()
        ceiling = getattr(scope, "max_item_effort_minutes", None)
        step_cap = self.extension_cap(limit, scope)
        if used != expected:
            await db.rollback()
            raise self._total_changed(used)
        if minutes > step_cap:
            await db.rollback()
            raise EffortLimitError("extension_exceeds_cap", status_code=400, message=(
                f"One extension is at most {step_cap} minutes for a limit of {limit} minutes."))
        room = self.ceiling_room(limit, used, ceiling)
        if room is not None and minutes > room:
            preset_id = getattr(scope, "preset_id", None)
            await db.rollback()
            if status in _EXTENDABLE_STATUSES and preset_id is not None:
                # Beyond the ceiling the Supervisor decides (#1102): tell them once for this attempt.
                try:
                    await self._add_ceiling_notice(
                        db, preset_id=preset_id, scope_id=scope_id, work_item_id=work_item_id,
                        dispatch_nonce=dispatch_nonce, requested=minutes, leader_member_id=leader_member_id)
                    await db.commit()
                except Exception:  # the unique delivery key also refuses a concurrent second notice
                    await db.rollback()
                    logger.exception("Could not record the effort ceiling notice for work item %s", work_item_id)
            raise EffortLimitError("extension_exceeds_ceiling", status_code=400, message=(
                f"{room} minutes of room are left: the limit of {limit} minutes plus the extensions of {used} minutes "
                f"may not pass the scope ceiling of {ceiling} minutes (max_item_effort_minutes)."))
        refusal, contact = await self._read_activity(db, item, scope, now)
        if refusal is not None:
            await db.rollback()
            raise EffortLimitError(refusal)
        # The writer is held, so nothing can change between the read above and this update; the update still
        # repeats every check (acknowledged, no pause, the same owner contact) so the decision never rests on a
        # read alone.
        result = await db.execute(update(GithubWorkItem).where(
            GithubWorkItem.id == work_item_id, GithubWorkItem.dispatch_nonce == dispatch_nonce,
            GithubWorkItem.dispatch_status.in_(_EXTENDABLE_STATUSES), GithubWorkItem.effort_limit_minutes.is_not(None),
            GithubWorkItem.effort_extension_minutes == expected, GithubWorkItem.ack_received_at.is_not(None),
            ~self._pause_exists(work_item_id),
            exists(select(GithubWorkspace.id).where(
                GithubWorkspace.leased_item_id == work_item_id,
                GithubWorkspace.lease_last_owner_contact_at == contact)),
        ).values(effort_extension_minutes=expected + minutes, effort_extension_decided_at=now,
                 effort_extension_reason=reason)
            .execution_options(synchronize_session=False))
        if result.rowcount != 1:
            # The writer is held, so another connection cannot have granted in between: a concurrent call waits and
            # is refused above by the expected total. A refused update means this transaction's own row changed.
            await db.rollback()
            raise EffortLimitError("effort_extension_stale")
        from app.services import factory_audit_service as audit

        try:
            # One record per granted extension, in the extension's own transaction (#1100).
            await audit.record_event(
                db, event_kind="effort_extension", source="github_effort_limit_service.extend", occurred_at=now,
                actor=audit.derive_actor(actor_kind="member", member_id=leader_member_id),
                action_outcome="granted", scope_id=scope_id, item_id=work_item_id,
                sanitized_reason=(f"Extended by {minutes} minutes to an effective limit of {limit + expected + minutes}"
                                  f" minutes. Reason: {reason[:150]} Evidence: {evidence}"))
            await self._record_effort_waits(db, work_item_id, now=now, extended_by=leader_member_id,
                                            extended_from=expected)
        except Exception:
            await db.rollback()
            raise
        await db.commit()
        await db.refresh(item)
        return self._result(item)

    async def _add_ceiling_notice(self, db: AsyncSession, *, preset_id: int, scope_id: int, work_item_id: int,
                                  dispatch_nonce: str, requested: int, leader_member_id: int) -> None:
        """Add (never commit) the Supervisor's notice for a request beyond the ceiling: one per item and attempt.

        #1300: its wait on the Supervisor starts with it, so the two commit together. The refusal released the
        writer, so a scope change may have landed since (V1 review 6069720545): take the writer again, read the item
        and the ceiling anew, and write nothing when the asked step fits or the attempt changed.

        Built like the auto-merge notice (#939): a system row to the team with no member receipts, which only the
        bound Supervisor's inbox shows through its exact payload shape. No team Supervisor means no row.
        """
        from app.models.database import AgentTeamPreset, MailMessage

        await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == work_item_id)
                         .values(updated_at=GithubWorkItem.updated_at).execution_options(synchronize_session=False))
        row = (await db.execute(select(
            GithubWorkItem.dispatch_nonce, GithubWorkItem.dispatch_status, GithubWorkItem.effort_limit_minutes,
            GithubWorkItem.effort_extension_minutes, GithubWorkItem.issue_number, GithubWorkItem.launch_id,
        ).where(GithubWorkItem.id == work_item_id))).one_or_none()
        ceiling = await db.scalar(select(TeamGithubScope.max_item_effort_minutes).where(TeamGithubScope.id == scope_id))
        if row is None or row[0] != dispatch_nonce or row[1] not in _EXTENDABLE_STATUSES or row[2] is None:
            return
        _nonce, _status, limit, used, issue_number, launch_id = row
        room = self.ceiling_room(limit, used, ceiling)
        if room is None or requested <= room:
            return
        supervisor = await db.scalar(select(AgentTeamPreset.supervisor_actor_id).where(AgentTeamPreset.id == preset_id))
        if supervisor is None:
            return
        key = f"github-effort-ceiling:{work_item_id}:{dispatch_nonce}:operator"
        if await db.scalar(select(MailMessage.id).where(MailMessage.delivery_key == key)) is not None:
            return
        notice = MailMessage(
            kind="message", sender_type="system", audience_type="team_preset", audience_id=str(preset_id),
            subject="Effort ceiling reached",
            body_markdown=(
                f"Issue #{issue_number}: the Leader asked to extend the effort limit by {requested} minutes, beyond "
                f"the scope ceiling of {ceiling} minutes (limit {limit}, extensions so far {used}, {room} minutes of "
                "room left). The Leader extends within the ceiling; beyond it, the Supervisor decides: raise the "
                "scope's max_item_effort_minutes, split the issue, or stop the work. This notice grants no extension."),
            payload={"notice": "effort_ceiling_reached", "work_item_id": work_item_id, "issue_number": issue_number,
                     "limit_minutes": limit, "extension_minutes": used, "ceiling_minutes": ceiling,
                     "requested_minutes": requested, "supervisor_actor_id": supervisor},
            delivery_key=key,
        )
        db.add(notice)
        await decision_waits.record_ceiling_wait_started(
            db, notice, item_id=work_item_id, leader_member_id=leader_member_id, launch_id=launch_id,
            source="github_effort_limit_service.extend")

    @staticmethod
    def _total_changed(recorded: int) -> EffortLimitError:
        return EffortLimitError("effort_extension_total_changed", message=(
            f"The recorded extension total is {recorded} minutes, not the expected total. Read the item's effort "
            f"again and decide from the current total."))

    @staticmethod
    def _result(item: GithubWorkItem) -> dict:
        return {
            "work_item_id": item.id, "limit_minutes": item.effort_limit_minutes,
            "extension_minutes": item.effort_extension_minutes,
            "effective_limit_minutes": item.effort_limit_minutes + item.effort_extension_minutes,
            # A repeated call is now refused by the expected total, so a result is always a fresh grant.
            "decided_at": item.effort_extension_decided_at, "repeated": False,
        }


effort_limit_service = GithubEffortLimitService()
