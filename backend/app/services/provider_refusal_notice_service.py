"""Tell the Leader and the Supervisor that a member is blocked by a provider refusal (#1092).

One notice goes to each of them per refusal episode, naming the member and its active work item. Both are plain
rows: nobody is woken and nothing is sent to a pane. A delivery key per episode and recipient keeps a second writer
from adding another. The Supervisor reads only strictly shaped system notices, so its notice has a fixed subject and
an exact payload; see `_is_provider_refusal_notice` in `agent_team_supervisor_service`.

This module is called from `_record_input_waits`, in the same transaction as the claim of the episode. It imports
only models, because `agent_activity_service` imports it.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import AgentTeamSlot, GithubWorkItem, MailMessage, MailReceipt, MailTeamMember

NOTICE = "provider_refusal"
SUBJECT = "Provider refusal"
# The states in which #1060's wake follow-up notice counts a work item as the member's. It is not imported from
# there: wake_followup_service imports agent_status_service, which imports agent_activity_service, which imports
# this module. A test pins the two together.
ACTIVE_ITEM_STATUSES = ("dispatched", "verifying")
KEY = "provider-refusal:{binding}:{episode}:{who}"
TITLE_LIMIT = 200


async def send_provider_refusal_notices(db: AsyncSession, *, preset_id: int, slot_id: int, binding_id: int,
                                        started_at: datetime, supervisor_actor_id: int | None,
                                        leader_slot_id: int | None) -> int | None:
    """Write the notices of one refusal episode. Return the id to keep on the binding, or None if none was written.

    The Supervisor's notice is kept when there is one, because it is the one a human reads first; otherwise the
    Leader's. A recipient whose key already exists is skipped, so calling this twice for one episode adds nothing.
    Any other integrity failure is raised.
    """
    member = (await db.scalars(select(MailTeamMember).where(
        MailTeamMember.team_preset_id == preset_id, MailTeamMember.team_slot_id == slot_id)
        .order_by(MailTeamMember.updated_at.desc(), MailTeamMember.id.desc()).limit(1))).first()
    if member is None:
        return None  # the claim needs an authenticated member, so this is unreachable; send nothing malformed
    item = (await db.scalars(select(GithubWorkItem).where(
        GithubWorkItem.owner_slot_id == slot_id, GithubWorkItem.dispatch_status.in_(ACTIVE_ITEM_STATUSES))
        .order_by(GithubWorkItem.id.desc()).limit(1))).first()
    leader = None
    if leader_slot_id is not None:
        leader = (await db.scalars(select(MailTeamMember).join(
            AgentTeamSlot, AgentTeamSlot.id == MailTeamMember.team_slot_id).where(
            MailTeamMember.team_preset_id == preset_id, MailTeamMember.team_slot_id == leader_slot_id,
            AgentTeamSlot.preset_id == preset_id, AgentTeamSlot.enabled.is_(True))
            .order_by(MailTeamMember.updated_at.desc(), MailTeamMember.id.desc()).limit(1))).first()

    if item is not None:
        title = " ".join((item.issue_title or "").split())[:TITLE_LIMIT]
        work = f"issue #{item.issue_number} ({title})" if title else f"issue #{item.issue_number}"
    else:
        work = "no active work item"
    body = (
        f"{member.display_name} is blocked by a provider refusal. It is not waiting for input.\n\n"
        f"- Work: {work}\n"
        f"- Slot: {slot_id}\n\n"
        "The member reads as ordinary idle, so idle nudges and the re-wake still try to recover it. "
        "This notice grants no authority and wakes nobody."
    )
    payload = {"notice": NOTICE, "member_id": member.id, "slot_id": slot_id,
               "work_item_id": item.id if item is not None else None,
               "issue_number": item.issue_number if item is not None else None}
    episode = started_at.strftime("%Y%m%dT%H%M%S%f")
    kept: list[int] = []

    if supervisor_actor_id is not None:
        message = await _write(db, MailMessage(
            kind="message", sender_type="system", audience_type="team_preset", audience_id=str(preset_id),
            subject=SUBJECT, body_markdown=body, payload={**payload, "supervisor_actor_id": supervisor_actor_id},
            delivery_key=KEY.format(binding=binding_id, episode=episode, who="supervisor")))
        if message is not None:
            kept.append(message.id)
    if leader is not None:
        message = await _write(db, MailMessage(
            kind="message", sender_type="system", audience_type="member", audience_id=str(leader.id),
            recipient_member_id=leader.id, subject=SUBJECT, body_markdown=body, payload=payload,
            delivery_key=KEY.format(binding=binding_id, episode=episode, who="leader")), receipt_for=leader.id)
        if message is not None:
            kept.append(message.id)
    return kept[0] if kept else None


async def _write(db: AsyncSession, message: MailMessage, *, receipt_for: int | None = None) -> MailMessage | None:
    """Insert one notice. Return None only when another writer already sent this delivery key.

    Any other integrity failure (a receipt that cannot be stored, for example) is not a delivered duplicate: it is
    raised, so the caller can roll the whole episode back instead of latching an incomplete delivery.
    """
    key = message.delivery_key
    try:
        async with db.begin_nested():
            db.add(message)
            await db.flush()
            if receipt_for is not None:
                db.add(MailReceipt(message_id=message.id, member_id=receipt_for))
                await db.flush()
    except IntegrityError:
        if await db.scalar(select(MailMessage.id).where(MailMessage.delivery_key == key)) is not None:
            return None  # exactly one is kept: another writer wrote this episode first
        raise
    return message
