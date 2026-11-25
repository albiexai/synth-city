"""Prepare trust for the selected team workspace before native launch."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass

import tomlkit

_PROC = Path("/proc")
_CONFIG_KEYS = ("HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME")
_WRITE_LOCK = threading.Lock()
_ERROR = "Workspace trust could not be prepared. Check native configuration access."
FileIdentity = tuple[int, int, int, int, int, int]


@dataclass(frozen=True)
class NativeUser:
    home: Path
    uid: int
    environment: dict[str, str]


def native_user() -> NativeUser:
    """Read the owner and selected environment of the target tmux server."""
    result = subprocess.run(["tmux", "display-message", "-p", "#{pid}"],
                            capture_output=True, text=True, timeout=3)
    if result.returncode:
        # No server means new-session will start one as this process's user.
        if not any(reason in result.stderr.lower() for reason in (
                "no server running", "no such file or directory", "error connecting")):
            raise ValueError(_ERROR)
        environment = {key: os.environ[key] for key in _CONFIG_KEYS if key in os.environ}
        home = Path(environment.get("HOME") or pwd.getpwuid(os.getuid()).pw_dir)
        if not home.is_absolute():
            raise ValueError(_ERROR)
        return NativeUser(home, os.getuid(), environment)
    pid = int(result.stdout.strip())
    uid = (_PROC / str(pid)).stat().st_uid
    environment = {}
    for key in _CONFIG_KEYS:
        value = subprocess.run(["tmux", "show-environment", "-g", key],
                               capture_output=True, text=True, timeout=3)
        if value.returncode:
            if "unknown variable" in value.stderr.lower():
                continue
            raise ValueError(_ERROR)
        for line in value.stdout.splitlines():
            if line.startswith(key + "="):
                environment[key] = line[len(key) + 1:]
    home = Path(environment.get("HOME") or pwd.getpwuid(uid).pw_dir)
    if not home.is_absolute() or (_PROC / str(pid)).stat().st_uid != uid:
        raise ValueError(_ERROR)
    return NativeUser(home, uid, environment)


def _workspace_under_root(path: Path, root: Path) -> Path:
    if not path.is_absolute() or path == root or not path.is_relative_to(root) or ".." in path.parts:
        raise ValueError(_ERROR)
    for component in (path, *path.parents):
        if not stat.S_ISDIR(component.lstat().st_mode):
            raise ValueError(_ERROR)
    return path


def _primary_checkout(directory: Path, *, work_root: Path | None = None) -> Path:
    git = ["git"]
    environment = None
    if work_root is not None:
        # The caller already checked this exact workspace. Do not consult the
        # controller's environment or modify Git's persistent trust settings.
        git = ["/usr/bin/git", "-c", f"safe.directory={directory}", "-c", "core.fsmonitor=false"]
        environment = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_CONFIG_GLOBAL": "/dev/null"}
    result = subprocess.run([*git, "-C", str(directory), "worktree", "list", "--porcelain", "-z"],
                            env=environment, capture_output=True, text=True, timeout=3)
    if result.returncode:
        # Plain directories are valid team workspaces. Git errors are not.
        if "not a git repository" in result.stderr.lower():
            return directory
        raise ValueError(_ERROR)
    records = result.stdout.split("\0\0")
    if not records or "bare" in records[0].split("\0"):
        raise ValueError(_ERROR)
    primary = next((line.removeprefix("worktree ") for line in records[0].split("\0")
                    if line.startswith("worktree ")), None)
    if primary is None:
        raise ValueError(_ERROR)
    path = Path(primary)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError(_ERROR)
    return _workspace_under_root(path, work_root) if work_root is not None else path.resolve()


def _snapshot(path: Path) -> FileIdentity | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return _file_identity(info)


def _file_identity(info: os.stat_result) -> FileIdentity:
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(_ERROR)
    return info.st_ino, info.st_mtime_ns, info.st_size, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)


@contextmanager
def _pinned_config(path: Path, home: Path, uid: int) -> Iterator[Path]:
    """Pin the agent's directories without opening symbolic links."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open("/", flags)
    try:
        for part in home.parts[1:]:
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        if os.fstat(directory).st_uid != uid:
            raise ValueError(_ERROR)
        # Each helper invocation locks the same home inode. Claude does not
        # take this lock; the helper separately checks for a live session.
        fcntl.flock(directory, fcntl.LOCK_EX)
        if path.parent == home:
            yield _PROC / "self" / "fd" / str(directory) / path.name
        else:
            try:
                os.mkdir(".codex", mode=0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(".codex", flags, dir_fd=directory)
            try:
                if os.fstat(child).st_uid != uid:
                    raise ValueError(_ERROR)
                yield _PROC / "self" / "fd" / str(child) / path.name
            finally:
                os.close(child)
    finally:
        os.close(directory)


def _read_restricted(path: Path, before: FileIdentity) -> str:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), encoding="utf-8") as stream:
        if _file_identity(os.fstat(stream.fileno())) != before:
            raise ValueError(_ERROR)
        content = stream.read()
        if _file_identity(os.fstat(stream.fileno())) != before:
            raise ValueError(_ERROR)
        return content


