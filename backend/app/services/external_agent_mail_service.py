"""External local orchestration surface for Agent Mail."""
import asyncio
import hashlib
import hmac
import re
import secrets
from collections import deque
from datetime import datetime, timedelta
from typing import Deque, Literal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import MailExternalActor, MailMessage, MailTeamMember
from app.models.schemas import (
    ExternalAgentMailContextRequest,
    ExternalAgentMailDeliveryRecipient,
    ExternalAgentMailHandoffRequest,
    ExternalAgentMailMessageRequest,
    ExternalAgentMailRequestStatus,
    ExternalAgentMailSendResponse,
    MailExternalActorCreate,
    MailExternalActorCreateResponse,
    MailExternalActorResponse,
    MailMessageCreate,
    MailThreadResponse,
    TeamListResponse,
)
from app.services import decision_wait_service as decision_waits
from app.services import factory_audit_service as audit
from app.services.agent_mail_service import agent_mail_service
from app.services.sqlite_busy_retry import retry_when_busy

ACTOR_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{2,80}$")
EXTERNAL_RATE_LIMIT_MAX_MESSAGES = 30
EXTERNAL_RATE_LIMIT_WINDOW_SECONDS = 60
EXTERNAL_WAIT_MAX_SECONDS = 30
EXTERNAL_WAIT_POLL_SECONDS = 0.5
_REQUEST_RECIPIENT = object()


class ExternalAgentMailAuthError(ValueError):
    """Raised when a bearer token cannot be mapped to an external actor."""


class ExternalAgentMailCredentialError(ValueError):
    def __init__(self, code: str, status_code: int) -> None:
        super().__init__(code)
        self.status_code = status_code


