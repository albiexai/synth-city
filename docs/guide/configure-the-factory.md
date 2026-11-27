# Configure the factory

This page tells an operator what to set, and in which order, after the installation. Read [Start here: how Synth City works](/guide/how-it-works) first. It explains the ideas and the words. The [Glossary](/guide/glossary) defines each term.

Each section links to the reference page that has the details. This page does not repeat them. Ports, paths, user names, labels and repository names in the examples are examples. Use your own.

## Day one, in order

1. **Server settings.** Set the operator token and the GitHub token in `.env`. Restart the backend. See [Server settings](#server-settings).
2. **GitHub.** Create the labels. Make sure that CI runs on pull requests (PRs) into your base branch. See [GitHub: labels, branch and CI](#github-labels-branch-and-ci).
3. **Agent Mail.** Install the Agent Mail hooks and the MCP server for each harness (the agent program) that you use. See [Installation](/guide/installation) and [Agent Mail](/features/agent-mail).
4. **Team.** Create a team, add its slots and choose the Leader. See [The team](#the-team) and [Slots](#slots-one-per-team-member).
5. **Repository scope.** Add a repository scope. Save it disabled. See [The repository scope](#the-repository-scope).
6. **Policies.** Set the delivery policy and the continuation policy. See [Delivery policy](#delivery-policy) and [Continuation policy](#continuation-policy).
7. **Workspaces.** Register one dispatch workspace for each item that may run at the same time. See [Dispatch workspaces](#dispatch-workspaces).
8. **Launch.** Launch the team and check that each member is ready.
9. **Readiness.** Read the activation readiness of the scope. When it has no blockers, enable the scope.
10. **Autonomy.** Turn autonomy on, with low concurrency. Watch the first item from plan to merge.
11. **Ready labels.** Add the ready label to the first issues.

The guided setup on the [Repositories](/features/repositories) page does steps 4 to 8 for you, and it keeps the new scope disabled until you enable it. See [Turn it on](#turn-it-on).

## Server settings

These settings live in `.env`. Nothing here is needed to start Synth City. They matter when you run teams and autonomy. Setting names are not case-sensitive: `operator_token` and `OPERATOR_TOKEN` are the same setting.

The backend reads `.env` from its working folder. Real environment variables override the file. A setting that holds a list needs a JSON value, for example `GITHUB_COORDINATION_HOLD_PATHS=["/path/to/HOLD.json"]`.

| Setting | Default | Example | What it decides |
|---|---|---|---|
| `OPERATOR_TOKEN` | empty | the output of `openssl rand -hex 32` | Protects every operator change. **Set it.** |
| `GITHUB_TOKEN` | empty | a token with access to the repository | Polls issues, PRs and checks, and merges. Autonomy needs it. |
| `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_PATH`, `GITHUB_APP_BOT_LOGIN` | empty | — | Optional. Set all three to let the factory act as a GitHub App. A partial set is an error. |
| `HOST`, `PORT` | `127.0.0.1`, `8000` | `127.0.0.1`, `8011` | `PORT` is the port that agents use to call back (the Mail shim and the git credential helper). The address that the backend listens on comes from the command that starts it (`--host`, `--port`). Keep it on localhost. |
| `DATABASE_URL` | `sqlite+aiosqlite:///./synth_city.db` | a path in your state folder | The path is relative to the working folder. Use an absolute path on a factory. |
| `HARNESS_AGENT_USER` | none | `agentuser` | The user whose `~/.claude` folder the Harnesses pages show as "global". Use it when the agents run as another user. |
| `MAIL_CAPABILITY_TOKENS_REQUIRED` | `true` | `true` | Agents must prove their session for Agent Mail writes. This ties a session to its terminal pane, and it works on Linux only. |
| `GITHUB_DISPATCH_INTERVAL_SECONDS` | `60` | `60` | How often the watcher polls GitHub. |
| `GITHUB_MIN_AVAILABLE_MEMORY_MB` | `3000` | `3000` | Dispatch waits while the available memory (`MemAvailable`) is below this value. |
| `GITHUB_LEADER_ACK_TIMEOUT_SECONDS`, `GITHUB_LEADER_ACK_MAX_WAIT_SECONDS` | `300`, `1800` | — | How long the Leader has to answer a plan. Each team can override them. See [The team](#the-team). |
| `GITHUB_OWNER_IDLE_TIMEOUT_SECONDS` | `900` | — | When an idle builder gets a nudge. |
| `MAIL_REQUEST_UNANSWERED_SECONDS` | `1200` | — | When an unanswered Mail request counts as stalled (60 to 86400). |
| `GITHUB_COORDINATION_HOLD_PATHS` | empty list | `["/path/to/HOLD.json"]` | An emergency stop. While any listed file exists, new dispatch, automatic merge, reminders and Leader coordination hold. Agents that already run keep running. Use absolute paths. |
| `GITHUB_PUBLISH_SUMMARIES_ENABLED` | `false` | `true` | Synth City keeps one comment on each PR that shows the head, the checks, the review verdict and the remaining work. It edits that comment in place. Each team can override this. |

::: tip Why so many timers?
An agent cannot tell you that it is stuck. It stops talking. Synth City watches for silence, not for errors. Each timer means: after this long without news, do something. It nudges the builder, reminds the Leader, tells the Supervisor or escalates. Change a timer only after you have seen a real problem.
:::

These timers and limits change rarely. The default value is in brackets.

- `GITHUB_CHECK_SIGNAL_GRACE_SECONDS` (120), `GITHUB_OWNER_REGISTRATION_GRACE_SECONDS` (120), `GITHUB_NUDGE_GRACE_SECONDS` (180)
- `GITHUB_DESIGN_ACK_MULTIPLIER` (3): design issues get this multiple of the Leader windows.
- `GITHUB_TEAM_IDLE_TIMEOUT_SECONDS` (1800), `WAKE_FOLLOWUP_ENABLED` (true), `WAKE_FOLLOWUP_DELAY_SECONDS` (120)
- `GITHUB_STALE_LEASE_BACKSTOP_SECONDS` (21600), `GITHUB_BRIEF_DELIVERY_MAX_NUDGES` (2), `GITHUB_CONTINUATION_PROPOSAL_EXPIRY_SECONDS` (3600)
- Three `…_NUDGE_COOLDOWN_SECONDS` settings (180 each).
- `BRIDGE_ATTACHMENT_*`: where pasted images are kept, and their limits (10 MB each, 7 days, 100 for each session each day).

## GitHub: labels, branch and CI

The factory reads labels. It does not read minds. A label is visible on the issue, it has a history, and anyone can change it from any device. You steer the factory by changing labels.

| GitHub item | Example | Effect |
|---|---|---|
| **Ready label** (`dispatch_label`) | `my-project-ready` | Adding it queues the issue. Removing it while the item is pending or running escalates the item. |
| **Design label** (`design_label`) | `my-project-design` | The issue becomes a design item. A person always reviews a design PR. |
| **Review hold label** (`review_hold_label`) | `my-project-review-hold` | While the PR or its issue has this label, automatic merge waits. The backend only reads the label. People and agents add and remove it. |
| **Area labels** | `area-core`, `area-ui` | Routing. The first enabled builder, in roster order, whose `area_labels` include one of the issue's labels gets the issue. The match is exact and case-sensitive. If none matches, the issue stays pending with the reason `no_eligible_owner`. |
| **Difficulty labels** | `difficulty:low`, `difficulty:medium`, `difficulty:high` | Only builders whose capability tier allows the difficulty can get the issue. See [Slots](#slots-one-per-team-member). |
| **Priority labels** | `priority:1` to `priority:9999` | Queue order. The lowest number goes first. An issue without a priority label goes after all the others. |
| **Milestone** | `v1.0` | The team's `backlog_milestone`. The team admits only issues in that milestone. See [The team](#the-team). |
| **Base branch** | `release/v1.0` | Where PRs go. Its CI must run on PRs into that branch. If CI does not run, no checks appear and nothing merges. |
| **CI checks** | five job names | The checks that the delivery policy names in `required_checks`. See [Delivery policy](#delivery-policy). |

::: tip Lanes
Give each builder its own area label, and give each issue one area label and one priority. Two builders then rarely work on the same part of the code, and the queue stays clear. An issue without an area label is never picked up. That keeps tracking issues out of the queue.
:::

Never hold a queue by removing ready labels, because that escalates items. Give an item a higher priority number instead, or turn autonomy off. See [Autonomous GitHub Dispatch](/autonomy).

## Which GitHub account the factory uses

Every action on GitHub is done as some account. The factory has three kinds of actor. You choose the account for each of them.

| GitHub action | Done by | As which account |
|---|---|---|
| Read issues, PRs and checks | The backend | The owner of `GITHUB_TOKEN` |
| Merge, and close an issue after a merge | The backend | The owner of `GITHUB_TOKEN`, also in App mode |
| Push the branch | The builder | **Ambient mode:** the agent user's own `git` and `gh` login. **App mode:** a short-lived App token from a credential helper. The helper works only while the builder holds the lease of its workspace. |
| Open the draft PR | The builder (ambient mode), or the backend (App mode) | Ambient mode: the agent's login. App mode: the App's bot. |
| Add or remove labels, and comment | The builder or the verifier, with `gh` | The agent user's `gh` login |
| Author of a commit | The builder | Not a GitHub account. It is the slot identity that the backend writes into the workspace. |

The mode is a field of the repository scope: `github_auth_mode` is `unknown` (the default), `ambient` or `app`. It is not a server setting. With `unknown`, the first dispatch uses `app` when the App settings are complete and the App is installed on the repository. Otherwise it uses `ambient`.

Three choices work, from simple to strong:

| | Your own account | A machine user | A GitHub App |
|---|---|---|---|
| What it is | Your own login, for the agents and for `GITHUB_TOKEN` | A separate GitHub account for the factory, invited to the factory's repositories only | An App that you create and install on the repositories. Synth City makes short-lived tokens from its private key. |
| Setup | None | A new account, an invitation with **Write** access (not Admin), and a fine-grained token | Create the App, keep its key, install it, and set the three `GITHUB_APP_*` settings |
| If it is misused | Everything that you can do | Only the factory's repositories | Only the installed repositories, and pushes only while a lease is held |
| Who did what on GitHub | You cannot tell your actions from the factory's | The factory's account and yours are separate | The bot and people are separate |
| Good for | A trial on a test repository | A first real factory | A long-running factory, or several teams |

::: tip Recommendation
Use a machine user for a dedicated factory from the first day. Give its fine-grained token only these permissions, and only on the factory's repositories:

| Permission | Access | Why |
|---|---|---|
| Contents | Read and write | Push branches, and merge |
| Pull requests | Read and write | Open PRs, mark them ready, and merge |
| Issues | Read and write | Labels, comments, and closing issues |
| Checks, Commit statuses, Actions | Read | Required checks and their jobs |
| Metadata | Read | Required by GitHub |

Log the agent user in as that account (`gh auth login`, as the agent user). Use a token of the same account for `GITHUB_TOKEN`. Never give the agents an Owner or an Admin token. Keep branch protection with required checks on your main branch as a safety net.
:::

In App mode the backend still uses `GITHUB_TOKEN` for polling and for the merge call. With Auto merge policy it needs write access to contents, pull requests and issues. A read-only token works only with Human merge policy, and then the backend cannot close issues.

## The team

A team is a preset: a roster of slots with one Leader, and the campaign settings. One team for each campaign is a good habit.

| Field | Default | Example | What it decides |
|---|---|---|---|
| `name` | none | `my-project-v1` | Unique. |
| `description` | none | "the first release" | Shown on the Teams page and to agents. |
| `backlog_milestone` | none | `v1.0` | The team admits only issues in this GitHub milestone. An issue in another milestone is deferred. Overview shows the progress of the milestone. Two teams on one repository must not claim the same milestone: neither would dispatch. |
| `parent_issue_url` | none | the URL of a tracking issue | A fallback for progress: the backlog reader uses the milestone of that issue. It must be an HTTPS GitHub issue URL. |
| `planned_decisions` | none | up to 12 entries, each with a text and a state (`later`, `now` or `done`) | Your own list of decisions for the campaign. Overview shows it. |
| `leader_ack_timeout_seconds`, `leader_ack_max_wait_seconds` | the server default (300, 1800) | `300`, `1800` | How long the Leader has to answer a plan request (30 to 86400). The maximum must not be below the timeout. |
| **Leader** (`PUT …/presets/{id}/leader`) | none | the Leader slot | The Leader approves plans. It never owns an issue. You can change it only while autonomy is off and the team is quiet (no dispatched item, pending approval or leased workspace). |
| `autonomy_enabled` | `false` | `false` until launch | The master switch for dispatch. Turning it on needs an enabled Leader slot. |

The UI shows the milestone, the planned decisions and the Leader timers, but it does not edit them. Change them with `PATCH /api/v1/agent-teams/presets/{preset_id}` and the operator token. See the [Agent Teams API](/api/agent-teams).

::: tip Why a Leader, and why a deadline?
Builders are good at doing, but not at judging scope. They tend to do too much. The Leader reads each plan before any code exists, when a correction is cheap. It never builds, so it never approves its own work.

A plan request blocks a builder. After `leader_ack_timeout_seconds` the Leader gets a reminder, and a bound Supervisor gets a notice. After `leader_ack_max_wait_seconds` an inactive Leader's request can end, so a busy or broken Leader cannot hold builders for ever.
:::

::: tip Duplicate a team
Duplicate the last team for a new campaign. `POST /api/v1/agent-teams/presets/{preset_id}/duplicate` copies the description, the milestone, the planned decisions, the Leader timers and every slot with its charter and launch options. It maps the Leader to the new slot. It does **not** copy autonomy (the copy is off), the Supervisor binding or any repository scope. It also keeps each `session_id`. Clear every copied `session_id`, so that each member starts a new conversation.
:::

## Slots: one per team member

A slot is one seat in the team. The agent in it is the member. Add the slots on the Teams page or in the guided setup. See [Teams](/features/agent-teams).

| Field | Default | Example | What it decides |
|---|---|---|---|
| `display_name` | required | `Builder 1 (core)` | Shown everywhere. Name the role and the model. |
| `provider` | `codex-cli` | `claude-code` | The harness: `claude-code`, `codex-cli`, `copilot-cli`, `opencode-cli`, `pi-cli`, `muse-cli` or `antigravity-cli`. |
| `role` | none | `coordinator`, `implementer`, `verifier` | Free text. Two values have a meaning in the code. A `coordinator` slot never gets routed work. A `verifier` slot can accept a head for automatic merge. The Leader is chosen with the Leader field, not with the role. |
| `repo_path` | required | `/home/agentuser/work/build-1` | The member's own folder. It must be an absolute path under the home folder. Keep it in the same repository as the scope. |
| `area_labels` | none | `["area-core"]` | Routing: which issues this builder takes. |
| `capability_tier` | unset (counts as high) | `medium` | `low`, `medium` or `high`. With difficulty labels, only slots whose tier allows the difficulty get the issue. Otherwise the issue stays pending with `difficulty_above_tier`. |
| `charter` | none | the member's rules, in plain sentences | **The most important field.** See [Recommended charter practices](#recommended-charter-practices). |
| `bootstrap_prompt` | none | "Read your charter, then the open issues." | When set, it replaces the default first message. The Leader instructions and the delivery defaults are still added. |
| `launch_mode` | `plain` | `plain`, then `resume` | `plain` starts a new conversation. `resume` continues a saved one. See below. |
| `launch_options` | `{}` | see the next table | Flags for the harness. |
| `controlled_language_enabled` | `true` | `true` | Asks the agent to write short ASD-STE100 sentences. |
| `expertise` | none | — | Stored. It does **not** route work. Use area labels. |
| `enabled` | `true` | `true` | A disabled slot gets no work and cannot accept a review. You cannot disable the Leader slot. |
| `ui_color`, `position` | none | `blue` | Display only. The color is one of blue, purple, green, amber, red, cyan or slate. |

::: tip Why must a different agent verify the work?
An agent that reviews its own work sees what it meant to write, not what it wrote. So the owner of an item and the Leader can never accept a PR. Only an enabled slot with the role `verifier`, in the same team, can. The acceptance is tied to one exact commit (the head), because a push after the review could add code that nobody reviewed. After a push, the item shows "Head changed since acceptance", and automatic merge waits until a verifier accepts the new head.
:::

### Plain or resume?

`plain` starts a new conversation. The agent knows only its charter and its first message. `resume` continues a saved conversation (`session_id`) with everything that the agent learned.

- **Claude Code:** the modes are `plain`, `worktree` (needs `launch_options.worktree_name`) and `resume`. After a plain launch, Synth City writes the new `session_id` into the slot and switches the slot to `resume`.
- **Codex CLI:** the modes are `plain`, `resume` and `fork`. A plain launch stays plain. You switch to `resume` yourself. Synth City can see the activity of a Codex member only in `resume` mode with an explicit session id. Before that, the activity shows as unknown.
- **Other harnesses:** `plain` and `resume`.

### Launch options

A key that belongs to the other harness is refused with `Unsupported launch_options for <provider>`. A `model` must be a concrete model id.

| Option | Claude Code | Codex CLI | Example |
|---|---|---|---|
| `model` | `--model` | `--model` | a concrete model id |
| `reasoning_effort` | `--effort`: `low`, `medium`, `high`, `xhigh`, `max` | `model_reasoning_effort`: `low`, `medium`, `high`, `xhigh` | `high` |
| `skip_permissions` | `--dangerously-skip-permissions` | not accepted | `true` for unattended members |
| `dangerously_bypass_approvals_and_sandbox` | not accepted | bypass approvals and sandbox | `true` for unattended members |
| `sandbox`, `approval_policy` | not accepted | `--sandbox`, `--ask-for-approval` | limits instead of the bypass |
| `no_alt_screen` | — | `--no-alt-screen` | `true` (keeps the pane readable) |
| `session_id` | `--resume <id>` in `resume` mode | resumes that session (`fork <id>` in `fork` mode) | set after the first launch |
| `use_last` | — | `resume --last` | `false` for team members |
| `profile` | — | `--profile` | a Codex profile name |
| `prompt` | the last argument | the last argument | optional |

::: warning Unattended members
Unattended members need the bypass options. Without them, a member stops at the first permission prompt and waits for no one. So the charter, the review hold and the verifier carry the safety, not the permission prompts of the harness.
:::

### Recommended charter practices

Synth City ships charter templates for the Leader, the builders and the reviewers. Guided setup offers them with **Start from a template**. See [Charter templates](/features/charter-templates). These practices have worked in earlier campaigns.

| Practice | Why |
|---|---|
| Keep a plan to about 60 active minutes. The scope enforces its plan limit, and a plan over the limit is refused with `plan_estimate_exceeds_limit`. | Big plans fail review and stall. Split them. |
| Run focused tests locally. Run the full suites on CI. | Builders do not wait for the full suites. CI runs them while the verifier reviews. |
| Put a test evidence block in every handoff. | The verifier trusts the run without repeating it. |
| Put the review hold label on every PR. The accepting verifier removes it. | The backend blocks automatic merge while the label is on the PR or the issue. Who removes it is a team rule. |
| Never use interactive question tools. | No one answers, and the agent waits for hours. |
| Send each question to the right place: plans to the Leader, incidents to the Supervisor, roster, model, budget and release questions to the Director. | A question goes to whoever can decide. |
| Never use `pkill` or pattern kills. | All members can share one operating system user. |
| PRs target the release branch, never the main branch. | Nothing reaches the main branch before the release. |

## The repository scope

A repository scope is one team's watch on one GitHub repository. Add it on the [Repositories](/features/repositories) page (guided setup), or with `POST /api/v1/agent-teams/presets/{preset_id}/github-scopes`. A team has one scope for each exact owner and name of a repository. See [Repositories](/features/repositories) for the definition.

| Field | Default | Example | What it decides |
|---|---|---|---|
| `repo_owner`, `repo_name` | required | `example-org`, `example-repo` | The GitHub repository. Use the same spelling every time. |
| `repo_path` | required | `/home/agentuser/work/main-clone` | An existing main clone (the primary checkout). The dispatch workspaces are created beside it. |
| `base_ref` | `origin/HEAD` | `origin/release/v1.0` | What each item branches from, and where its PR goes. |
| `dispatch_label` | `albiex-synth-city-ready` | `my-project-ready` | The ready label. |
| `design_label` | `albiex-synth-city-design` | `my-project-design` | The design label. |
| `review_hold_label` | none | `my-project-review-hold` | Blocks automatic merge while it is on the PR or the issue. |
| `merge_policy` | `human` | `auto` | `human`: PRs wait for a person. `auto`: the backend merges when the required checks are green, a verifier accepted the exact head and no hold label is on. |
| `max_auto_merges_per_day` | `5` | `10` | A cap on automatic merges in 24 hours. |
| `max_concurrent_dispatched` | `3` | `2` | Items that run at the same time. Each one needs its own workspace. |
| `max_item_effort_minutes` | `120` | `120` | The effort ceiling of an item (15 to 1440). See below. |
| `max_plan_estimate_minutes` | `60` (never above the ceiling) | `60` | The largest estimate that a first plan may state (15 to 1440). Set it by API. |
| `initial_paths_required` | `false` | `false` | When `true`, a first plan must list its files. |
| `max_approval_rounds` | `3` | `3` | Plan rounds before the item escalates. |
| `max_verification_retries` | `2` | `2` | Review or CI retries before the item escalates. |
| `github_auth_mode` | `unknown` | `ambient` | `ambient` uses the host's login. `app` uses the GitHub App. See [Which GitHub account the factory uses](#which-github-account-the-factory-uses). |
| `builds_out_of_tree`, `build_dir_template`, `build_command_hint`, `max_build_parallelism` | `false`, `build`, none, `4` | defaults | For compiled projects that build outside the source folder. |
| `enabled` | `true` by API | `false` until launch | Whether the watcher polls this scope. Guided setup saves a new scope disabled, with lower limits (one concurrent item, one verification retry, and no automatic merges under Human policy). |

The Repositories page edits the review hold label, the effort ceiling and `initial_paths_required` (the **Review hold and effort limit** dialog), and the continuation policy. The other fields change with `PATCH /api/v1/agent-teams/github-scopes/{scope_id}` and the Director key. See the [Agent Teams API](/api/agent-teams).

::: tip Why limits on the scope?
Each limit stops a different kind of runaway.

| Limit | Without it | Example |
|---|---|---|
| `max_concurrent_dispatched` | Builders pile up items that they cannot finish, and the CI queue grows. | `2` is one item for each builder. |
| `max_approval_rounds` | A builder and a Leader argue about a plan for ever. | After 3 rejected plans the item escalates. The issue probably needs a split. |
| `max_verification_retries` | A PR keeps failing CI, and the builder keeps trying. | With `2`, the third failed round escalates. |
| `max_plan_estimate_minutes` and `max_item_effort_minutes` | Huge plans get approved. | A plan that states 90 minutes is refused with the defaults. Split the issue. |
| `max_auto_merges_per_day` | A bad rule or a broken check merges many PRs before you notice. | Use a low number while you build trust. |
:::

**Effort.** The estimate in the approved plan becomes the effort limit of the item. Reaching it stops nothing. The Leader and the builder each get one message. The Leader can then extend the limit in steps. Each step is at most 30 minutes, or 25% of the original limit if that is less. An extension never goes past `max_item_effort_minutes`. It is a speedometer, not a brake. A plan with no estimate gets the ceiling as its limit.

**Merge policy.** Start with `human` while you learn how your team works. Nothing reaches your branch without your click. Move to `auto` when you trust the verifier and the checks. Design work always waits for a person, whatever the policy.

## Delivery policy

The delivery policy holds the rules of your project for "delivered". It makes Synth City work with any repository and any CI. It belongs to the repository scope. It has a revision number and a history. No page edits it yet. Use the API.

| Field | Default | Example | What it decides |
|---|---|---|---|
| `required_checks` | `[]` (up to 32 entries) | five job names, each with `app_slug` `github-actions` | The CI jobs that must succeed on the exact head. **A head with no check runs is not green.** |
| `broad_checks` | `local` | `hosted` | `hosted`: the full suites run on CI, and builders run focused tests. It needs `required_checks`. |
| `accepted_base_update` | `disabled` | `merge` | Allows an operator integration update (`fast_forward` or `merge`). See below. |
| `owner_contact` | `reports` | `native` | `native`: Synth City reads the builder's own session activity. `reports`: it waits for progress reports. |
| `owner_idle_seconds` | unset (server: 900) | `1800` | Idle time before an owner nudge (60 to 86400). |
| `owner_nudge_grace_seconds` | unset (server: 180) | `120` | Grace after a nudge (30 to 3600). |
| `owner_observation_wait_seconds` | unset | `60` | A short wait when native evidence is unknown (30 to 1800). It needs `native`. |
| `owner_observation_resume_seconds` | `900` | `900` | Native observation timing (60 to 3600). |
| `checkpoint_delay_report_seconds` | `1800` | `600` | When a slow checkpoint is reported (60 to 86400). |
| `caller_inventory_required`, `regression_proof_required` | `false` | `false` | Ask builders for extra evidence. |

Change the policy with `PATCH /api/v1/agent-teams/github-scopes/{scope_id}/delivery-policy`. The body has `expected_revision`, `policy` and a `reason`. A stale revision returns 409. `GET …/delivery-policy/history` lists the latest 100 changes. A change reaches a running attempt only after `PATCH /api/v1/agent-teams/github-work-items/{item_id}/delivery-policy`. See the [Agent Teams API](/api/agent-teams#delivery-policy-fields-and-attempt-snapshots).

- **`required_checks`.** Your CI can run many jobs. Name the ones that must pass. Other jobs, such as a slow nightly analysis, do not block a merge by themselves. Name each check exactly as GitHub shows it. A renamed CI job silently stops counting.
- **`broad_checks: hosted`.** The full suites can take 10 to 20 minutes. With `hosted`, they run on CI while the verifier reviews, so builders do not wait for them.
- **`accepted_base_update`.** While an item runs, other PRs merge into the base branch. Sometimes the builder needs one of them. This setting allows an integration update: the operator command `scripts/factory-maintenance.py integration-update` brings the workspace of the running item up to date with the base, after a checkpoint. The merged PR must be accepted and have every required check. The builder keeps its approval, lease and counters. With `disabled`, use GitHub's **Update branch** on the PR by hand. See the [Supervised Factory Playbook](/guide/supervised-factory-playbook).
- **`owner_contact: native`.** A builder that is busy thinking is not nudged for being silent. Claude Code transcripts and Codex session files show its activity.

## Continuation policy

An item can stop. Synth City then calls it **escalated**. CI can fail twice, a check can never appear, or the effort can run far over the plan. You have three ways to go on.

| Option | What you keep or lose |
|---|---|
| Retry from the start | Possible only when the item has no PR. With a PR, the retry is refused (`pr_preserved`). |
| Let the builder go on outside its plan | You lose the guarantee that every change was approved. |
| **Continuation** | The same builder keeps the same branch, PR and workspace. It asks for a new, small, approved revision of the work item scope. |

A continuation is possible when the item is escalated, has a PR, and stopped for one of these reasons: `retry_count_exhausted`, `plan_blocked`, `owner_idle_timeout`, `owner_offline`, `leader_offline`, `leader_ack_timeout`, `continuation_revision_exhausted` or `amendment_budget_exhausted`. The Leader approves the revision as it approved the first plan.

::: warning Enforced by Synth City
When the builder reports a continuation complete, Synth City compares every changed file with the approved list (`allowed_paths`). A file outside the list is refused with `continuation_paths_out_of_scope`. A file that came from the base branch, or from a recorded integration update, is not refused. A first plan gets a comparison only when it names files (see `initial_paths_required`). Then a file outside the list holds automatic merge (`initial_paths_out_of_scope`). A first plan without a file list has no automatic comparison, and the verifier is the safeguard.
:::

The revision has a phase:

| Phase | For | Rule |
|---|---|---|
| `diagnostic` | Finding out what is wrong | Temporary changes only. They are reverted before the item goes on. |
| `implementation` | Fixing it | The changes stay in the PR. They are reviewed and tested again. |

Without limits, an item could loop for ever. Each limit stops one kind of loop. Set them with `PATCH /api/v1/agent-teams/github-scopes/{scope_id}/continuation-policy`, which needs all six fields, or with the **Recovery** section of the repository settings. The limits count for each attempt.

| Setting | Default | Counts | Stops this loop |
|---|---|---|---|
| `continuation_enabled` | `false` | — | When off, an escalated item can only be retried (without a PR) or ended. |
| `max_continuation_revisions` | `6` | Continuation revisions | "One more try" without end. |
| `max_continuation_failed_heads` | `8` | Pushes whose checks failed, across all revisions | Pushing guesses until something passes. |
| `max_failed_heads_per_revision` | `2` | Failed pushes inside one revision (not above the total) | A revision that keeps failing. Ask for a better one. |
| `max_scope_paths` | `32` | Files and folders that one revision lists | A "small" continuation that becomes a rewrite. |
| `max_scope_commands` | `16` | Commands that one revision lists | The same, for commands. |

When a limit runs out, the item stays escalated. The Leader or the operator then ends it, or files a follow-up issue.

| Situation | Use | Why |
|---|---|---|
| The item is healthy, and the builder needs one more file | **Amendment**: the Leader approves the extra files. | Nothing failed. The plan grows a little. Each attempt has fixed limits: 3 approved amendments, 8 requests and 16 added files. |
| The item is escalated, and the next step is small and clear | **Continuation** | It keeps the branch, the PR and the review history. |
| The work is a different or much bigger problem | **A new issue** | A new plan, a new review and honest counts. |
| A review asks for corrections inside the approved files | **None of these.** The builder fixes and pushes. | The plan already covers it. |

See [Autonomous GitHub Dispatch](/autonomy) for the details.

## Coordination policy

This policy is optional. The Leader reviews a list of issues and records, for each one, whether it can start. Labels say what is ready. They do not say what depends on what.

| Field | Default | What it decides |
|---|---|---|
| `enabled` | `false` | Turns the policy on. It needs at least one issue. |
| `issue_numbers` | none | Up to 32 issues. |
| `fallback_seconds` | `1800` (300 to 86400) | How long to wait before the Leader gets a new request, if nothing changed. |
| `max_daily_requests` | `12` (1 to 48) | A cap on Leader requests in 24 hours. |

The Leader records one of these results for each issue: `eligible`, `dependency_blocked`, `human_decision_blocked`, `resource_blocked`, `needs_scope_clarification`, `completed` or `standing`. Builders then do not get work that cannot finish yet. Set the policy with `PUT /api/v1/agent-teams/github-scopes/{scope_id}/coordination-policy` (it needs `expected_version`), or in the Autonomy panel of the Teams page. Most teams leave it off. They add ready labels in the order of their dependencies.

## Dispatch workspaces

Each item that runs at the same time needs its own folder: a dispatch workspace. Two builders in one folder would overwrite each other's files. Each workspace is leased to one item. Nobody else writes there until the item ends.

- When you enable a scope, Synth City creates up to `max_concurrent_dispatched` workspaces, named `dispatch-1`, `dispatch-2` and so on, beside the primary checkout.
- To add one by hand, call `POST /api/v1/agent-teams/github-scopes/{scope_id}/workspaces` with `{"path": "/home/agentuser/work/dispatch-1"}`. The path must be a direct sibling of the primary checkout. The checkout itself is refused.
- With no workspace, the item stays pending (`queued_no_workspace`). The Work page shows the reason, and `GET …/workspaces/readiness` explains it.
- The owner releases the workspace when the item is merged. If an item ends badly, the lease can stay. `POST …/workspaces/{workspace_id}/force-release` frees it. It needs the operator, `expected_leased_at` and a reason. It shows the unpushed work that it discards.

When the controller and the agents run as different operating-system users, you need one `sudoers` rule and a `safe.directory` entry for each checkout. See [Installation](/guide/installation#separate-controller-and-agent-users) and the [Supervised Factory Playbook](/guide/supervised-factory-playbook).

## The supervisor

A supervisor is optional. It is recommended for teams that run without a person at the keyboard. The Leader is inside the team. It cannot restart a crashed member, upgrade the factory or notice that it is stuck itself. The Supervisor watches from outside. It checks in on a schedule, reads the state of the team and its Mail, and handles incidents. It can be a person or an agent. You then decide only what only you can decide: the roster, the models, the budget and the releases. See the [Factory Operating Model](/guide/operating-model).

1. **Create an external actor.** Call `POST /api/v1/external/agent-mail/actors` with an `actor_key`, a `display_name` and a `kind`. The call works from the same machine only. The token is shown once. Keep it in a file that only that user can read. Calling it again with the same `actor_key` rotates the token.
2. **Bind the actor to the team.** Call `PUT /api/v1/agent-teams/presets/{preset_id}/supervisor` with `actor_id` and, if you want, `grace_seconds`. Or use **Edit** in the supervisor section of the team page. One team has one supervisor.
3. **Check in.** The Supervisor calls `POST /api/v1/external/agent-mail/supervisor-checkin` with the `preset_id` and the time of its next check-in (`next_check_at`, at most 24 hours ahead). It gets the supervisor observation back. It reads its Mail with the supervisor inbox.

After `next_check_at` passes, the supervision status is `late`. After the grace period it is `absent`. The default grace period is the larger of 30 minutes and twice the last check-in interval. Nothing restarts anyone for you: the status is a signal. Write the escalation order in every charter: plans to the Leader, incidents to the Supervisor, and decisions about the roster, the models, the budget and the releases to the Director.

## Turn it on

An item goes through these states. The Work page shows each one.

| State | Meaning |
|---|---|
| `pending` | The ready label is on. The issue waits for an eligible builder and a free workspace. |
| `dispatched` | A builder has the item. It plans, the Leader approves, and it builds. |
| `verifying` | The PR is open. Checks run, and a verifier reviews. |
| `ready_for_review` | The checks and the review are done. Under Auto policy the backend merges next. Under Human policy a person merges. Design PRs wait in `awaiting_human_review`. |
| `merged` | The PR is merged. |
| `escalated` | The item stopped and waits for a decision. |

**What escalated means.** Nothing is lost. The branch, the PR and the workspace are kept. The Work page shows the reason. Common reasons: a budget ran out (`approval_rounds_exhausted`, `retry_count_exhausted`), the ready label was removed (`dispatch_label_removed`), the Leader did not answer in time (`leader_ack_timeout`), or the owner went quiet (`owner_idle_timeout`). The usual next steps are a continuation, a retry (only without a PR) or ending the item and filing a follow-up.

Before you turn autonomy on:

1. Read `GET /api/v1/agent-teams/github-scopes/{scope_id}/activation-readiness`. It must report no blockers: a bound Leader, one distinct owner for each slot, and a ready Agent Mail session for each member. Read `…/workspaces/readiness` too.
2. Turn team autonomy on with `PATCH /api/v1/agent-teams/presets/{preset_id}` and `{"autonomy_enabled": true}`. It needs an enabled Leader slot.
3. Enable the scope with `PATCH /api/v1/agent-teams/github-scopes/{scope_id}` and `{"enabled": true}`. Check that `last_polled_at` moves. The watcher polls a scope only when the scope is enabled and the team's autonomy is on.
4. Start with low concurrency. Watch the first item from plan to PR, review and merge. Then raise the limit.

## Steer a running campaign

| You want to | Do this | Avoid |
|---|---|---|
| Start an issue | Add the ready label, an area label and a priority label. | — |
| Change the order | Change `priority:N`. | Removing ready labels. |
| Pause everything | Turn team autonomy off. | Killing terminal sessions. |
| Stop now | Create a file that `GITHUB_COORDINATION_HOLD_PATHS` lists, or send Mail with a stop word in the subject. | `pkill` |
| Split a big item | The Leader proposes the parts. You file them and remove the ready label from the original. | Closing an issue whose item is not finished. |
| Upgrade the factory | Follow the upgrade steps in the [Supervised Factory Playbook](/guide/supervised-factory-playbook). | Upgrading with items that are mid-flight. |
| Unstick a merged item | `POST /api/v1/agent-teams/presets/{preset_id}/work-items/{item_id}/finalize-merged`. Run it first with `dry_run`. | Editing the database. |
| End a stopped item | `POST /api/v1/agent-teams/presets/{preset_id}/work-items/{item_id}/end-stopped` | — |
| Free a stale workspace | `POST …/github-scopes/{scope_id}/workspaces/{workspace_id}/force-release` | Deleting the folder. |
| Change a model or the roster | Turn autonomy off, edit the slot and relaunch that member. | Changes in the middle of an item. |

The Leader has matching routes for recovery under `/api/v1/agent-teams/github-work-items/{item_id}/leader-recovery/`. See the [Agent Teams API](/api/agent-teams).

There are three ways to stop, from gentle to firm:

| | Autonomy off | Stop Mail | Hold file |
|---|---|---|---|
| New work | Stops. | Not affected. | Stops. |
| Work in progress | The builders keep running in their panes. Synth City stops dispatching, verifying and merging that scope. | The member stops editing until it reads the message. | Dispatch, merge and coordination hold. Running agents are not stopped. |
| Use it when | You want an orderly pause, for an upgrade or the end of the day. | One member must stop now. | The whole factory must stop, even if the UI is down. |

A stop Mail has one of these words in its subject: `stop`, `hold`, `pause`, `halt`, `cancel`, `revoke` or `defer`. It works when the Leader, the bound Supervisor or Synth City sends it. It applies to Claude Code members. The hold file is any path in `GITHUB_COORDINATION_HOLD_PATHS`. See [Agent Mail](/features/agent-mail) and [Server settings](#server-settings).

## A complete example setup

This example shows the values of a small factory. Names, labels and paths are examples.

```yaml
team:
  name: my-project-v1
  backlog_milestone: v1.0
  autonomy_enabled: false        # on at launch
  leader: leader
  slots:
    - {name: leader,    provider: codex-cli,   role: coordinator, effort: high}
    - {name: builder-1, provider: codex-cli,   role: implementer, effort: xhigh, areas: [area-core]}
    - {name: builder-2, provider: claude-code, role: implementer, effort: high,  areas: [area-ui]}
    - {name: verifier-1, provider: codex-cli,  role: verifier,    effort: high}
    - {name: verifier-2, provider: claude-code, role: verifier,   effort: high}
scope:
  repo: example-org/example-repo
  base_ref: origin/release/v1.0
  labels: {ready: my-project-ready, design: my-project-design, hold: my-project-review-hold}
  merge_policy: human            # change to auto when you trust the checks
  max_auto_merges_per_day: 5
  max_concurrent_dispatched: 2
  max_item_effort_minutes: 120
  enabled: false                 # on at launch
  delivery_policy:
    required_checks: [Unit tests, Lint, End-to-end tests]
    broad_checks: hosted
    accepted_base_update: merge
    owner_contact: native
  continuation: {enabled: true, revisions: 6, failed_heads: 8, paths: 32, commands: 16}
  workspaces: [dispatch-1, dispatch-2]
supervisor: an external actor, bound with a grace period
```

Then follow [Day one, in order](#day-one-in-order). Read [Supervised Factory Playbook](/guide/supervised-factory-playbook) before you run a campaign.
