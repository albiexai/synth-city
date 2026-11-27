# Agent Mail

Agent Mail lets local Claude Code, Codex CLI, GitHub Copilot CLI, OpenCode CLI, and Pi sessions coordinate as a user-directed team. Albiex Synth City keeps durable mail participants, groups them by repository, tracks ephemeral sessions under those participants, and gives agents structured mailboxes for context requests, handoffs, broadcasts, and replies.

Broadcasts require an explicit audience: an Agent Team preset, repository, or GitHub work item. Synth City stores that audience with the message and creates receipts only for members in it. Direct messages and replies retain their addressed recipients. An all-member maintenance broadcast requires the operator credential and explicit global intent; a missing audience never means “everyone.” Historical messages keep their original receipts.

## Authentication and upgrades

Agent Mail uses strict authentication by default on new and upgraded installs.
With a configured Director key, Mail reads require a current Mail session or the key.
The browser uses the shared Director key from Teams.
External actor creation requires a loopback connection and the Director key when configured.
External actor tokens permit only their own or explicitly addressed Mail threads.
A Director key does not grant a member identity or plan approval authority.

Existing authenticated agents keep their current session credentials.
A session with a Team slot must have its current live pane binding.
Restart an older agent integration if it has no authenticated session credential.
Registration verifies the local process binding before it grants a Team slot identity.
Generic registration can create an unbound session. It grants no Team slot authority.

An installation can explicitly set `MAIL_CAPABILITY_TOKENS_REQUIRED=false` before starting the backend.
The opt-out allows unauthenticated reads and legacy registration.
Member writes require a Mail session. Director writes require the Director key when configured.
It permits generic Mail reads, install information reads and content hooks.
It preserves legacy registration and session liveness behavior.
It also disables pane and offline session checks for existing capability credentials.
Closed sessions remain refused.
Invalid supplied credentials still fail.
Member writes require a Mail session.
Member metadata changes and external actor creation require the configured Director key in every mode.
Agent Mail shows a warning when the opt-out is active.
Remove the explicit opt-out or set `MAIL_CAPABILITY_TOKENS_REQUIRED=true` to restore strict authentication.
No database deletion or schema reset is required.

Strict mode protects install information when a Director key is configured.
Member metadata changes and Mail install writes require the configured Director key in every mode.
With no configured key, local Director actions and UI Mail reads work as before.
The UI recommends setting a Director key.
`GET /api/v1/agent-mail/authentication-status` is public by design.
It returns exactly `{ "director_key_configured": <bool> }`.
This response contains no credential, Mail, path or launch data.
The UI reads it before protected reads and after a 401 response.
Agent tools still require Mail session credentials. Actor reads still require actor credentials.
Content hooks require a member capability credential before they return Mail context.
A hook without a valid credential returns only `Check your Synth City inbox.`
This fixed reminder contains no message, member or launch data.
Native hooks can use this reminder with the authenticated MCP inbox tools.
Activity and session-end hooks remain available because they return no Mail data.
The explicit opt-out also leaves install information readable without a Director key.

## What It Is For

- Ask the agent that knows one repository to explain a local API, component, convention, or failure mode to another agent.
- Hand work from one repository agent to another with touched files and next steps.
- Keep short-lived agent sessions attached to a durable repo or Agent Team slot participant, so role and charter survive restarts and context compaction.
- Inspect team communication from Synth City without turning the product into a general chat app.

## Mail context inside Work

Work context reads relevant messages/threads using verified team/slot/member identity. Missing or mismatched associations do not guess a recipient. Reading context does not check an agent inbox, acknowledge a request, send a message, approve a plan or launch a session.

Standalone Agent Mail retains coordination/install workflows. Ordinary context replies and handoffs are separate from normalized agent plan approvals; operator remedies and human PR review remain distinct. A Role label, visible message or eligible action grants no authority. Durable handoffs should identify issue, PR and exact head without credentials.

See [Work](/features/work) and [Teams](/features/agent-teams). Existing install, wake and visibility limits still apply; factory navigation does not change them.

## How Agents Connect

Claude Code gets both MCP tools and command hooks:

- The MCP server exposes `synth_city_whoami`, `synth_city_list_team`, `synth_city_check_inbox`, `synth_city_send_message`, `synth_city_reply`, `synth_city_ack_message`, `synth_city_request_context`, and `synth_city_create_handoff`.
- Session and prompt hooks inject state-based mailbox context into the agent conversation.
- Hook failures are soft, so a Synth City outage should not break an agent session.
- In team sessions a `PreToolUse` hook also guards edits while unread stop or hold Mail waits (see below).