class ExternalAgentMailRateLimitError(ValueError):
    """Raised when an external actor exceeds the local message rate limit."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("External Agent Mail rate limit exceeded")
        self.retry_after_seconds = retry_after_seconds


class ExternalAgentMailService:
    """Token-bound external actor helpers for Agent Mail orchestration."""

    def __init__(self) -> None:
        self._send_windows: dict[int, Deque[datetime]] = {}

    def _hash_token(self, token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def actor_response(self, actor: MailExternalActor) -> MailExternalActorResponse:
        return MailExternalActorResponse(
            id=actor.id,
            actor_key=actor.actor_key,
            display_name=actor.display_name,
            kind=actor.kind,
            description=actor.description,
            created_at=actor.created_at,
            last_used_at=actor.last_used_at,
            revoked_at=actor.revoked_at,
            revoked_by=actor.revoked_by,
        )

    @staticmethod
    def _actor_profile(request: MailExternalActorCreate) -> dict:
        actor_key = request.actor_key.strip()
        display_name = request.display_name.strip()
        kind = request.kind.strip() or "external_tool"
        description = request.description.strip() if request.description else None
        if not ACTOR_KEY_PATTERN.match(actor_key):
            raise ValueError("actor_key must be 2-80 chars using letters, numbers, _, ., :, or -")
        if not display_name:
            raise ValueError("display_name is required")
        if len(kind) > 80:
            raise ValueError("kind must be 80 chars or less")
        return dict(actor_key=actor_key, display_name=display_name, kind=kind, description=description)

    async def actor_id_for_key(self, db: AsyncSession, key: str) -> int | None:
        return await db.scalar(select(MailExternalActor.id).where(MailExternalActor.actor_key == key.strip()))

    async def create_actor(self, db: AsyncSession, request: MailExternalActorCreate) -> MailExternalActorCreateResponse:
        """Insert a fresh identity. A competing insert never grants rotation authority."""
        profile = self._actor_profile(request)
        token = secrets.token_urlsafe(32)

        async def insert_actor():
            actor = MailExternalActor(**profile, token_hash=self._hash_token(token))
            db.add(actor)
            await db.flush()
            return actor

        try:
            actor = await retry_when_busy(db, insert_actor, route="external_actor_create")
        except IntegrityError:
            await db.rollback()
            if await self.actor_id_for_key(db, profile["actor_key"]) is not None:
                raise ExternalAgentMailCredentialError("external_actor_key_exists", 409) from None
            raise
        response = MailExternalActorCreateResponse(actor=self.actor_response(actor), token=token)
        await db.commit()
        return response

    async def change_actor_credential(
        self, db: AsyncSession, actor_id: int, action: Literal["revoke", "rotate", "restore"],
        director: dict, profile_request: MailExternalActorCreate | None = None,
    ) -> MailExternalActorResponse | MailExternalActorCreateResponse:
        now = datetime.utcnow()
        restore = action == "restore"
        revoke = action == "revoke"
        conditions = [MailExternalActor.id == actor_id,
                      MailExternalActor.revoked_at.is_not(None) if restore else MailExternalActor.revoked_at.is_(None)]
        values = self._actor_profile(profile_request) if profile_request is not None else {}
        if profile_request is not None:
            conditions.append(MailExternalActor.actor_key == values.pop("actor_key"))
        token = None if revoke else secrets.token_urlsafe(32)
        if token is not None:
            values["token_hash"] = self._hash_token(token)
        if revoke or restore:
            values.update(revoked_at=now if revoke else None, revoked_by=director["actor_reference"] if revoke else None)

        statement = (update(MailExternalActor).where(*conditions).values(**values)
            .returning(MailExternalActor).execution_options(populate_existing=True))
        # The decision and audit insert share the credential writer's transaction.
        try:
            actor = await retry_when_busy(db, lambda: db.scalar(statement), route="external_actor_credential")
            if actor is None:
                if await db.get(MailExternalActor, actor_id) is None:
                    raise ExternalAgentMailCredentialError("external_actor_not_found", 404)
                raise ExternalAgentMailCredentialError("external_actor_state_conflict", 409)
            await audit.record_event(db, event_kind="policy_change", source=f"external_agent_mail.{action}_actor",
                occurred_at=now, actor=director, context_snapshot={"external_actor_id": actor.id},
                before_values={"status": "revoked" if restore else "active"},
                after_values={"status": "revoked" if revoke else "active"}, action_outcome="applied")
            response = self.actor_response(actor)
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        return MailExternalActorCreateResponse(actor=response, token=token) if token is not None else response

    async def authenticate_actor(self, db: AsyncSession, token: str | None) -> MailExternalActor:
        if not token:
            raise ExternalAgentMailAuthError("Missing bearer token")
        hashed = self._hash_token(token)
        result = await db.execute(select(MailExternalActor.id, MailExternalActor.token_hash)
            .where(MailExternalActor.revoked_at.is_(None)))
        actor_id = next((row.id for row in result if hmac.compare_digest(row.token_hash, hashed)), None)
        if actor_id is not None:
            # The last-use write must see the same live credential after any concurrent change.
            statement = update(MailExternalActor).where(
                MailExternalActor.id == actor_id, MailExternalActor.token_hash == hashed,
                MailExternalActor.revoked_at.is_(None),
            ).values(last_used_at=datetime.utcnow()).returning(MailExternalActor).execution_options(populate_existing=True)
            actor = await retry_when_busy(db, lambda: db.scalar(statement), route="external_actor_authenticate")
            if actor is not None:
                await db.commit()
                return actor
            await db.rollback()
        raise ExternalAgentMailAuthError("Invalid bearer token")

    def check_send_rate_limit(self, actor_id: int) -> None:
        now = datetime.utcnow()
        cutoff = now - timedelta(seconds=EXTERNAL_RATE_LIMIT_WINDOW_SECONDS)
        window = self._send_windows.setdefault(actor_id, deque())
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= EXTERNAL_RATE_LIMIT_MAX_MESSAGES:
            retry_after = max(
                1,
                int((window[0] + timedelta(seconds=EXTERNAL_RATE_LIMIT_WINDOW_SECONDS) - now).total_seconds()),
            )
            raise ExternalAgentMailRateLimitError(retry_after)
        window.append(now)

    async def list_members(self, db: AsyncSession) -> TeamListResponse:
        await agent_mail_service.sync_observed_sessions(db)
        return TeamListResponse(members=await agent_mail_service.list_team(db))

    async def send_message(
        self,
        db: AsyncSession,
        actor: MailExternalActor,
        request: ExternalAgentMailMessageRequest,
        *,
        kind: str = "message",
        recipient_member_id: int | None | object = _REQUEST_RECIPIENT,
        thread_root_id: int | None = None,
        subject: str | None = None,
        body_markdown: str | None = None,
        payload: dict | None = None,
    ) -> ExternalAgentMailSendResponse:
        if kind != "broadcast" and (
            request.audience_type is not None or request.audience_id is not None
        ):
            raise ValueError("audience_fields_only_allowed_for_broadcasts")
        self.check_send_rate_limit(actor.id)
        resolved_recipient_id = (
            request.recipient_member_id
            if recipient_member_id is _REQUEST_RECIPIENT
            else recipient_member_id
        )
        if resolved_recipient_id is not None:
            await self._require_member(db, int(resolved_recipient_id))
        message_request = MailMessageCreate(
            kind=kind,
            recipient_member_id=resolved_recipient_id,
            thread_root_id=thread_root_id,
            subject=subject if subject is not None else request.subject,
            body_markdown=body_markdown if body_markdown is not None else request.body_markdown,
            payload=payload if payload is not None else request.payload,
            audience_type=request.audience_type if kind == "broadcast" else None,
            audience_id=request.audience_id if kind == "broadcast" else None,
        )
        message = await agent_mail_service.send_message(
            db,
            message_request,
            auto_nudge=False,
            sender_actor_id=actor.id,
        )
        recipient_ids = await agent_mail_service.recipient_ids_for_message(db, message.id)
        wake_results = await agent_mail_service.wake_members_with_results(db, recipient_ids)
        recipients = await self._delivery_recipients(db, recipient_ids, wake_results)
        return ExternalAgentMailSendResponse(
            actor=self.actor_response(actor),
            message=message,
            delivery_state=self._delivery_state(recipients),
            recipients=recipients,
        )

    async def send_direct_message(
        self,
        db: AsyncSession,
        actor: MailExternalActor,
        request: ExternalAgentMailMessageRequest,
    ) -> ExternalAgentMailSendResponse:
        if request.recipient_member_id is None:
            raise ValueError("recipient_member_id is required")
        return await self.send_message(db, actor, request, kind="message")

    async def send_broadcast(
        self,
        db: AsyncSession,
        actor: MailExternalActor,
        request: ExternalAgentMailMessageRequest,
    ) -> ExternalAgentMailSendResponse:
        return await self.send_message(db, actor, request, kind="broadcast", recipient_member_id=None)

    async def send_context_request(
        self,
        db: AsyncSession,
        actor: MailExternalActor,
        request: ExternalAgentMailContextRequest,
    ) -> ExternalAgentMailSendResponse:
        payload = {
            "why_needed": request.why_needed,
            "files_or_symbols": request.files_or_symbols,
        }
        return await self.send_message(
            db,
            actor,
            ExternalAgentMailMessageRequest(
                recipient_member_id=request.recipient_member_id,
                subject=request.subject,
                body_markdown=request.body_markdown,
            ),
            kind="context_request",
            payload=payload,
        )

    async def send_handoff(
        self,
        db: AsyncSession,
        actor: MailExternalActor,
        request: ExternalAgentMailHandoffRequest,
    ) -> ExternalAgentMailSendResponse:
        payload = {
            "files": request.files,
            "next_steps": request.next_steps,
        }
        return await self.send_message(
            db,
            actor,
            ExternalAgentMailMessageRequest(
                recipient_member_id=request.recipient_member_id,
                subject=request.subject,
                body_markdown=request.body_markdown,
            ),
            kind="handoff",
            payload=payload,
        )

    async def reply_in_thread(
        self,
        db: AsyncSession,
        actor: MailExternalActor,
        root_id: int,
        request: ExternalAgentMailMessageRequest,
    ) -> ExternalAgentMailSendResponse:
        await self.thread(db, actor, root_id)
        return await self.send_message(
            db,
            actor,
            request,
            kind="message",
            recipient_member_id=None,
            thread_root_id=root_id,
            subject=request.subject,
            body_markdown=request.body_markdown,
            payload=request.payload,
        )

    async def thread(
        self,
        db: AsyncSession,
        actor: MailExternalActor,
        message_id: int,
    ) -> MailThreadResponse:
        thread = await agent_mail_service.get_thread(db, message_id)
        from app.services.agent_team_supervisor_service import is_supervisor_message, require_supervisor_thread
        if is_supervisor_message(thread.root):
            await require_supervisor_thread(db, actor, thread.root)
        else:
            self._require_actor_owns_thread(thread, actor)
        return thread

    async def request_status(
        self,
        db: AsyncSession,
        actor: MailExternalActor,
        message_id: int,
    ) -> ExternalAgentMailRequestStatus:
        thread = await self.thread(db, actor, message_id)
        root = thread.root
        if root.kind not in {"context_request", "handoff"}:
            raise ValueError("Message is not a request")
        answered = root.request_status in {"answered", "acknowledged"} or any(
            reply.kind == "answer" for reply in thread.replies
        )
        acknowledged = root.request_status == "acknowledged"
        return ExternalAgentMailRequestStatus(
            message_id=root.id,
            kind=root.kind,
            request_status=root.request_status,
            is_stale=root.is_stale,
            answered=answered,
            acknowledged=acknowledged,
            root=root,
            replies=thread.replies,
        )

    async def wait_for_request_status(
        self,
        db: AsyncSession,
        actor: MailExternalActor,
        message_id: int,
        timeout_seconds: int,
    ) -> ExternalAgentMailRequestStatus:
        timeout = max(0, min(timeout_seconds, EXTERNAL_WAIT_MAX_SECONDS))
        deadline = datetime.utcnow() + timedelta(seconds=timeout)
        status = await self.request_status(db, actor, message_id)
        while (
            status.request_status == "pending"
            and not status.answered
            and not status.is_stale
            and datetime.utcnow() < deadline
        ):
            # Do not pin a DB transaction/connection while long-polling.
            await db.rollback()
            db.expire_all()
            await asyncio.sleep(EXTERNAL_WAIT_POLL_SECONDS)
            status = await self.request_status(db, actor, message_id)
        return status

    async def acknowledge_external_request(
        self,
        db: AsyncSession,
        actor: MailExternalActor,
        message_id: int,
    ) -> ExternalAgentMailRequestStatus:
        root = await db.get(MailMessage, message_id)
        if root is None:
            raise ValueError("Message not found")
        if root.sender_actor_id != actor.id:
            raise ValueError("External actors can only acknowledge requests they created")
        if root.kind not in {"context_request", "handoff"}:
            raise ValueError("Message is not a request")
        if root.request_status == "answered":
            root.request_status = "acknowledged"
            await db.commit()
        return await self.request_status(db, actor, message_id)

    async def acknowledge_actor_request(
        self,
        db: AsyncSession,
        actor: MailExternalActor,
        message_id: int,
    ) -> ExternalAgentMailRequestStatus:
        """Acknowledge a request as an external actor without member evidence."""
        # Apply the same actor access gate before any request state change.
        thread = await self.thread(db, actor, message_id)
        root = await db.get(MailMessage, thread.root.id)
        if root.kind not in {"context_request", "handoff"}:
            raise ValueError("Message is not a request")
        if root.request_status in {"pending", "answered"}:
            if root.request_status == "pending":
                # #1240: the request ends without an answer from a known member.
                await decision_waits.record_mail_wait_ended(
                    db, root.id, outcome="acknowledged",
                    source="external_agent_mail_service.acknowledge_actor_request")
            root.request_status = "acknowledged"
            await db.commit()
        return await self.request_status(db, actor, root.id)

    async def _delivery_recipients(
        self,
        db: AsyncSession,
        recipient_ids: set[int],
        wake_results: dict[int, dict[str, str | bool]],
    ) -> list[ExternalAgentMailDeliveryRecipient]:
        members = {member.id: member for member in (await agent_mail_service.list_team(db))}
        recipients: list[ExternalAgentMailDeliveryRecipient] = []
        for member_id in sorted(recipient_ids):
            member = members.get(member_id)
            if member is None:
                db_member = await db.get(MailTeamMember, member_id)
                member_name = db_member.display_name if db_member is not None else f"Member {member_id}"
                wake_state = "offline"
            else:
                member_name = member.display_name
                wake_state = member.wake_state
            wake = wake_results.get(member_id, {})
            wake_attempted = bool(wake.get("wake_attempted", False))
            wake_succeeded = bool(wake.get("wake_succeeded", False))
            if wake_succeeded:
                status = "wake_succeeded"
            elif wake_attempted:
                status = "wake_failed"
            elif wake_state == "offline":
                status = "stored_offline"
            elif wake_state == "delivered_waiting":
                status = "delivered_waiting"
            else:
                status = "stored"
            recipients.append(
                ExternalAgentMailDeliveryRecipient(
                    member_id=member_id,
                    member_name=member_name,
                    status=status,
                    wake_state=wake_state,
                    wake_attempted=wake_attempted,
                    wake_succeeded=wake_succeeded,
                    wake_method=str(wake["wake_method"]) if "wake_method" in wake else None,
                    wake_error=str(wake["wake_error"]) if "wake_error" in wake else None,
                )
            )
        return recipients

    async def _require_member(self, db: AsyncSession, member_id: int) -> None:
        if await db.get(MailTeamMember, member_id) is None:
            raise ValueError(f"Recipient member {member_id} not found")

    def _require_actor_owns_thread(
        self,
        thread: MailThreadResponse,
        actor: MailExternalActor,
    ) -> None:
        if thread.root.sender_actor_id != actor.id:
            raise PermissionError("External actors can only read threads they created")

    def _delivery_state(self, recipients: list[ExternalAgentMailDeliveryRecipient]) -> str:
        if not recipients:
            return "stored_no_recipients"
        statuses = {recipient.status for recipient in recipients}
        if "wake_succeeded" in statuses:
            return "wake_succeeded"
        if "wake_failed" in statuses:
            return "wake_failed"
        if "delivered_waiting" in statuses:
            return "delivered_waiting"
        if "stored_offline" in statuses:
            return "stored_offline"
        return "stored"


external_agent_mail_service = ExternalAgentMailService()
