"""Provision an owned dispatch worktree without writing into an occupied path."""
from __future__ import annotations

import asyncio
import ctypes
import os
import pwd
import re
import stat
import tempfile
from collections.abc import Awaitable, Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime
from itertools import count
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.database import GithubWorkspace, TeamGithubScope
from app.services.agent_config_client import FACTORY_WORK_ROOT, prepare_dispatch_git_trust_via_helper
from app.services.github_coordination_service import hold_code
from app.services.github_workspace_service import GithubWorkspaceError, GithubWorkspaceService, github_workspace_service
from app.services.team_workspace_trust import NativeUser, prepare_dispatch_git_trust

DirectoryIdentity = tuple[int, int]
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_AUTOMATIC_PREFIX = "workspace_auto_provision_"
_PENDING = _AUTOMATIC_PREFIX + "pending"


def _identity(info: os.stat_result) -> DirectoryIdentity:
    return info.st_dev, info.st_ino


@contextmanager
def _open_directory(path: Path) -> Iterator[int]:
    if not path.is_absolute() or ".." in path.parts:
        raise GithubWorkspaceError("A provisioning directory must have a canonical path", "workspace_path_changed")
    fd = os.open("/", _DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _verify_path(path: Path, expected: DirectoryIdentity) -> None:
    with _open_directory(path) as fd:
        if _identity(os.fstat(fd)) != expected:
            raise GithubWorkspaceError("A provisioning directory changed", "workspace_path_changed")


def _rename_noreplace(source: str, source_fd: int, destination_name: str, destination_fd: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(source_fd, os.fsencode(source), destination_fd, os.fsencode(destination_name), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


@contextmanager
def _owned_backlink(git_dir: Path, common_dir: Path, staged: Path) -> Iterator[BinaryIO]:
    if git_dir.parent != common_dir / "worktrees":
        raise GithubWorkspaceError("Git returned an unrelated worktree directory", "workspace_path_changed")
    with _open_directory(git_dir) as admin_fd:
        fd = os.open("gitdir", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=admin_fd)
        with os.fdopen(fd, "r+b") as stream:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                    or stream.read(8192) != os.fsencode(staged / ".git") + b"\n"):
                raise GithubWorkspaceError("Git's worktree backlink changed", "workspace_path_changed")
            yield stream


async def provision_owned_worktree(
    service: GithubWorkspaceService,
    scope: TeamGithubScope,
    destination: Path,
    *,
    staging_root: Path | None = None,
) -> DirectoryIdentity:
    """Stage Git privately and atomically publish into a free sibling name."""
    repo = Path(scope.repo_path)
    parent = destination.parent
    if parent != repo.parent or not re.fullmatch(r"dispatch-[1-9][0-9]*", destination.name):
        raise GithubWorkspaceError("Expected a numbered sibling dispatch workspace", "workspace_path_outside_root")
    try:
        with ExitStack() as stack:
            parent_fd = stack.enter_context(_open_directory(parent))
            repo_fd = stack.enter_context(_open_directory(repo))
            parent_stat = os.fstat(parent_fd)
            repo_identity = _identity(os.fstat(repo_fd))
            if staging_root is None:
                # A private single-user parent also permits a repository on a separate filesystem.
                parent_is_private = parent_stat.st_uid == os.geteuid() and not parent_stat.st_mode & 0o022
                staging_root = parent if parent_is_private else Path.home()
            staging_fd = stack.enter_context(_open_directory(staging_root))
            staging_stat = os.fstat(staging_fd)
            if (staging_stat.st_uid != os.geteuid() or staging_stat.st_mode & 0o022
                    or staging_stat.st_dev != parent_stat.st_dev):
                raise GithubWorkspaceError(
                    "No private staging directory on the workspace filesystem", "workspace_staging_unavailable")
            try:
                os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise GithubWorkspaceError("Dispatch workspace path is occupied", "workspace_path_occupied")

            protected_source = f"/proc/{os.getpid()}/fd/{repo_fd}"
            source = await service._probe_identity(protected_source)
            if source is None or Path(source[2]) != repo:
                raise GithubWorkspaceError("Scope checkout identity is unavailable", "workspace_path_changed")
            _verify_path(repo, repo_identity)
            private_path = Path(f"/proc/{os.getpid()}/fd/{staging_fd}")
            container = Path(tempfile.mkdtemp(prefix=".synth-worktree-", dir=private_path))
            container_fd = os.open(container.name, _DIRECTORY_FLAGS, dir_fd=staging_fd)
            try:
                os.mkdir(destination.name, mode=0o770, dir_fd=container_fd)
                owned_fd = os.open(destination.name, _DIRECTORY_FLAGS, dir_fd=container_fd)
                try:
                    os.fchown(owned_fd, -1, parent_stat.st_gid)
                    os.fchmod(owned_fd, 0o2770)
                    owned_identity = _identity(os.fstat(owned_fd))
                    staged_path = Path(f"/proc/{os.getpid()}/fd/{container_fd}") / destination.name
                    await service._provision_worktree(scope, str(staged_path), protected_source=protected_source)
                    staged = staged_path.resolve(strict=True)
                    _verify_path(staged, owned_identity)
                    result = await service._probe_identity(str(staged_path))
                    if result is None or result[1] != source[1] or Path(result[2]) != staged:
                        raise GithubWorkspaceError("Staged worktree identity changed", "workspace_path_changed")
                    _verify_path(repo, repo_identity)
                    _verify_path(parent, _identity(parent_stat))
                    with _owned_backlink(Path(result[0]), Path(result[1]), staged) as backlink:
                        _rename_noreplace(destination.name, container_fd, destination.name, parent_fd)
                        _verify_path(destination, owned_identity)
                        backlink.seek(0)
                        backlink.write(os.fsencode(destination / ".git") + b"\n")
                        backlink.truncate()
                    _verify_path(destination, owned_identity)
                finally:
                    os.close(owned_fd)
                # Only the protected, now-empty staging parent is ever removed.
                os.rmdir(container.name, dir_fd=staging_fd)
            finally:
                os.close(container_fd)
            return owned_identity
    except FileExistsError as exc:
        raise GithubWorkspaceError("Dispatch workspace path is occupied", "workspace_path_occupied") from exc
    except (OSError, AttributeError) as exc:
        # Git metadata or a successfully published directory may exist. Keep both for inspection.
        raise GithubWorkspaceError("Owned workspace could not be safely published", "workspace_path_changed") from exc


async def prepare_dispatch_workspace_trust(repo: Path, destination: Path, identity: DirectoryIdentity) -> None:
    """Set exact Git trust for the two configured factory users."""
    agent_name = (settings.harness_agent_user or "").strip()
    try:
        if not agent_name or pwd.getpwnam(agent_name).pw_uid == os.geteuid():
            # An owned checkout is already trusted on a single-user installation.
            return
        if (agent_name != "deckproduct" or not settings.team_workspace_trust_helper_enabled
                or not destination.is_relative_to(FACTORY_WORK_ROOT)):
            raise ValueError("The configured agent has no fixed Git trust helper")
        user = NativeUser(Path(pwd.getpwuid(os.geteuid()).pw_dir), os.geteuid(), {})
        await asyncio.to_thread(prepare_dispatch_git_trust, str(destination), str(repo), identity,
                                user=user, work_root=FACTORY_WORK_ROOT)
        await asyncio.to_thread(prepare_dispatch_git_trust_via_helper, str(destination), str(repo), identity)
    except (KeyError, OSError, ValueError) as exc:
        raise GithubWorkspaceError("Exact Git trust could not be verified for both users",
                                 "workspace_git_trust_unavailable") from exc


def automatic_provision_is_incomplete(workspace: GithubWorkspace) -> bool:
    return (workspace.provision_error or "").startswith(_AUTOMATIC_PREFIX)


def _scope_identity(scope: TeamGithubScope) -> tuple[str, str, str, str]:
    return scope.repo_owner, scope.repo_name, scope.repo_path, scope.base_ref


async def _pool(db: AsyncSession, scope_id: int) -> list[GithubWorkspace]:
    return list(await db.scalars(select(GithubWorkspace).where(
        GithubWorkspace.scope_id == scope_id, GithubWorkspace.kind == "worktree",
        or_(GithubWorkspace.enabled.is_(True) & GithubWorkspace.dispatchable.is_(True),
            GithubWorkspace.provision_error == _PENDING),
    ).order_by(GithubWorkspace.id).execution_options(populate_existing=True)))


@dataclass(frozen=True)
class DispatchWorktreeBinding:
    workspace_id: int
    path: str
    created_at: datetime
    updated_at: datetime
    pending: bool


async def dispatch_worktree_bindings(db: AsyncSession, scope_id: int) -> tuple[DispatchWorktreeBinding, ...]:
    return tuple(
        DispatchWorktreeBinding(row.id, row.path, row.created_at, row.updated_at, row.provision_error == _PENDING)
        for row in await _pool(db, scope_id)
    )


async def dispatch_worktrees_match_checkout(bindings: tuple[DispatchWorktreeBinding, ...], repo_path: str) -> bool:
    """Verify the existing pool against the proposed scope checkout before taking the writer."""
    if not bindings:
        return True
    if any(binding.pending or binding.path == repo_path for binding in bindings):
        return False
    try:
        proposed = await github_workspace_service._probe_identity(repo_path)
        if proposed is None or proposed[2] != repo_path:
            return False
        for binding in bindings:
            identity = await github_workspace_service._probe_identity(binding.path)
            if (identity is None or identity[1] != proposed[1] or identity[0] == identity[1]
                    or identity[2] != binding.path):
                return False
        return True
    except OSError:
        return False


async def ensure_dispatch_capacity(
    db: AsyncSession, scope_id: int, reserve_scope: Callable[[AsyncSession, int], Awaitable[TeamGithubScope]],
) -> None:
    """Reserve each missing slot in the database before a private filesystem write."""
    if hold_code():
        return
    initial = await db.get(TeamGithubScope, scope_id, populate_existing=True)
    if initial is None or not initial.enabled:
        return
    scope_identity = _scope_identity(initial)
    for _ in range(initial.max_concurrent_dispatched):
        scope = await reserve_scope(db, scope_id)
        if not scope.enabled or _scope_identity(scope) != scope_identity or hold_code():
            await db.commit()
            return
        if len(await _pool(db, scope_id)) >= scope.max_concurrent_dispatched:
            await db.commit()
            return

        repo = Path(scope.repo_path)
        prefix = str(repo.parent / "dispatch-")
        reserved = set(await db.scalars(select(GithubWorkspace.path).where(
            GithubWorkspace.path.startswith(prefix, autoescape=True))))
        destination = next(path for number in count(1)
            if not os.path.lexists(path := repo.parent / f"dispatch-{number}") and str(path) not in reserved)
        workspace = GithubWorkspace(
            scope_id=scope_id, path=str(destination), kind="worktree", enabled=False,
            dispatchable=False, provision_error=_PENDING,
        )
        db.add(workspace)
        await db.flush()
        workspace_id, created_at = workspace.id, workspace.created_at
        await db.commit()

        problem = None
        identity = None
        try:
            if hold_code():
                raise GithubWorkspaceError("Provisioning is on hold", "workspace_provision_held")
            identity = await provision_owned_worktree(github_workspace_service, scope, destination)
            if hold_code():
                raise GithubWorkspaceError("Provisioning is on hold", "workspace_provision_held")
            await prepare_dispatch_workspace_trust(repo, destination, identity)
        except GithubWorkspaceError as exc:
            problem = exc.block_code

        current = await reserve_scope(db, scope_id)
        reserved_workspace = await db.get(GithubWorkspace, workspace_id, populate_existing=True)
        if (reserved_workspace is None or reserved_workspace.scope_id != scope_id
                or reserved_workspace.created_at != created_at or reserved_workspace.path != str(destination)
                or reserved_workspace.provision_error != _PENDING or reserved_workspace.leased_item_id is not None
                or reserved_workspace.enabled or reserved_workspace.dispatchable):
            await db.commit()
            return
        if problem is None:
            if (_scope_identity(current) != scope_identity or not current.enabled or hold_code()
                    or sum(row.enabled for row in await _pool(db, scope_id)) >= current.max_concurrent_dispatched):
                problem = "workspace_capacity_changed"
            elif identity is None:
                problem = "workspace_path_changed"
            else:
                try:
                    _verify_path(destination, identity)
                except (OSError, GithubWorkspaceError):
                    problem = "workspace_path_changed"
        reserved_workspace.provision_error = _AUTOMATIC_PREFIX + "failed:" + problem if problem else None
        reserved_workspace.enabled = reserved_workspace.dispatchable = problem is None
        reserved_workspace.updated_at = datetime.utcnow()
        await db.commit()
        if problem:
            return
