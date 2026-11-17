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


def current_scope(data_dir: Path | None = None) -> ScopeInfo:
    """Which user's home this request read, and whether what the view reads can be listed (#1066).

    `data_dir` is the folder the view lists (projects, plans, the Codex home), always one derived from the scoped
    home, never a path the request chose. When it exists but cannot be listed, `readable` is False: the answer is
    "cannot read", not "empty". The same os.access rule as /harness-scope decides the `.claude` part.
    """
    home = get_user_home()
    claude_dir = home / ".claude"
    user = next((u.name for u in scope_users() if u.home == home), controller_user_name())
    try:
        exists = claude_dir.exists()
        readable = os.access(claude_dir if exists else home, os.R_OK | os.X_OK)
        if readable and data_dir is not None and data_dir.exists():
            readable = os.access(data_dir, os.R_OK | os.X_OK)
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
