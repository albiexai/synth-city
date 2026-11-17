"""Re-wake a member once when a delivered wake gets no activity (#1059).

A wake is "delivered" when the text reached the pane. That does not mean the member acted on it.
After a delay this service looks for activity since the wake. It wakes the member again, once, only
on positive evidence of no activity:

* Mail shows none: no receipt read or acked and no message sent since the wake; and
* the native state is known and shows none: the member is idle, and its last native event is from
  before the wake.

Anything else leaves the member alone. A member that is working, or finished a turn after the wake,
acted. Unknown native evidence (no observer for the provider, a stopped process, an idle state with
no timestamp) stays unknown: a member that acted must not be woken a second time on a guess. An
attempt with unknown evidence stays a candidate until it is too old.

MailAgentSession.last_seen_at is not evidence. Session sync and heartbeats refresh it for a member
that is doing nothing.
"""
from __future__ import annotations

import asyncio
import logging
import weakref
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import exists, func, insert, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.database import MailMessage, MailReceipt, MailTeamMember, MailWakeAttempt
from app.services.agent_mail_service import MailWakeError, agent_mail_service
from app.services.agent_status_service import agent_status_service

logger = logging.getLogger(__name__)

# The wakes that tell a member to act: after a message, a decision, an approval or a direct request.
WAKE_SOURCES = ("auto_nudge", "external_delivery", "manual_operator", "manual_session")
REWAKE_SOURCE = "rewake"
# An older wake is history, not a member that is still waiting.
MAX_AGE_SECONDS = 3600


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


