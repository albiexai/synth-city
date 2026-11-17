"""Which OS users' global scope the Harnesses pages can show, and the directory each one reads."""
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.models.schemas import ScopeInfo
from app.utils.path_utils import controller_user_name, get_user_home, scope_users

router = APIRouter()


class HarnessScopeUser(BaseModel):
    name: str
    is_controller: bool
    is_default: bool
    claude_dir: str
    exists: bool
    readable: bool
    writable: bool


class HarnessScopeResponse(BaseModel):
    users: list[HarnessScopeUser]
    default_user: str


def _describe(name: str, home: Path, is_controller: bool, is_default: bool) -> HarnessScopeUser:
    claude_dir = home / ".claude"
    # Writes create the directory, so a missing one is writable when its home is.
    probe = claude_dir if claude_dir.exists() else home
    return HarnessScopeUser(
        name=name,
        is_controller=is_controller,
        is_default=is_default,
        claude_dir=str(claude_dir),
        exists=claude_dir.exists(),
        readable=os.access(probe, os.R_OK | os.X_OK),
        writable=os.access(probe, os.W_OK | os.X_OK),
    )


def _can_list(folder: Path) -> bool:
    """Whether a folder the view lists can be listed. A folder that does not exist yet is empty, not unreadable.

    An existing folder needs read and search permission on itself. For one that is not there, the nearest existing
    ancestor must at least be searchable, so that creating or reaching it is possible. Permissions on the folders
    above an existing one do not matter beyond being able to reach it, which `exists()` already shows.
    """
    if folder.exists():
        return os.access(folder, os.R_OK | os.X_OK)
    for parent in folder.parents:
        if parent.exists():
            return os.access(parent, os.X_OK)
    return False


def current_scope(data_dir: Path | None = None) -> ScopeInfo:
    """Which user's home this request read, and whether what the view reads can be listed (#1066).

    `data_dir` is the folder the view lists (projects, plans, the Codex home), always one derived from the scoped
    home, never a path the request chose. Readability is that folder's alone: a `.claude` that can only be searched
    (mode 0111) still lets a view read its readable children, and the Codex plans never read `.claude` at all. When
    the folder exists but cannot be listed, `readable` is False: the answer is "cannot read", not "empty". With no
    data folder known (a configured plans folder, which is never opened), the same rule as /harness-scope judges
    `.claude`. `claude_dir` and `exists` always describe the user's `.claude`.
    """
    home = get_user_home()
    claude_dir = home / ".claude"
    user = next((u.name for u in scope_users() if u.home == home), controller_user_name())
    try:
        exists = claude_dir.exists()
        if data_dir is not None:
            readable = _can_list(data_dir)
        else:
            readable = os.access(claude_dir if exists else home, os.R_OK | os.X_OK)
    except OSError:
        exists, readable = False, False
    return ScopeInfo(user=user, claude_dir=str(claude_dir), exists=exists, readable=readable)


@router.get("/harness-scope", response_model=HarnessScopeResponse)
async def get_harness_scope() -> HarnessScopeResponse:
    """The users whose global scope can be shown, the default one, and each user's resolved ~/.claude."""
    try:
        users = scope_users()
    except LookupError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    described = [_describe(u.name, u.home, u.is_controller, u.is_default) for u in users]
    return HarnessScopeResponse(users=described, default_user=next(u.name for u in users if u.is_default))
