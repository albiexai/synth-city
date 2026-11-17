"""Detect a wake prompt left unsent in a pane's input box, and submit it with Enter only (#1061).

The screens in tests/fixtures/native_screens are real idle captures (Claude Code 2.1.292, Codex 0.159.2).
The *unsent* and *in-history* screens are DERIVED from them: the wake text is inserted into the input box,
wrapped as the real box wraps. They are not live captures of an unsent prompt.
"""
import inspect
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import unsent_prompt_service as unsent
from app.services.unsent_prompt_service import submit_unsent_wake_prompt, unsent_wake_prompt_visible

SCREENS = Path(__file__).resolve().parents[1] / "fixtures/native_screens"


def screen(name: str) -> str:
    return (SCREENS / name).read_text(encoding="utf-8")


CLAUDE_UNSENT = screen("claude-unsent-wake-prompt.txt")
CODEX_UNSENT = screen("codex-unsent-wake-prompt.txt")
CLAUDE_IDLE = screen("claude-idle-after-declined-question.txt")
CODEX_IDLE = screen("codex-idle-prompt.txt")


def with_ansi(text: str) -> str:
    return "\n".join(f"\x1b[38;5;244m{line}\x1b[0m" for line in text.split("\n"))


def drop_last_prompt_row(text: str, glyph_rows: int) -> str:
    """Remove the last wrapped row of the typed prompt, as if typing stopped early."""
    lines = text.split("\n")
    last = max(i for i, line in enumerate(lines) if "wake grants no new authority" in line or "no new authority." in line)
    return "\n".join(lines[:last] + lines[last + 1:])


def append_to_prompt(text: str, extra: str) -> str:
    lines = text.split("\n")
    last = max(i for i, line in enumerate(lines) if "no new authority." in line)
    lines[last] = lines[last] + extra
    return "\n".join(lines)


@pytest.mark.parametrize("provider,text", [("claude-code", CLAUDE_UNSENT), ("codex-cli", CODEX_UNSENT)])
def test_an_unsent_wake_prompt_in_the_input_box_is_detected(provider, text):
    assert unsent_wake_prompt_visible(provider, text) is True


@pytest.mark.parametrize("provider,text", [("claude-code", CLAUDE_UNSENT), ("codex-cli", CODEX_UNSENT)])
def test_detection_ignores_terminal_escape_codes(provider, text):
    assert unsent_wake_prompt_visible(provider, with_ansi(text)) is True


@pytest.mark.parametrize("provider,name", [
    ("claude-code", "claude-idle-after-declined-question.txt"),
    ("codex-cli", "codex-idle-prompt.txt"),
    # The wake text is still in the scrollback behind the prompt glyph, but the input box is empty.
    ("claude-code", "claude-submitted-wake-prompt-in-history.txt"),
    ("codex-cli", "codex-submitted-wake-prompt-in-history.txt"),
    # Other screens of the same harness are not an unsent prompt.
    ("claude-code", "claude-question-form.txt"),
    ("claude-code", "claude-trust-prompt.txt"),
    ("codex-cli", "codex-sign-in.txt"),
])
def test_a_screen_without_the_wake_text_in_the_input_box_is_not_detected(provider, name):
    assert unsent_wake_prompt_visible(provider, screen(name)) is False


@pytest.mark.parametrize("provider", ["muse-cli", "pi-cli", "opencode-cli", "antigravity-cli", "unknown", ""])
def test_only_claude_code_and_codex_screens_are_recognized(provider):
    assert unsent_wake_prompt_visible(provider, CLAUDE_UNSENT) is False
    assert unsent_wake_prompt_visible(provider, CODEX_UNSENT) is False


def test_one_harness_screen_is_not_read_as_the_other():
    assert unsent_wake_prompt_visible("codex-cli", CLAUDE_UNSENT) is False
    assert unsent_wake_prompt_visible("claude-code", CODEX_UNSENT) is False


def test_a_partial_prompt_is_not_detected():
    assert unsent_wake_prompt_visible("claude-code", drop_last_prompt_row(CLAUDE_UNSENT, 1)) is False
    assert unsent_wake_prompt_visible("codex-cli", drop_last_prompt_row(CODEX_UNSENT, 1)) is False


