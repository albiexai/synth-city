"""Workspace readiness distinguishes capacity from operator-repairable blockers."""
import pytest

from app.models.database import AgentTeamPreset, GithubWorkspace, TeamGithubScope
from app.services.github_workspace_readiness import explain_workspace_wait, read_workspace_readiness
from app.services.github_workspace_service import github_workspace_service


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
        ("unreadable-worktree", "blocked", "workspace_identity_unavailable", "workspace_identity_unavailable"),
        ("unreadable-repo", "blocked", "repository_identity_unavailable", "repository_identity_unavailable"),
        ("provision-pending", "blocked", "workspace_provision_incomplete", "workspace_provision_incomplete"),
        ("provision-failed", "blocked", "workspace_provision_incomplete", "workspace_provision_incomplete"),
        ("preparation-failed", "blocked", "workspace_preparation_failed", "workspace_preparation_failed"),
    ],
)
async def test_workspace_readiness_cases(db, tmp_path, monkeypatch, case, state, reason, wait_reason):
    repo = tmp_path / "repo"
    repo.mkdir()
    path = tmp_path / "checkout"
    if case not in {"missing", "provision-pending", "provision-failed"}:
        path.mkdir()
    preset = AgentTeamPreset(name="Workspace readiness", created_by="test")
    db.add(preset)
    await db.flush()
    scope = TeamGithubScope(preset_id=preset.id, repo_owner="owner", repo_name="repo", repo_path=str(repo))
    db.add(scope)
    await db.flush()
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
        common = repo / ".git"
        if args[1] == str(repo):
            if case == "unreadable-repo":
                raise OSError("private diagnostic")
            return 0, f"{common}\n{common}\n{repo}"
        assert args[1] == str(path)
        if case == "unreadable-worktree":
            return 128, "private diagnostic"
        if case == "mismatch":
            common = tmp_path / "unrelated" / ".git"
        git_dir = common / "worktrees" / path.name
        if case == "standalone":
            git_dir = common = path / ".git"
        return 0, f"{git_dir}\n{common}\n{path}"

    monkeypatch.setattr(github_workspace_service, "_runner", git_read)
    result = await read_workspace_readiness(db, scope)
    row = next(row for row in result.workspaces if row.workspace_id == workspace.id)
    assert (row.state, row.reason, row.leased_issue_number) == (state, reason, None)
    assert "private diagnostic" not in result.model_dump_json()
    assert await explain_workspace_wait(db, scope) == wait_reason
