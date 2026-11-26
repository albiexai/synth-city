"""Workspace readiness distinguishes capacity from operator-repairable blockers."""
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.database import _run_sqlite_compat_migrations
from app.models.database import AgentTeamPreset, GithubWorkItem, GithubWorkspace, TeamGithubScope
from app.services.github_workspace_readiness import explain_workspace_wait, read_workspace_readiness
from app.services.github_workspace_service import github_workspace_service


@pytest_asyncio.fixture
async def workspace_scope(db, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    preset = AgentTeamPreset(name="Workspace readiness", created_by="test")
    db.add(preset)
    await db.flush()
    scope = TeamGithubScope(preset_id=preset.id, repo_owner="owner", repo_name="repo", repo_path=str(repo))
    db.add(scope)
    await db.flush()
    return scope


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "state", "reason", "wait_reason"),
    [
        ("linked", "free", None, "no_free_workspace"),
        ("primary", "blocked", "workspace_primary", "no_free_workspace"),
        ("disabled", "blocked", "workspace_disabled", "no_free_workspace"),
        ("not-dispatchable", "blocked", "workspace_not_dispatchable", "no_free_workspace"),
        ("missing", "missing", "workspace_missing", "workspace_missing"),
        ("mismatch", "blocked", "repository_identity_mismatch", "repository_identity_mismatch"),
        ("standalone", "blocked", "repository_identity_mismatch", "repository_identity_mismatch"),
        ("linked-path", "blocked", "repository_identity_mismatch", "repository_identity_mismatch"),
        ("file-path", "blocked", "repository_identity_mismatch", "repository_identity_mismatch"),
        ("scope-checkout-path", "blocked", "repository_identity_mismatch", "repository_identity_mismatch"),
        ("scope-top-level-elsewhere", "blocked", "repository_identity_mismatch", "repository_identity_mismatch"),
        ("main-checkout", "blocked", "repository_identity_mismatch", "repository_identity_mismatch"),
        ("worktree-top-level-elsewhere", "blocked", "repository_identity_mismatch", "repository_identity_mismatch"),
        ("unreadable-worktree", "blocked", "workspace_identity_unavailable", "workspace_identity_unavailable"),
        ("unreadable-repo", "blocked", "repository_identity_unavailable", "repository_identity_unavailable"),
        ("provision-pending", "blocked", "workspace_provision_incomplete", "workspace_provision_incomplete"),
        ("provision-failed", "blocked", "workspace_provision_incomplete", "workspace_provision_incomplete"),
        ("preparation-failed", "blocked", "workspace_preparation_failed", "workspace_preparation_failed"),
    ],
)
async def test_workspace_readiness_cases(
    db, tmp_path, monkeypatch, workspace_scope, case, state, reason, wait_reason,
):
    scope = workspace_scope
    repo = tmp_path / "repo"
    path = tmp_path / "checkout"
    if case == "scope-checkout-path":
        path = repo
    elif case == "linked-path":
        path.symlink_to(repo, target_is_directory=True)
    elif case == "file-path":
        path.touch()
    elif case not in {"missing", "provision-pending", "provision-failed"}:
        path.mkdir()
    if case == "mismatch":
        db.add(GithubWorkspace(scope_id=scope.id, path=str(tmp_path / "other-missing")))
    workspace = GithubWorkspace(
        scope_id=scope.id, path=str(path),
        kind="primary" if case == "primary" else "worktree",
        enabled=case not in {"disabled", "provision-pending", "provision-failed"},
        dispatchable=case not in {"not-dispatchable", "provision-pending", "provision-failed"},
        provision_error={
            "provision-pending": "workspace_auto_provision_pending",
            "provision-failed": "workspace_auto_provision_failed: private diagnostic",
            "preparation-failed": "private diagnostic",
        }.get(case),
    )
    db.add(workspace)
    await db.commit()

    async def git_read(args):
        assert args[2:] == ["rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir", "--show-toplevel"]
        assert case not in {"linked-path", "file-path"}, "Invalid paths must be rejected before the Git read"
        common = (path if case == "main-checkout" else repo) / ".git"
        if args[1] == str(repo):
            if case == "unreadable-repo":
                raise OSError("private diagnostic")
            # A repository scope can point at a linked checkout of the same repository.
            git_dir = common / "worktrees" / repo.name if case in {"main-checkout", "scope-checkout-path"} else common
            top_level = repo.parent if case == "scope-top-level-elsewhere" else repo
            return 0, f"{git_dir}\n{common}\n{top_level}"
        assert args[1] == str(path)
        if case == "unreadable-worktree":
            return 128, "private diagnostic"
        if case == "mismatch":
            common = tmp_path / "unrelated" / ".git"
        git_dir = common / "worktrees" / path.name
        if case == "standalone":
            git_dir = common = path / ".git"
        if case == "main-checkout":
            git_dir = common
        top_level = path.parent if case == "worktree-top-level-elsewhere" else path
        return 0, f"{git_dir}\n{common}\n{top_level}"

    monkeypatch.setattr(github_workspace_service, "_runner", git_read)
    result = await read_workspace_readiness(db, scope)
    row = next(row for row in result.workspaces if row.workspace_id == workspace.id)
    assert (row.state, row.reason, row.leased_issue_number) == (state, reason, None)
    assert "private diagnostic" not in result.model_dump_json()
    assert await explain_workspace_wait(db, scope) == wait_reason


@pytest.mark.asyncio
async def test_workspace_wait_reason_migration_preserves_waiting_item(db, workspace_scope):
    item = GithubWorkItem(
        scope_id=workspace_scope.id,
        issue_number=7,
        issue_title="An existing workspace wait",
        issue_url="https://example.test/issues/7",
        github_updated_at=datetime.utcnow(),
        dispatch_status="pending",
        pending_reason="queued_no_workspace",
    )
    db.add(item)
    await db.commit()

    async with db.bind.connect() as conn:
        await conn.execute(text("ALTER TABLE github_work_items DROP COLUMN workspace_wait_reason"))
        await conn.commit()
        await _run_sqlite_compat_migrations(conn)
        await _run_sqlite_compat_migrations(conn)
        row = (await conn.execute(
            text(
                "SELECT dispatch_status, pending_reason, workspace_wait_reason "
                "FROM github_work_items WHERE id = :id"
            ),
            {"id": item.id},
        )).one()
        assert row == ("pending", "queued_no_workspace", None)
