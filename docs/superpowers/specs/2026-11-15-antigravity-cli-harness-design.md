# Antigravity CLI harness support: spike spec

Date: 2026-11-15, live observation added 2026-11-16. Issues: #734 (documentation), #952 (runtime acceptance).
Target: release/v4.2.

This spec records what Google's public documentation and terms say, and what #952 observed with `agy` 1.5.9 in an
isolated runtime (see [Live observation (#952)](#live-observation-952)). Rows labelled **observed** come from that run.
Rows still labelled **unknown** were not observed and must not be relied on.

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
| **observed** | Seen in #952's isolated run of `agy` 1.5.9: a loopback-only network namespace, a dummy `GEMINI_API_KEY`, a local scripted Gemini-compatible stub, a private home directory, and no sign-in. A stub shows harness mechanics only, not real-model behaviour. |
| **unknown** | No source found, and not observed. |

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

## Live observation (#952)

`agy` 1.5.9 (Linux x86-64 tarball from the official update manifest, sha512 checked against it; the installer and
`agy install` were not run) ran in an unprivileged, loopback-only user and network namespace, with a minimal environment,
a private home directory and a private tmux socket. Every model request went to a local stub. Nobody signed in, no real
key was used, and Google's terms were not accepted.

- **API-key route.** With `{"modelProvider": "gemini"}` in `~/.gemini/antigravity-cli/settings.json`, `GEMINI_API_KEY`
  and `GOOGLE_GEMINI_BASE_URL`, `agy` skips sign-in and sends `POST v1beta/models/<model>:streamGenerateContent?alt=sse`
  with the key header to the configured endpoint. Without `modelProvider` it shows only "You are currently not signed
  in" with Google OAuth or a Google Cloud project, even when `GEMINI_API_KEY` is set. Each new turn sends a side request
  to a `flash-lite` model and the main request, which offers 14 tools including `ask_question`.
- **First-run onboarding.** A new user sees a colour-scheme picker, then "Terms of Service & Data Use" with a
  data-collection opt-in that is checked by default. The interactive screen cannot be used until it is finished; turns
  still run behind it. **Synth City never accepts it and never presets its settings: the terms and the data-use choice
  belong to the user.** In a later human trial (#995), the Director completed onboarding himself in an isolated scratch
  home: `agy` wrote `~/.gemini/antigravity-cli/cache/onboarding.json` with `onboardingComplete: true` (also
  `consumerOnboardingComplete`, which a sign-in may set, and `enterpriseOnboardingComplete: false`). Readiness reads
  only `onboardingComplete`, read-only and fail-closed (`antigravity_cli.antigravity_onboarding_complete`), and returns
  the onboarding instruction (`provider_readiness_service.ANTIGRAVITY_ONBOARDING_REASON`) until it is true. The Agent
  Mail gate is checked first and stays closed (#994).
- **Argv.** A prompt must be one `-i` (or `-p`) argument. A stray positional argument exits with code 2: "Prompts are
  read only from -p/--print, -i/--prompt-interactive, or stdin".
- **Not observed:** the trust prompt and the question screen (both hidden behind onboarding), `PostToolUse`, real-model
  behaviour, an expired login, linked git worktrees, and behaviour inside a Synth City team launch.

### Agent Mail observations (#736)

#736 ran the same pinned `agy` 1.5.9 three more times in the same isolation, with a stdio MCP stub server and recorder
hooks. All rows here are **observed**:

- **MCP server entry.** A plain `{"mcpServers": {"<name>": {"command": ..., "args": [...], "env": {...}}}}` in
  `~/.gemini/config/mcp_config.json`, with no `type` or `transport` field, is loaded. `agy` starts the stdio server as
  its own child and sends `server/discover`, `initialize`, `notifications/initialized` and `tools/list`. The model does
  not see the server's tools by name: it gets three generic tools, `call_mcp_tool`, `list_resources` and
  `read_resource`. A real `call_mcp_tool` → `tools/call` round trip was **not observed** (still not observed in #993,
  below).
- **Hook output.** Claude Code's `{"hookSpecificOutput": ...}` printed by a `SessionStart` or `PreInvocation` hook is
  rejected (`failed to unmarshal result ... via protojson`) and ignored; the turn still runs. Empty output is accepted.
- **Blocking a question.** A `PreToolUse` hook that prints `{"decision": "deny", "reason": "<text>"}` blocks
  `ask_question`: the model receives the tool error `tool call denied by pre-tool hook: <text>`, the turn continues, and
  `Stop` fires with `fullyIdle: true`. No `PostToolUse` fires for the denied call.
- **Hook process.** A command hook runs as `agy` → `sh -c <command>` → the command, so the command's parent is a
  short-lived shell and `agy` is its grandparent.

### Agent Mail round trip (#993)

#993 ran the same pinned `agy` 1.5.9 (sha256 checked) twice more in the same loopback-only isolation, with a dummy key
and the local stub model. A scratch Synth City backend ran on loopback with a scratch database and home, and Synth City's
own Antigravity install (with the `synth-city-mail` server key from #991) was applied through it. Both runs used
`--dangerously-skip-permissions` and a `trustedWorkspaces` entry. The first-run onboarding colour-scheme screen was
displayed from the first sample (3 s) in both runs and **was never interacted with**: no keys were sent and no onboarding
or consent setting was preset (Supervisor decision, 2026-11-16). No other screen appeared.

**Observed:**

- **Run 1 (new conversation):** `agy` loaded the Mail server under `synth-city-mail` and wrote its tool schema files,
  with **no** invalid-tool-name errors. The model request offered only the 14 built-in tools; **`call_mcp_tool` was not
  declared**, and a scripted `call_mcp_tool` for `synth_city_whoami` was refused with `unknown tool: "call_mcp_tool"`.
  The `SessionStart` and `PreInvocation` hooks reached Synth City (`hooks/session-start` and
  `hooks/user-prompt-submit`, both accepted), and Synth City recorded one connected `antigravity-cli` MCP session.
- **Run 2 (`--conversation <id> -i` resume in the same home, with the server already loaded):** the same result. The
  14 built-in tools only, the scripted call refused. No `SessionStart`; `PreInvocation` reached Synth City twice; the MCP
  session was connected.

**Not established:**

- A `call_mcp_tool` → `tools/call` round trip **remains unproven**. Why `call_mcp_tool` was not declared is unknown. In
  #736 P1, which declared it, the MCP server was a minimal stub and neither `--dangerously-skip-permissions` nor
  `trustedWorkspaces` was used. Those differences were not tested, and the run evidence does not single out one cause.
- Team-member (slot-bound) registration, real-model behaviour and any permission or other screen were not observed.

Synth City's Antigravity readiness gates are unchanged by these runs.

## Q2–Q7: what is documented and what was observed

Each row states the source of every claim.

### Q2: hooks

| Point | Finding | Label |
|---|---|---|
| Global hooks file | `~/.gemini/config/hooks.json` | official (<https://antigravity.google/docs/hooks>), issue |
| Workspace hooks file | `.agents/hooks.json` at the project root; a plugin's own `hooks.json` also loads | official |
| Not loaded | Hooks placed in `~/.gemini/antigravity-cli/settings.json` | official |
| Layout | Named top-level groups, then the event: `{"my-linter-hook": {"PostToolUse": [{"matcher": "run_command", "hooks": [{"type": "command", "command": "./scripts/lint.sh", "timeout": 10}]}]}}`. This differs from Claude Code's and Muse's `{"hooks": {...}}` layout. | official |
| Layout for events without a matcher | **Flat command entries**: `{"my-stop-hook": {"Stop": [{"type": "command", "command": "...", "timeout": 10}]}}`. The `{"hooks": [...]}` wrapper is rejected for `SessionStart`, `PreInvocation`, `PostInvocation` and `Stop` ("command hook must specify 'command'"), and then no hook fires. `PreToolUse` and `PostToolUse` take `{"matcher": "*", "hooks": [...]}`. | observed |
| Events | `SessionStart`, `PreToolUse`, `PostToolUse`, `PreInvocation`, `PostInvocation`, `Stop`. No `UserPromptSubmit`. | official, issue |
| `SessionStart` payload | `conversationId`, `workspacePaths`, `transcriptPath`, `artifactDirectoryPath`, `modelName`, `lastUserInput` | official |
| `PreToolUse` payload | adds `toolCall` (`name`, `args`) and `stepIdx` | official |
| `Stop` payload | adds `executionNum`, `terminationReason`, `error`, `fullyIdle` | official |
| `PreToolUse` decision | output field `decision`: `allow`, `auto_approve`, `deny`, `ask`, `force_ask`, `deny_unless_prior_grant` | official |
| Matchers | `""` or `*` for all tools, exact names, `a\|b`, regex such as `browser_.*`; ignored for `SessionStart`, `PreInvocation`, `PostInvocation` and `Stop` | official |
| Payload fields seen | Every event: `hookEventName`, `conversationId`, `transcriptPath`, `artifactDirectoryPath`, `modelName`, `lastUserInput`, `workspacePaths`, `cwd`. `PreInvocation` and `PostInvocation` add `invocationNum` and `initialNumSteps`. `Stop` adds `executionNum`, `terminationReason` (for example `NO_TOOL_CALL`), `error`, `fullyIdle` and `continuationActive`. `PreToolUse` adds `toolCall` (`name`, `args`, `id`) and `stepIdx`. | observed |
| `SessionStart` on a new conversation versus `--conversation <id>` | A new conversation fires `SessionStart`, `PreInvocation`, `PostInvocation` and `Stop`. A resume with a prompt fires `PreInvocation`, `PostInvocation` and `Stop`, but **not** `SessionStart`. A resume without a prompt fires nothing. Session identity must not depend on `SessionStart` alone. | observed |
| Which hooks actually fire in the CLI | `SessionStart`, `PreInvocation`, `PostInvocation`, `Stop` and `PreToolUse` fired from the global file. `PostToolUse` was not observed, because the only tool call (`ask_question`) was not answered; it is neither confirmed nor ruled out. The workspace file was not tested. | observed; `PostToolUse` unknown |

### Q3: questions

| Point | Finding | Label |
|---|---|---|
| Tool | `ask_question`, "Ask multiple-choice questions", with argument `questions` (each with `question`, `options`, `is_multi_select`). The Hooks page lists it among the tools that hook matchers can name ([Supported tools, Interaction and media](https://antigravity.google/docs/hooks#interaction-and-media)). | official, issue |
| Name seen by `PreToolUse` | Documented as `ask_question` (above). Whether `PreToolUse` actually fires for it in the CLI, and whether a `deny` decision then blocks the question, is unknown. Muse's `PreToolUse` does not fire for its question tool. | official (documented name); firing and denial unknown |
| `permissions.deny` | The documented rule actions are `read_file`, `write_file`, `read_url`, `execute_url`, `command`, `unsandboxed` and `mcp` (<https://antigravity.google/docs/permissions?tab=cli>). No action covers a question tool. | official; blocking it is unknown |
| Rule precedence | "Your fine-grained `allow`, `ask`, and `deny` rules always take precedence (`Deny > Ask > Allow`)." ([Execution modes and the terminal sandbox](https://antigravity.google/docs/permissions?tab=cli#execution-modes-and-the-terminal-sandbox)) | official |
| `--dangerously-skip-permissions` | A separate behaviour: it sets the `toolPermission` setting to `always-proceed`, and "`strict` and `always-proceed` (`--dangerously-skip-permissions`) take precedence over `auto` mode." On the page as fetched on 2026-11-15, it does not override explicit `deny` rules. The page names no version, and it can change. Whether the flag suppresses `ask_question`, or affects a hook's `deny`, is not stated. A permission override is not evidence that the question screen is suppressed. | official; effect on questions and hooks unknown |
| `PreToolUse` for `ask_question` | Fires, with `toolCall.name` `ask_question` and the questions in `toolCall.args`. Without a decision the turn then waits: no `PostToolUse`, no `Stop` and no further model request while the question is unanswered. With `{"decision": "deny", "reason": ...}` the question is blocked and the turn continues (#736). | observed |
| Open question in the transcript, the summaries database and on screen | unknown. The question screen was hidden behind onboarding. | unknown |

### Q4: identity and resume

| Point | Finding | Label |
|---|---|---|
| Conversation id from a hook | `SessionStart` carries `conversationId` and `transcriptPath` | official |
| Conversation id from state | `~/.gemini/antigravity-cli/conversations/<uuid>.db` and `conversation_summaries.db` with `workspace_uris` | issue |
| Conversation id of a live process | The process holds `presence/<uuid>.lock` open, also after a resume; `workspace_uris` holds the trusted workspace's `file://` URI | observed (#737) |
| Resume | `--conversation <id>`, "Resume a previous conversation by ID"; `/resume` lists earlier conversations, and the CLI prints the resume command on exit | issue (flag), official (`/resume`, exit message) |
| On exit | Prints "Resume with -c (or command below):" and `agy --conversation=<id>` | observed |
| Same conversation resumed exactly | Yes. `agy --conversation <id> -i <prompt>` and `--conversation=<id>` resume the same conversation id with its earlier turns, and deliver the prompt (`lastUserInput`). Without `-i`, nothing is sent. Conversation ids are UUIDs. | observed |

### Q5: activity

| Point | Finding | Label |
|---|---|---|
| Transcript | `brain/<conversation id>/.system_generated/logs/transcript*.jsonl` | issue |
| Summaries | `conversation_summaries.db` columns `status` and `not_fully_idle` | issue |
| `Stop` | `fullyIdle` and `terminationReason` | official |
| Summaries during a turn | `status` `CASCADE_RUN_STATUS_RUNNING` with `not_fully_idle` 1 during a turn, including while waiting; `CASCADE_RUN_STATUS_IDLE` with 0 after it. `step_count` can lag the live steps. | observed (#737) |
| Live `steps(idx, step_type, status)` | Types 14 user input, 15 planner reply, 101 system message, 132 tool or wait step. Status 3 done; 2 while `run_command` ran (`--dangerously-skip-permissions`); 9 while `ask_question` waited; 6 only after the process exited. | observed (#737) |
| Transcript records | `step_index`, `source`, `type`, `status`, `created_at` (whole seconds), `tool_calls[].name`; statuses are not updated later; a resume appends | observed (#737) |
| Permission review, other tools, other modes | Not observed. Synth City reports them as unknown. | unknown |
| Classifier | Synth City's read-only observer (#737) uses only the observed shapes above. The screen fallback is deferred. | implemented |
| Team guard | `team-supervisor` reads the transcript reply-only and fails closed on every unobserved record (#984 Part 1; see [Team guard](#team-guard-984-part-1)). | implemented |

#### Team guard (#984 Part 1)

The team supervisor (`scripts/product-factory/team-supervisor`) stops a team on a provider safety notice in an agent's own output. For an `antigravity-cli` member it works like this:

- **Which conversation.** The slot's `session_id` must be a canonical lowercase UUID, as for the #737 observer; otherwise the guard fails closed (`native_session_identity_missing`).
- **Which files.** The data root is the agent user's passwd home plus `.gemini/antigravity-cli`, unless the guard's config names `antigravity_data_root`. `presence/<id>.lock` and `brain/<id>/.system_generated/logs/transcript.jsonl` must exist. Every directory on the way and both files must belong to the agent user, and none may be a symlink. Anything else fails closed.
- **The proof holds through the open (#1020 review).** The guard walks from the data root one component at a time. It opens each directory relative to the one before, without following a link, and checks its type and owner on the open descriptor. It checks the lock relative to the held `presence` descriptor, and opens the transcript relative to the held `logs` descriptor. A component swapped after it was opened can't redirect the read.
- **The saved position is bound to the file.** The guard saves the transcript's device and inode next to its offset, and reads the size from the opened file. A smaller file, a different file at a saved offset, or a saved offset without a saved identity fails closed.
- **Limit.** The lock shows that the conversation is present. The guard can't check which process holds it, because another user's `/proc` file-descriptor table isn't readable.
- **What is matched.** Only the text of a `PLANNER_RESPONSE` from `MODEL`. User input, system messages and tool-call arguments are never matched.
- **Observed set.** Only the record shapes in the #737 fixtures pass:
  - `USER_INPUT` from `USER_EXPLICIT`;
  - `SYSTEM_MESSAGE` from `SYSTEM`;
  - `PLANNER_RESPONSE` from `MODEL` with text and no tool calls;
  - `PLANNER_RESPONSE` from `MODEL` with a non-empty list of named tool calls and no text.
- **Every other record fails closed** (`antigravity_record_unobserved`) before the guard saves its position in the transcript. That includes an `ERROR` step (never captured, so not classified), any unknown type, and the records agy writes for finished tool steps. An armed team stops and needs recovery; a disarmed one reports a startup gap. Neither looks healthy, and the record is read again on the next tick.
- **Shared skips are not record validation.** The guard's shared line reader still skips a line of only NUL bytes (crash padding) and a damaged line the operator acknowledged by its SHA-256. Neither is an observed Antigravity shape, and neither skip validates an Antigravity record.
- **Consequence.** Until #984 Part 3 proves more shapes, **a routine Antigravity tool record that is not in the observed set stops an armed team.** Antigravity's team readiness stays blocked in v4.2 (the Mail and onboarding gates), so no Antigravity member runs in a team yet.

### Q6: trust and login

| Point | Finding | Label |
|---|---|---|
| Trust setting | `trustedWorkspaces` in `~/.gemini/antigravity-cli/settings.json` | issue |
| Effect of `trustedWorkspaces` | `"trustedWorkspaces": ["<absolute path>"]` is accepted (`agy` rewrites the file and keeps it). With it, hook payloads report `workspacePaths` `["<path>"]` and `cwd` `<path>`; without it, `[]` and `""`. Observed for a plain directory only. Synth City writes this entry when it launches a team member (`team_workspace_trust.py`). | observed (workspace registration) |
| Trust prompt, and whether `trustedWorkspaces` pre-empts it | unknown. No trust prompt was seen with or without the setting, because onboarding covered the screen. Suppressing a prompt is **not** claimed. | unknown |
| Credential storage | The operating system keyring ("Linux Secret Service/dbus"); browser sign-in locally, a device code over SSH | official (install page) |
| Keyring in a headless tmux member | unknown. A member without a Secret Service session may not find its sign-in; the API key route avoids the keyring. | unknown, interpretation |
| Expired login on screen | unknown | unknown |

### Q7: process

| Point | Finding | Label |
|---|---|---|
| Install | `curl -fsSL https://antigravity.google/cli/install.sh \| bash`, default `~/.local/bin/agy`; installer flags `--skip-aliases` and `--skip-path` | official (<https://antigravity.google/docs/getting-started>, install page) |
| Version | 1.3.5 on the Director's machine; 1.5.9 in #952 | issue, observed |
| argv, `pane_current_command`, and whether `agy` is one process or a launcher | One process named `agy` under the pane's shell in #952's run; `pane_current_command` itself was not recorded. | observed (process), unknown (tmux field) |

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
  - Observed in #952: `agy` 1.5.9 honours the variable, and a local stub is compatible enough, once
    `modelProvider` is `gemini` in `settings.json`. The page's "skips the sign-in screen" did not hold without that
    setting.
  - Whether question behaviour (Q3) needs the real service is **unknown**. A stub that emits an `ask_question` tool
    call may exercise the question screen and the hooks; that is a test-plan decision, not yet observed.
  - An expired account login (Q6) is a distinct case. It needs a real account session by definition.
  - Any case that does need external access needs its own, separately approved network route.
- **G4, isolated Synth City:** the e2e instance for hook and Agent Mail observations, never the live instance.
- **Runtime support:** the isolated runtime kit (`scripts/isolated-runtime-kit.sh`) has no `antigravity-cli` provider.
  Adding one is a Deck code change and is outside #734.

## Design consequences so far

These follow from the official documentation alone. They are inputs for #735–#738, not decisions.

- The Agent Mail install for `agy` (#736) must write the Antigravity hooks layout (named groups), with flat entries
  for events without a matcher, not the `{"hooks": {...}}` layout that Claude Code and Muse use, and must not put hooks
  in `settings.json`.
- Deck cannot rely on `UserPromptSubmit`, which does not exist. A new conversation's `SessionStart` carries
  `conversationId`, but a resume does not fire it, so resumed identity needs `PreInvocation`'s `conversationId` or the
  launch argv.
- `PreToolUse` fires for `ask_question`, and a `deny` decision blocks it (#736). #736's hook denies it for team members
  even when Synth City is unreachable. A question that still opens, for example without the hooks installed, is
  possible, so #737's waiting-for-input detection has to cover it.
- The provider (#735) resumes with `--conversation <id>` and, when a prompt is given, `-i <prompt>` (observed, #952).
- Readiness is blocked until the user has completed onboarding (the `cache/onboarding.json` signal, #995) and the
  Agent Mail gate is lifted (#994). Synth City never accepts the terms or presets onboarding.
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
