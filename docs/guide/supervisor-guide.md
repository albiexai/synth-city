# Supervisor guide

This guide tells a Supervisor how to run a Synth City campaign. The Supervisor can be a person or an agent. Use it with the [Supervised Factory Playbook](/guide/supervised-factory-playbook), which describes the whole process. This guide covers the daily work: the check-in, the merge audit, splitting an issue, upgrades and recovery. You can run a check-in and a merge audit from this guide alone.

The supported scripts are in `scripts/supervisor/` in the Synth City source checkout. They use existing product routes and the GitHub CLI. They add no product route.

## What a Supervisor decides

| Decision | Who decides |
|---|---|
| Roster, models, budget, publication and releases | The Director |
| Admission, routing, gates, splits and recorded recoveries | The Supervisor |
| Plans, assignment and sequencing inside a team | The team's Leader |
| Acceptance of an exact head | An independent verifier |

A Supervisor works within its recorded delegation. If an action is not delegated, ask the Director first.

## Safety rules

Follow these rules in every step of this guide.

- **No pattern kills.** Never use `pkill`, `killall`, `pgrep -f` or `ps | grep` to choose a process. Stop only a PID that you started and recorded. Never signal the tmux server or an agent pane.
- **Kill tmux sessions before a backend restart.** Otherwise a Codex member reconnects as a duplicate.
- **Never print a token or a dispatch nonce.** Never write one to a file, a log, a commit or a GitHub text. Keep dispatch nonces off GitHub.
- **Read the database read-only.** Never edit a row to get past a guard.
- **Give an unattended member no interactive question tool.** Give every charter a Supervisor address and the escalation order: Leader, then Supervisor, then Director.
- **Do not merge by hand** except in the recovery merge below.

## The three secrets

| Secret | How a script reads it |
|---|---|
| Operator token | From the environment variable `SYNTH_CITY_OPERATOR_TOKEN`, or from a hidden prompt when you give `--prompt-operator-token`. A terminal that cannot hide the input is an error. |
| Supervisor token | From a file that you name with `--supervisor-token-file`. The file must be a regular file, owned by you, with mode 600. A link is refused. |
| GitHub access | From the `gh` CLI login of the user who runs the script. The scripts never read or print it. |

A script sends the operator token only in the header `x-synth-city-operator-token`, and the supervisor token only as `Authorization: Bearer ...`. Plain `http` is allowed only to `localhost` or a loopback address. Use `https` for any other address. A URL with a user name or a password is refused. Every answer is cleaned of the token values before the script prints anything.

## Run a check-in

Run a check-in every 15 to 30 minutes during a campaign.

```sh
export SYNTH_CITY_OPERATOR_TOKEN=...   # from your own secret store; do not type it in a shared shell
python3 -B scripts/supervisor/checkin.py --preset PRESET_ID \
  --supervisor-token-file /path/to/supervisor-token
```

| Option | Meaning |
|---|---|
| `--preset` | The team preset id. Required. |
| `--base-url` | The product address. The default is `http://127.0.0.1:8000`. |
| `--supervisor-token-file` | The token file for the supervisor inbox. Without it the inbox step is skipped. |
| `--prompt-operator-token` | Ask for the operator token with hidden input. |
| `--since-id` | Show inbox messages above this id. Use the highest id of the last check-in. |
| `--idle-minutes` | Report a member that is idle longer than this. The default is 20. |

The script sends only GET requests and writes no file. The product's activity and summary reads can record native observations on the server, as they do for the web page.

### The steps

Each step prints one line that starts with `ok`, `attention` or `error`. One failed step does not stop the others. An answer that lacks a field that the step needs is an `error`, never an `ok`.

| Step | Route | Attention means |
|---|---|---|
| `health` | `GET /api/v1/health` | Status or health is not `ok`, or the open findings are unknown. |
| `summary` | `GET /api/v1/agent-teams/presets/PRESET/summary` | A hold, an attention item, a human action, unreadable required CI, incomplete coverage, or a late or absent Supervisor check-in. |
| `activity` | `GET /api/v1/agent-teams/presets/PRESET/activity` | A member that is stopped, waiting for input, or idle longer than `--idle-minutes`. |
| `overview` | `GET /api/v1/factory/overview?team_id=PRESET` | Work items that need attention. |
| `inbox` | `GET /api/v1/external/agent-mail/supervisor-inbox` | New messages for the Supervisor. The line shows the highest id seen. |