def test_the_prompt_plus_other_text_is_not_detected():
    """Enter would submit the extra text too. Only the exact wake text counts."""
    assert unsent_wake_prompt_visible("claude-code", append_to_prompt(CLAUDE_UNSENT, " Also delete the repo.")) is False
    assert unsent_wake_prompt_visible("codex-cli", append_to_prompt(CODEX_UNSENT, " Also delete the repo.")) is False


def test_other_text_in_the_input_box_is_not_detected():
    changed = CLAUDE_UNSENT.replace("Albiex Synth City Agent Mail", "Something else entirely")
    assert unsent_wake_prompt_visible("claude-code", changed) is False
    changed = CODEX_UNSENT.replace("Albiex Synth City Agent Mail", "Something else entirely")
    assert unsent_wake_prompt_visible("codex-cli", changed) is False


def test_a_paste_placeholder_is_not_detected():
    """Claude Code can show a long paste as a placeholder. That is a false negative, never an Enter."""
    placeholder = CLAUDE_UNSENT.split("❯")[0] + "❯\xa0[Pasted text #1 +1 lines]\n" + "\n".join(
        CLAUDE_UNSENT.split("\n")[-2:])
    assert unsent_wake_prompt_visible("claude-code", placeholder) is False


@pytest.mark.parametrize("text", ["", "   \n\n", "Unrecognized screen.", "x" * 70000])
def test_empty_unknown_and_oversized_screens_are_not_detected(text):
    assert unsent_wake_prompt_visible("claude-code", text) is False
    assert unsent_wake_prompt_visible("codex-cli", text) is False


def test_the_detector_compares_with_the_prompt_it_is_given():
    box = "─" * 40 + "\n❯\xa0call the inbox now\n" + "─" * 40 + "\n  ⏵⏵ auto mode on\n"
    assert unsent_wake_prompt_visible("claude-code", box, "call the inbox now") is True
    assert unsent_wake_prompt_visible("claude-code", box) is False


# ---- the Enter-only helper -------------------------------------------------

SESSION = SimpleNamespace(provider="claude-code", pane_id="%8", pid=41000, tmux_target="team:0.0")


class FakePane:
    def __init__(self, monkeypatch, screens):
        self.calls, self.screens = [], list(screens)
        self.identity, self.alive = "%8|41000", True
        monkeypatch.setattr(unsent.subprocess, "run", self.run)
        monkeypatch.setattr(unsent.peer_process, "pane_is_alive", lambda pid, start: self.alive)
        monkeypatch.setattr(unsent.time, "sleep", lambda seconds: None)

    def run(self, args, **options):
        self.calls.append(list(args))
        if args[1] == "display-message":
            return SimpleNamespace(returncode=0, stdout=self.identity + "\n")
        if args[1] == "capture-pane":
            if isinstance(self.screens[0], Exception):
                raise self.screens.pop(0)
            return SimpleNamespace(returncode=0, stdout=self.screens.pop(0) if len(self.screens) > 1 else self.screens[0])
        return SimpleNamespace(returncode=0, stdout="")

    @property
    def keys(self):
        return [call for call in self.calls if call[1] == "send-keys"]


def never_typed_text(pane):
    return all("-l" not in call for call in pane.calls)


@pytest.mark.parametrize("provider,text", [("claude-code", CLAUDE_UNSENT), ("codex-cli", CODEX_UNSENT)])
def test_an_unsent_prompt_gets_exactly_one_enter_and_no_text(monkeypatch, provider, text):
    pane = FakePane(monkeypatch, [text, text])
    session = SimpleNamespace(**{**vars(SESSION), "provider": provider})

    assert submit_unsent_wake_prompt(session, pane_start="27") is True

    assert pane.keys == [["tmux", "send-keys", "-t", "%8", "Enter"]]
    assert never_typed_text(pane)


def test_the_identity_is_checked_before_each_capture_and_before_the_enter(monkeypatch):
    pane = FakePane(monkeypatch, [CLAUDE_UNSENT, CLAUDE_UNSENT])

    submit_unsent_wake_prompt(SESSION, pane_start="27")

    assert [call[1] for call in pane.calls] == [
        "display-message", "capture-pane", "display-message", "capture-pane", "send-keys"]