def _atomic_write(
    path: Path, text: str, uid: int, before: FileIdentity | None, *,
    private: bool = False, before_replace: Callable[[], None] | None = None,
) -> None:
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.stat().st_uid != uid:
            os.chown(directory, uid, -1)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".deck-trust-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            mode = before[5] if before and not private else 0o600
            owner, group = (before[3], before[4]) if before else (uid, -1)
            if owner != os.getuid() or (group != -1 and group != os.getgid()):
                os.fchown(stream.fileno(), owner, group)
            os.fchmod(stream.fileno(), mode)
            stream.flush()
            os.fsync(stream.fileno())
        if before_replace is not None:
            before_replace()
        if _snapshot(path) != before:
            raise ValueError(_ERROR)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare_workspace_trust(
    provider: str, directory: str, *, user: NativeUser | None = None, work_root: Path | None = None,
    before_write: Callable[[], None] | None = None,
) -> bool:
    """Return whether trust changed. Never expose native configuration values."""
    if provider not in {"claude-code", "codex-cli", "antigravity-cli"}:
        return False
    try:
        restricted = work_root is not None
        user = user or native_user()
        if restricted and (user.uid != os.getuid() or not user.home.is_absolute() or ".." in user.home.parts
                           or provider not in {"codex-cli", "claude-code"}):
            raise ValueError(_ERROR)
        workspace = _workspace_under_root(Path(directory), work_root) if work_root is not None else (
            Path(directory).resolve(strict=True)
        )
        if not workspace.is_dir():
            raise ValueError(_ERROR)
        if provider == "claude-code":
            workspace = _primary_checkout(workspace, work_root=work_root)
            configured = None if restricted else user.environment.get("CLAUDE_CONFIG_DIR")
            path = Path(configured) / ".claude.json" if configured else user.home / ".claude.json"
        elif provider == "antigravity-cli":
            # Observed on agy 1.5.9 (#952) for a plain workspace directory: this entry registers the
            # workspace. Whether it also suppresses a trust prompt was not observed.
            path = user.home / ".gemini" / "antigravity-cli" / "settings.json"
        else:
            configured = None if restricted else user.environment.get("CODEX_HOME")
            path = (Path(configured) if configured else user.home / ".codex") / "config.toml"
        if not path.is_absolute():
            raise ValueError(_ERROR)
        with _WRITE_LOCK, ExitStack() as stack:
            if restricted:
                path = stack.enter_context(_pinned_config(path, user.home, user.uid))
            before = _snapshot(path)
            if before and before[3] != user.uid:
                raise ValueError(_ERROR)
            content = ""
            if before:
                content = _read_restricted(path, before) if restricted else path.read_text(encoding="utf-8")
            if provider == "claude-code":
                document = json.loads(content) if before else {}
                if not isinstance(document, dict):
                    raise ValueError(_ERROR)
                projects = document.setdefault("projects", {})
                if not isinstance(projects, dict):
                    raise ValueError(_ERROR)
                project = projects.setdefault(str(workspace), {})
                if not isinstance(project, dict):
                    raise ValueError(_ERROR)
                if project.get("hasTrustDialogAccepted") is True:
                    return False
                project["hasTrustDialogAccepted"] = True
                updated = json.dumps(document, indent=2) + "\n"
            elif provider == "antigravity-cli":
                # Only the trust entry is written. Onboarding, terms and data-use choices stay the user's.
                document = json.loads(content) if before else {}
                if not isinstance(document, dict):
                    raise ValueError(_ERROR)
                trusted = document.setdefault("trustedWorkspaces", [])
                if not isinstance(trusted, list) or not all(isinstance(entry, str) for entry in trusted):
                    raise ValueError(_ERROR)
                if str(workspace) in trusted:
                    return False
                trusted.append(str(workspace))
                updated = json.dumps(document, indent=2) + "\n"
            else:
                document = tomlkit.parse(content)
                projects = document.setdefault("projects", tomlkit.table())
                if not isinstance(projects, dict):
                    raise ValueError(_ERROR)
                project = projects.setdefault(str(workspace), tomlkit.table())
                if not isinstance(project, dict):
                    raise ValueError(_ERROR)
                if project.get("trust_level") == "trusted":
                    return False
                project["trust_level"] = "trusted"
                updated = tomlkit.dumps(document)
            if before_write is not None:
                before_write()
            if restricted and before:
                backup = path.with_name(path.name + ".synth-city.bak")
                backup_before = _snapshot(backup)
                if backup_before and backup_before[3] != user.uid:
                    raise ValueError(_ERROR)
                _atomic_write(backup, content, user.uid, backup_before, private=True)
            _atomic_write(path, updated, user.uid, before, before_replace=before_write)
            return True
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        raise ValueError(_ERROR) from None