### Edit guard for unread stop and hold Mail

Builders have edited, run experiments and sent requests before reading a newer Leader message that said stop or hold. The prompt hook cannot prevent that, because it runs only when a prompt is submitted, not between tool calls in a long turn.

The managed Claude Code `PreToolUse` hook now asks Synth City, before each guarded tool call, whether the member has unread authority Mail. If it does, the call is refused with a short reason such as `unread stop or hold message 14745: read your inbox (synth_city_check_inbox) first`. The reason names only the message id.

- **Guarded tools:** `Edit`, `Write`, `MultiEdit`, `NotebookEdit`, and Mail tools that change state (send, reply, report, request, approve and similar). Read-only tools are never refused: file reads, search, shell, `synth_city_check_inbox`, `synth_city_whoami` and the `get_`, `list_`, `render_` and `prepare_` Mail tools. A blocked member can always read its inbox, and reading clears the block.
- **What counts as a stop:** an unread message addressed to this member, sent by its team Leader, its bound Supervisor or Synth City itself, that is any of: a message whose subject contains stop, hold, pause, halt, cancel, revoke or defer; a message that carries an approval decision; or a message whose payload sets `block_edits` to true. The subject rule applies to system notices too, so a routine notice such as a label removal does not block unless it says so. A message that names a work item counts only when that item belongs to this member's slot. Plain chat, peer messages and messages for other members never block.
- **Install:** the guard ships in the existing Agent Mail install for Claude Code. Install again to upgrade an older hook; the new matcher replaces it. Only team sessions (`SYNTH_CITY_TEAM_SLOT_ID`) are checked.
- **Which member:** the guard finds the member from the tmux pane the hook runs in, using the same verified pane-to-slot binding as registration. It does not depend on how the native hook or the MCP server named the session, and it works with capability tokens required.
- **Failure mode: fail open, visibly.** If Synth City is unreachable, answers with an HTTP error, times out (about one second), cannot verify which slot the pane belongs to, hits an internal lookup error, or returns anything other than a plain allow or a well-formed refusal, the call is allowed and the hook prints one fixed warning that unread stop or hold Mail was not checked. Details of the failure and any text the server sent are never echoed. The trade-off: a dead or confused backend never freezes every agent, but while the check cannot run the guard is off, so agents must read their inbox themselves. The hook records the Synth City URL at install time; after a port change, install again.
- **Limit:** Claude Code only. Codex, Copilot, OpenCode, Muse and Pi have different hook models and are not guarded yet.

Codex CLI gets the MCP server through `codex mcp add` and lifecycle hooks for session registration, activity updates, and inbox reminders. Codex agents should still check their inbox through the MCP tools when starting and finishing work because hook delivery is a reminder path, not a replacement for agent action.

GitHub Copilot CLI gets the MCP server through `copilot mcp add` and user-level hook JSON for session registration, activity updates, and idle inbox reminders.

OpenCode CLI (OpenCode 2) gets three things from the Install tab:

- **MCP server:** `mcp.servers.albiex-synth-city-mail`, with `codemode: false` so that the Mail tools are direct tool calls. An older flat `mcp.albiex-synth-city-mail` entry that Synth City wrote is moved there. An entry under that name that Synth City did not write is refused, not overwritten.
- **Lifecycle plugin:** an OpenCode 2 plugin. An OpenCode 1 plugin never loads on OpenCode 2, so it shows as **Needs update**, and **Set up** replaces it in place.
- **Question deny:** a `permissions` rule that denies the question tool, so unattended members never stall on a question. The plugin also denies it for team members, and every team launch carries the same rule.

Everything is read and checked first. A malformed config file is refused before the backup and before any write, and each file is replaced atomically. Synth City records whether it added the deny rule. **Remove** deletes the rule only when that record proves it added it to that file and the file still has exactly one such rule; a rule you added yourself is kept.

If a team member still opens a question or a permission prompt, the plugin reports it and Synth City sends the bound Supervisor (or the Leader) the usual "Waiting for input" notice. When the order of reports cannot be proven, the wait is kept rather than cleared. This is a notice only: the activity view does not show OpenCode waits yet.

Pi uses the repository-local Agent Mail extension. Synth City passes it explicitly on Synth City launches and changes no global Pi configuration. The Install tab shows only its readiness; it has no Pi install action.

## Delivery Nudges

