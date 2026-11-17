"""Detect a wake prompt left unsent in a pane's input box, and submit it with Enter only (#1061).

A wake types the prompt into the pane and then presses Enter. Sometimes the text is typed but the Enter
does not land, so the text sits in the input box and the member never sees a wake.

``unsent_wake_prompt_visible`` is a pure check of a captured screen. It reads only the active input box at
the bottom, never the scrollback, and it answers True only when the text in the box is exactly the wake
prompt. A partial text, the prompt plus other text, an empty box, a placeholder, a paste placeholder or the
prompt only in scrollback all answer False: a false negative costs nothing, an Enter on something else does.

``submit_unsent_wake_prompt`` sends one Enter after the same identity checks the wake transport makes, and
never types text. It is not called by anything yet; wiring it into the follow-up is separate work.

Limits, point in time: the checks verify the pane's identity, not what is happening in it. The text can change
between the last capture and the Enter. A paste placeholder is not recognized.
"""
from __future__ import annotations

import re
import subprocess
import time
from typing import Callable

from app.services.agent_mail_service import INBOX_CHECK_PROMPT
from app.utils import peer_process

SUPPORTED_PROVIDERS = {"claude-code", "codex-cli"}
MAX_SCREEN_CHARS = 65536
# The pause between the two captures. Text that is still there after it is not being typed right now.
RECHECK_DELAY_SECONDS = 0.25

_OSC = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_RULE = re.compile(r"^[─━]{10,}$")
# Below the Claude Code input box sit a few status lines. Anything deeper is scrollback.
_CLAUDE_STATUS_LINES = 6
# Below the Codex input box sit a blank line and a status line.
_CODEX_STATUS_LINES = 4


def _collapse(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").split())


def _plain_lines(screen: str) -> list[str]:
    plain = _CSI.sub("", _OSC.sub("", screen))
    lines = [line.rstrip() for line in plain.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _claude_box_text(lines: list[str]) -> str | None:
    """The text in the Claude Code input box: between the last two rule lines, after the glyph."""
    rules = [index for index, line in enumerate(lines) if _RULE.match(line.strip())]
    if len(rules) < 2:
        return None
    top, bottom = rules[-2], rules[-1]
    if len(lines) - 1 - bottom > _CLAUDE_STATUS_LINES:
        return None  # the last rules are in the scrollback, not around the active box
    box = lines[top + 1:bottom]
    if not box or not box[0].startswith("❯"):
        return None
    return " ".join([box[0][1:]] + [line.strip() for line in box[1:]])


def _codex_box_text(lines: list[str]) -> str | None:
    """The text in the Codex input box: the last prompt line and its indented continuation lines."""
    starts = [index for index, line in enumerate(lines) if line.startswith("›")]
    if not starts:
        return None
    start = starts[-1]
    end = start + 1
    while end < len(lines) and lines[end].strip() and lines[end].startswith("  "):
        end += 1
    if len(lines) - end > _CODEX_STATUS_LINES:
        return None  # the last prompt line is in the scrollback
    return " ".join([lines[start][1:]] + [line.strip() for line in lines[start + 1:end]])


def unsent_wake_prompt_visible(provider: str, screen: str, prompt: str = INBOX_CHECK_PROMPT) -> bool:
    """True only when the active input box holds exactly the wake prompt. Pure: it reads nothing else."""
    if provider not in SUPPORTED_PROVIDERS or not screen or len(screen) > MAX_SCREEN_CHARS:
        return False
    lines = _plain_lines(screen)
    text = _claude_box_text(lines) if provider == "claude-code" else _codex_box_text(lines)
    return text is not None and bool(_collapse(prompt)) and _collapse(text) == _collapse(prompt)


def _tmux(arguments: list[str]):
    return subprocess.run(["tmux", *arguments], capture_output=True, text=True, timeout=5, check=True)


def submit_unsent_wake_prompt(
    session,
    *,
    pane_start: str,
    prompt: str = INBOX_CHECK_PROMPT,
    transport_guard: Callable[[], bool] | None = None,
) -> bool:
    """Press Enter once if the wake prompt is still unsent in the pane. True only if the Enter was sent.

    Nothing is sent unless the pane is the exact bound process, the guard allows it, and two captures a
    moment apart both show the exact wake text in the input box. Any failure sends nothing.
    """
    provider = getattr(session, "provider", None)
    if provider not in SUPPORTED_PROVIDERS:
        return False
    if not session.tmux_target or not session.pane_id or session.pid is None:
        return False

    def same_pane() -> bool:
        if peer_process.pane_is_alive(session.pid, pane_start) is not True:
            return False
        if transport_guard is not None and not transport_guard():
            return False
        lookup = _tmux(["display-message", "-p", "-t", session.pane_id, "#{pane_id}|#{pane_pid}"])
        return lookup.stdout.strip() == f"{session.pane_id}|{session.pid}"

    def prompt_unsent() -> bool:
        shot = _tmux(["capture-pane", "-p", "-t", session.pane_id])
        return unsent_wake_prompt_visible(provider, shot.stdout, prompt)

    try:
        if not same_pane() or not prompt_unsent():
            return False
        time.sleep(RECHECK_DELAY_SECONDS)
        if not same_pane() or not prompt_unsent():
            return False
        _tmux(["send-keys", "-t", session.pane_id, "Enter"])
    except (OSError, subprocess.SubprocessError):
        return False
    return True
