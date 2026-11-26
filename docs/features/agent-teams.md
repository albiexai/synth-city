# Agent Teams

Agent Teams are saved rosters of local Claude Code, Codex CLI, GitHub Copilot CLI, OpenCode CLI, and Pi sessions. Use them when the same group of repositories should be launched or reused together, such as a project team, DevOps team, or release validation team.

For the complete operator and supervisor workflow, use the [Supervised factory playbook](../guide/supervised-factory-playbook.md). It covers roster preparation, goals, external review, maintenance and recovery.

## Teams inside the work flow

Teams retains saved rosters and manual launch planning alongside factory work. Overview/Work span teams; here configure a roster and its repositories. The preset's `leader_slot_id` supplies Leader authority. Slot order and descriptive Role text do not select the Leader.

## Current autonomy and repository setup

Autonomy configures watched repositories, existing primary checkout, labels, merge policy and finite dispatch/recovery limits. GitHub polling uses host access separately from operator authorization; GitHub App dispatch settings alone do not authenticate polling. Repository setup checks access and labels without creating records or labels. New teams start with automation off; new repository scopes start disabled. Explicit Leader changes require operator authority, a disabled team, and a quiescent team.

Team/scope enablement controls intake. Pausing retains configuration; inspect current attempts/processes separately. Code follows configured merge policy with the rolling automatic-merge cap; design-labelled work always requires human PR review. Leader plan approval, operator intervention and human PR review are separate.

Build directory, command and parallelism fields are hints in the owner's brief; Albiex Synth City does not run those build commands. Editable numeric drafts validate on save; clearing a field does not silently increase a budget or authorize recovery.

### Continuation path scope

A continuation request must list its allowed paths explicitly. `README.md` permits changes to that exact file. `docs/` permits changes below that directory, including nested files. It does not permit changes below `docs-old/`. It does not permit replacement of the directory itself with a file.

Directory permission requires the trailing slash in the approved request. Existing entries such as `docs` keep their exact-file meaning. Use a new approved revision to change the scope. Globs, absolute paths, parent paths and the repository root are not valid scopes.

The path limit counts entries in the approved request. Each directory entry grants the stated subtree. Tree observations, commands, actions, ownership, workspace leases and failed-head budgets retain their own limits. Completion checks each changed file against the approved scope. An accepted-source import records only exact accepted files outside that scope. It does not grant edit permission.

## Authorization, reuse and deletion

Roster, watched-repository, autonomy and recovery settings/remedies require the configured operator token. Browser launch planning uses the existing per-tab flow. Protected reads and constrained agent launch APIs have separate authenticated Mail-session principal rules; credentials are separate from GitHub polling and are not a role/bootstrap prompt.

Normal reuse requires a live pane already bound to its intended slot. Unbound panes block the default plan; an operator must review target/PID and explicitly adopt or choose fresh spawn. An offline actor's link only selects launch context. Agent sessions cannot adopt, include disabled slots, override paths/prompts, force respawn or bypass plan confirmation.

Retry is confirmed and can discard attempt markers; a leased workspace can defer re-dispatch until normal owner release. A response is not proof of a new dispatch. Remedies depend on current state/principal and finite policy. Enabling recovery while autonomy is live requires explicit live-effect confirmation.