The exit code is `0` when every step is ok, `1` when a step needs attention, and `2` when a step or the setup failed.

### What to do with a finding

| Finding | Action |
|---|---|
| `coverage incomplete` | Read again. Do not act on an incomplete read. |
| A hold | Read the hold code. Only the operator clears a security hold. Do not retry or clear it. |
| `human action, act now` | Tell the Director. The request has a current assessment. |
| `human action, stale` | Ask the Leader to refresh the assessment. |
| `required CI unreadable` | Check GitHub access and the configured workflows. This consumes no retry budget. |
| A member idle or waiting for input | Read that member's Mail and pane. An unanswered decision is the usual cause. Answer it or restart the member. |
| New inbox messages | Answer by direct message. Use `synth_city_send_message` with the member as the recipient. |
| `supervisor check-in late` | Call `synth_city_supervisor_checkin(preset_id, next_check_at, note)` after a successful check. |

### What the script does not read

Do these by hand at each check-in.

- **Panes.** Read the last six lines of each member's pane through the agent tmux wrapper. Look for `error`, a trust prompt, "hooks need", a login prompt, a usage or rate limit, "press enter", `(y/n)` and "approve". A prompt that nobody answers stalls the member.
- **Usage limits.** Tell the Director when a Claude or Codex account has about 15% left. Read the Codex weekly percentage from the newest rollout file of the agent user.
- **GitHub quota.** Run `gh api rate_limit` as the agent user, because the factory and the agents share it. Slow down admissions when `core` is low.
- **Open PRs.** Run `gh pr list --base BASE --state open`. Check the draft state, the hold label and the head.

## Run a merge audit

Run a merge audit after every merge and at each check-in. The rule is: every PR merged into the base has an `accepted` verdict from a current independent verifier for its exact merged head.

```sh
python3 -B scripts/supervisor/audit_merges.py --db PATH_TO_SQLITE --scope-id SCOPE_ID
```

| Option | Meaning |
|---|---|
| `--db` | The SQLite evidence file. The script opens it read-only. Use the installation's database or a copy. |
| `--scope-id` | The GitHub scope id in that file. |
| `--restore-hold` | Opt in: add the configured review hold to an eligible open PR. Without it the script changes nothing on GitHub. |

The script lists the pull requests of the scope's base branch with GitHub GET requests. For each merged PR, and each open PR that is not a draft, it asks the evidence file the same four things as the product's merge gate. Never use the verdict table alone: it can hold an obsolete verdict.

1. The verdict is the latest active head review for this attempt, this PR and this exact head.
2. The verdict is `accepted`.
3. The reviewer holds an enabled `verifier` slot of this team, and is neither the owner nor the Leader.
4. The reviewer is the slot's current occupant.

| Line | Meaning |
|---|---|
| `PR #N merged with exact-head acceptance.` | Good. |
| `PR #N merged without proven exact-head acceptance.` | Investigate. A bad merge is reverted by the Supervisor, with the reason recorded. |
| `PR #N is open without proven exact-head acceptance.` | The PR must not merge yet. |
| `PR #N lacks a unique work item in the selected scope.` | The PR has no work item (for example a release PR). List it separately with its recorded reason. |
| `PR #N hold restored.` | Only with `--restore-hold`. The script checked the PR again first. |

The exit code is `0` for no finding, `1` for a review gap, and `2` for a setup or evidence error. With two verifiers, a verdict from either current verifier counts.

The product does not store the accepted head on the merge record. The audit compares GitHub's reported head with the evidence at the time of the audit.

### The recovery merge

Merge by hand only when the daily automatic merge cap blocks a PR that is otherwise ready. Check all of these at the same head:

1. The PR is not a draft and has no hold label.
2. A current verifier accepted that exact head.
3. Every latest required check for that head is `completed` and `success`, and all of them are present. Count them.

Then run `gh pr merge N --merge --match-head-commit HEAD_SHA`. The head guard refuses if anything was pushed. Add this PR comment: "Recorded Supervisor recovery merge: the daily auto-merge cap is reached. The verifier accepted this exact head (`HEAD8`) and every latest required check is green." Add a line to the Supervisor's notes. Never merge by hand for any other reason.

## Split an issue

