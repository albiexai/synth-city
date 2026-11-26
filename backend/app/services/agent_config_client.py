"""Controller adapter for the fixed, trust-only agent configuration helper."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from app.config import settings
from app.services.team_workspace_trust import _primary_checkout, _workspace_under_root

FACTORY_WORK_ROOT = Path("/home/deckproductctl/work")
_PROVIDERS = frozenset({"codex-cli", "claude-code"})
_COMMAND = ["/usr/bin/sudo", "-n", "-u", "deckproduct", "--",
            "/opt/albiex-synth-city-product/libexec/agent-config"]


class AgentConfigError(ValueError):
    code = "agent_config_refused"
    message = "Workspace trust could not be prepared. Check the agent configuration helper."

    def __init__(self) -> None:
        super().__init__(self.message)


class AgentConfigBusy(AgentConfigError):
    code = "agent_config_busy"
    message = "Folder trust is pending because the agent's configuration is busy. Launch again to retry."


def uses_agent_config(provider: str) -> bool:
    return settings.team_workspace_trust_helper_enabled and provider in _PROVIDERS


def resolve_claude_checkout(folder: str) -> str:
    """Use the writer's work-root checks and primary checkout resolution."""
    try:
        workspace = _workspace_under_root(Path(folder), FACTORY_WORK_ROOT)
        return str(_primary_checkout(workspace, work_root=FACTORY_WORK_ROOT))
    except (OSError, ValueError, subprocess.SubprocessError):
        raise AgentConfigError from None


def prepare_workspace_trust_via_helper(provider: str, folder: str) -> bool:
    if provider not in _PROVIDERS:
        raise AgentConfigError
    return _run_agent_config({"operation": "folder_trust", "provider": provider, "folder": folder})


def prepare_dispatch_git_trust_via_helper(folder: str, repo: str, identity: tuple[int, int]) -> bool:
    return _run_agent_config({"operation": "dispatch_git_trust", "folder": folder, "repo": repo,
                              "device": identity[0], "inode": identity[1]})


def _run_agent_config(request: dict[str, str | int]) -> bool:
    try:
        result = subprocess.run(
            _COMMAND, input=json.dumps(request), capture_output=True, encoding="utf-8", check=False,
            cwd="/", env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"}, timeout=6,
        )
        lines = result.stdout.splitlines()
        reply = json.loads(lines[0]) if len(lines) == 1 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        raise AgentConfigError from None
    if (result.returncode == 75 and isinstance(reply, dict) and reply.keys() == {"ok", "error"}
            and reply["ok"] is False and reply["error"] == "agent_config_busy"):
        raise AgentConfigBusy
    if (result.returncode == 0 and isinstance(reply, dict) and reply.keys() == {"ok", "changed"}
            and reply["ok"] is True and isinstance(reply["changed"], bool)):
        return reply["changed"]
    raise AgentConfigError
