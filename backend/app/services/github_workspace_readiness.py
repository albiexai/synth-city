"""Read-only diagnostics for the dispatch workspace pool."""
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import GithubWorkItem, GithubWorkspace, TeamGithubScope
from app.models.schemas import (
    GithubWorkspaceReadiness,
    GithubWorkspaceReadinessResponse,
    GithubWorkspaceWaitingItem,
)
from app.services.dispatch_workspace_provisioning import automatic_provision_is_incomplete
from app.services.github_workspace_service import github_workspace_service

# Prefer a repairable problem over pool exhaustion. Order is independent of row IDs.
_WAIT_REASONS = (
    "repository_identity_mismatch",
    "workspace_missing",
    "repository_identity_unavailable",
    "workspace_identity_unavailable",
    "workspace_provision_incomplete",
    "workspace_preparation_failed",
    "no_free_workspace",
)


def work_item_workspace_wait_reason(item: GithubWorkItem) -> str | None:
    if item.dispatch_status != "pending" or item.pending_reason != "queued_no_workspace":
        return None
    if item.workspace_wait_reason in _WAIT_REASONS:
        return item.workspace_wait_reason
    return "no_free_workspace"


def _workspace_path_blocker(workspace: GithubWorkspace) -> str | None:
    if workspace.kind == "primary":
        return "workspace_primary"
    if automatic_provision_is_incomplete(workspace):
        return "workspace_provision_incomplete"
    if not workspace.dispatchable:
        return "workspace_not_dispatchable"
    if not workspace.enabled and not workspace.provision_error:
        return "workspace_disabled"
    try:
        path = Path(workspace.path)
        resolved = path.resolve(strict=True)
        if str(resolved) != workspace.path or not resolved.is_dir():
            return "repository_identity_mismatch"
    except (FileNotFoundError, NotADirectoryError):
        return "workspace_missing"
    except (OSError, RuntimeError, ValueError):
        return "workspace_identity_unavailable"
    return None


async def _probe_identity(path: str) -> tuple[str, str, str] | None:
    try:
        return await github_workspace_service._probe_identity(path)
    except (OSError, ValueError):
        return None


async def _worktree_blocker(
    workspace: GithubWorkspace,
    repo_path: str,
    repo_identity: tuple[str, str, str] | None,
) -> str | None:
    if workspace.path == repo_path or (repo_identity and repo_identity[2] != repo_path):
        return "repository_identity_mismatch"
    if repo_identity is None:
        return "repository_identity_unavailable"
    identity = await _probe_identity(workspace.path)
    if identity is None:
        return "workspace_identity_unavailable"
    git_dir, common_dir, top_level = identity
    if git_dir == common_dir or common_dir != repo_identity[1] or top_level != workspace.path:
        return "repository_identity_mismatch"
    if workspace.provision_error:
        return "workspace_preparation_failed"
    return None


async def _read_workspaces(
    db: AsyncSession, scope: TeamGithubScope,
) -> list[GithubWorkspaceReadiness]:
    rows = await db.execute(
        select(GithubWorkspace, GithubWorkItem.issue_number)
        .outerjoin(
            GithubWorkItem,
            (GithubWorkItem.id == GithubWorkspace.leased_item_id)
            & (GithubWorkItem.scope_id == scope.id),
        )
        .where(GithubWorkspace.scope_id == scope.id)
        .order_by(GithubWorkspace.id)
    )
    workspaces = []
    repo_identity = None
    repo_checked = False
    for workspace, issue_number in rows:
        reason = None
        state = "leased"
        if workspace.leased_item_id is None:
            reason = _workspace_path_blocker(workspace)
            if reason is None:
                if not repo_checked:
                    repo_identity = await _probe_identity(scope.repo_path)
                    repo_checked = True
                reason = await _worktree_blocker(workspace, scope.repo_path, repo_identity)
            state = "blocked" if reason else "free"
            if reason == "workspace_missing":
                state = "missing"
        workspaces.append(GithubWorkspaceReadiness(
            workspace_id=workspace.id,
            path=workspace.path,
            kind=workspace.kind,
            state=state,
            reason=reason,
            leased_issue_number=issue_number,
        ))
    return workspaces


async def explain_workspace_wait(db: AsyncSession, scope: TeamGithubScope) -> str:
    workspaces = await _read_workspaces(db, scope)
    blockers = {workspace.reason for workspace in workspaces}
    return next((reason for reason in _WAIT_REASONS if reason in blockers), "no_free_workspace")


async def read_workspace_readiness(
    db: AsyncSession, scope: TeamGithubScope,
) -> GithubWorkspaceReadinessResponse:
    workspaces = await _read_workspaces(db, scope)
    items = await db.scalars(
        select(GithubWorkItem)
        .where(
            GithubWorkItem.scope_id == scope.id,
            GithubWorkItem.dispatch_status == "pending",
            GithubWorkItem.pending_reason == "queued_no_workspace",
            GithubWorkItem.milestone_replaced_at.is_(None),
        )
        .order_by(GithubWorkItem.id)
    )
    waiting_items = [
        GithubWorkspaceWaitingItem(work_item_id=item.id, issue_number=item.issue_number, reason=reason)
        for item in items
        if (reason := work_item_workspace_wait_reason(item)) is not None
    ]
    return GithubWorkspaceReadinessResponse(
        scope_id=scope.id, workspaces=workspaces, waiting_items=waiting_items,
    )
