"""Claude Code PreToolUse guard for unattended Albiex Synth City team sessions.

It refuses interactive questions, and it refuses edit and state-changing Mail tools while the
member has an unread stop or hold message (#930). The second check asks Synth City over
loopback; it fails open with a visible warning when Synth City cannot answer.
"""
import argparse
import json
import os
import sys
import urllib.request

try:
    from mcp_shim.environment_warning import warn_legacy_environment
except ModuleNotFoundError:
    from environment_warning import warn_legacy_environment

MAIL_TOOL_PREFIX = "mcp__albiex-synth-city-mail__"
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
# Mail tools that only read stay allowed so a blocked member can always read its inbox.
_MAIL_READ_ONLY_PREFIXES = (
    "synth_city_get_", "synth_city_list_", "synth_city_render_", "synth_city_prepare_",
)
_MAIL_READ_ONLY_NAMES = frozenset({"synth_city_check_inbox", "synth_city_whoami"})
_GUARD_TIMEOUT_SECONDS = 1.0
_REASON_LIMIT = 300


def is_guarded_tool(tool_name) -> bool:
    """True for edit tools and for Mail tools that change state; never for read-only tools."""
    if not isinstance(tool_name, str):
        return False
    if tool_name in EDIT_TOOLS:
        return True
    if not tool_name.startswith(MAIL_TOOL_PREFIX):
        return False
    name = tool_name[len(MAIL_TOOL_PREFIX):]
    return name not in _MAIL_READ_ONLY_NAMES and not name.startswith(_MAIL_READ_ONLY_PREFIXES)


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


def _question_denial() -> dict:
    return _deny(
        "Albiex Synth City team sessions cannot use interactive question prompts. "
        "Use Agent Mail. Ask the Leader about assignments, plans, and sequencing. "
        "Ask the Supervisor about incidents, gates, harnesses, workspaces, and stalls. "
        "Use an operator action record for a retained operator decision. "
        "Continue other authorized work while you wait."
    )


def _guard_warning() -> dict:
    # No detail from the failure is echoed: it can carry a URL or a server message.
    return {"systemMessage": (
        "Agent Mail edit guard: Synth City did not answer, so unread stop or hold Mail was "
        "not checked for this tool call. Read your inbox before you continue."
    )}


def _ask_synth_city(url: str, payload: dict, tool_name: str, slot_id: int) -> dict:
    """Return the hook output Synth City chose, or raise when it cannot answer."""
    body = {
        "provider": "claude-code",
        "session_id": str(payload.get("session_id") or os.getppid()),
        "tool_name": tool_name,
        "team_slot_id": slot_id,
    }
    request = urllib.request.Request(
        f"{url.rstrip('/')}/api/v1/agent-mail/hooks/pre-tool-use",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=_GUARD_TIMEOUT_SECONDS) as response:  # noqa: S310
        answer = json.loads(response.read())
    if not isinstance(answer, dict):
        raise ValueError("unexpected answer")
    output = answer.get("hookSpecificOutput")
    if isinstance(output, dict) and output.get("permissionDecision") == "deny":
        return _deny(str(output.get("permissionDecisionReason") or "")[:_REASON_LIMIT])
    return {}


def main() -> int:
    warn_legacy_environment()
    marker = os.environ.get("SYNTH_CITY_TEAM_SLOT_ID", "")
    if not marker.isascii() or not marker.isdecimal() or int(marker) <= 0:
        return 0
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--synth-city-url", default=os.environ.get("SYNTH_CITY_URL", "http://127.0.0.1:8000"))
    arguments, _ = parser.parse_known_args()
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        payload = {}
    tool_name = payload.get("tool_name") if isinstance(payload, dict) else None
    if tool_name in {None, "AskUserQuestion"}:
        output = _question_denial()
    elif is_guarded_tool(tool_name):
        try:
            output = _ask_synth_city(arguments.synth_city_url, payload, tool_name, int(marker))
        except Exception:
            output = _guard_warning()
    else:
        return 0
    if output:
        print(json.dumps(output, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
