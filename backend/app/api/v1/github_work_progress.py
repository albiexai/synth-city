"""Safe work observations. These reads grant no execution or recovery authority."""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_mail_session
from app.database import get_db
from app.models.database import GithubWorkItem, GithubWorkspace, MailAgentSession, TeamGithubScope
from app.models.github_work_progress_schemas import GithubWorkProgressResponse
from app.services.github_client import github_client
from app.services.github_coordination_service import CoordinationError, github_coordination_service
from app.services.github_summary_publication_service import SummaryUnavailable, github_summary_publication_service
from app.services.github_work_progress_service import _TERMINAL_REPORT_STATUSES, github_work_progress_service
from mcp_shim.work_remaining_protocol import public_generation

router = APIRouter()


async def _progress(item_id: int, response: Response, db: AsyncSession) -> GithubWorkProgressResponse:
    """The full internal result, nonce included. Only an authenticated route may use it and it never returns it."""
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await asyncio.wait_for(github_work_progress_service.summary(db, item_id), timeout=9)
    except TimeoutError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="progress_observation_unavailable") from None
    if result is None:
        raise HTTPException(status_code=404, detail="GitHub work item not found")
    return result


@router.get("/github-work-items/{item_id}/progress", response_model=GithubWorkProgressResponse)
async def work_progress(item_id: int, response: Response, db: AsyncSession = Depends(get_db)):
    """Unauthenticated: the dispatch is named by its public generation, never by the nonce."""
    result = await _progress(item_id, response, db)
    return result.model_copy(update={"dispatch_nonce": None})


@router.get("/github-work-items/{item_id}/remaining-work-context", response_model=GithubWorkProgressResponse)
async def remaining_work_context(item_id: int, response: Response,
                                principal: MailAgentSession = Depends(require_mail_session),
                                db: AsyncSession = Depends(get_db)):
    """Only the current team Leader prepares the public, advisory report context."""
    response.headers["Cache-Control"] = "no-store"
    principal_id = principal.id
    item = await db.get(GithubWorkItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="GitHub work item not found")
    scope_id = item.scope_id
    try:
        await github_coordination_service.require_leader(db, scope_id, principal)
        result = await _progress(item_id, response, db)
        # Progress observation ends its DB snapshot. Recheck current membership and scope after its asynchronous
        # source reads: the FINAL awaited Leader check comes first, then ONE coherent joined projection of the item
        # and its workspace lease, then only synchronous comparisons and the return. Nothing awaits after the
        # projection, so no later change can slip between a check and the answer.
        current = await db.get(MailAgentSession, principal_id, populate_existing=True)
        if current is None:
            raise HTTPException(status_code=409, detail="progress_context_changed")
        await github_coordination_service.require_leader(db, scope_id, current)
        row = (await db.execute(
            select(GithubWorkItem.scope_id, GithubWorkItem.dispatch_nonce, GithubWorkItem.owner_slot_id,
                   GithubWorkItem.active_scope_revision, GithubWorkItem.dispatch_status,
                   GithubWorkItem.last_verified_sha, GithubWorkspace.id.label("leased_workspace_id"))
            .select_from(GithubWorkItem)
            .outerjoin(GithubWorkspace, GithubWorkspace.leased_item_id == GithubWorkItem.id)
            .where(GithubWorkItem.id == item_id))).first()
        context = result.remaining_work_context
        if row is None or row.scope_id != scope_id:
            raise HTTPException(status_code=409, detail="progress_context_changed")
        if (context is None or result.dispatch_nonce != row.dispatch_nonce or result.owner_slot_id != row.owner_slot_id
                or context.owner_slot_id != row.owner_slot_id or context.scope_revision != row.active_scope_revision
                or context.generation != public_generation(item_id, row.dispatch_nonce or "")
                or result.remaining_work_context_status != row.dispatch_status):
            raise HTTPException(status_code=409, detail="progress_context_unavailable")
        if result.remaining_work_context_basis == "recorded_head" and (
                row.dispatch_status not in _TERMINAL_REPORT_STATUSES or row.leased_workspace_id is not None
                or row.last_verified_sha != context.source_sha):
            raise HTTPException(status_code=409, detail="progress_context_unavailable")
        # The nonce binds this check privately; the response the Leader receives never carries it.
        return result.model_copy(update={"dispatch_nonce": None})
    except CoordinationError as error:
        raise HTTPException(status_code=error.status, detail=error.code) from error


async def _leader_scope(db: AsyncSession, scope_id: int, principal: MailAgentSession) -> TeamGithubScope:
    scope = await db.get(TeamGithubScope, scope_id)
    if scope is None:
        raise HTTPException(status_code=404, detail="scope_not_found")
    try:
        await github_coordination_service.require_leader(db, scope_id, principal)
    except CoordinationError as error:
        raise HTTPException(status_code=error.status, detail=error.code) from error
    return scope


@router.get("/github-pull-summaries/{scope_id}/{pull_number}")
async def render_pull_summary(scope_id: int, pull_number: int,
                              principal: MailAgentSession = Depends(require_mail_session),
                              db: AsyncSession = Depends(get_db)):
    """Render the generated review and remaining-work blocks for a pull request. This GET writes nothing."""
    scope = await _leader_scope(db, scope_id, principal)
    try:
        return await github_summary_publication_service.render(db, scope, pull_number, github_client)
    except SummaryUnavailable as error:
        raise HTTPException(status_code=502, detail="summary_unavailable") from error


@router.post("/github-pull-summaries/{scope_id}/{pull_number}/publish")
async def publish_pull_summary(scope_id: int, pull_number: int,
                               principal: MailAgentSession = Depends(require_mail_session),
                               db: AsyncSession = Depends(get_db)):
    """The Leader's one mutation: write or refresh the system-owned summary comment. It never edits a body."""
    scope = await _leader_scope(db, scope_id, principal)
    result = await github_summary_publication_service.publish_pull(db, scope, pull_number, github_client)
    return {"result": result, "pull_number": pull_number}
