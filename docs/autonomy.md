# Autonomous GitHub dispatch

This guide is for the operator of an Albiex Synth City team. Autonomy is off until you enable it for the team, and a watched repo can also be paused separately.

## Before enabling

1. Give the watcher GitHub access and configure the operator token described below before editing the roster. The code defaults that start the dashboard do not supply these factory settings. Add `github_token=<token>` to `backend/.env` and restart the backend. The watcher uses this host token to poll GitHub; private repos cannot be polled through GitHub App settings alone. If you use App-backed dispatch, also configure `github_app_id`, `github_app_private_key_path`, and `github_app_bot_login`. The repo card distinguishes a missing polling token from a dispatch mode not yet selected. Synth City selects the dispatch mode when eligible work is dispatched, not on the first poll. A selected mode is not proof of successful access; check the last poll and Activity.
2. Assign the Leader explicitly and launch it. Guided repository setup names the Leader slot for a new team. For an existing team, use **Set Leader** on an enabled slot in Roster while team automation is off; the server also requires a quiescent team. The team's `leader_slot_id` supplies Leader authority. Slot order and the Role field do not select it. The Leader approves plans and never owns an issue: routing skips the Leader slot and any slot whose role is `coordinator`, ignoring case and surrounding whitespace.
3. Add a repository scope: a team's watch on one GitHub repository (see [Repositories](/features/repositories)). Give its GitHub owner/name and an existing primary clone under your home directory. Synth City creates a separate issue worktree beside that clone; ordinary slot sessions keep using their own repo path.
4. Create the dispatch and optional design/area labels on GitHub. Adding the dispatch label queues an issue. Routing gives the issue to the first enabled slot, in roster order, whose area labels include one of the issue's label names. The Leader slot and slots whose role is `coordinator` are skipped; the match is exact and case-sensitive. If no slot matches, the item stays pending with `no_eligible_owner`, and nothing is handed to the Leader. Slot expertise is stored but does not route work yet. A design label on the same issue uses the human-review design pipeline. Removing the dispatch label during an attempt escalates it.
5. Choose merge policy and finite budgets, including `max_item_effort_minutes`, the most effort one item may use (default 120; 15 to 1440). Human policy leaves PRs for review. Auto policy merges eligible code PRs after checks pass, subject to the daily cap; design PRs always need human review. A head with no check runs is not green, so Auto policy does not merge it. Synth City omits `merge_method` from the merge request, so GitHub's merge API chooses the method. With Auto policy a code PR also needs an enabled slot of the same team whose role is `verifier` to have recorded an `accepted` verdict for its exact current head (a push after the acceptance waits for a new one). The role match ignores case and surrounding whitespace, and the item's owner and the Leader cannot supply that verdict. A scope can name a review hold label: while the PR or its issue carries that label, automatic merge is blocked. See [Agent Mail](/features/agent-mail#amend-an-approved-work-item-scope-and-end-a-submitted-revision) and the [API workflow](/api/agent-mail) for the exact rules. The delivery policy fields for a repository scope and for an attempt are in [Delivery policy fields](/api/agent-teams#delivery-policy-fields-and-attempt-snapshots). Enable autonomy when the roster and watched repos are ready.

Synth City polls GitHub every 60 seconds by default. The Activity table refreshes every five seconds while open; it does not trigger another GitHub poll. Issues move through queued, dispatched, verifying, and ready for human review or merged. An escalated issue has stopped and needs attention.

### Stuck work and silent background failures

Synth City watches itself for four conditions that used to be found only by hand. Each opens a finding, shows it, tells the Supervisor once, and clears it when the condition ends:

| Reason code | What it means | When it opens |
|---|---|---|
| `verifying_after_merge` | A work item is still verifying although GitHub reports its PR merged. | 15 minutes after GitHub's own merge time; at most 8 PRs are read per poll, and the next poll continues where the last stopped. |
| `item_skipped` | Verification skipped the same work item. | 5 polls in a row, counted per repository scope. |
| `admission_missing` | An issue carries the dispatch label but has no work item. | 3 polls in a row, per repository scope. |
| `poll_rolled_back` | A poll of a repository failed, so everything it had not yet committed was discarded. | At the first failed run; it clears at the next good run. |

The thresholds are fixed in code. A finding holds identifiers and counts only, never a prompt, token or raw error. It is stored together with its notice in one transaction, so a failure leaves neither and the next poll tries again; a condition that appears again after it cleared opens a new finding and a new notice. Detection never blocks the poll or the verification it watches.

While a finding is open, `/api/v1/health` reports `"health": "degraded"` with `reasons` (the codes) and `open_findings`, and `/health` adds the same fields. If the findings store itself cannot be read, both report `findings_unreadable`; a database that holds none of the application's tables yet counts as no findings, but an initialized database that lost the findings table does not. The existing `status` values do not change, because the deploy gate and scripts read them. `/api/v1/health/findings` lists open findings first, then recently resolved ones, and the Overview shows a "Needs a look: operations" card while any is open.

### When automatic merge does not fire

With Auto policy, an accepted code PR can still wait for a human. Synth City now says why, instead of leaving the PR unmerged without a reason.

- **Daily cap reached.** The work item note ends with `[auto_merge_cap_reached: n of N automatic merges in the last 24 hours]`. The Leader side gets the existing "Code PR ready for review" notice with the reason in its payload, and the Supervisor gets one notice. Repeated polls send nothing more. The cap is the per-scope setting **Max auto-merges per day**; the repository settings page shows "n of N in the last 24 hours" next to it.
- **A cause that clears.** A PR left for a human only because of the daily cap, or because checks stalled for the configured `merge_check_wait_minutes` (60 by default), goes back to the automatic merge path on a later poll once the cause has cleared: usage is below the cap, or every check and status on the same verified head has finished green. Every gate applies again, and polls while the cause persists send nothing. Any other fallback stays with a human. A check run again later on the same head gets a fresh wait using the scope setting.
- **Escalated item.** An accepted, ready, unheld PR on an escalated item records `[item_not_eligible: escalated: <reason>]` and sends the same two notices once.
- **Settled diagnostic escalation.** An item that is escalated only because of a completed diagnostic revision, whose PR head is unchanged since it was verified and was accepted by the reviewer, goes back to ready for review once for that head. The normal automatic merge path then applies every gate: hold label, reviewer acceptance of the exact head, Leader approval, the daily cap and green checks. Any other escalation stays with a human.

These notices grant no merge authority.

Automatic merge uses GitHub merge commits so accepted source imports can verify that the PR head
is an ancestor of the merge commit. The base-side path exceptions rely on that same history.
[Base-side path exceptions](https://github.com/albiexai/synth-city/issues/1279)

### Dispatch priority and queue order

The Director can order ready issues with GitHub labels `priority:1` through
`priority:9999`. Lower numbers run first. The syntax is exact: lowercase,
positive decimal, no leading zero. If an issue has several valid priority
labels, the lowest number wins. Invalid labels are ignored. Explicit priorities
come before issues without a valid priority; ties and unprioritized work use
ascending issue number (oldest first), then work-item ID. GitHub poll order and
work-item creation time do not decide priority.
An owner slot that frees while a poll is working through its ranked list goes
to the highest-ranked waiting item for that slot on the next poll, not to
whichever item the poll happens to check next. Other slots are not held up, and
an item whose own prepared attempt holds the slot can still start.

Each poll observes priority labels and clears removed values. Pending issues
are refreshed again before the dispatch batch is ranked. Existing databases
start with no explicit priority until labels are observed. Priority only orders
attempts within one repository scope. It does not admit dependencies,
preempt active work or order different repository scopes. The Leader still
handles dependencies written in prose. Keep dependent issues unready until
their prerequisites are satisfied.

All label, autonomy, pause/HOLD, routing/area, Leader, memory, reservation,
workspace and concurrency gates still apply. A high-priority item waiting for
its owner does not block eligible work for another free owner. Priority grants
no approval, retry, lease, release or merge authority.

The team summary's `campaign.dispatch_order` and the Progress card show the
complete observed pending attempt order, with repository, issue, effective
priority and position within that scope. This includes ready work outside a
selected campaign milestone; existing campaign counts remain milestone-scoped.
The list works when milestone backlog data is unavailable. It reflects the last
observed labels, not a guaranteed next launch: routing and capacity gates may
skip queued entries. Label changes appear after the next successful poll.

### CI polling permissions

The polling credential needs Checks:read and Actions:read for private repositories.
Synth City uses the configured host `github_token` for these reads. App dispatch credentials
do not replace the polling credential. Do not grant Actions write permission for polling.

Synth City reads current workflow runs and jobs before it classifies CI results. It ignores
an older execution only when the workflow and execution context prove replacement.
An incomplete or unavailable observation keeps the item unverified. It does not
consume a verification retry or a diagnostic failure budget. The status note gives
a short reason. Check credential access if this note persists.

CI observation is bounded to five pages per list, 64 requests and 45 seconds per
item. More than 16 current execution contexts is unavailable. These limits do not
establish that an incomplete history is green. Cancellation retains its existing
conservative accounting for unclassified pushed heads.

## Build hints

Synth City does not run the build command. The optional build settings are instructions in the owner's brief. An out-of-tree build directory may include `{issue_number}`. The command hint may include `{build_dir}` and `{parallelism}`. The parallelism value also tells the agent to cap build jobs.

## Leader plan-approval wait

The initial plan clock starts when the bound approval request is created.
`github_leader_ack_timeout_seconds` defaults to 300 seconds. At that boundary,
Synth City sends one Leader reminder and a system notice to the team's currently
bound Supervisor. The pending request, builder lease and workspace stay intact.
The reminder cannot interrupt a running provider turn.

Positive activity from the current authenticated, bound Leader can preserve the
request until `github_leader_ack_max_wait_seconds` (default 1800 seconds) from its
creation. This is a fixed maximum, not a sliding renewal. Native working evidence
is read with bounded effort and checked against the current process/session
identity. Recent authenticated Mail/MCP contact within the initial window can
also qualify. Display status, stale or future contact, retired panes, unknown or
ambiguous identity, and native requests waiting for human input do not qualify.
An inactive request can expire after the one reminder's existing
`github_nudge_grace_seconds` (default 180 seconds). The hard maximum always ends
an unanswered request with `leader_ack_timeout`. Durable reminder and extension
records survive service restarts and prevent repeated notices.

An operator can set nullable `leader_ack_timeout_seconds` and
`leader_ack_max_wait_seconds` on the team preset through the existing protected
`POST /api/v1/agent-teams/presets` and `PATCH .../presets/{preset_id}` routes.
Both fields accept integer seconds from 30 through 86400. The effective maximum
must be at least the effective initial window. Omission on PATCH preserves the
value; explicit `null` restores the global default. Preset responses expose the
overrides, and duplication and creation from Mail/Agent Bridge preserve them.
For example, `{ "leader_ack_timeout_seconds": 600,
"leader_ack_max_wait_seconds": 2400 }` gives a team a ten-minute initial window
and a forty-minute maximum. Design items multiply both windows by
`github_design_ack_multiplier` (default 3).

Leader guidance: finish a running turn normally, then read the pending request
and record an explicit approval or rejection. Activity and a soft notice grant
no approval. The owner still needs the current normalized plan decision and
acknowledgement before implementation. Pause, HOLD, offline, binding, dispatch,
lease and review gates retain their existing force.

## Ordinary review corrections

An initial implementation plan remains the basis for work while its normalized
approval and owner acknowledgement remain valid. Opening a PR or reaching
`ready_for_review` does not end that plan. The Leader sends review findings back
to the current owner. The owner can correct findings within that same plan on
the existing branch and PR after reconciling owner identity, dispatch nonce,
workspace lease and approval evidence. Operator pauses and safety holds stop
work. Missing or stale authority must be resolved before edits.

`continuation_disabled` describes recovery of an escalated attempt. It does not
by itself prohibit ordinary corrections under an existing initial approval.
Do not escalate, retry, release the workspace, change recovery policy or request
a continuation just to make review corrections inside the work item scope. Do not send a second
`pr_opened` report for an already tracked PR.

Every pushed head needs independent review and hosted CI for its full SHA.
Previous review acceptance and CI readiness apply to the previous head. Preserve
the configured merge policy; human policy still requires human merge. The owner
context returned by `synth_city_get_work_item_context` includes `review_rework_guidance`;
that text explains the workflow and does not grant approval.

When the approved plan no longer fits the work, the owner can request a healthy work item scope amendment instead of escalating. The work item scope is the files, commands and actions that the approved plan lets one work item use. The Leader reviews the new paths, actions and commands, and the owner acknowledges the approval before editing. An approved plan also carries the companion files that normally travel with the files it names (tests, fixtures, docs and generated contracts). An amendment names its whole path set: the approved paths it keeps plus the new ones, with any approved path it drops listed explicitly as withdrawn. Each attempt has fixed limits on approved amendments, amendment requests and added paths. When review requires changes to a submitted revision, the reviewer records a structured disposition for the exact head and the Leader ends that revision, so the owner stays on the same branch and lease. See [Amend an approved work item scope](/features/agent-mail#amend-an-approved-work-item-scope-and-end-a-submitted-revision), the [API workflow](/api/agent-mail#healthy-work-item-scope-amendments-and-submitted-review-settlement) and [Companion paths](/guide/supervised-factory-playbook#companion-paths).

This rule applies to nonterminal, non-escalated initial implementation attempts
with `active_scope_revision=0`. Scoped and diagnostic continuations retain their
approved paths, commands and finite budgets. After an implementation revision
is settled, the owner needs a fresh approved scope amendment and acknowledgement
before making another correction on the same PR, even within the previous
paths. The Leader's reviewer assignment returns
`settled_head_amendment_required` for a different head under the old revision.
Escalated attempts use the supported recovery route.

For the unchanged settled head, a consumed accepted revision review from a
current independent verifier supplies the exact-head acceptance. An active
exact-head verdict takes precedence. The verifier's identity, fresh CI, hold,
readiness and configured merge policy still apply.

An unreadable CI warning is tied to the head whose check read failed. A verified
new head discards the old warning, and repeated failures on the same head keep
the original grace period. Clearing a warning does not mark CI successful.
Work-item-scoped summaries show only that item's unreadable CI warning.

## Recovery and operator token

An escalated issue with an open PR may be continued within a bounded work item scope revision. The owner proposes a plan, allowed files, and allowed commands; the Leader approves; the owner continues in the same workspace. A failed head is a pushed PR commit whose GitHub checks fail. The policy caps revisions and failed heads so recovery cannot loop indefinitely. A checkpoint hold pauses the Leader's decision or owner's acknowledgement until an operator releases it.

To create an operator token on the Synth City host, run `openssl rand -hex 32`, put the result in `backend/.env` as `operator_token=<value>`, run `chmod 600 backend/.env`, and restart the backend. Do not export this token into the agent environment. The UI stores it only in the current browser tab and asks for it when you edit the roster, watched repos, autonomy settings, recovery policy, or use operator remedies. The current authenticated Leader can request an eligible retry through Agent Mail without the operator token. Ordinary issue dispatch does not need the token. This token does not isolate agents running under the backend's OS user: such processes can read `backend/.env` despite its `0600` permissions.

Planning or launching a team requires an authenticated agent session or the operator token. Agent sessions can use the configured roster but cannot override launch prompts or paths, include disabled slots, force a respawn, or bypass plan confirmation. Terminating a session through Agent Bridge requires the operator token.

Plan launch reuses only a live pane already bound to its slot. A discovered pane with no verified slot binding blocks the default plan; it is **not** silently promoted to Leader. An operator can request a new plan that explicitly marks such a pane for adoption and names its tmux target and PID. Review the pane and its startup instructions before selecting **Adopt and launch**, or choose a forced fresh spawn instead. Agent sessions cannot request adoption. Agent Bridge's raw spawn route accepts a minimal same-repository plain spawn from an authenticated agent session, but requires the operator token for prompts and other overrides; the legacy CC Bridge spawn and kill routes are operator-only. This does not isolate same-UID agent panes from the backend's operator token or direct tmux access.

If the card says the polling token is not set, check `github_token` in `backend/.env` and restart the backend. A card that says the dispatch mode is not selected can remain that way after successful polls if no issue is eligible for dispatch. If Activity reports an auth error, verify the host token's repository access or the GitHub App installation and settings before retrying.

### Required-check completion and stopped teams

The Leader registers the assigned independent reviewer with
`synth_city_watch_required_checks(work_item_id, dispatch_nonce, scope_revision,
head_sha, reviewer_member_id)`. Use the full current PR SHA and repeat registration
for every new head. Pass `null` to clear the reviewer. The current designated Leader
and a current enabled reviewer from the same team are required; the owner cannot
be their own reviewer. A watch does not approve a plan, accept a review, change a
retry budget, or authorize a merge.

The existing verification monitor records each configured required check by exact
name, app slug and head. Missing or ambiguous evidence stays unknown. A legacy
policy without named required checks does not produce a required-check completion
notice. On the first completion for an attempt revision/head, success notifies the
Leader and registered reviewer; any required failure notifies the owner and Leader
with the failed check named. Repeated polls and backend restarts reuse durable
recipient records. A reviewer registered after completion can receive that same
completion once. Same-head reruns update the visible results without creating a
second completion event; a changed head needs its own watch and fresh evidence.

Mail and wake claims are durable before external delivery. Each recipient gets at
most one wake attempt per completion. A crash during a wake cannot prove delivery;
that claim remains unconfirmed, and the view says **checks finished, no next actor**.
The Leader reconciles the current owner/reviewer instead of relying on a manual CI
watch. No automatic check reruns occur. Independent acceptance, required hosted
checks and the configured merge policy remain separate gates.

The progress panel shows required-check counts and wake confirmation. The team
summary and Supervisor check-in expose advisory `team_idle` items after no semantic
mail/check/item-state progress for `GITHUB_TEAM_IDLE_TIMEOUT_SECONDS` (default 1800,
valid 30–86400). Polling, summary reads, heartbeats, system notices and Supervisor
check-ins do not renew this clock. Its first trusted observation starts a baseline;
no pre-upgrade idle duration is guessed. Human, native-input and external-check
waits, OFF/HOLD and incomplete bounded observations suppress this signal. Actual
progress clears it. Last actor names come from authenticated recorded evidence;
otherwise the view says Unknown. Idle attention grants no execution authority.

### Unanswered explicit requests

The team summary and the Supervisor check-in also show **unanswered requests**: a pending
Agent Mail `context_request` or `handoff` that is addressed to an idle member of the team and
is older than `MAIL_REQUEST_UNANSWERED_SECONDS` (default 1200 seconds, valid 60–86400; an age
equal to the threshold counts). The team card shows "C1 has not answered L1's request for 25
min" and the Supervisor line shows a second sentence with the count and the age of the oldest.
The names are display names only. A subject, a body or a payload is never shown.

- Only an **explicit** request counts. A plain message that asks for something carries no
  request marker, and Albiex Synth City does not guess from prose. Send a request that needs an
  answer as a context request addressed to the member directly.
- A request leaves the view when it is answered, superseded or acknowledged, or when the
  addressed member sends a plain reply in the request's own thread. A message from any other
  member, including the requester, never clears it. Approval requests have their own timeout
  flow and are not listed. A working member is active, so its requests are not listed.
- The view says when it is **incomplete**, with a fixed reason: `candidate_limit` (more than 64
  unresolved requests, or older history than the budget below was not examined), `native_unknown` (a member's activity is unknown, stopped or could not be
  read in time), `ambiguous_legacy_request` (an older request nested inside a thread cannot be
  matched to a reply), `reply_scan_limit` (the reply check reached its bound) or `entry_limit`
  (more than 16 unanswered requests; the count of those not listed is shown). A truncated or
  unproven view is never shown as complete.
- Requests are examined oldest first, page after page, and a request that is provably answered,
  including one answered only through an older nested reply in its own thread, is skipped without
  counting. The first 64 requests that are not provably answered are evaluated, and a 65th means
  `candidate_limit`. All of the reading shares one budget: 256 reply rows, 512 reads (each examined
  request is one read) and five seconds. History beyond that budget is reported as incomplete,
  never as complete, and it is not claimed to have been examined.
- The signal grants no authority and does not change `team_idle` or the next actor.

### Thread view limits

A thread read returns at most 500 replies. It reads the thread breadth first, by depth and then by
id, with indexed page reads, so the work depends on the replies kept and not on how many
descendants exist. The depth bound is 33. A thread with at most 500 replies and a depth below 33
is returned whole, in the usual order (creation time, then id).

When a thread is **truncated**, the reply list says so with `replies_complete: false` and the
reason `reply_limit` (more than 500 replies) or `depth_limit` (a reply at depth 33). A truncated
view holds the 500 replies with the smallest ids by depth and then id. It does **not** hold the
500 earliest replies by time, and it must not be read that way. The kept replies are still shown
ordered by creation time and id.
