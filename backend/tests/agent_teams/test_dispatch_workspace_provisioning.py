"""Automatic worktrees are published only from an owned, isolated directory."""
from __future__ import annotations

import asyncio
import errno
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1 import agent_teams
from app.database import Base
from app.models.database import AgentTeamPreset, GithubWorkItem, GithubWorkspace, TeamGithubScope
from app.models.schemas import TeamGithubScopeCreate, TeamGithubScopeUpdate
from app.services.github_workspace_service import GithubWorkspaceError, GithubWorkspaceService


def git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path):
    group_root = tmp_path / "scopes"
    group_root.mkdir()
    repo = group_root / "repo"
    repo.mkdir()
    git(repo, "init", "--quiet")
    git(repo, "config", "user.email", "workspace-test@example.invalid")
    git(repo, "config", "user.name", "Workspace fixture")
    git(repo, "remote", "add", "origin", "https://github.com/example/fixture.git")
    (repo / "example.txt").write_text("fixture worktree\n", encoding="utf-8")
    git(repo, "add", "example.txt")
    git(repo, "commit", "--quiet", "-m", "Fixture")
    private_root = tmp_path / "controller"
    private_root.mkdir(mode=0o700)
    return SimpleNamespace(repo_path=str(repo), base_ref="HEAD"), group_root / "dispatch-1", private_root


@pytest.mark.asyncio
async def test_publishes_owned_checkout_with_valid_git_backlink(checkout):
    from app.services.dispatch_workspace_provisioning import provision_owned_worktree

    scope, destination, private_root = checkout
    identity = await provision_owned_worktree(GithubWorkspaceService(), scope, destination, staging_root=private_root)

    assert identity == (destination.stat().st_dev, destination.stat().st_ino)
    assert (destination / "example.txt").read_text(encoding="utf-8") == "fixture worktree\n"
    assert git(destination, "status", "--porcelain") == ""
    assert f"worktree {destination}" in git(Path(scope.repo_path), "worktree", "list", "--porcelain")
    assert list(private_root.iterdir()) == []
    assert destination.stat().st_gid == destination.parent.stat().st_gid


def test_atomic_publication_preserves_an_occupied_destination(tmp_path):
    from app.services.dispatch_workspace_provisioning import _open_directory, _rename_noreplace

    staged = tmp_path / "staged"
    existing = tmp_path / "destination"
    staged.mkdir()
    existing.mkdir()
    original_inode = existing.stat().st_ino

    with _open_directory(tmp_path) as parent:
        with pytest.raises(FileExistsError):
            _rename_noreplace(staged.name, parent, existing.name, parent)

    assert staged.is_dir()
    assert existing.stat().st_ino == original_inode


@pytest.mark.asyncio
async def test_git_runs_only_in_private_staging_when_publication_is_refused(checkout, monkeypatch):
    from app.services import dispatch_workspace_provisioning as provisioning

    scope, destination, private_root = checkout
    checked_stages = []

    def refuse_publication(source, source_fd, destination_name, destination_fd):
        assert destination_name == destination.name
        owned = Path("/proc/self/fd") / str(source_fd) / source
        assert (owned / "example.txt").read_text(encoding="utf-8") == "fixture worktree\n"
        assert owned.resolve().is_relative_to(private_root)
        checked_stages.append(owned.resolve())
        raise FileExistsError(errno.EEXIST, "Unrelated destination is owned elsewhere")

    monkeypatch.setattr(provisioning, "_rename_noreplace", refuse_publication)
    with pytest.raises(GithubWorkspaceError, match="occupied"):
        await provisioning.provision_owned_worktree(GithubWorkspaceService(), scope, destination,
            staging_root=private_root)

    assert len(checked_stages) == 1
    assert not destination.exists()


