"""Automatic worktrees are published only from an owned, isolated directory."""
from __future__ import annotations

import asyncio
import errno
import os
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta
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


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["staged", "common_dir", "source", "parent", "published"])
async def test_provision_refuses_changed_identity_at_each_boundary(checkout, monkeypatch, boundary):
    from app.services import dispatch_workspace_provisioning as provisioning

    scope, destination, private_root = checkout
    repo = Path(scope.repo_path)
    service = GithubWorkspaceService()
    original_verify = provisioning._verify_path
    original_probe = service._probe_identity
    original_backlink = provisioning._owned_backlink
    source_checks = 0
    backlink_open = False

    def verify(path, identity):
        nonlocal source_checks
        if path == repo:
            source_checks += 1
        if ((boundary == "staged" and path.is_relative_to(private_root))
                or (boundary == "source" and path == repo and source_checks > 1)
                or (boundary == "parent" and path == destination.parent)
                or (boundary == "published" and path == destination and backlink_open)):
            identity = (identity[0], -1)
        original_verify(path, identity)

    async def probe(path):
        result = await original_probe(path)
        if boundary == "common_dir" and Path(path).name == destination.name:
            git_dir, _, top = result
            return git_dir, str(private_root / "another-common-dir"), top
        return result

    @contextmanager
    def open_backlink(*args):
        nonlocal backlink_open
        assert boundary != "common_dir", "The probe must reject another common directory before opening a backlink"
        with original_backlink(*args) as stream:
            backlink_open = True
            try:
                yield stream
            finally:
                backlink_open = False

    monkeypatch.setattr(provisioning, "_verify_path", verify)
    monkeypatch.setattr(service, "_probe_identity", probe)
    monkeypatch.setattr(provisioning, "_owned_backlink", open_backlink)

    with pytest.raises(GithubWorkspaceError) as refused:
        await provisioning.provision_owned_worktree(service, scope, destination, staging_root=private_root)

    assert refused.value.block_code == "workspace_path_changed"
    assert f"worktree {destination}" not in git(repo, "worktree", "list", "--porcelain")
    assert destination.exists() == (boundary == "published")