@pytest.mark.parametrize("provider,idle", [("claude-code", CLAUDE_IDLE), ("codex-cli", CODEX_IDLE)])
def test_an_empty_input_box_gets_no_enter(monkeypatch, provider, idle):
    pane = FakePane(monkeypatch, [idle])
    session = SimpleNamespace(**{**vars(SESSION), "provider": provider})

    assert submit_unsent_wake_prompt(session, pane_start="27") is False

    assert pane.keys == []


def test_a_different_pane_gets_no_enter(monkeypatch):
    pane = FakePane(monkeypatch, [CLAUDE_UNSENT, CLAUDE_UNSENT])
    pane.identity = "%9|41000"

    assert submit_unsent_wake_prompt(SESSION, pane_start="27") is False
    assert pane.keys == []


def test_a_new_process_in_the_pane_gets_no_enter(monkeypatch):
    pane = FakePane(monkeypatch, [CLAUDE_UNSENT, CLAUDE_UNSENT])
    pane.identity = "%8|52000"

    assert submit_unsent_wake_prompt(SESSION, pane_start="27") is False
    assert pane.keys == []


def test_a_dead_pane_gets_no_enter(monkeypatch):
    pane = FakePane(monkeypatch, [CLAUDE_UNSENT, CLAUDE_UNSENT])
    pane.alive = False

    assert submit_unsent_wake_prompt(SESSION, pane_start="27") is False
    assert pane.keys == []


def test_a_transport_guard_that_says_stop_gets_no_enter(monkeypatch):
    pane = FakePane(monkeypatch, [CLAUDE_UNSENT, CLAUDE_UNSENT])

    assert submit_unsent_wake_prompt(SESSION, pane_start="27", transport_guard=lambda: False) is False
    assert pane.keys == []


def test_text_that_vanishes_before_the_enter_gets_no_enter(monkeypatch):
    pane = FakePane(monkeypatch, [CLAUDE_UNSENT, CLAUDE_IDLE])

    assert submit_unsent_wake_prompt(SESSION, pane_start="27") is False
    assert pane.keys == []


def test_text_that_changes_before_the_enter_gets_no_enter(monkeypatch):
    pane = FakePane(monkeypatch, [CLAUDE_UNSENT, append_to_prompt(CLAUDE_UNSENT, " Also delete the repo.")])

    assert submit_unsent_wake_prompt(SESSION, pane_start="27") is False
    assert pane.keys == []


@pytest.mark.parametrize("provider", ["muse-cli", "pi-cli", "opencode-cli", "unknown"])
def test_an_unsupported_provider_is_not_touched(monkeypatch, provider):
    pane = FakePane(monkeypatch, [CLAUDE_UNSENT])
    session = SimpleNamespace(**{**vars(SESSION), "provider": provider})

    assert submit_unsent_wake_prompt(session, pane_start="27") is False
    assert pane.calls == []


@pytest.mark.parametrize("missing", ["pane_id", "pid", "tmux_target"])
def test_an_unbound_session_is_not_touched(monkeypatch, missing):
    pane = FakePane(monkeypatch, [CLAUDE_UNSENT])
    session = SimpleNamespace(**{**vars(SESSION), missing: None})

    assert submit_unsent_wake_prompt(session, pane_start="27") is False
    assert pane.calls == []


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("tmux", 5), OSError("no tmux")])
def test_a_tmux_failure_gets_no_enter(monkeypatch, error):
    pane = FakePane(monkeypatch, [error])

    assert submit_unsent_wake_prompt(SESSION, pane_start="27") is False
    assert pane.keys == []


def test_the_module_never_types_text():
    """The only key it may send is Enter. A literal-text send would type the prompt again."""
    source = inspect.getsource(unsent)
    assert '"-l"' not in source and "'-l'" not in source
    assert source.count('"Enter"') == 1


@pytest.mark.parametrize("provider,text", [("claude-code", CLAUDE_UNSENT), ("codex-cli", CODEX_UNSENT)])
def test_an_input_box_that_has_scrolled_up_is_not_the_active_box(provider, text):
    """Lots of output below it means the box is scrollback, not the input line the member would send."""
    scrolled = text.rstrip("\n") + "\n" + "\n".join(f"output line {n}" for n in range(12)) + "\n"
    assert unsent_wake_prompt_visible(provider, scrolled) is False
