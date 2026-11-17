"""Recognize a provider refusal or content-filter notice in a Codex pane (#1062).

Provenance. The three notice lines are the REAL capture quoted in issue #1062 (a Codex CLI pane, the v4.2 V1
incident, 2026-11-17). The screen around them in `codex-provider-refusal.txt` is the real idle Codex capture
`codex-idle-prompt.txt`; where the notice sits on that screen is derived, not observed. There is no real Claude
Code capture, so there is no Claude Code fixture text and the recognizer answers None for it.
"""
from pathlib import Path

import pytest

from app.services.agent_activity_service import recognize_native_input_wait, recognize_provider_refusal

SCREENS = Path(__file__).resolve().parents[1] / "fixtures/native_screens"


def screen(name: str) -> str:
    return (SCREENS / name).read_text(encoding="utf-8")


REFUSAL = screen("codex-provider-refusal.txt")
IDLE = screen("codex-idle-prompt.txt")
HEADER = "ⓘ This content can’t be shown"
DETAIL = "  We take extra care with some cybersecurity requests."
MORE = "  Learn more: https://help.openai.com/en/articles/20001326"
COMPOSER = "› Ask Codex to do anything"
FOOTER = "  GPT-6-Astra high · /home/…  ⚠ 1 warning · f2 to view"


def with_ansi(text: str) -> str:
    return "\n".join(f"\x1b[2m{line}\x1b[0m" for line in text.split("\n"))


def codex(text: str) -> str | None:
    return recognize_provider_refusal("codex-cli", text)


def test_the_real_notice_above_the_composer_is_recognized():
    assert HEADER in REFUSAL and DETAIL in REFUSAL and MORE in REFUSAL
    assert codex(REFUSAL) == "provider_refusal"


def test_recognition_ignores_terminal_escape_codes():
    assert codex(with_ansi(REFUSAL)) == "provider_refusal"


def test_a_straight_apostrophe_is_accepted():
    assert codex(REFUSAL.replace("can’t", "can't")) == "provider_refusal"


def test_the_learn_more_line_is_optional_because_the_url_can_change():
    assert codex(REFUSAL.replace(MORE + "\n", "")) == "provider_refusal"
    assert codex(REFUSAL.replace("20001326", "99999999")) == "provider_refusal"


def test_the_notice_last_in_the_screen_is_recognized():
    assert codex("\n".join([HEADER, DETAIL, MORE])) == "provider_refusal"


def test_the_last_notice_counts_when_there_are_two():
    two = REFUSAL.replace(COMPOSER, "› try again\n\n" + HEADER + "\n" + DETAIL + "\n" + COMPOSER)
    assert codex(two) == "provider_refusal"


@pytest.mark.parametrize("name", [
    "codex-idle-prompt.txt", "codex-sign-in.txt", "codex-unsent-wake-prompt.txt",
    "codex-submitted-wake-prompt-in-history.txt", "claude-idle-after-declined-question.txt",
    "claude-question-form.txt", "claude-trust-prompt.txt", "muse-idle-prompt.txt",
])
def test_other_real_screens_are_not_a_refusal(name):
    assert codex(screen(name)) is None


@pytest.mark.parametrize("provider", ["claude-code", "muse-cli", "pi-cli", "opencode-cli", "antigravity-cli", "unknown", ""])
def test_only_codex_is_recognized(provider):
    """No real Claude Code capture exists. Claude Code gets no recognizer and no invented fixture."""
    assert recognize_provider_refusal(provider, REFUSAL) is None


@pytest.mark.parametrize("text", ["", "  \n\n", "Unrecognized screen.", "x" * 70000])
def test_empty_unknown_and_oversized_screens_are_not_a_refusal(text):
    assert codex(text) is None


# ---- ordinary text that mentions the same words --------------------------------------------------------


@pytest.mark.parametrize("name,line", [
    ("assistant text", "• We take extra care with some cybersecurity requests."),
    ("assistant text, whole notice", "• ⓘ This content can’t be shown"),
    ("a user prompt", "› Please review the cybersecurity requests handling and explain why we take extra care."),
    ("a quoted notice", "> ⓘ This content can’t be shown"),
    ("an indented quote of the header", "  ⓘ This content can’t be shown"),
    ("a review sentence", "  This content can’t be shown to users who lack the role; we take extra care with some "
                          "cybersecurity requests."),
])
def test_ordinary_text_that_mentions_the_words_is_not_a_refusal(name, line):
    text = IDLE.replace(COMPOSER, line + "\n\n" + COMPOSER)
    assert codex(text) is None


def test_the_header_alone_is_not_a_refusal():
    assert codex(REFUSAL.replace(DETAIL + "\n", "").replace(MORE + "\n", "")) is None


def test_the_detail_without_the_header_is_not_a_refusal():
    assert codex(REFUSAL.replace(HEADER + "\n", "")) is None


def test_a_different_second_line_is_not_a_refusal():
    assert codex(REFUSAL.replace("cybersecurity requests", "billing requests")) is None


def test_a_review_that_quotes_both_lines_in_prose_is_not_a_refusal():
    prose = ("• The pane showed: " + HEADER + " " + DETAIL.strip() + "\n"
             "  so the turn stopped.")
    assert codex(IDLE.replace(COMPOSER, prose + "\n\n" + COMPOSER)) is None


# ---- a refusal the member has moved past -----------------------------------------------------------------


@pytest.mark.parametrize("name,after", [
    ("new assistant output", "• Checking the inbox."),
    ("a new work item", "• Working (3s • esc to interrupt)"),
    ("a later notice of another kind", "⚠ Something else happened"),
    ("a tool line", "• Ran git status"),
    ("a new top-level item with a continuation", "• Ran git status\n  └ clean"),
])
def test_a_notice_followed_by_new_activity_is_not_a_current_refusal(name, after):
    moved_on = REFUSAL.replace(COMPOSER, "› next prompt\n\n" + after + "\n\n" + COMPOSER)
    assert codex(moved_on) is None


def test_a_notice_that_has_scrolled_up_behind_output_is_not_a_current_refusal():
    output = "\n".join(f"• output {n}" for n in range(20))
    assert codex(REFUSAL.replace(COMPOSER, output + "\n\n" + COMPOSER)) is None


def test_the_composer_and_footer_after_the_notice_are_allowed():
    typed = REFUSAL.replace(COMPOSER, "› a half typed prompt\n  that wraps")
    assert codex(typed) == "provider_refusal"
    assert FOOTER in typed


# ---- the existing recognizer is untouched ---------------------------------------------------------------


def test_the_refusal_screen_is_not_an_input_wait():
    """The notice screen is idle with a composer: it is a different fact from a prompt that waits."""
    assert recognize_native_input_wait("codex-cli", REFUSAL) == "clear"


def test_the_whole_notice_indented_as_a_quoted_block_is_not_a_refusal():
    """A quote of the notice is indented. The real notice starts at column 0."""
    quoted = "\n".join("    " + line for line in (HEADER, DETAIL.strip(), MORE.strip()))
    assert codex(IDLE.replace(COMPOSER, quoted + "\n\n" + COMPOSER)) is None
    assert codex(IDLE.replace(COMPOSER, "  " + HEADER + "\n" + DETAIL + "\n" + MORE + "\n\n" + COMPOSER)) is None