Deletion refuses enabled autonomy, nonterminal/unknown work, residual workspace authority, nonterminal approvals/revisions and affected or unavailable recovery protection. Inspect bounded safe blockers and resolve through normal controls; pausing, a stopped process or an empty-looking table does not establish safe deletion. After a valid deletion, the [audit ledger](/guide/factory-audit-and-metrics#filters) keeps its recorded facts under their context keys, with their historical attribution; live links to the deleted records become unavailable.

See [Work](/features/work), [Repositories](/features/repositories), [Live sessions](/features/agent-bridge) and [Agent Mail](/features/agent-mail).

## What A Team Contains

Each team has slots. A slot stores:

- provider
- repository path
- display name
- role and charter
- area labels, the GitHub label names that route an issue to this slot (exact, case-sensitive match)
- expertise, a note that is stored but does not route work yet
- optional UI color for Agent Bridge cards and terminals
- optional bootstrap prompt
- controlled language, enabled by default for each member
- launch mode and provider options
- enabled or disabled state

### Claude Code model and reasoning effort

A Claude Code slot can set its **Model** and **Reasoning effort** on every platform, not only on Amazon Bedrock. Both are optional launch options; leave them on **Default** to use the harness's own choice.

- **Model:** choose **Default**, one of the listed examples, or **Custom model** and type a concrete model ID (for example `claude-sonnet-5-5`). Synth City refuses a value that is not a concrete model ID with the code `invalid_model`. A launch passes it as `--model <id>`.
- **Reasoning effort:** `low`, `medium`, `high`, `xhigh` or `max`. Any other value is refused with `invalid_reasoning_effort`. A launch passes it as `--effort <value>`.
- **Amazon Bedrock:** a Bedrock member sets **Bedrock model ID**, not **Model**. A member that sets both is refused with `bedrock_model_conflict`; the editor offers **Clear model**. A launch that sets only the Bedrock model ID works as before.
- **Where you see it:** the slot row shows the harness, the model and the effort, for example `Claude Code · claude-opus-5-5 · high effort` (a Bedrock member shows its Bedrock model ID). The launch plan row shows `Model: <model> · <effort> effort`, or `default` when only the effort is set.

Other harnesses keep their own launch options (see [Harnesses](/features/harnesses)).

Agent Teams do not create a second messaging system. Once agents are launched or reused, use Agent Mail for messages, context requests, and handoffs.

## Controlled Language

Each member uses ASD-STE100 guidance by default. Clear **Controlled language (ASD-STE100)** in the member form to select another writing style.
The option applies to one member. A copied roster keeps each member's choice. Existing slots use the enabled default after migration.

Synth City includes the guidance in launch prompts, Agent Mail identity and session-start context, and new work instructions.
Custom bootstrap prompts and launch prompt overrides retain this guidance. A saved change does not rewrite an active conversation.
An active member receives the new choice with its next identity or session-start context, or its next work instructions.

The guidance applies to team messages, GitHub issue and PR text, comments, reports, and documentation.
It asks agents to use short sentences, active voice, simple verb forms, and consistent terms.
Agents must preserve code, commands, identifiers, quotations, safety conditions, decision gates, and evidence limits.

The [official ASD-STE100 standard](https://www.asd-ste100.org/about_STE.html) contains writing rules and a controlled dictionary.
Synth City supplies agent instructions. It does not check the full dictionary or certify output compliance.

## Summary for Human Review

Before agents request human review or merge, they must add a brief **Human review summary**.
The section belongs near the start of both the PR body and its main issue body.
This rule also applies when a member selects another writing style.

The summary must state:

- **Goal:** The user outcome, in one or two sentences.
- **Changes:** The main changes, in a short list.
- **Checks:** The completed checks and material limits. Distinguish source review, automatic checks, and human trials.
- **Action:** The requested human action, PR link, target branch, and any separate decision gate.

Agents must preserve the original issue facts and PR metadata. They must update the summary when the head, results, or requested action changes.
They must link detailed evidence below the summary. A comment alone does not meet this instruction.
Synth City supplies this rule in launch and Agent Mail context, work instructions, and review notifications.
These instructions do not prove summary quality or replace independent review and the configured merge policy.

## Work Remaining

The progress view shows three short lines:

- **Remaining:** The tasks and decisions that still need work.
- **Estimate:** A range of active effort, with confidence and scope, or **Unknown**.
- **Next:** The next actor and action.

The owner supplies the estimate. The Leader calls `synth_city_prepare_work_remaining_summary` after a safe published checkpoint or review handoff.
The Leader places the returned block near the start of the main issue and PR. Keep the other issue facts and action records.
The tool formats the block. It does not write to GitHub. Use existing authorized GitHub access.

An estimate excludes waiting for another actor. It is not a promised finish time.
Do not infer completion from commits, elapsed time, or passing tests. State the unit and scope of any count.
Put completed work and estimate assumptions in the optional detail fields. The UI keeps them in a closed disclosure.

The report names its author, update time, and source checkpoint. It expires after two hours.
A changed item, dispatch, owner, scope revision, published source, or phase makes it historical.
When Synth City cannot confirm the source or find a current report, it shows **Unknown** and the current next action.
Refresh the block after changes or review corrections. A team report does not clear approval, review, CI, merge, or milestone gates.

## Reported Human Decisions

The **Human actions and decision gates** section appears above the Roster and Autonomy tabs.
It shows the human requests from the Leader's current assessment and dispatch state.

The Leader must assign human decisions to the operator. This includes milestone acceptance and pilot decisions.
Synth City rejects a new assessment that assigns an incomplete human decision to the Leader or another actor.
The check covers the stated reason and explicit milestone or pilot actions.
The Leader must include the action and its readiness before requesting human input.
Use **requested** when the human can act now. Use **waiting for prerequisites** when required evidence is absent.

Synth City also shows a human gate from an older report with a conflicting actor.
It marks that gate as waiting and warns that the assessment needs confirmation.
It does not present the gate as ready or treat its evidence as accepted.

These reports describe the requested action. They do not approve work or satisfy a milestone.

## Clear Instructions for Human Actions

Synth City keeps the detailed instructions in the main GitHub issue. A comment history or team Mail exchange is insufficient.
The **Current operator actions** section belongs near the start of the issue body.
It states why the request exists, who must act, the exact steps, the evidence, and the condition that clears it.
It names delegated maintenance explicitly. A delegated repair must not look like an unexplained new decision for the operator.

The team Leader prepares and publishes these instructions through the team's existing authorized GitHub access.
This task covers automatic dispatch escalations and review requests as well as milestone and pilot decisions.
The Leader updates the record when the request changes and clears or supersedes it when the request ends.
Other issue facts and records from other scopes remain intact.

Synth City checks the current record before showing an action as ready. A PR record identifies its target and reviewed head.
A new assessment with missing or stale instructions is refused. Older or automatic requests remain visible as **Action details pending**.
The Leader must prepare those details. Autonomy inspection remains available for urgent recovery.
Missing instructions do not clear a gate, hide the stopped attempt, or authorize a retry.

The panel shows a short action label, its readiness, and a **Read action instructions** link.
The link opens the current section in GitHub. Detailed steps stay in the issue.
Instruction records expire after 24 hours and need an update if the request remains current.
A failed GitHub read or publication cannot make an unexplained action ready.

These records do not approve work, release a workspace, change merge policy, or satisfy a milestone.
Synth City checks the record's structure and identity. The Leader remains responsible for clear and accurate explanations.
See the [API publication contract](../api/agent-teams.md#current-operator-action-records) for the preparation tools and record format.

## Same-Repo Role Workflows

Use Agent Teams when multiple agents need distinct roles inside the same repository. A common setup is:

1. create one slot named `Planner` for the repository
2. create a second slot named `Reviewer` for the same repository
3. give each slot a role, charter, and optional bootstrap prompt
4. plan and launch the team from Agent Teams
5. use Agent Mail for context requests, replies, and handoffs between the slots

Launching through Agent Teams is important because each slot receives its own Agent Mail identity. Two manually started sessions in the same repository may be represented as one repo-level participant, which is not reliable for planner/reviewer routing.

Use `reuse existing` only when the existing sessions already belong to the intended team slots. For a clean planner/reviewer workflow, spawn fresh slots from the team.

## Creating Teams

You can create a team manually, import selected Agent Mail members, or snapshot currently visible Agent Bridge sessions.

`From Mail` uses Agent Mail participants and copies their current role and charter into slot-specific values. This is a snapshot; editing a team slot does not update existing mail history.

`From Bridge` uses live Agent Bridge tmux sessions. If multiple sessions are visible for the same repo, Albiex Synth City keeps each session as a separate slot so the resulting team can have distinct same-repo roles.

## Launch Planning

Before launch, Synth City computes a plan. The plan checks:

- provider availability
- Agent Mail MCP/hooks readiness
- live Agent Bridge tmux sessions that can be reused
- disabled slots
- provider launch option validity
- unsafe launch combinations, such as multiple same-repo Codex slots using `resume --last` or Copilot slots using `--continue`

By default, launch only includes enabled slots and only reuses wakeable sessions observed through Agent Bridge. Connected non-tmux Agent Mail sessions can still communicate, but they are not reliable team launch/reuse targets.

For same-repo Codex or Copilot teams, prefer fresh sessions (`plain`) or explicit `resume` session ids per slot. Do not use Codex `resume --last` or Copilot `--continue` for more than one slot in the same repo: each slot can resume the same conversation and lose the role-specific team boundary.

## Agent Activity and Your Actions

The roster and work-item owner show activity for the owner's current harness.
This is separate from the issue's delivery status:

| Label | Meaning | Appearance |
| --- | --- | --- |
| Working | A native turn started and has recent native progress. | Gentle halo pulse; text stays readable. |
| Idle | The native turn finished or was interrupted. The harness can still be running. | Steady. |
| Stopped | The bound process ended or was suspended. | Steady. |
| Activity unknown | The binding, provider, log access or fresh observation cannot confirm activity. | Steady. |

The pulse respects the system's reduced-motion preference. Activity is shared
across an owner's issues; it does not prove which specific issue is being worked
on. A dispatched issue, an enabled autonomy switch and a running harness do not
start the pulse.

Human review and merge gates show **Your review or merge is needed**, explain why
the team is waiting and link to the PR. The activity summary counts items needing
your action and can filter them. Leader approvals are labeled separately. For
operator recovery checkpoints or stranded initial approvals, the action remains
visible for the operator instead of being labeled as a Leader decision. Under
automatic merge policy, a review-ready item needs your action only when Synth City has
explicitly fallen back to human merge. A code PR under automatic merge policy merges
only after the team's verifier slot has accepted its exact current head; the item's
owner and the Leader cannot supply that acceptance. Passing checks do not prove independent
review acceptance. Check the current PR head and review evidence before merging.

When you move an issue to another milestone, end its work item as deferred and
name the target milestone. The team learns about the move from this action until
milestone routing is available. [#1223](https://github.com/albiexai/synth-city/issues/1223)

### Observation limits

The initial adapter supports Codex CLI sessions with an explicit resume UUID and
an authenticated current process binding. Other providers and fresh sessions
without a pinned native identity show Activity unknown. The controller must be
able to read the process's open file descriptors and the corresponding native
rollout log. An exact-ID lookup in the Codex state database is used as a fallback
when the process has no open rollout descriptor and the database is readable.
Each Codex slot must have a distinct UUID across Synth City presets. Reused UUIDs show
Activity unknown, including reuse in disabled slots, because the shared log cannot
distinguish which harness supplied an event.

The read-only `GET /api/v1/agent-teams/presets/{preset_id}/activity` endpoint
returns only slot, state, reason and timestamps. It does not expose transcript
content, credentials, native IDs or filesystem paths. It checks process start
identity, current process lifetime, session metadata and repository path, and reads a bounded log tail
outside the event loop. No activity is inferred from terminal text or Mail prose.

The UI polls the selected team every five seconds while the page is visible.
Responses expire after fifteen seconds; request failure, hidden pages and team
switches clear the pulse. A working native event older than three minutes becomes
unknown, including long work that produces no native progress events. A completed
turn can remain Idle while the same process and identity are still current.

### Deployment with separate controller and agent users

Set `harness_agent_user` to the agent user's name so that the Harnesses pages show the global scope that the agents load, not the controller's own. See [Which user the Harnesses pages show as "global"](/guide/installation#which-user-the-harnesses-pages-show-as-global).

If the controller cannot read the runtime user's Codex history, labels remain
steady at Activity unknown. Before enabling observations, arrange read access to
the dedicated runtime's process descriptors and rollout logs, with directory
traversal access. The indexed fallback also needs access to the native state
database and SQLite WAL/SHM files. SQLite can require writable scratch files for
that fallback even with a read-only connection; the process descriptor lookup
avoids that requirement. New rollout files must inherit the same read access.
Grant read access only for that dedicated runtime; do not make home directories
or histories world-readable. This PR does not change live filesystem permissions.

For custom `CODEX_HOME` or `HOME`, the process environment must be readable by the
controller. When it is not readable, the adapter uses the process owner's default
Codex home and returns unknown if it cannot find and validate the exact session.

For Muse Code, the observer finds each member's sessions root from the observed
process, not from the controller. When the process environment is readable, it
uses the process's own `XDG_DATA_HOME/muse`, then `MUSE_DATA_DIR`, then
`HOME/.local/share/muse`; otherwise it uses the process owner's
`~/.local/share/muse`. A relative value is refused. Muse 1.4.20 wrote its sessions
under `XDG_DATA_HOME/muse` when both variables were set; `MUSE_DATA_DIR` on its own
has not been observed. A controller-wide `MUSE_DATA_DIR` override is therefore no
longer needed for observation. The guard keeps its own explicit
`muse_sessions_root` setting.

Muse writes its session folders with mode `770` and its files with mode `660`, in
the agent's primary group, so the controller cannot read them by default. To give
the controller read access:

- Choose a group that both the agent user and the controller user belong to.
- Give the agent's Muse `sessions` tree that group and set the setgid bit on its
  directories, so that new date and session folders inherit the group.
- Keep the tree closed to other users. Do not make the home directory or the
  sessions world-readable.

When the controller still cannot read a member's session log, its activity shows
"The controller cannot read this member's native activity log" (reason
`native_log_unreadable`) instead of a missing log. This change does not alter live
filesystem permissions.

A first plain Muse launch learns its session id from the process's open
descriptors: the pane process itself holds its session's `.session.lock`, which
must be the only one it holds, under that process owner's root, with a session log
for the pane's working directory. Reading another user's descriptors needs the
controller service's ptrace read capability (`cap_sys_ptrace`); the production unit
is reported to grant it, but that has not been verified independently. If the
controller cannot read the descriptors, set the member's first session id by hand
and restart the slot in resume mode. The evidence and the route are recorded on
issue #746.

#### Verify first-session discovery in production (read-only)

`scripts/muse-discovery-probe.py` runs the production discovery function against one process and prints a single
JSON object of facts. It reports capability names, whether the target belongs to another user, whether its
descriptor directory can be listed (and, if not, only the kind of failure), counts of the `.session.lock`
descriptors it holds, and whether discovery returned an id; with `--expect-session-id` it also says whether the
discovered id matched. It never prints a path, an id, an environment value, a log line or a command line. It writes
nothing, sends no signal and makes no network call, and it starts and stops no member.

Whether to run it, against which process, and whether any Muse member has to exist for it, is the operator's
decision. A member started for the check, as the agent user, needs the Supervisor's explicit approval first.
Run it as the same user and with the same capabilities as the controller service, from the repository root, with the
backend virtualenv's Python:

```
setpriv <the service's capability set> backend/venv/bin/python scripts/muse-discovery-probe.py \
    --pid <the Muse pane process> --cwd <that pane's working directory> [--expect-session-id <the session id>]
```

Read the result like this:

- `fd_listable: false` with `fd_failure: permission_denied` means the descriptors of another user's process cannot
  be read with this capability set, so first-launch discovery cannot work there and the manual first session id
  applies.
- `lock_descriptors: 1` and `locks_under_sessions_root: 1` with `discovered: true` is the expected result; with
  `--expect-session-id`, `matches_expected: true` confirms it is the right session.
- `lock_descriptors` above 1 means the process holds several sessions, so discovery refuses by design.
- `locks_under_sessions_root: 0` with a lock descriptor means the lock is outside the sessions root the observer
  derived from the process environment; see the root order above.

Run it once with the capability set, once without it as a control (it should report `permission_denied` for a
process of another user), and once for a process that sets only `MUSE_DATA_DIR`. The probe checks the read; it does
not by itself prove the production unit's capabilities, which are only as reported until it has been run there.

### Antigravity CLI members

Antigravity CLI (`agy`) members cannot be launched yet. Readiness stays blocked
until Agent Mail for `agy` is installed (#736) and until onboarding can be
confirmed. On first run, `agy` shows its own onboarding, which includes Google's
Terms of Service and a data-use choice. **Synth City never accepts these terms and
never presets onboarding settings**: each user completes onboarding once, in their
own `agy`, as the agent user. Completing onboarding makes `agy` write
`~/.gemini/antigravity-cli/cache/onboarding.json` with `"onboardingComplete": true`
(recorded in a human trial, #995). Readiness only reads that file: a missing, unreadable
or other value counts as not completed, and Synth City never writes it or reads the
sign-in files. Until Agent Mail through `agy` is proven (#994), readiness stays blocked
by the Agent Mail gate before onboarding is checked.

When a member launches, Synth City adds the workspace to `trustedWorkspaces` in the
agent user's `~/.gemini/antigravity-cli/settings.json` and reports
`workspace_trust_changed`. Issue #952 observed that this registers the workspace
with `agy` 1.5.9; it did not observe whether it also suppresses a trust prompt.
Other settings in that file are kept unchanged. A resumed member is launched with
`agy --conversation <id>` and, if the slot has a prompt, `-i <prompt>`.

#### Antigravity activity (#737)

Synth City reads an `agy` member's activity read-only from the agent user's
`<passwd home>/.gemini/antigravity-cli` (never from `$HOME`). Every directory from the
home down must belong to the agent user, must not be a symlink and must not be
world-writable.

- **Identity.** The live `agy` process holds `presence/<conversation id>.lock` open. On a
  first plain launch, Synth City takes the id from the single lock the process holds,
  checks that `conversation_summaries.db` lists the member's workspace, saves the id to
  the slot and asks for a restart in resume mode. An unknown result never clears a saved
  id. A conversation id configured on more than one slot is never observed or
  discovered (`duplicate_native_identity`).
- **Directories.** Every directory below the home is opened without following symlinks
  and held open while its files are read; a symlinked, foreign-owned or world-writable
  directory, or one replaced during the read, gives `unknown`.
- **Snapshots.** Two matching copy rounds of both SQLite databases and WAL files
  bracket a transcript sample. The named transcript and its open file descriptor must
  keep the same identity and metadata throughout the sample. A database change gets
  one retry; persistent changes, damaged copies and database files over 16 MiB give
  `unknown`. SQLite reads private copies with fixed queries and an instruction budget
  checked every 100 virtual-machine instructions. The live files are read as bytes.
  The matching reads establish an overlap for the sampled files. The databases can
  still contain different logical states while the harness commits them separately.
- **Deadline.** Discovery and observation each have a one-second deadline. The activity
  service runs the read in a child with the controller's permissions and kills a timed-out
  child. The parent owns and cleans up the copy directory. An overdue result gives
  `unknown` and an overdue discovery provides no conversation ID.
- **Transcript.** From `brain/<id>/.system_generated/logs/transcript.jsonl`, only the
  step index, type, source, time and tool names are read; message text is ignored.
- **States**, from the shapes observed on `agy` 1.5.9 (the turn is the latest user input,
  from a later second than the process start and present in the live steps):

  | State | Evidence |
  |---|---|
  | working | Summary RUNNING. The last live step is the input, a system message, a finished tool step after a single-tool reply, or a running (`2`) `run_command` step right after its reply when the member was launched with exactly `--dangerously-skip-permissions`. |
  | waiting for an answer | Summary RUNNING. A single `ask_question` call, then its step waiting (`9`). |
  | idle | Summary IDLE with matching step counts, and the last live step is a finished reply after the input. |
  | unknown | Everything else, including other tools running or waiting (`native_wait_unconfirmed`), permission reviews, more than one tool call, and status `6` (seen only after exit). |

  Only `run_command` with `--dangerously-skip-permissions` was observed running, so a
  running step counts as working only when the bound process's command line has that
  flag. Other tools, other modes and permission reviews were not observed and stay
  unknown. Every record of the current turn must be well formed and dated between the
  input and now.
- **Screen evidence.** One passive `agy` 1.5.9 launch with the isolated #993/#994
  runner reached the first-run colour-scheme screen. The scratch backend applied
  Agent Mail configuration. The run stopped at onboarding with no input. It produced
  no transcript or requests to the local model stub, and the trial processes exited.
  Agent Mail delivery through `agy` and the screens needed for working, waiting and
  idle recognition remain unverified. The classifier keeps unproven activity `unknown`.
  See [#1215](https://github.com/albiexai/synth-city/issues/1215).
- **Separate controller and agent users.** The controller must be able to read the
  agent user's `antigravity-cli` files. Otherwise the status is `native_log_unreadable`.
  The passive launch used one host user. Access across two users remains unverified.

## Supervisor Binding

The Supervisor line of a team page has an **Edit** button. It opens the supervisor binding editor. The editor needs the Director key.

- **Bind:** Enter the id of an external actor. Select **Save**. The server checks the id. An unknown id gives "No external actor has this id." The actor list is not in the UI yet (#1291), so you type the id.
- **Change the grace:** Enter whole seconds, 1 or more. Select **Use the default grace** to remove the override. The default grace is at least 30 minutes.
- **Remove:** Select **Remove the supervisor** and confirm. This also clears the grace setting.
- **History:** Each change is recorded in the audit history. The editor lists these changes, newest first, with the actor and the reason.
- **Other clients:** Each save carries the binding version and the team key that you reviewed. The team key names the life of the team: if another client changed the binding first, or deleted the team and made a new team with the same id, the server refuses the save and the editor shows the current binding. Review it, then save again. A removal confirmation is valid only for the binding that you confirmed.
- **Older server:** If the server does not report a binding version and a team key, or the team has no key yet, the editor is read-only. Update the server to edit the supervisor.
- **API:** `PUT /api/v1/agent-teams/presets/{preset_id}/supervisor` accepts the optional `expected_binding_version` and `expected_team_context_key`. They come together (one alone gives `422`). Without them, the write is not checked. With them, a different stored version, or a key that is not the active context key of the team, gives `409 supervisor_binding_changed` and changes nothing. Every binding write adds 1 to `binding_version`; check-ins do not. Every team created through the API gets its context key at once, and a deleted team's key is retired, so a number is never reused for another team.

## Milestone Moves

A team admits the issues that have its backlog milestone. When an issue gets a different milestone, two states can follow. The Teams page (Autonomy tab) and the Work page show both. Each row links to the GitHub issue.

- **Deferred by a milestone move:** the team ended its work item because the issue now has another milestone. The row shows the new milestone, the team, and when it happened. If the milestone was not recorded, the row says so.
- **Waiting for a team:** the issue has the ready label, and no team has its milestone. The row shows the milestone. A wait stores no issue title, so the row shows the issue number only.

A deferral shows only while it is the newest item of its issue in the repository. An issue that another team admits, or that was merged, does not show as deferred. A cleared wait does not show.

A row keeps the repository it was recorded for. A deferral takes it from the GitHub issue URL that its work item stored, and a deferral whose URL does not name its issue is not shown. A wait stores no repository, so changing the repository of a scope deletes the waits of that scope. The next poll records the waits of the new repository. A poll that began before such a change writes nothing for that scope.

The Teams page shows the rows of the selected team. The Work page follows the team and repository filters of its URL. The view is read-only. It shows at most 100 rows of each kind and says when a list is cut. It shows nothing when both lists are empty.

API: `GET /api/v1/factory/milestone-states` with the optional filters `team_id` and `scope_id`. It needs no operator key. Waits and deferrals come from the watcher; this view only reads them.

## External Local Agents

Local external agents can use the JSON API:

1. `GET /api/v1/agent-teams/presets`
2. `POST /api/v1/agent-teams/presets/{preset_id}/plan-launch`
3. inspect `items` and `plan_hash`
4. `POST /api/v1/agent-teams/presets/{preset_id}/launch`

Launch requires an operator credential or eligible authenticated Mail-session principal and a reviewed current `confirm_plan_hash`. A stale plan returns `409` with the updated plan. Explicit skip-plan-confirmation, adoption and launch overrides are operator-only; authenticated agent sessions cannot bypass confirmation.

After a launch, use the [External Agent Orchestration](./external-agent-orchestration.md) Agent Mail API to discover registered participants, send context requests, create handoffs, and poll for answers.