@pytest.mark.parametrize("fault", ["content", "owner", "link_count"])
def test_worktree_backlink_requires_its_original_content_and_ownership(tmp_path, monkeypatch, fault):
    from app.services import dispatch_workspace_provisioning as provisioning

    common_dir = tmp_path / "git"
    git_dir = common_dir / "worktrees" / "fixture"
    git_dir.mkdir(parents=True)
    staged = tmp_path / "staged"
    backlink = git_dir / "gitdir"
    target = tmp_path / "different-checkout" if fault == "content" else staged
    content = os.fsencode(target / ".git") + b"\n"
    backlink.write_bytes(content)
    original = backlink.stat()
    fstat = os.fstat

    def observed_stat(fd):
        info = fstat(fd)
        if (info.st_dev, info.st_ino) != (original.st_dev, original.st_ino):
            return info
        return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid + int(fault == "owner"),
                               st_nlink=info.st_nlink + int(fault == "link_count"))

    monkeypatch.setattr(provisioning.os, "fstat", observed_stat)
    with pytest.raises(GithubWorkspaceError):
        with provisioning._owned_backlink(git_dir, common_dir, staged):
            pytest.fail("An unverified backlink was opened for writing")

    assert backlink.read_bytes() == content


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [0o770, 0o702])
async def test_worktree_staging_directory_must_be_private(checkout, mode):
    from app.services.dispatch_workspace_provisioning import provision_owned_worktree

    scope, destination, private_root = checkout
    private_root.chmod(mode)

    async def runner(_arguments):
        pytest.fail("Git ran without a private staging directory")

    with pytest.raises(GithubWorkspaceError) as refused:
        await provision_owned_worktree(GithubWorkspaceService(runner), scope, destination, staging_root=private_root)

    assert refused.value.block_code == "workspace_staging_unavailable"
    assert list(private_root.iterdir()) == []
    assert not destination.exists()


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
@pytest.mark.parametrize("change", ["different_clone", "same_common_dir", "pool_changed", "registered_worktree", "resolved_path"])
async def test_scope_growth_checks_existing_worktrees_against_the_proposed_checkout(scope_database, monkeypatch, change):
    from app.services import dispatch_workspace_provisioning as provisioning

    sessions, repo_path, team_id = scope_database
    repo = Path(repo_path)
    alternate = repo.parent / "alternate-checkout"
    if change == "different_clone":
        git(repo, "clone", "--quiet", str(repo), str(alternate))
        git(alternate, "remote", "set-url", "origin", git(repo, "remote", "get-url", "origin"))
    elif change != "registered_worktree":
        git(repo, "worktree", "add", "--quiet", "--detach", str(alternate))

    async with sessions() as db:
        scope = await agent_teams.create_github_scope(team_id, TeamGithubScopeCreate(repo_owner="example",
            repo_name="fixture", repo_path=str(repo), base_ref="HEAD", max_concurrent_dispatched=1), None, db)
        scope_id = scope.id
        query = select(GithubWorkspace).where(GithubWorkspace.scope_id == scope_id).order_by(GithubWorkspace.id)
        (original,) = (await db.scalars(query)).all()
        original_id = original.id
        if change == "registered_worktree":
            alternate = Path(original.path)
        untouched = Path(original.path) / "retained.txt"
        untouched.write_text("Prior worktree contents", encoding="utf-8")
        request = TeamGithubScopeUpdate(repo_path=str(alternate), max_concurrent_dispatched=2)

        if change == "resolved_path":
            normalize = agent_teams.agent_team_service.normalize_repo_path
            checked = False

            def normalize_once(path):
                nonlocal checked
                if path == str(alternate):
                    assert not checked, "The saved checkout must use the path validated by the preflight"
                    checked = True
                return normalize(path)

            monkeypatch.setattr(agent_teams.agent_team_service, "normalize_repo_path", normalize_once)

        if change == "pool_changed":
            original_probe = provisioning.github_workspace_service._probe_identity

            async def probe_with_pool_change(path):
                identity = await original_probe(path)
                if path == str(alternate):
                    async with sessions.begin() as concurrent:
                        row = await concurrent.get(GithubWorkspace, original_id)
                        row.updated_at += timedelta(seconds=1)
                return identity

            monkeypatch.setattr(provisioning.github_workspace_service, "_probe_identity", probe_with_pool_change)

        compatible = change in {"same_common_dir", "resolved_path"}
        if compatible:
            await agent_teams.update_github_scope(scope_id, request, None, db, None)
        else:
            with pytest.raises(HTTPException) as refused:
                await agent_teams.update_github_scope(scope_id, request, None, db, None)
            assert refused.value.status_code == 409
            expected_block = ("scope_workspace_pool_changed" if change == "pool_changed"
                              else "scope_workspace_repository_mismatch")
            assert refused.value.detail == expected_block

        current = await db.get(TeamGithubScope, scope_id, populate_existing=True)
        expected_root = alternate if compatible else repo
        expected_size = 2 if compatible else 1
        assert current.repo_path == str(expected_root)
        assert current.max_concurrent_dispatched == expected_size
        rows = (await db.scalars(query)).all()
        assert len(rows) == expected_size and rows[0].id == original_id
        assert untouched.read_text(encoding="utf-8") == "Prior worktree contents"
        active_common = git(expected_root, "rev-parse", "--path-format=absolute", "--git-common-dir")
        for row in rows:
            assert row.enabled and row.dispatchable and row.leased_item_id is None
            assert git(Path(row.path), "rev-parse", "--path-format=absolute", "--git-common-dir") == active_common


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
@pytest.mark.parametrize("change", ["scope", "trust", "capacity", "directory", "leased_row", "replaced_row"])
async def test_scope_or_trust_changed_before_publication_keeps_workspace_disabled(scope_database, monkeypatch, change):
    from app.services import dispatch_workspace_provisioning as provisioning

    sessions, repo, team_id = scope_database
    async with sessions.begin() as db:
        scope = TeamGithubScope(preset_id=team_id, repo_owner="example", repo_name="fixture",
                                repo_path=repo, base_ref="HEAD", max_concurrent_dispatched=1)
        db.add(scope)
        await db.flush()
        scope_id = scope.id

    verify = provisioning._verify_path
    ending_identity = None

    def verify_at_finalization(path, identity):
        if ending_identity is not None and path == ending_identity:
            identity = (identity[0], -1)
        verify(path, identity)

    async def change_before_enable(_repo, path, _identity):
        nonlocal ending_identity
        if change == "trust":
            raise GithubWorkspaceError("Fixture trust unavailable", "workspace_git_trust_unavailable")
        if change == "directory":
            ending_identity = path
            return
        async with sessions.begin() as db:
            if change == "scope":
                (await db.get(TeamGithubScope, scope_id)).base_ref = "different"
            elif change == "capacity":
                db.add(GithubWorkspace(scope_id=scope_id, path=str(Path(repo).parent / "other-worktree")))
            else:
                row = await db.scalar(select(GithubWorkspace).where(GithubWorkspace.path == str(path)))
                if change == "replaced_row":
                    row.created_at += timedelta(seconds=1)
                else:
                    item = GithubWorkItem(scope_id=scope_id, issue_number=42, issue_title="Fixture lease",
                        issue_url="https://github.com/example/fixture/issues/42", github_updated_at=datetime.utcnow())
                    db.add(item)
                    await db.flush()
                    row.leased_item_id = item.id

    monkeypatch.setattr(provisioning, "_verify_path", verify_at_finalization)
    monkeypatch.setattr(provisioning, "prepare_dispatch_workspace_trust", change_before_enable)
    async with sessions() as db:
        await provisioning.ensure_dispatch_capacity(db, scope_id, agent_teams._reserve_scope_writer)
        workspace = await db.scalar(select(GithubWorkspace).where(GithubWorkspace.path.endswith("/dispatch-1")))
        assert not workspace.enabled and not workspace.dispatchable
        assert provisioning.automatic_provision_is_incomplete(workspace)
        if change in {"leased_row", "replaced_row"}:
            assert workspace.provision_error == provisioning._PENDING
        if change == "leased_row":
            assert workspace.leased_item_id is not None
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
