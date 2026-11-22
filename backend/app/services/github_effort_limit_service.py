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

logger = logging.getLogger(__name__)

# A single tick adds at most this much, so a missed monitor never books a long gap as work.
MAX_TICK_SECONDS = 300
EXTENSION_CAP_MINUTES = 30
EXTENSION_CAP_PERCENT = 25
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


class GithubEffortLimitService:
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
    def extension_cap(limit_minutes: int) -> int:
        """The most one Leader extension may add: the smaller of 30 minutes and 25 percent of the limit."""
        return max(0, min(EXTENSION_CAP_MINUTES, limit_minutes * EXTENSION_CAP_PERCENT // 100))

    @staticmethod
    def ceiling_room(limit_minutes: int, extension_minutes: int, ceiling_minutes) -> int | None:
        """The minutes the limit and its extensions leave under ``max_item_effort_minutes``, or ``None`` unread."""
        if isinstance(ceiling_minutes, int) and not isinstance(ceiling_minutes, bool):
            return max(0, ceiling_minutes - limit_minutes - extension_minutes)
        return None

    @classmethod
    def extension_cap_for(cls, limit_minutes: int, ceiling_minutes, extension_minutes: int = 0) -> int:
        """The most the next extension may add (#968, #1101): the per-step cap, clamped to the room under the ceiling.

        The per-step cap comes from the original limit, so repeated steps never grow it. A ceiling that cannot be
        read leaves the per-step cap.
        """
        cap = cls.extension_cap(limit_minutes)
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
            "extension_cap_minutes": cls.extension_cap_for(limit, ceiling, extension),
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
            await self._notify_at_limit(db, item)
        return "counted"

    async def _notify_at_limit(self, db: AsyncSession, item: GithubWorkItem) -> None:
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

    # -- Leader extensions ----------------------------------------------------------------------------------------

    async def _read_activity(self, db: AsyncSession, item: GithubWorkItem, scope, now: datetime):
        """Why an extension must be refused for want of recent owner activity, as ``(code, contact)``.

        ``code`` is ``None`` when the attempt is acknowledged, no observation pause is waiting or paused, and the
        leased workspace shows owner contact within the owner idle window (the window the idle monitor uses: the
        delivery policy's, else the setting). ``contact`` is the contact time read, for the guarded update to repeat.
        """
        if item.ack_received_at is None:
            return "effort_extension_not_acknowledged", None
        if await db.scalar(select(self._pause_exists(item.id))):
            return "effort_extension_paused", None
        contact = await db.scalar(select(GithubWorkspace.lease_last_owner_contact_at).where(
            GithubWorkspace.leased_item_id == item.id))
        from app.services.factory_delivery_policy import effective_policy

        window = timedelta(seconds=effective_policy(item, scope).owner_idle_seconds
                           or settings.github_owner_idle_timeout_seconds)
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
        status, issue_number = item.dispatch_status, item.issue_number
        # Read as it is now: the session may hold an older copy of the scope row (V1 review 5925693104).
        scope = (await db.execute(select(TeamGithubScope).where(TeamGithubScope.id == item.scope_id)
                                  .execution_options(populate_existing=True))).scalar_one_or_none()
        ceiling = getattr(scope, "max_item_effort_minutes", None)
        step_cap = self.extension_cap(limit)
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
                        db, preset_id=preset_id, work_item_id=work_item_id, dispatch_nonce=dispatch_nonce,
                        issue_number=issue_number, limit=limit, used=used, ceiling=ceiling, requested=minutes,
                        room=room)
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
        except Exception:
            await db.rollback()
            raise
        await db.commit()
        await db.refresh(item)
        return self._result(item)

    async def _add_ceiling_notice(self, db: AsyncSession, *, preset_id: int, work_item_id: int, dispatch_nonce: str,
                                  issue_number: int, limit: int, used: int, ceiling: int, requested: int,
                                  room: int) -> None:
        """Add (never commit) the Supervisor's notice for a request beyond the ceiling: one per item and attempt.

        Built like the auto-merge notice (#939): a system row to the team with no member receipts, which only the
        bound Supervisor's inbox shows through its exact payload shape. No team Supervisor means no row.
        """
        from app.models.database import AgentTeamPreset, MailMessage

        supervisor = await db.scalar(select(AgentTeamPreset.supervisor_actor_id).where(AgentTeamPreset.id == preset_id))
        if supervisor is None:
            return
        key = f"github-effort-ceiling:{work_item_id}:{dispatch_nonce}:operator"
        if await db.scalar(select(MailMessage.id).where(MailMessage.delivery_key == key)) is not None:
            return
        db.add(MailMessage(
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
        ))

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
