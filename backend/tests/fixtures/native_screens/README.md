# Native harness screens

Fixtures for #549 (scope 8): recognizing a team pane that waits at an interactive screen. Each file is one `tmux capture-pane` of a native session. `.txt` is plain text (`capture-pane -p`), and `.ansi` keeps the escape codes (`capture-pane -p -e`).

| File | Harness | Expected state |
|---|---|---|
| `claude-question-form.txt` / `.ansi` | Claude Code 2.1.292 | waiting: question form (one question, "↑/↓ to navigate") |
| `claude-question-form-two-questions.txt` | Claude Code 2.1.292 | waiting: question form (two questions, "Tab/Arrow keys to navigate", wrapped footer) |
| `claude-trust-prompt.txt` / `.ansi` | Claude Code 2.1.292 | waiting: workspace trust prompt |
| `claude-trust-prompt-with-permissions.txt` | Claude Code 2.1.292 | waiting: trust prompt with the "pre-approves tool permissions" warning |
| `codex-sign-in.txt` / `.ansi` | Codex CLI 0.159.2 | waiting: sign-in screen |
| `claude-idle-after-declined-question.txt` | Claude Code 2.1.292 | **not** waiting: the normal prompt. The scrollback still mentions the declined question |
| `codex-idle-prompt.txt` | Codex CLI 0.159.2 | **not** waiting: the normal prompt |
| `claude-unsent-wake-prompt.txt` | Claude Code 2.1.292 | **derived**: an unsent wake prompt in the input box |
| `claude-submitted-wake-prompt-in-history.txt` | Claude Code 2.1.292 | **derived**: the wake text sits in scrollback behind `❯`, the input box is empty |
| `codex-unsent-wake-prompt.txt` | Codex CLI 0.159.2 | **derived**: an unsent wake prompt in the input box |
| `codex-submitted-wake-prompt-in-history.txt` | Codex CLI 0.159.2 | **derived**: the wake text sits in scrollback behind `›`, the input box is empty |

How they were made (2026-10-13, by the v3.1 supervisor):
- Live captures: each harness ran in a **private tmux server** with a throwaway home, never in the team's server, with no Agent Mail server configured. The Codex sign-in screen needs `--no-daemon` with a fresh `CODEX_HOME`. `codex-idle-prompt.txt` is a read-only capture of an idle team pane.
- `claude-question-form-two-questions.txt` and `claude-trust-prompt-with-permissions.txt` are transcribed from live team panes (2026-10-12). They are plain text only.
- Sanitized: temporary paths are `<workspace>`, and home paths are `/home/<user>`.

Recognize the **active** prompt area at the bottom of the screen, not the scrollback. Screens change between CLI versions, so record the version with each new capture.

Derived fixtures (#1061, 2026-11-17, by the v4.3 builder B2): the four `*-wake-prompt*` files are **not live captures**. Each starts from the real idle capture above and inserts the wake prompt (`INBOX_CHECK_PROMPT`) into the input box, wrapped the way that box wraps (Claude Code: 120 columns, 2-space continuation lines between the two `─` rules; Codex: 80 columns, 2-space continuation lines under `›`). The `-in-history` files put the same text in the scrollback behind the prompt glyph, which is the worst case for a false positive, and keep the real empty input box at the bottom. No live CLI was run for them. Replace them with live captures when an isolated runtime is available. Two display cases are not covered: Claude Code may show a long typed prompt as a `[Pasted text ...]` placeholder, and a screen that is busy with output.