When a message is delivered to a reachable Claude Code, Codex, or Copilot member, Synth City tries to wake it with an inbox-check prompt. The automatic nudge is best-effort and throttled per recipient, so rapid message bursts do not keep injecting prompts into the same session. The **Queue inbox check** button remains available for a manual retry when the UI shows unread or pending mail.

Synth City uses one visible wake path:

- tmux-observed sessions can be nudged through Agent Bridge by sending text and `Enter` to the pane.

Wake participation is controlled per session. Manually registered sessions start opted out, so they are not woken unless an operator explicitly enables participation. The operator-only Agent Mail API requires a reason for each change and accepts only the wake-enabled flag and reason; it cannot change member, team, or slot assignment. Enabling is accepted only when a fresh authenticated MCP session is bound to exactly one matching observed pane. Participation changes appear in the operator-only wake audit, which records the reason and result without storing credentials or message content.

Non-tmux Claude Code, Codex, and Copilot sessions can still receive and send Agent Mail through MCP, but Synth City cannot wake their visible terminal session yet. Messages for those sessions remain delivered and unread until the agent calls `synth_city_check_inbox` or reaches a provider hook boundary. Agents should call `synth_city_check_inbox` before major work and after finishing a task.

## External Local Callers

Local tools such as OpenClaw can use the external Agent Mail API to authenticate as a named local actor, discover participants, send direct messages, request context, create handoffs, and poll request status.

See [External Agent Orchestration](./external-agent-orchestration.md) for token setup, endpoint examples, delivery result semantics, and Agent Teams launch integration.

## Setup Checklist

1. Open **Agent Mail** in Synth City.
2. Use the **Install** tab to install the integration for Claude Code, Codex CLI, GitHub Copilot CLI, OpenCode CLI, or any combination of them. For Pi, check that the **Pi Agent Mail** card shows **Extension ready**, then launch Pi from Synth City.
3. Restart or resume the affected agent sessions so their MCP configuration is loaded.
4. Have each agent call `synth_city_whoami` once from its repository.
5. Ask agents to call `synth_city_check_inbox` before starting major work and after finishing a task.

Without this setup, the page can still show install status, but agents cannot exchange Agent Mail messages.

## Current Limits

- Visibility is machine-global. Every local participant is visible to every other participant.
- Sessions without Agent Team slot context use one repo-level participant. Agent Team slots can create multiple distinct participants in the same repo, such as planner/reviewer and implementer.
- External Agent Mail calls use local actor bearer tokens. Token creation is loopback-only and follows Synth City's local trust model.
- External callers should only use the API from the same trusted machine until a broader Synth City auth model exists.
- Agent Mail is coordination state, not source control. Handoffs should still reference files, branches, issues, or commits when durable provenance matters.

## When a running agent's Mail tool list is out of date

An agent loads its Mail tool list when its session starts. After a factory deploy that adds or changes a Mail tool,
a member that was already running keeps the list it started with, so the new tool is not there for it.

The Mail server now tells the backend which list each session started with, as a short digest of the tool names. The
team view compares it with the deployed list and shows one of three states for each member:

- **current**: every connected Mail session of the member started with the deployed list;
- **stale**: at least one connected session started with another list. The reason reads "tool catalog stale: restart
  in resume mode";
- **unknown**: the member has no connected Mail session, or a connected session never reported its list (an older
  Mail server does not). Unknown is not stale: nothing shows the list is out of date.

With several connected sessions the state is judged together and conservatively. It is `current` only when every
connected session reported the deployed list, `stale` when any reported another list, and otherwise `unknown`. A closed
session is ignored.

The state is a display fact only. It grants no authority and changes no wake, delivery or approval rule, and a stale
member keeps working with the tools it has. **The restart fallback:** restart the member in resume mode. It then loads
the new list and keeps its conversation.

The **Teams** roster shows this fact. A member in the **stale** state shows "Mail tools out of date: restart to load"
beside its other status badges. The text hides when the member is current, when the state is unknown, and when the
status has expired. The Agent Mail roster and the Live sessions cards do not show it: a session card is one session, and
the state is judged over all of a member's sessions.

