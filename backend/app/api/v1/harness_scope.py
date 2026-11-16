"""Which OS users' global scope the Harnesses pages can show, and the directory each one reads."""
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.utils.path_utils import scope_users

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


@router.get("/harness-scope", response_model=HarnessScopeResponse)
async def get_harness_scope() -> HarnessScopeResponse:
    """The users whose global scope can be shown, the default one, and each user's resolved ~/.claude."""
    try:
        users = scope_users()
    except LookupError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    described = [_describe(u.name, u.home, u.is_controller, u.is_default) for u in users]
    return HarnessScopeResponse(users=described, default_user=next(u.name for u in users if u.is_default))