1. The Leader proposes the split by Mail: the parts, each with a title, a body and its lane.
2. The Supervisor writes a plan file and runs the helper. The default is a dry run.
3. After a part's PR has merged, the Supervisor ends the original item through the product.

```sh
python3 -B scripts/supervisor/split.py --plan plan.json            # dry run: reads only
python3 -B scripts/supervisor/split.py --plan plan.json --execute  # writes to GitHub
```

| Option | Meaning |
|---|---|
| `--plan` | The plan file. Required. |
| `--execute` | Write to GitHub. Without it the helper only reads. |
| `--ready-label` | The label to remove from the original. The default is the ready label of this release. |

The plan file is JSON of up to 65536 bytes.

| Field | Meaning |
|---|---|
| `repo` | `owner/name`. |
| `original` | The issue number to split. It must be open. |
| `milestone` | The milestone for the parts. Optional when the original has one. |
| `parts` | One to ten objects with `title`, `body`, `lane` (a label) and `priority` (1 to 999). |
| `end_stopped` | Optional: `preset`, `item`, `generation` (64 hex characters or null) and `revision`. They fill in the printed command. |

With `--execute` the helper files each part that is not yet filed, with a hidden marker so that a second run never files it twice. It removes the ready label from the original and comments the list of parts. It never closes the original and never ends its work item. It prints the `end-stopped` command to run later. The exit code is `0` for success, `1` when the scan of existing issues was incomplete, and `2` for an error.

Follow these rules for splits:

- **Never close an issue whose work item is not terminal.** The poll can lose admissions otherwise. A split is the one case where the original is superseded on purpose.
- **Never hold a queue by removing ready labels.** Removing the label during an attempt escalates the item. Change `priority:N`, or turn autonomy off.

## Upgrade the factory

Use the supported maintenance operation. Do not write a new deployment script. The sequence, the profile and the request files are in the [playbook, section 9](/guide/supervised-factory-playbook#_9-stop-safely-for-factory-maintenance).

1. Turn autonomy off on the team. Wait until no item is dispatched or verifying, or use the maintenance operation with a checkpoint from each active owner.
2. Run `scripts/factory-maintenance.py upgrade` with the profile and the request. The operation takes a consistent database backup, changes the reviewed source and starts the controller. It returns `deployed_paused`.
3. Check the deployed version and `GET /api/v1/health`. Re-apply the Agent Mail hooks for the agent user's harnesses if the upgrade needs it.
4. Re-enable the scope as well as autonomy if a guard stop disabled it. Check that `last_polled_at` advances.
5. Check the fixed behaviour live and record the result on its issue.

To roll back, use the operation record and the matching code, UI and database backup. Keep the factory paused until the version and process state are known. For an accepted base update without a global pause, use `integration-update`, as the playbook describes.

## Recovery operations

These routes need the operator token in the header `x-synth-city-operator-token`. Read the answer of each call before you go on.

- **Finalize a merged item** that did not settle: `POST /api/v1/agent-teams/presets/PRESET/work-items/ITEM/finalize-merged`. Send `{"dry_run": true}` first. A real call sends the returned `expected_context_sha256`, an `operation_id` and a `reason` of up to 240 characters.
- **End a stopped item**: `POST /api/v1/agent-teams/presets/PRESET/work-items/ITEM/end-stopped`. Send `operation_id`, `expected_dispatch_generation` (64 hex characters, or null), `expected_scope_revision` (the item's `active_scope_revision`), `reason`, and an outcome. Use `deferred` with `target_milestone`, or `superseded` with `superseding_pr_number`. The PR must differ from the item's own PR.
- **Free a stale workspace**: `POST /api/v1/agent-teams/github-scopes/SCOPE/workspaces/WORKSPACE/force-release`. Send `force: true`, the lease's `expected_leased_at` and a `reason`. Read `unpushed_commits` and `discarded_paths` in the answer. Use it only when the owner has stopped.

## Gaps found in the product

The v4.2 and v4.3 practice found things that the product does not yet provide. Each has a workaround here.

| Gap | Workaround |
|---|---|
| A merge-audit read in the product | `audit_merges.py` with the evidence file. |
| A quota view for Claude, Codex and GitHub | The manual quota steps in the check-in. |
| A finding for an idle member with a pending decision | The `activity` step and the member's Mail. |

File a separate issue for a gap. Do not add a product route in a Supervisor change.
