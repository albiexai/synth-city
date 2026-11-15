# Antigravity CLI harness support: spike spec

Date: 2026-11-15. Status: **draft checkpoint, not a finished spike.** Issue: #734. Target: release/v4.2.

This checkpoint records what Google's public documentation and terms say, and what is still unknown. It contains no live
observation: no `agy` binary was installed or run, nobody signed in, and no model call was made. Questions 2–7 of #734
therefore stay **open**. This document does not complete #734, and it does not unblock #735–#738.

## Goal

Deck runs Google's Antigravity CLI (`agy`, provider id `antigravity-cli`) as a team member, with the same supervision as
the other harnesses. Antigravity CLI is a Go binary, separate from the Node-based Gemini CLI (`gemini`), although both
keep files under `~/.gemini/`. The model for this spec is the Muse Code spec
(`docs/superpowers/specs/2026-10-14-muse-code-harness-design.md`): a design only after live observation.

## Evidence classes

Every claim below carries one of these labels.

| Label | Meaning |
|---|---|
| **official** | From a Google page on `antigravity.google`, fetched 2026-11-15. Text in quotation marks was extracted from the fetched page; everything else is a paraphrase. A reviewer should check each quote against the live page, because Google can change these pages without notice. |
| **issue** | From the #734 issue body: checked on `agy` 1.3.5 on the Director's machine without a model call, or read from Antigravity's bundled docs. Not re-checked here. |
| **interpretation** | The author's reading of an official text. It is not a legal opinion and not a Google statement. |
| **unofficial** | Third-party guides or Google developer-forum posts. Context only; never relied on. |
| **unknown** | No source found. Needs live observation. |

Live observation from a member in tmux, which #734's "Done when" requires, is **not present for any question**.

## Q1: subscription terms

**Status: answered from the published terms; the use decision is open for the Director.**

### What the terms say (official)