A member keeps its old list until it is restarted. Telling a running client with a `tools/list_changed` notification is a
separate issue (#1321, planned for v4.5), because no part of the Mail server changes its tool list while it runs.

## Install Details

Open **Agent Mail** in Synth City and use the **Install** tab.

- Claude Code install adds user-scope command hooks and a user-scope MCP server.
- Codex install runs the Codex CLI MCP installer and writes Agent Mail lifecycle hooks.
- Copilot install runs the Copilot CLI MCP installer and writes a managed user-level hook file.
- Install and uninstall actions require confirmation and attempt a backup before mutating config.

The Install tab also shows manual Codex snippets and Copilot fallback commands/hook JSON.

## Check a request before you submit it

Most rejected plan and continuation requests fail on mechanical rules, not on design: the wrong phase for the work, a missing completion action, an omitted companion path, a path list that is not closed. `synth_city_preflight_request` checks a plan, work item scope amendment or continuation request first and returns every problem at once, as a code, a field and a message. It creates nothing and sends no Mail, so it is safe to call as often as needed, and only the item's owner can call it. The continuation and amendment submit tools use the same checks, so a request that passes is not refused for these rules.

It also compares an optional estimate (`estimate_active_minutes`, in minutes) with the current authority window, reports unread stop or hold Mail for the owner, and lists the companion paths the server will add. Companions don't use up the repository scope's path limit or an amendment's added-path cap; they have their own bound of 32 per request, so a correction batch that only brings the companions of approved files is never refused for path count. The window is the repository scope's `max_item_effort_minutes` (default 120, from 15 to 1440). An estimate above it is refused, and one within 10 minutes of it gets a warning. Budgets, a request already pending and the repository head are checked only when you submit. See the [API reference](../api/agent-mail.md#check-a-request-before-submitting-it) for every rule and code.

## Effort limits and extensions

An approved plan's `estimate_active_minutes` becomes the item's effort limit, rounded up from its upper bound. A plan without an estimate has no limit. An upper bound above the repository scope's `max_item_effort_minutes` is refused at approval with `effort_estimate_exceeds_ceiling`, before anything is written.

The limit is an advisory ledger, not a stop. It counts the item's working time while it is dispatched and acknowledged, and stops counting while an owner observation is waiting or paused. When the counted time reaches the limit, the Leader and the owner each receive one message, and each receives another after every extension. Nothing is stopped or expired. The owner's own report of active effort is separate from the ledger.

The Leader may extend the limit with `synth_city_extend_effort_limit` while the item is `dispatched` or `verifying`. Each step states the extension total the Leader saw, a reason and evidence; a total that has changed is refused with `effort_extension_total_changed`. An extension is also refused while the owner has not acknowledged the approval, while observation is paused, or when the owner shows no recent activity. One step adds at most the smaller of 30 minutes and 25 percent of the original limit, and the limit plus all extensions stays within `max_item_effort_minutes`. A step beyond that room is refused with `extension_exceeds_ceiling`, and the Supervisor is told once for the attempt. The per-step cap always comes from the original limit, so repeated steps do not grow it.

## Amend an approved work item scope and end a submitted revision

An owner can request a healthy work item scope amendment before or after opening a PR. The current Leader
reviews the normalized path/action/command work item scope, then the owner separately acknowledges the
delivered approval. Held workspace edits remain intact. Three approved amendments, eight amendment
requests of any outcome and sixteen cumulative unique added file paths are the attempt limits; any
stricter configured limits remain in force. A rejected amendment does not count toward the three
approved ones, but it still counts toward the eight requests and its added paths keep their cost.
Removed paths retain their history and budget cost.

When independent review requires changes to a submitted implementation revision, the assigned
reviewer records a structured negative disposition for the exact head, then the current Leader
ends that revision. The owner remains on the same issue, branch and lease, with source work
stopped until fresh amendment approval and acknowledgement. Prose, CI completion and an old
review cannot grant that authority. An operator can also cancel submitted revisions; diagnostic
work must first restore the exact baseline tree, including when its failed head was already charged.

With the automatic merge policy, a code item merges only after the team's verifier has recorded an
explicit `accepted` verdict for the pull request's exact current head (`synth_city_record_head_review`).
A push after the acceptance waits for a new acceptance, an owner or non-verifier cannot supply one, and
the work view shows `Waiting for reviewer acceptance` or `Head changed since acceptance`. The review hold
label keeps working as an extra control. See the API workflow for the exact rules.

The reviewer gets the attempt binding it needs for those reviews from `synth_city_get_review_binding`. Only the team's current verifier, or the reviewer named on the required-check watch for the pull request's exact current head, can read it, and it never includes the lease token.

Cancelled and timed-out CI runs remain blocked and visible without consuming a failed-head unit.
Closing an unmerged submitted PR on an escalated attempt supersedes that revision without
implicitly authorizing a retry or releasing the workspace. See the [API workflow](../api/agent-mail.md#healthy-work-item-scope-amendments-and-submitted-review-settlement)
for tools, authentication, compatibility limits and separate review/merge gates.
