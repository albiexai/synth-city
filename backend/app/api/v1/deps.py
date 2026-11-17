"""Shared route dependencies for capability-token enforcement."""

import hmac
from typing import AsyncIterator, Optional

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.database import MailAgentSession
from app.services.agent_mail_service import agent_mail_service
from app.utils import peer_process
from app.utils.path_utils import resolve_scope_user, set_scope_home, _scope_home


def resolve_request_pane_detailed(
    http_request: Request,
    *,
    max_parent_walk: int = 32,
) -> peer_process.PeerPaneResolution:
    """Resolve a live request's process tree while its socket still exists."""
    client = http_request.client
    if client is None:
        return peer_process.PeerPaneResolution(
            None, (), "client_unavailable", max_parent_walk
        )
    local_port = http_request.scope.get("server", (None, None))[1]
    return peer_process.resolve_peer_pane_detailed(
        client.host,
        client.port,
        local_port=local_port,
        max_parent_walk=max_parent_walk,
    )


def resolve_request_pane(
    http_request: Request,
) -> Optional[peer_process.PeerPane]:
    """Compatibility projection used by Agent Mail registration."""
    return resolve_request_pane_detailed(http_request).pane


def require_current_mail_session(session: MailAgentSession) -> None:
    """Reject ended sessions and role-bearing sessions whose pane is stale."""
    if session.closed_at is not None:
        raise HTTPException(status_code=401, detail="session_token_closed")
    if not settings.mail_capability_tokens_required:
        return
    if session.mailbox_status == "offline":
        raise HTTPException(status_code=401, detail="session_token_stale")
    if session.team_slot_id is None:
        return
    if session.bound_pane_pid is None or session.bound_pane_proc_start is None:
        raise HTTPException(status_code=401, detail="session_token_stale")
    if (
        peer_process.pane_is_alive(
            session.bound_pane_pid,
            session.bound_pane_proc_start,
        )
        is not True
    ):
        raise HTTPException(status_code=401, detail="session_token_stale")