- Google Antigravity Additional Terms of Service, opening clause, before section 1 (<https://antigravity.google/terms>):
  > "If you are accessing the Service through Gemini Enterprise (Google Cloud), Gemini Enterprise for Business or a
  > Google Workspace subscription on the Google Cloud Pre-GA Offering Terms, or with an API Key in Gemini for business,
  > then you are subject to the terms of use accepted or signed by your administrator applicable to such service
  > (including its applicable terms for downloadable software)."
- The same terms, section 6 (<https://antigravity.google/terms>; the page shows no
  effective date):
  > "You must not abuse, harm, interfere with, or disrupt the Service. This includes, but is not limited to, using the
  > Service in connection with products not provided by us. Using third party software, tools, or services to access
  > the Service (e.g. using OpenClaw with Antigravity OAuth) is a breach of this Agreement. Such actions may be grounds
  > for suspension or termination of your Antigravity and/or Gemini CLI accounts."
- Antigravity FAQ (<https://antigravity.google/docs/faq>):
  > "Using third-party software, tools, or services to access Antigravity is a violation of our Terms of Service and
  > severely degrades the experience for legitimate product users."

  The FAQ names Claude Code, OpenClaw and OpenCode as examples, and says that such use "can result in suspension or
  termination of your account." For using a third-party coding agent with Gemini, the same answer recommends "a Gemini
  Enterprise or Google AI Studio API key" ([FAQ answer](https://antigravity.google/docs/faq#why-cant-i-use-third-party-software-such-as-claude-code-openclaw-or-opencode-with-my-antigravity-login)).
- The same FAQ, on headless mode:
  > "Antigravity CLI headless mode (`agy -p`) is intended for direct scripting and CI workflows. Wrapping `agy` inside
  > third-party coding agents or applications, or automating account rotation, is not supported under consumer plans."
- Installation and auth page (<https://antigravity.google/docs/install?tab=cli>) lists the supported accounts: personal
  Google accounts (Free, Pro and Ultra plans), Gemini Enterprise and Google Cloud accounts, and Gemini API key
  authentication. A Gemini API key is used by setting `modelProvider` to `gemini` in
  `~/.gemini/antigravity-cli/settings.json` and exporting `GEMINI_API_KEY`. Vertex AI and enterprise use is enabled by
  `AGY_ADC_AUTH=true` after `gcloud auth application-default login`.

### Interpretation

- A Deck team member is `agy` started, prompted and supervised by Deck in a tmux pane. That is close to the FAQ's
  "wrapping `agy` inside third-party coding agents or applications", which "is not supported under consumer plans".
  Section 6 adds "using the Service in connection with products not provided by us".
- So running a Deck member on the **Director's consumer subscription** (Google AI Pro or Ultra sign-in) carries a real
  risk of the account being suspended or terminated. This is unlike Muse Code, whose terms permit use "with Muse Code
  only" and so allow Deck to launch Muse Code itself.
- Google itself names alternatives: the FAQ recommends a Gemini Enterprise or Google AI Studio API key for
  third-party coding agents. The terms' opening clause puts access through Gemini Enterprise (Google Cloud), Gemini
  Enterprise for Business, a Google Workspace subscription on the Pre-GA Offering Terms, or "an API Key in Gemini for
  business" under the terms the administrator accepted for that service. So section 6 is the governing text for
  consumer sign-ins; for those enterprise and business routes, the administrator's terms govern.
- The routes are distinct, and each has its own governing terms:
  - consumer sign-in (Free, Pro, Ultra): these Additional Terms, section 6 and the FAQ apply;
  - Google AI Studio API key (`modelProvider` `gemini` and `GEMINI_API_KEY`): recommended by the FAQ; its governing terms
    (the Gemini API terms) are **not reviewed here**;
  - API key in Gemini for business, Gemini Enterprise, Workspace: the administrator's accepted terms, **not reviewed
    here**;
  - Vertex AI with `AGY_ADC_AUTH`: Google Cloud terms, **not reviewed here**.
- The FAQ recommendation does not establish that every enterprise or API-key deployment of a Deck member is permitted.
  That depends on the chosen route's own terms.
- Deck does not extract or proxy the credential: `agy` keeps its own sign-in in the operating system keyring
  (official, install page). The terms quoted above do not make that distinction for "wrapping".

### Unofficial context (not relied on)

The Google AI developer forum (`discuss.ai.google.dev`) has many threads asking whether a local orchestrator may
launch the official `agy` with a subscription sign-in. Several posts report accounts disabled with "Service disabled
for violation of Terms of Service" (HTTP 403) after automated use. The answers in those threads are informal, and none
is an authoritative Google statement about a case like Deck's.

### Decision needed (Director)

Before any live member runs, the Director decides:

1. Whether Deck may run `agy` on a consumer subscription sign-in at all, given section 6 and the FAQ. The author's
   recommendation is **no**.
2. If not, which route the spike and the provider use: a Google AI Studio API key, an API key in Gemini for business,
   Gemini Enterprise, or Vertex AI. The chosen route's own terms must then be reviewed and quoted here.
3. Who signs in. The sign-in is interactive (browser, or a device code over SSH: official, install page). An agent never
   handles the credential.

## Q2–Q7: what is documented and what is still unknown

None of these has live evidence. Each row states the source of every claim.

### Q2: hooks

| Point | Finding | Label |
|---|---|---|
| Global hooks file | `~/.gemini/config/hooks.json` | official (<https://antigravity.google/docs/hooks>), issue |
| Workspace hooks file | `.agents/hooks.json` at the project root; a plugin's own `hooks.json` also loads | official |
| Not loaded | Hooks placed in `~/.gemini/antigravity-cli/settings.json` | official |
| Layout | Named top-level groups, then the event: `{"my-linter-hook": {"PostToolUse": [{"matcher": "run_command", "hooks": [{"type": "command", "command": "./scripts/lint.sh", "timeout": 10}]}]}}`. This differs from Claude Code's and Muse's `{"hooks": {...}}` layout. | official |
| Events | `SessionStart`, `PreToolUse`, `PostToolUse`, `PreInvocation`, `PostInvocation`, `Stop`. No `UserPromptSubmit`. | official, issue |
| `SessionStart` payload | `conversationId`, `workspacePaths`, `transcriptPath`, `artifactDirectoryPath`, `modelName`, `lastUserInput` | official |
| `PreToolUse` payload | adds `toolCall` (`name`, `args`) and `stepIdx` | official |
| `Stop` payload | adds `executionNum`, `terminationReason`, `error`, `fullyIdle` | official |
| `PreToolUse` decision | output field `decision`: `allow`, `auto_approve`, `deny`, `ask`, `force_ask`, `deny_unless_prior_grant` | official |
| Matchers | `""` or `*` for all tools, exact names, `a\|b`, regex such as `browser_.*`; ignored for `SessionStart`, `PreInvocation`, `PostInvocation` and `Stop` | official |
| `SessionStart` on a new conversation versus `--conversation <id>` | unknown | unknown |
| Which hooks actually fire in the CLI | unknown. Forum posts report `Stop` and `PostToolUse` not firing in the Antigravity IDE. | unknown, unofficial |

### Q3: questions

| Point | Finding | Label |
|---|---|---|
| Tool | `ask_question`, "Ask multiple-choice questions", with argument `questions` (each with `question`, `options`, `is_multi_select`). The Hooks page lists it among the tools that hook matchers can name ([Supported tools, Interaction and media](https://antigravity.google/docs/hooks#interaction-and-media)). | official, issue |
| Name seen by `PreToolUse` | Documented as `ask_question` (above). Whether `PreToolUse` actually fires for it in the CLI, and whether a `deny` decision then blocks the question, is unknown. Muse's `PreToolUse` does not fire for its question tool. | official (documented name); firing and denial unknown |
| `permissions.deny` | The documented rule actions are `read_file`, `write_file`, `read_url`, `execute_url`, `command`, `unsandboxed` and `mcp` (<https://antigravity.google/docs/permissions?tab=cli>). No action covers a question tool. | official; blocking it is unknown |
| Rule precedence | "Your fine-grained `allow`, `ask`, and `deny` rules always take precedence (`Deny > Ask > Allow`)." ([Execution modes and the terminal sandbox](https://antigravity.google/docs/permissions?tab=cli#execution-modes-and-the-terminal-sandbox)) | official |
| `--dangerously-skip-permissions` | A separate behaviour: it sets the `toolPermission` setting to `always-proceed`, and "`strict` and `always-proceed` (`--dangerously-skip-permissions`) take precedence over `auto` mode." On the page as fetched on 2026-11-15, it does not override explicit `deny` rules. The page names no version, and it can change. Whether the flag suppresses `ask_question`, or affects a hook's `deny`, is not stated. A permission override is not evidence that the question screen is suppressed. | official; effect on questions and hooks unknown |
| Open question in the transcript, the summaries database and on screen | unknown | unknown |

### Q4: identity and resume

| Point | Finding | Label |
|---|---|---|
| Conversation id from a hook | `SessionStart` carries `conversationId` and `transcriptPath` | official |
| Conversation id from state | `~/.gemini/antigravity-cli/conversations/<uuid>.db` and `conversation_summaries.db` with `workspace_uris` | issue |
| Resume | `--conversation <id>`, "Resume a previous conversation by ID"; `/resume` lists earlier conversations, and the CLI prints the resume command on exit | issue (flag), official (`/resume`, exit message) |
| Same conversation resumed exactly | unknown | unknown |

### Q5: activity

| Point | Finding | Label |
|---|---|---|
| Transcript | `brain/<conversation id>/.system_generated/logs/transcript*.jsonl` | issue |
| Summaries | `conversation_summaries.db` columns `status` and `not_fully_idle` | issue |
| `Stop` | `fullyIdle` and `terminationReason` | official |
| Which source is reliable for working, idle and waiting during a turn | unknown | unknown |

### Q6: trust and login

| Point | Finding | Label |
|---|---|---|
| Trust setting | `trustedWorkspaces` in `~/.gemini/antigravity-cli/settings.json` | issue |
| Trust prompt, and whether `trustedWorkspaces` pre-empts it | unknown. The permissions page does not cover workspace trust. | unknown |
| Credential storage | The operating system keyring ("Linux Secret Service/dbus"); browser sign-in locally, a device code over SSH | official (install page) |
| Keyring in a headless tmux member | unknown. A member without a Secret Service session may not find its sign-in; the API key route avoids the keyring. | unknown, interpretation |
| Expired login on screen | unknown | unknown |

### Q7: process

| Point | Finding | Label |
|---|---|---|
| Install | `curl -fsSL https://antigravity.google/cli/install.sh \| bash`, default `~/.local/bin/agy`; installer flags `--skip-aliases` and `--skip-path` | official (<https://antigravity.google/docs/getting-started>, install page) |
| Version | 1.3.5 on the Director's machine | issue |
| argv, `pane_current_command`, and whether `agy` is one process or a launcher | unknown | unknown |

## Live phase: plan and prerequisites

The live phase needs its own approval after the Q1 decision. It is listed here so that the gates are visible. Nothing
in it is approved by this checkpoint.

- **G1, terms and credential:** the Director's Q1 decision (above). The chosen route's terms are quoted in this spec
  before any sign-in.
- **G2, binary staging:** the official installer with an explicit private install directory inside an isolated runtime
  root, never an agent user's home or factory configuration. The version and the binary's sha256 are recorded. The
  installer edits shell profiles unless `--skip-aliases` and `--skip-path` are given. Whether it supports a custom
  directory is unofficial (a third-party guide shows `--dir`).
- **G3, network:** the probe rules from #729 apply.
  - The installation and auth page documents a custom endpoint under the Gemini API key route: "To send model
    requests to a different Gemini-compatible endpoint, set the `GOOGLE_GEMINI_BASE_URL` environment variable"
    ([Point the CLI to a custom endpoint](https://antigravity.google/docs/install?tab=cli#point-the-cli-to-a-custom-endpoint),
    official). The same page says that with an API key, model requests go directly to the Gemini API without an
    account session, and "The CLI skips the sign-in screen and opens the main interface directly."
  - Untested: whether the pinned `agy` binary honours the variable, and whether a local stub is compatible enough.
    That is a concrete candidate for an approved probe inside a loopback-only namespace, as in the OpenCode spike. It
    needs no account sign-in.
  - Whether question behaviour (Q3) needs the real service is **unknown**. A stub that emits an `ask_question` tool
    call may exercise the question screen and the hooks; that is a test-plan decision, not yet observed.
  - An expired account login (Q6) is a distinct case. It needs a real account session by definition.
  - Any case that does need external access needs its own, separately approved network route.
- **G4, isolated Synth City:** the e2e instance for hook and Agent Mail observations, never the live instance.
- **Runtime support:** the isolated runtime kit (`scripts/isolated-runtime-kit.sh`) has no `antigravity-cli` provider.
  Adding one is a Deck code change and is outside #734.

## Design consequences so far

These follow from the official documentation alone. They are inputs for #735–#738, not decisions.

- The Agent Mail install for `agy` (#736) must write the Antigravity hooks layout (named groups), not the
  `{"hooks": {...}}` layout that Claude Code and Muse use, and must not put hooks in `settings.json`.
- Deck cannot rely on `UserPromptSubmit`, which does not exist. Session identity can use `SessionStart`'s
  `conversationId`, which is the same pattern as Muse.
- Denying the question tool depends on `PreToolUse` firing for `ask_question`. Until that is observed, #736 and #737
  must treat questions as possible, and the guard's waiting-for-input detection has to cover them.
- The provider (#735) must not assume a consumer subscription sign-in. Its credential route follows the Director's
  Q1 decision.

## Sources

All fetched on 2026-11-15.

- Google Antigravity Additional Terms of Service: <https://antigravity.google/terms>
- Antigravity FAQ: <https://antigravity.google/docs/faq>
- Installation and auth (CLI): <https://antigravity.google/docs/install?tab=cli>
- Getting started: <https://antigravity.google/docs/getting-started>
- Hooks: <https://antigravity.google/docs/hooks>
- Permissions (CLI): <https://antigravity.google/docs/permissions?tab=cli>
- Settings and tips: <https://antigravity.google/docs/cli/using/>
- Unofficial context: Google AI developer forum threads on `discuss.ai.google.dev` about orchestrating `agy`, and the
  third-party guide <https://computingforgeeks.com/antigravity-cli-cheat-sheet/>.
