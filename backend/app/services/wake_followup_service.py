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

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
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
    async def tick(self, db: AsyncSession, now: datetime | None = None) -> list[int]:
        """Re-wake the members that stayed silent. Returns their ids. Never raises."""
        if not settings.wake_followup_enabled:
            return []
        try:
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
            MailWakeAttempt.created_at <= now - delay,
            MailWakeAttempt.created_at >= now - timedelta(seconds=MAX_AGE_SECONDS),
        ).order_by(MailWakeAttempt.id))).all()
        # The newest delivered wake of a member decides: activity is measured from it.
        latest = {attempt.member_id: attempt for attempt in attempts}
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
        members = list((await db.scalars(select(MailTeamMember).where(MailTeamMember.id.in_(silent)))).all())
        try:
            statuses = await agent_status_service.for_members(db, members)
        except (OSError, ValueError):
            logger.warning("Wake follow-up could not read native activity; no member was woken")
            return []
        woken: list[int] = []
        for member_id in silent:
            attempt = pending[member_id]
            if not self._known_idle_before(statuses.get(member_id), attempt.created_at):
                continue
            try:
                await agent_mail_service._wake_member(
                    db, member_id, now, source=REWAKE_SOURCE, reason_code=f"rewake:{attempt.id}")
            except MailWakeError as error:
                # The refusal is recorded as a rewake row, so it is not repeated.
                logger.debug("Wake follow-up for member %s refused: %s", member_id, error.code)
                continue
            woken.append(member_id)
        return woken

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