async def mail_session(
    x_synth_city_session_token: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Optional[MailAgentSession]:
    """Resolve the calling session from its capability token."""
    if not x_synth_city_session_token:
        if settings.mail_capability_tokens_required:
            raise HTTPException(status_code=401, detail="session_token_required")
        return None

    hashed = agent_mail_service.hash_capability_token(x_synth_city_session_token)
    result = await db.execute(
        select(MailAgentSession).where(MailAgentSession.capability_token_hash.is_not(None))
    )
    for session in result.scalars().all():
        if hmac.compare_digest(session.capability_token_hash, hashed):
            require_current_mail_session(session)
            return session
    raise HTTPException(status_code=401, detail="session_token_invalid")


async def require_mail_session(
    session: Optional[MailAgentSession] = Depends(mail_session),
) -> MailAgentSession:
    """Like mail_session, but never None."""
    if session is None:
        raise HTTPException(status_code=401, detail="session_token_required")
    return session


async def close_mail_session(
    x_synth_city_session_token: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> MailAgentSession:
    if not x_synth_city_session_token:
        raise HTTPException(status_code=401, detail="session_token_required")
    hashed = agent_mail_service.hash_capability_token(x_synth_city_session_token)
    sessions = (await db.execute(
        select(MailAgentSession).where(MailAgentSession.capability_token_hash.is_not(None))
    )).scalars().all()
    for session in sessions:
        if hmac.compare_digest(session.capability_token_hash, hashed):
            return session
    raise HTTPException(status_code=401, detail="session_token_invalid")


def require_session_slot(session: MailAgentSession) -> int:
    """Return the bound slot id or refuse an unbound session."""
    if session.team_slot_id is None:
        raise HTTPException(status_code=403, detail="session_not_slot_bound")
    return session.team_slot_id


async def require_operator(
    x_synth_city_operator_token: str | None = Header(default=None),
) -> None:
    """Authenticate the operator by a secret no agent is given.

    A sibling of require_session_slot, not a variant: that one authenticates an
    agent by what the kernel says about it, this one authenticates the operator
    by a shared secret. Do not merge them -- an agent's own session token must
    never open an operator route.

    Three distinguishable refusals, in this order:

      settings.operator_token empty  -> 503 operator_token_unconfigured
      no header (or an empty one)    -> 401 operator_token_required
      a header that does not match   -> 401 operator_token_invalid

    The empty check comes FIRST and that ordering is load-bearing. hmac.
    compare_digest("", "") returns True, so an implementation that leaves the
    empty setting to the comparison authorizes every caller who sends no header
    -- measured: 200 with the full workspace listing -- while its source still
    reads fail-closed. It refuses a *garbage* header, so a suite that never
    sends an empty one would not notice.

    The comparison is over BYTES because compare_digest raises TypeError on str
    values holding non-ASCII characters, and an unhandled TypeError here is an
    HTTP 500 rather than a refusal (measured).

    settings.operator_token is read at CALL time, not captured at import: the
    settings object is built when config.py is imported, so a module-level
    constant would freeze the empty default and make the 503 unconditional.

    What this credential is worth: the backend and every agent pane share a
    uid, so a determined pane can read backend/.env. This is a boundary against
    an opportunistic adversary, not a co-resident one -- it moves the attack
    from knowing a URL to deliberately reading a 600 file. Do not describe it
    as authenticating a human.
    """
    expected = settings.operator_token
    if not expected:
        raise HTTPException(status_code=503, detail="operator_token_unconfigured")
    if not x_synth_city_operator_token:
        raise HTTPException(status_code=401, detail="operator_token_required")
    if not hmac.compare_digest(
        x_synth_city_operator_token.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="operator_token_invalid")


_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


async def operator_write_context(
    origin: Optional[str] = Header(default=None),
    x_synth_city_session_token: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """How an operator-key write reached the server, for the audit trail (#945).

    Use it AFTER ``require_operator``: the key is the authority and this never grants or refuses anything. A usable Mail
    session token is the one server-derived fact (``mail_tool``, with the session id read from the session row, never
    from the request). The other paths are labels from the browser's Origin header: a configured origin reads ``ui``,
    anything else ``api``. Neither proves who called. A token that is unknown, closed or stale does not refuse the
    write; it is reported as ``session_invalid`` so the event records the write as unattributed.
    """
    session = None
    invalid = False
    if x_synth_city_session_token:
        hashed = agent_mail_service.hash_capability_token(x_synth_city_session_token)
        rows = await db.execute(
            select(MailAgentSession).where(MailAgentSession.capability_token_hash.is_not(None)))
        match = next((row for row in rows.scalars().all()
                      if hmac.compare_digest(row.capability_token_hash, hashed)), None)
        try:
            if match is None:
                raise HTTPException(status_code=401, detail="session_token_invalid")
            require_current_mail_session(match)
            session = match
        except HTTPException:
            invalid = True
    if session is not None:
        via = "mail_tool"
    else:
        via = "ui" if origin and origin in settings.cors_origins else "api"
    return {"via": via, "session_id": session.id if session is not None else None, "session_invalid": invalid}


async def require_operator_for_writes(
    request: Request,
    x_synth_city_operator_token: str | None = Header(default=None),
) -> None:
    """Guard the writes of a router that changes the controller's own settings.

    Hooks, MCP servers and the status line are commands that run as the
    controller, and the MCP test route starts a configured command at once, so
    a write on these routers is as strong as running a command. When a Director
    key is configured, every write needs it; reads stay open.

    An install without a configured key (one user on a laptop) keeps working as
    before. That is deliberate and is the only difference from require_operator,
    which refuses with 503 when no key is configured.
    """
    if request.method in _READ_METHODS or not settings.operator_token:
        return
    await require_operator(x_synth_city_operator_token)


async def require_mail_session_or_operator(
    x_synth_city_session_token: Optional[str] = Header(default=None),
    x_synth_city_operator_token: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Optional[MailAgentSession]:
    """Authenticate either one agent session or the configured operator."""
    if x_synth_city_session_token:
        return await mail_session(x_synth_city_session_token, db)
    await require_operator(x_synth_city_operator_token)
    return None


def _permission_error(error: BaseException) -> Optional[PermissionError]:
    """The PermissionError that caused this error, if one did."""
    seen = 0
    while error is not None and seen < 8:
        if isinstance(error, PermissionError):
            return error
        error = error.__cause__ or error.__context__
        seen += 1
    return None


async def apply_harness_scope(
    request: Request,
    x_harness_scope_user: Optional[str] = Header(default=None),
) -> AsyncIterator[None]:
    """Show one OS user's global scope for this request, and nothing else.

    The user comes from the X-Harness-Scope-User header and must be one of scope_users(): the controller
    itself or the configured agent user. With no header the default applies: the agent user when one is
    configured, otherwise the controller. Other routers never run this, so backups and the controller's
    own data keep the controller's home.

    A non-controller home that the controller cannot read or write answers 403 and names the directory,
    instead of a 500.
    """
    try:
        user = resolve_scope_user(x_harness_scope_user)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    token = set_scope_home(user.home)
    try:
        yield
    except (PermissionError, HTTPException) as error:
        # Most routes turn an OSError into their own 500. Look for the permission error behind it.
        denied = _permission_error(error)
        if denied is None or user.is_controller:
            raise
        action = "read" if request.method in _READ_METHODS else "write"
        where = denied.filename or str(user.home / ".claude")
        raise HTTPException(
            status_code=403,
            detail=f"The controller cannot {action} {where}. It belongs to {user.name}.",
        ) from error
    finally:
        try:
            _scope_home.reset(token)
        except ValueError:
            pass