@pytest.mark.asyncio
async def test_failed_provision_preserves_unverified_public_path(checkout):
    from app.services.dispatch_workspace_provisioning import provision_owned_worktree

    scope, destination, private_root = checkout
    destination.mkdir()
    (destination / "retained.txt").write_text("owned by another workspace", encoding="utf-8")
    calls = []

    async def runner(arguments):
        calls.append(arguments)
        return 1, "Fixture Git should not run"

    with pytest.raises(GithubWorkspaceError, match="occupied"):
        await provision_owned_worktree(GithubWorkspaceService(runner), scope, destination, staging_root=private_root)

    assert calls == []
    assert list(destination.iterdir()) == [destination / "retained.txt"]
    assert (destination / "retained.txt").read_text(encoding="utf-8") == "owned by another workspace"


@pytest_asyncio.fixture
async def scope_database(tmp_path, monkeypatch, checkout):
    from app.services import dispatch_workspace_provisioning as provisioning

    scope, _, private_root = checkout
    provision = provisioning.provision_owned_worktree

    async def isolated_provision(service, source, destination):
        return await provision(service, source, destination, staging_root=private_root)

    async def trust_fixture(*args):
        return None

    async def skip_job_sync(*args):
        return None

    monkeypatch.setattr(provisioning, "provision_owned_worktree", isolated_provision)
    monkeypatch.setattr(provisioning, "prepare_dispatch_workspace_trust", trust_fixture, raising=False)
    monkeypatch.setattr(provisioning, "hold_code", lambda: None, raising=False)
    monkeypatch.setattr(agent_teams, "_sync_github_jobs", skip_job_sync)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'scope-fixture.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions.begin() as db:
        team = AgentTeamPreset(name="Workspace pool")
        db.add(team)
        await db.flush()
        team_id = team.id
    try:
        yield sessions, scope.repo_path, team_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_scope_create_grow_and_shrink_preserve_existing_leased_worktree(scope_database):
    from datetime import datetime

    sessions, repo, team_id = scope_database
    async with sessions() as db:
        scope = await agent_teams.create_github_scope(team_id, TeamGithubScopeCreate(repo_owner="example",
            repo_name="fixture", repo_path=repo, base_ref="HEAD", max_concurrent_dispatched=4), None, db)

        query = select(GithubWorkspace).where(GithubWorkspace.scope_id == scope.id).order_by(GithubWorkspace.id)
        workspaces = (await db.scalars(query)).all()
        assert [Path(row.path).name for row in workspaces] == [f"dispatch-{n}" for n in range(1, 5)]
        assert all(row.enabled and row.dispatchable and Path(row.path, "example.txt").is_file()
                   for row in workspaces)

        await agent_teams.update_github_scope(scope.id, TeamGithubScopeUpdate(max_concurrent_dispatched=5), None, db, None)
        workspaces = (await db.scalars(query)).all()
        assert len(workspaces) == 5
        last = workspaces[-1]
        assert last.enabled and last.dispatchable
        item = GithubWorkItem(scope_id=scope.id, issue_number=42, issue_title="Fixture lease",
            issue_url="https://github.com/example/fixture/issues/42", github_updated_at=datetime.utcnow())
        db.add(item)
        await db.flush()
        last.leased_item_id = item.id
        last.leased_at = datetime.utcnow()
        last.lease_token = "fixture-lease"
        leased_id, item_id, last_path = last.id, item.id, Path(last.path)
        await db.commit()

        await agent_teams.update_github_scope(scope.id, TeamGithubScopeUpdate(max_concurrent_dispatched=2), None, db, None)
        assert len((await db.scalars(query)).all()) == 5
        held = await db.get(GithubWorkspace, leased_id)
        assert held.enabled and held.leased_item_id == item_id and held.lease_token == "fixture-lease"
        assert (last_path / "example.txt").is_file()


