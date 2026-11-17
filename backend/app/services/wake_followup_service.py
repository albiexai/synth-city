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

from sqlalchemy import exists, insert, literal, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.database import (
    AgentTeamPreset, GithubWorkItem, MailMessage, MailReceipt, MailTeamMember, MailWakeAttempt,
)
from app.models.schemas import MailMessageCreate
from app.services.agent_mail_service import MailWakeError, agent_mail_service
from app.services.agent_status_service import agent_status_service
from app.services.session_recovery import recover_session

logger = logging.getLogger(__name__)

# The wakes that tell a member to act: after a message, a decision, an approval or a direct request.
WAKE_SOURCES = ("auto_nudge", "external_delivery", "manual_operator", "manual_session")
REWAKE_SOURCE = "rewake"
NOTICE_KEY = "rewake-notice:{attempt_id}:{who}"
SUPERVISOR_NOTICE_SUBJECT = "Re-wake without activity"
# The work items a member can be working on. Same set the dispatch monitor treats as busy.
ACTIVE_ITEM_STATUSES = ("dispatched", "verifying")
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
        except Exception as caught_error:
            await recover_session(db, caught_error)
            logger.exception("Wake follow-up failed")
            await db.rollback()
            return []

    async def _tick(self, db: AsyncSession, now: datetime) -> list[int]:
        woken = await self._rewake(db, now)
        await self._notify_silent_rewakes(db, now)
        return woken

    async def _rewake(self, db: AsyncSession, now: datetime) -> list[int]:
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

    async def _notify_silent_rewakes(self, db: AsyncSession, now: datetime) -> None:
        """Tell the Leader and the Supervisor, once each, about a re-wake that also got no activity.

        The evidence rule is the one for the re-wake: no Mail activity since the re-wake and a known
        native idle state from before it. Unknown stays unknown and sends nothing. A notice wakes
        nobody, so it cannot start another follow-up and chain.
        """
        delay = timedelta(seconds=settings.wake_followup_delay_seconds)
        rows = (await db.scalars(select(MailWakeAttempt).where(
            MailWakeAttempt.source == REWAKE_SOURCE,
            MailWakeAttempt.result == "delivered",
            MailWakeAttempt.member_id.is_not(None),
            MailWakeAttempt.created_at >= now - timedelta(seconds=MAX_AGE_SECONDS),
        ).order_by(MailWakeAttempt.created_at, MailWakeAttempt.id))).all()
        newest = {row.member_id: row for row in rows}
        due = {}
        for member_id, row in newest.items():
            _, _, original = (row.reason_code or "").partition(":")
            if original.isdigit() and row.created_at <= now - delay:
                due[member_id] = (row, int(original))
        if not due:
            return
        keys = {NOTICE_KEY.format(attempt_id=original, who=who)
                for _, original in due.values() for who in ("leader", "supervisor")}
        sent = set((await db.scalars(select(MailMessage.delivery_key).where(
            MailMessage.delivery_key.in_(keys)))).all())
        todo = {member_id: (row, original) for member_id, (row, original) in due.items()
                if not {NOTICE_KEY.format(attempt_id=original, who="leader"),
                        NOTICE_KEY.format(attempt_id=original, who="supervisor")} <= sent}
        silent = [member_id for member_id, (row, _) in todo.items()
                  if not await self._mail_activity_since(db, member_id, row.created_at)]
        if not silent:
            return
        members = {member.id: member for member in (await db.scalars(
            select(MailTeamMember).where(MailTeamMember.id.in_(silent)))).all()}
        try:
            statuses = await agent_status_service.for_members(db, list(members.values()))
        except (OSError, ValueError):
            logger.warning("Wake follow-up could not read native activity; no notice was sent")
            return
        for member_id in silent:
            row, original = todo[member_id]
            member = members.get(member_id)
            if member is None or not self._known_idle_before(statuses.get(member_id), row.created_at):
                continue
            # The native read awaited. A member that read Mail meanwhile gets no notice.
            if await self._mail_activity_since(db, member_id, row.created_at):
                continue
            await self._send_notices(db, member, row, original, sent)

    async def _send_notices(self, db: AsyncSession, member: MailTeamMember, rewake: MailWakeAttempt,
                            original: int, sent: set[str]) -> None:
        preset = await db.get(AgentTeamPreset, member.team_preset_id) if member.team_preset_id else None
        if preset is None:
            return  # not on a team: there is nobody to tell
        item = None
        if member.team_slot_id is not None:
            item = (await db.scalars(select(GithubWorkItem).where(
                GithubWorkItem.owner_slot_id == member.team_slot_id,
                GithubWorkItem.dispatch_status.in_(ACTIVE_ITEM_STATUSES),
            ).order_by(GithubWorkItem.id.desc()).limit(1))).first()
        work = (f"issue #{item.issue_number} ({item.issue_title})" if item is not None
                else "no active work item")
        subject = f"No activity after a re-wake: {member.display_name}"
        body = (
            f"{member.display_name} did not act after a wake and a re-wake.\n\n"
            f"- Work: {work}\n"
            f"- Re-woken at {rewake.created_at:%Y-%m-%d %H:%M:%S} UTC\n"
            "- Seen: no Mail read, ack or sent message since the re-wake, and the native state was idle "
            "from before it.\n\n"
            "This is a best-effort observation, not proof. This notice grants no authority and wakes nobody."
        )
        payload = {"notice": "wake_followup_silent", "member_id": member.id, "slot_id": member.team_slot_id,
                   "attempt_id": original, "issue_number": item.issue_number if item is not None else None}
        leader_key = NOTICE_KEY.format(attempt_id=original, who="leader")
        supervisor_key = NOTICE_KEY.format(attempt_id=original, who="supervisor")
        leader = None
        if preset.leader_slot_id is not None and leader_key not in sent:
            leader = (await db.scalars(select(MailTeamMember).where(
                MailTeamMember.team_preset_id == preset.id,
                MailTeamMember.team_slot_id == preset.leader_slot_id,
            ).order_by(MailTeamMember.updated_at.desc(), MailTeamMember.id.desc()).limit(1))).first()
        if leader is not None:
            await agent_mail_service.send_message(db, MailMessageCreate(
                recipient_member_id=leader.id, audience_type="member", audience_id=str(leader.id),
                subject=subject, body_markdown=body, payload=payload),
                sender_type="system", commit=False, auto_nudge=False, delivery_key=leader_key)
        if preset.supervisor_actor_id is not None and supervisor_key not in sent:
            try:
                async with db.begin_nested():
                    # The Supervisor reads only strictly shaped system notices (see
                    # agent_team_supervisor_service.is_supervisor_message): a fixed subject and an
                    # exact payload. The member and the work item are in the body.
                    db.add(MailMessage(
                        kind="message", audience_type="team_preset", audience_id=str(preset.id),
                        subject=SUPERVISOR_NOTICE_SUBJECT, body_markdown=body, delivery_key=supervisor_key,
                        payload={**payload, "supervisor_actor_id": preset.supervisor_actor_id}))
                    await db.flush()
            except IntegrityError:
                pass  # another sweep or process wrote it first: exactly one is kept
        await db.commit()

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
            if not self._known_idle_before(fresh.get(member.id), since):
                return False
            # The native read awaited. The member can have read or acked Mail meanwhile, so the last
            # word before the transport is the Mail check. Nothing awaits between this and the send.
            return not await self._mail_activity_since(db, member.id, since)
        return guard

    @staticmethod
    async def _mail_activity_since(db: AsyncSession, member_id: int, since: datetime) -> bool:
        """The member read or acked mail, or sent a message, at or after the wake.

        One statement observes receipts and sent messages together. Two statements would leave a gap
        in which a read committed after the first one is missed.
        """
        activity = select(or_(
            exists().where(MailReceipt.member_id == member_id,
                           or_(MailReceipt.read_at >= since, MailReceipt.acked_at >= since)),
            exists().where(MailMessage.sender_member_id == member_id, MailMessage.created_at >= since),
        ))
        return bool(await db.scalar(activity))

    @staticmethod
    def _known_idle_before(status, since: datetime) -> bool:
        """True only for a known idle state whose last native event is older than the wake."""
        if status is None or status.activity_state != "idle" or status.idle_since is None:
            return False
        return _naive_utc(status.idle_since) < since


wake_followup_service = WakeFollowupService()
