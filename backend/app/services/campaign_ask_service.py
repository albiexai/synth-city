"""#1260: asking the Leader, the Supervisor or a verifier about a campaign, by Agent Mail (design §6).

An ask references an immutable snapshot version: a ``campaign_snapshots`` row, ``ask`` purpose for a live campaign, the
latest ``final`` row for a frozen one (#1244). There is no ask table. The request is a Mail message that carries the
version id; the reply is the answer in its thread, shown with the request's version id. One open request per agent per
campaign: the delivery key ``campaign-ask:CAMPAIGN:AGENT:N`` is unique, so two callers for the same N cannot both win.

An ask is stale when the current version id differs from the referenced one. ``metrics_unchanged`` says whether the
numbers are the same anyway; it never hides staleness. A reading that cannot be made is unknown with a reason.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from app.models.database import (
    AgentTeamPreset,
    AgentTeamSlot,
    Campaign,
    CampaignSnapshot,
    MailExternalActor,
    MailMessage,
    MailTeamMember,
    TeamGithubScope,
)
from app.models.schemas import (
    CampaignAskLesson,
    CampaignAskReply,
    CampaignAskView,
    MailMessageCreate,
)
from app.services import campaign_snapshot_service as snapshots
from app.services import factory_audit_service as audit
from app.services.agent_mail_service import MailDeliveryIntegrityError, agent_mail_service

logger = logging.getLogger(__name__)

KEY_PREFIX = "campaign-ask:"
LESSON_KEY_PREFIX = "campaign-ask-lesson:"
ROLE_QUESTIONS = {
    "leader": ("Coordination and flow: what did the queue, the assignments and the decisions do, and what held items "
               "up? Answer from the snapshot's numbers."),
    "supervisor": ("The factory and the process: hand merges, quotas and rate limits, merge gates and effects "
                   "across teams. Answer from the snapshot's numbers."),
    "verifier": ("Quality and rework: what was wrong in submissions, why items needed more rounds (missing tests, "
                 "unclear acceptance, scope drift, item size, builder or model patterns), and what would have "
                 "prevented it. Answer from the snapshot's numbers."),
}
VERIFIER_WARNING = ("This request takes the verifier from review. Review is often the busiest lane. Ask a verifier "
                    "in a live campaign only when you need the answer now.")
DEFAULT_VERIFIER_ROLES = frozenset({"verifier"})


class AskError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code, self.detail = code, detail or code


async def _verifier_roles(db, preset_id: int) -> frozenset[str]:
    roles = set(DEFAULT_VERIFIER_ROLES)
    for configured in await db.scalars(select(TeamGithubScope.verifier_roles).where(TeamGithubScope.preset_id == preset_id)):
        roles.update(str(role).strip().lower() for role in configured or [])
    return frozenset(roles)


async def _agent(db, preset: AgentTeamPreset, role: str, member_id: int | None):
    """The asked agent as (agent key, label, member id or None, supervisor actor id or None)."""
    if role == "supervisor":
        actor = await db.get(MailExternalActor, preset.supervisor_actor_id) if preset.supervisor_actor_id else None
        if actor is None:
            raise AskError("campaign_ask_role_unavailable", "The team has no Supervisor binding.")
        return f"supervisor:{actor.id}", actor.display_name, None, actor.id
    if role == "leader":
        query = select(MailTeamMember).where(MailTeamMember.team_preset_id == preset.id,
                                             MailTeamMember.team_slot_id == preset.leader_slot_id)
    else:
        if member_id is None:
            raise AskError("campaign_ask_role_unavailable", "Name the verifier's member.")
        query = select(MailTeamMember).join(AgentTeamSlot, AgentTeamSlot.id == MailTeamMember.team_slot_id).where(
            MailTeamMember.id == member_id, MailTeamMember.team_preset_id == preset.id,
            AgentTeamSlot.enabled.is_not(False))
    member = await db.scalar(query) if (role == "verifier" or preset.leader_slot_id is not None) else None
    if member is None:
        raise AskError("campaign_ask_role_unavailable", f"The team has no {role} to ask.")
    if role == "verifier":
        slot_role = await db.scalar(select(AgentTeamSlot.role).where(AgentTeamSlot.id == member.team_slot_id))
        if (slot_role or "").strip().lower() not in await _verifier_roles(db, preset.id):
            raise AskError("campaign_ask_role_unavailable", "That member is not a verifier.")
    return f"member:{member.id}", member.display_name, member.id, None


def _requests(campaign_id: int):
    return select(MailMessage).where(
        MailMessage.kind.in_(("context_request", "message")), MailMessage.thread_root_id.is_(None),
        MailMessage.payload["kind"].as_string() == "campaign_ask",
        MailMessage.payload["campaign_id"].as_integer() == campaign_id).order_by(MailMessage.id)


async def _thread(db, request: MailMessage) -> list[MailMessage]:
    return list((await db.scalars(select(MailMessage).where(MailMessage.thread_root_id == request.id)
                                  .order_by(MailMessage.id))).all())


def _valid_reply(request: MailMessage, thread: list[MailMessage]) -> MailMessage | None:
    """The first reply that the asked agent made: a member's answer, or the bound Supervisor's message."""
    payload = request.payload
    for message in thread:
        if payload["role"] == "supervisor":
            if message.sender_actor_id == payload["supervisor_actor_id"] and message.kind == "message" \
                    and (message.payload or {}).get("kind") != "campaign_ask_lesson":
                return message
        elif message.kind == "answer" and message.sender_member_id == request.recipient_member_id:
            return message
    return None


def _key_number(request: MailMessage) -> int:
    return int((request.delivery_key or "").rsplit(":", 1)[-1])


def _numbers(payload: dict, role: str) -> str:
    metrics = payload.get("metrics") or []
    first = ("review", "rework") if role == "verifier" else ()
    metrics = sorted(metrics, key=lambda m: not any(word in str(m.get("name")) for word in first))
    lines = []
    for metric in metrics:
        value = "unknown" if metric.get("value") is None else f"{metric['value']} {metric.get('unit') or ''}".strip()
        lines.append(f"- {metric.get('name')}: {value} (coverage {metric.get('coverage')}, "
                     f"{metric.get('sample_count')} samples)")
    return "\n".join(lines) or "- No numbers are recorded yet."


async def create_ask(db, campaign: Campaign, *, role: str, member_id: int | None, client, actor: dict,
                     director_question: str | None = None, acknowledge_verifier_warning: bool = False) -> CampaignAskView:
    campaign_id, preset_id = campaign.id, campaign.preset_id
    # Take the write reservation first, so concurrent callers run one after the other (SQLite) and each reads what
    # the one before wrote. The unique delivery key below is the backstop on any database.
    await db.commit()
    try:
        await db.execute(update(Campaign).where(Campaign.id == campaign_id).values(team_context_key=Campaign.team_context_key))
    except OperationalError:
        await db.rollback()
        raise AskError("campaign_ask_open", "Another ask is being made. Read the asks and try again.") from None
    campaign = await db.get(Campaign, campaign_id)
    preset = await db.get(AgentTeamPreset, preset_id)
    if campaign is None or preset is None or await audit.current_context_key(
            db, "team", preset_id) != campaign.team_context_key:
        raise AskError("campaign_ask_not_available", "The campaign's team no longer exists.")
    frozen = await snapshots.latest(db, campaign_id)
    if frozen is None and campaign.status == "finished":
        raise AskError("campaign_ask_not_available", "The campaign has finished and has no frozen version to ask about.")
    agent_key, label, asked_member, supervisor_actor = await _agent(db, preset, role, member_id)
    if role == "verifier" and frozen is None and not acknowledge_verifier_warning:
        raise AskError("campaign_ask_verifier_warning", VERIFIER_WARNING)

    earlier = [r for r in (await db.scalars(_requests(campaign_id))).all() if r.payload.get("agent_key") == agent_key]
    for request in earlier:
        if _valid_reply(request, await _thread(db, request)) is None:
            raise AskError("campaign_ask_open", "This agent has an open request in this campaign.")
    number = max((_key_number(r) for r in earlier), default=0) + 1

    version = frozen or await snapshots.ask_version(db, campaign, client)
    payload = {"kind": "campaign_ask", "campaign_id": campaign_id, "snapshot_version_id": version.id,
               "snapshot_version": version.version, "calculation_version": version.calculation_version, "role": role,
               "agent_key": agent_key, "request_token": uuid.uuid4().hex}
    body = (f"**Campaign {campaign_id}: a question for you ({role}).**\n\n"
            f"Snapshot version {version.id}, calculated up to {version.payload.get('as_of')}. "
            f"Page: /insights/retrospectives/{campaign_id}\n\n**Key numbers**\n{_numbers(version.payload, role)}\n\n"
            f"**Question**\n{ROLE_QUESTIONS[role]}")
    if director_question:
        body += f"\n\n**The Director also asks**\n{director_question}"
    key = f"{KEY_PREFIX}{campaign_id}:{agent_key}:{number}"
    try:
        if role == "supervisor":
            # Like the other Supervisor notices: a stored root that the Supervisor reads through its inbox.
            payload["supervisor_actor_id"] = supervisor_actor
            row = MailMessage(kind="message", sender_type="system", audience_type="team_preset", audience_id=str(preset_id),
                              subject=f"Campaign {campaign_id}: a question", body_markdown=body, payload=payload,
                              delivery_key=key)
            async with db.begin_nested():
                db.add(row)
                await db.flush()
            sent_id = row.id
        else:
            message = MailMessageCreate(kind="context_request", subject=f"Campaign {campaign_id}: a question",
                                        recipient_member_id=asked_member, body_markdown=body, payload=payload)
            sent_id = (await agent_mail_service.send_message(db, message, delivery_key=key, auto_nudge=False,
                                                             commit=False)).id
        await db.commit()
    except (MailDeliveryIntegrityError, IntegrityError):
        await db.rollback()
        raise AskError("campaign_ask_open", "This agent has an open request in this campaign.") from None
    except BaseException:
        await db.rollback()
        raise
    if asked_member is not None:
        try:
            await agent_mail_service.auto_nudge_members(db, {asked_member})
        except Exception:  # a missed wakeup never undoes the ask
            logger.warning("campaign ask %s: the wakeup failed", sent_id, exc_info=True)
    return (await _views(db, campaign, [await db.get(MailMessage, sent_id)]))[0]


async def list_asks(db, campaign: Campaign) -> list[CampaignAskView]:
    """Reads only: a reply is already stored in Mail, so nothing is synchronized."""
    return await _views(db, campaign, list((await db.scalars(_requests(campaign.id))).all()))


async def _views(db, campaign: Campaign, requests: list[MailMessage]) -> list[CampaignAskView]:
    current, current_reason = await snapshots.current_version(db, campaign)
    views = []
    for request in requests:
        payload = request.payload
        thread = await _thread(db, request)
        reply = _valid_reply(request, thread)
        lesson = next((m for m in thread if (m.payload or {}).get("kind") == "campaign_ask_lesson"), None)
        referenced = await db.get(CampaignSnapshot, payload["snapshot_version_id"])
        unknown = current_reason or ("version_missing" if referenced is None else
                                     "no_fingerprint" if referenced.purpose == snapshots.ASK
                                     and referenced.fingerprint is None else None)
        changed = None if unknown else (current.id if current is not None else None) != referenced.id
        same = None if unknown or current is None else (
            snapshots.metrics_digest(current.payload) == snapshots.metrics_digest(referenced.payload))
        question = request.body_markdown.partition("**The Director also asks**\n")[2] or None
        views.append(CampaignAskView(
            id=request.id, campaign_id=campaign.id, role=payload["role"], agent_key=payload["agent_key"],
            agent_label=payload["agent_key"] if referenced is None else await _label(db, payload),
            status="answered" if reply is not None else "open", director_question=question,
            created_at=request.created_at, referenced_version_id=payload["snapshot_version_id"],
            referenced_version=payload["snapshot_version"], calculation_version=payload["calculation_version"],
            current_version_id=None if unknown or current is None else current.id,
            version_changed=changed, stale=changed, metrics_unchanged=same,
            state="unknown" if unknown else "known", unknown_reason=unknown,
            reply=None if reply is None else CampaignAskReply(
                id=reply.id, body=reply.body_markdown, created_at=reply.created_at,
                snapshot_version_id=payload["snapshot_version_id"]),
            lesson=None if lesson is None else CampaignAskLesson(
                id=lesson.id, text=lesson.body_markdown, created_at=lesson.created_at,
                snapshot_version_id=payload["snapshot_version_id"])))
    return views


async def _label(db, payload: dict) -> str:
    kind, _, ident = payload["agent_key"].partition(":")
    if kind == "supervisor":
        actor = await db.get(MailExternalActor, int(ident))
        return actor.display_name if actor is not None else payload["agent_key"]
    member = await db.get(MailTeamMember, int(ident))
    return member.display_name if member is not None else payload["agent_key"]


async def record_lesson(db, campaign: Campaign, request_id: int, *, text: str, actor: dict) -> CampaignAskView:
    """Record the Director's diagnosis from an answered ask, once. It is a message in the ask's thread."""
    campaign_id = campaign.id
    request = await db.get(MailMessage, request_id)
    if request is None or request.thread_root_id is not None or (request.payload or {}).get("kind") != "campaign_ask" \
            or request.payload.get("campaign_id") != campaign_id:
        raise AskError("campaign_ask_not_found", "No such ask in this campaign.")
    thread = await _thread(db, request)
    if _valid_reply(request, thread) is None:
        raise AskError("campaign_ask_not_answered", "Record a lesson after the agent has answered.")
    if any((m.payload or {}).get("kind") == "campaign_ask_lesson" for m in thread):
        raise AskError("campaign_ask_lesson_recorded", "A lesson is already recorded for this ask.")
    payload = {"kind": "campaign_ask_lesson", "campaign_id": campaign_id, "request_id": request_id,
               "snapshot_version_id": request.payload["snapshot_version_id"], "recorded_by": actor["actor_kind"]}
    try:
        async with db.begin_nested():
            db.add(MailMessage(kind="message", sender_type="system", thread_root_id=request_id,
                               audience_type=request.audience_type, audience_id=request.audience_id,
                               body_markdown=text, payload=payload, delivery_key=f"{LESSON_KEY_PREFIX}{request_id}"))
            await db.flush()
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise AskError("campaign_ask_lesson_recorded", "A lesson is already recorded for this ask.") from None
    return next(view for view in await list_asks(db, campaign) if view.id == request_id)