@pytest.mark.asyncio
async def test_concurrent_pool_reservations_keep_unverified_checkout_disabled(scope_database, monkeypatch):
    from app.services import dispatch_workspace_provisioning as provisioning

    sessions, repo, team_id = scope_database
    async with sessions.begin() as db:
        scope = TeamGithubScope(preset_id=team_id, repo_owner="example", repo_name="fixture",
                                repo_path=repo, base_ref="HEAD", max_concurrent_dispatched=1)
        db.add(scope)
        await db.flush()
        scope_id = scope.id

    started, release = asyncio.Event(), asyncio.Event()
    original = provisioning.provision_owned_worktree

    async def wait_for_second_request(*args):
        started.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(provisioning, "provision_owned_worktree", wait_for_second_request)
    async def fill():
        async with sessions() as db:
            await provisioning.ensure_dispatch_capacity(db, scope_id, agent_teams._reserve_scope_writer)

    first = asyncio.create_task(fill())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        await asyncio.wait_for(fill(), timeout=5)
        async with sessions() as db:
            (pending,) = (await db.scalars(select(GithubWorkspace))).all()
            assert not pending.enabled and not pending.dispatchable
            with pytest.raises(HTTPException) as refused:
                await agent_teams.reprobe_github_workspace(scope_id, pending.id, None, db)
            assert refused.value.status_code == 409
    finally:
        release.set()
        await first

    async with sessions() as db:
        (ready,) = (await db.scalars(select(GithubWorkspace))).all()
        assert ready.enabled and ready.dispatchable and ready.provision_error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["scope", "trust", "capacity"])
async def test_scope_or_trust_changed_before_publication_keeps_workspace_disabled(scope_database, monkeypatch, change):
    from app.services import dispatch_workspace_provisioning as provisioning

    sessions, repo, team_id = scope_database
    async with sessions.begin() as db:
        scope = TeamGithubScope(preset_id=team_id, repo_owner="example", repo_name="fixture",
                                repo_path=repo, base_ref="HEAD", max_concurrent_dispatched=1)
        db.add(scope)
        await db.flush()
        scope_id = scope.id

    async def change_before_enable(*args):
        if change == "trust":
            raise GithubWorkspaceError("Fixture trust unavailable", "workspace_git_trust_unavailable")
        async with sessions.begin() as db:
            if change == "scope":
                (await db.get(TeamGithubScope, scope_id)).base_ref = "different"
            else:
                db.add(GithubWorkspace(scope_id=scope_id, path=str(Path(repo).parent / "other-worktree")))

    monkeypatch.setattr(provisioning, "prepare_dispatch_workspace_trust", change_before_enable)
    async with sessions() as db:
        await provisioning.ensure_dispatch_capacity(db, scope_id, agent_teams._reserve_scope_writer)
        workspace = await db.scalar(select(GithubWorkspace).where(GithubWorkspace.path.endswith("/dispatch-1")))
        assert not workspace.enabled and not workspace.dispatchable
        assert provisioning.automatic_provision_is_incomplete(workspace)
        assert Path(workspace.path, "example.txt").is_file()


@pytest.mark.asyncio
async def test_split_user_trust_uses_both_fixed_accounts_and_the_same_identity(tmp_path, monkeypatch):
    from app.services import dispatch_workspace_provisioning as provisioning

    current = os.geteuid()
    monkeypatch.setattr(provisioning.settings, "harness_agent_user", "deckproduct")
    monkeypatch.setattr(provisioning.settings, "team_workspace_trust_helper_enabled", True)
    monkeypatch.setattr(provisioning, "FACTORY_WORK_ROOT", tmp_path)
    monkeypatch.setattr(provisioning.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=current + 1))
    monkeypatch.setattr(provisioning.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path / "controller")))
    calls = []
    monkeypatch.setattr(provisioning, "prepare_dispatch_git_trust",
        lambda *args, **kwargs: calls.append(("controller", args, kwargs["user"].uid)))
    monkeypatch.setattr(provisioning, "prepare_dispatch_git_trust_via_helper",
        lambda *args: calls.append(("agent", args, None)))

    repo, destination = tmp_path / "scope" / "repo", tmp_path / "scope" / "dispatch-1"
    await provisioning.prepare_dispatch_workspace_trust(repo, destination, (12, 34))

    expected = (str(destination), str(repo), (12, 34))
    assert calls == [("controller", expected, current), ("agent", expected, None)]