class WakeFollowupService:
    def __init__(self) -> None:
        self._locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

    def _sweep_lock(self) -> asyncio.Lock:
        """One lock per event loop. Every repository job sweeps all members, so sweeps must not overlap."""
        loop = asyncio.get_running_loop()
        lock = self._locks.get(loop)
        if lock is None:
            lock = self._locks[loop] = asyncio.Lock()
        return lock

    async def tick(self, db: AsyncSession, now: datetime | None = None) -> list[int]:
        """Re-wake the members that stayed silent. Returns their ids. Never raises."""
        if not settings.wake_followup_enabled:
            return []
        try:
            async with self._sweep_lock():
                return await self._tick(db, _naive_utc(now) if now else datetime.utcnow())
        except Exception:
            logger.exception("Wake follow-up failed")
            await db.rollback()
            return []

    async def _tick(self, db: AsyncSession, now: datetime) -> list[int]:
        delay = timedelta(seconds=settings.wake_followup_delay_seconds)
        attempts = (await db.scalars(select(MailWakeAttempt).where(
            MailWakeAttempt.result == "delivered",
            MailWakeAttempt.source.in_(WAKE_SOURCES),
            MailWakeAttempt.member_id.is_not(None),
            MailWakeAttempt.unread_count + MailWakeAttempt.pending_count > 0,
            MailWakeAttempt.created_at >= now - timedelta(seconds=MAX_AGE_SECONDS),
        ).order_by(MailWakeAttempt.created_at, MailWakeAttempt.id))).all()
        # The newest delivered wake of a member decides, then the delay applies to it. An older
        # unanswered wake must not trigger a follow-up while a newer wake is still inside its delay.
        newest = {attempt.member_id: attempt for attempt in attempts}
        latest = {member_id: attempt for member_id, attempt in newest.items()
                  if attempt.created_at <= now - delay}
        if not latest:
            return []
        done = set((await db.scalars(select(MailWakeAttempt.reason_code).where(
            MailWakeAttempt.source == REWAKE_SOURCE,
            MailWakeAttempt.member_id.in_(latest)))).all())
        pending = {member_id: attempt for member_id, attempt in latest.items()
                   if f"rewake:{attempt.id}" not in done}
        silent = [member_id for member_id, attempt in pending.items()
                  if not await self._mail_activity_since(db, member_id, attempt.created_at)]
        if not silent:
            return []
        members = {member.id: member for member in (await db.scalars(
            select(MailTeamMember).where(MailTeamMember.id.in_(silent)))).all()}
        try:
            statuses = await agent_status_service.for_members(db, list(members.values()))
        except (OSError, ValueError):
            logger.warning("Wake follow-up could not read native activity; no member was woken")
            return []
        woken: list[int] = []
        for member_id in silent:
            attempt = pending[member_id]
            if not self._known_idle_before(statuses.get(member_id), attempt.created_at):
                continue
            # Claim the attempt before the transport, in one statement that SQLite runs under its
            # write lock. Another sweep, repository job, process or restart sees the claim, and only
            # one claimant gets a row. The sweep lock alone covers one process, not several.
            if not await self._claim(db, member_id, attempt):
                continue
            try:
                await agent_mail_service._wake_member(
                    db, member_id, now, source=REWAKE_SOURCE, reason_code=f"rewake:{attempt.id}",
                    delivery_guard=self._still_silent(db, members[member_id], attempt.created_at))
            except MailWakeError as error:
                # The wake records its own refusal. The claim stays, so the refusal is not repeated.
                if error.code == "wake_session_mismatch":
                    await db.execute(update(MailWakeAttempt).where(
                        MailWakeAttempt.source == REWAKE_SOURCE,
                        MailWakeAttempt.reason_code == f"rewake:{attempt.id}",
                        MailWakeAttempt.result == "claimed").values(result="cancelled"))
                    await db.commit()
                logger.debug("Wake follow-up for member %s refused: %s", member_id, error.code)
                continue
            woken.append(member_id)
        return woken

    @staticmethod
    async def _claim(db: AsyncSession, member_id: int, attempt: MailWakeAttempt) -> bool:
        """Insert the claim row unless a rewake row for this attempt exists. True for the one claimant."""
        reason = f"rewake:{attempt.id}"
        taken = exists().where(MailWakeAttempt.source == REWAKE_SOURCE, MailWakeAttempt.reason_code == reason)
        claim = select(
            literal(member_id), literal("server"), literal(REWAKE_SOURCE), literal(reason),
            literal(uuid4().hex), literal(attempt.unread_count), literal(attempt.pending_count),
            literal("claimed"), literal(datetime.utcnow()),
        ).where(~taken)
        result = await db.execute(insert(MailWakeAttempt).from_select(
            ["member_id", "actor_type", "source", "reason_code", "correlation_id", "unread_count",
             "pending_count", "result", "created_at"], claim))
        await db.commit()
        return result.rowcount == 1

    def _still_silent(self, db: AsyncSession, member: MailTeamMember, since: datetime):
        """The check made just before the transport: the member still shows no activity."""
        async def guard() -> bool:
            if await self._mail_activity_since(db, member.id, since):
                return False
            try:
                fresh = await agent_status_service.for_members(db, [member])
            except (OSError, ValueError):
                return False
            return self._known_idle_before(fresh.get(member.id), since)
        return guard

    @staticmethod
    async def _mail_activity_since(db: AsyncSession, member_id: int, since: datetime) -> bool:
        """The member read or acked mail, or sent a message, at or after the wake."""
        read = await db.scalar(select(func.count(MailReceipt.id)).where(
            MailReceipt.member_id == member_id,
            or_(MailReceipt.read_at >= since, MailReceipt.acked_at >= since)))
        if read:
            return True
        sent = await db.scalar(select(func.count(MailMessage.id)).where(
            MailMessage.sender_member_id == member_id, MailMessage.created_at >= since))
        return bool(sent)

    @staticmethod
    def _known_idle_before(status, since: datetime) -> bool:
        """True only for a known idle state whose last native event is older than the wake."""
        if status is None or status.activity_state != "idle" or status.idle_since is None:
            return False
        return _naive_utc(status.idle_since) < since


wake_followup_service = WakeFollowupService()
