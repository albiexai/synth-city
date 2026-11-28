# Factory audit and delivery metrics

The audit ledger records what the factory actually did. The metrics page counts those records in a time window. Both are observations. They never approve a plan, raise a limit, retry work, release a workspace or replay an action.

Open **Insights** in the main navigation, then the **Audit log** tab (`/insights/audit`). The old address `/audit` still works: it opens the same tab and keeps its query and hash. The safe metrics load without an operator token. The event list needs the operator token for this browser tab.

## What the ledger records

Each event records:

- the event kind, its source and its occurrence and recording times
- the actor: the shared operator role, an authenticated member and session, or a code-owned scheduler reference
- the action outcome: `applied`, `rejected` or `uncertain`
- deletion-safe links to the team, repository scope, item, revision and request, and immutable context keys
- a typed snapshot of safe labels from the time of the event
- allowlisted before and after values, and a sanitized reason

The ledger stores no credential, credential hash, token, nonce or private command. Free text is sanitized before it is stored. Assignment, header, URL and known token forms are replaced with `[redacted]`. Nested objects and unknown fields are dropped.

A refused action records `rejected`. When an external effect cannot be proved, the event records `uncertain`, for example a notice whose send failed after the action committed. The committed action is never repeated because its notice failed. A notice has its own fact, separate from the action fact.

## Coverage starts at the installation marker

The first start after an upgrade installs one coverage marker. Coverage starts at that marker and does not move. At installation the factory also imports the current state of each existing work item once, as an `observed_snapshot` at the marker time. The import has no fact time. It does not reconstruct history.

For each window, the metrics read reports:

- the requested window
- the available interval, from the later of the window start and the marker to the window end
- each missing interval before the marker
- `full`, `partial` or `unavailable` coverage on each ledger metric

A window wholly before the marker has no ledger values: they are unknown, not zero. An event inside a missing interval does not remove the gap.

## How to read the metrics

Each outcome metric counts **one current result per tracked attempt**. A delivered result outranks sourced non-delivery, which outranks unknown, so later evidence can resolve an unknown result but never lowers a delivered one. Repeated evidence counts once.

The fact that established the current result places it in a window: its known fact time, or its record time when the fact time is unknown. A weaker later fact, such as a routine issue closure, never moves an earlier delivery into a later window. Terminal tracking is placed at the first terminal fact of the attempt.

| Metric | Meaning |
| --- | --- |
| `terminal_tracking_in_window` | Tracked attempts with a terminal fact. Terminal tracking is not delivery. Deleting the live item does not change the count. |
| `delivered_in_window` | Attempts whose current result is delivered. A merged pull request, observed by the verification merge paths, records this result with the PR merge time and the verified head version. A trusted acceptance declaration of an exact design version also records it (see below). |
| `merged_code_in_window` | Code attempts whose current result is a sourced merged pull request. |
| `delivered_design_in_window` | Design attempts whose current result is delivered: a merged design pull request, or a trusted acceptance of an exact design version. Independent review is counted separately. |
| `closed_without_delivery` | Attempts with sourced proof that they ended without delivery. GitHub closed the issue as not planned or duplicate, with no pull request. Or a fresh read showed that every pull request of the attempt is closed without merge, the issue is closed, and no approval, revision or retry continues the attempt. |
| `unknown_outcomes` | Terminal tracking without result evidence, for example an ordinary issue closure with no merge evidence. |
| `independently_human_reviewed_design` | Delivered design attempts with a validated independent human review of the exact delivered version, counted once per attempt. The evidence must name a human reviewer and attest independence; an agent member or the operator credential never qualifies. A merge, Leader approval or a review of another version is not human review. |
| `elapsed_attempt_duration` | The median seconds from dispatch to the first terminal fact of the same launched attempt, within one work item lifetime. Each attempt counts once, in the window that contains its first terminal fact; a later closure adds no second sample. It is not execution time or operator hands-on time. Unpaired facts are unknown. |
| `implementation_retries`, `diagnostic_retries` | Recorded retry charges by class. |
| `implementation_retry_counters`, `diagnostic_retry_counters` | The authoritative budget counters now. An authorized retry can reset them, so they are not charge counts. |
| `recovery_success` | Preserved revision results that completed, one per revision lifetime: the retained work item lifetime plus the revision's recorded creation time. Several observations of one result count once, also after the live revision row is deleted. A reused numeric revision ID is a new revision. Older results without a recorded lifetime are excluded, because their revision population is ambiguous. The metric reports how many facts it excluded and why. Those facts stay visible in the audit history. A cancellation or resume request is not a recovery success. |
| `operator_interventions` | Distinct applied operator actions, one per action and exact resource. Notification facts and facts derived from an action, such as the delivery fact of a review declaration, are not actions; rejected and uncertain actions are excluded and reported. |
| `harness_failures` | Failed launch transitions only. |
| `current_queue`, `total_tracked_attempts`, `pending_reviews`, `active_revisions` | Present state from live records, labelled with their population. |
| `cost` | Unknown. There is no measured usage attribution. |

Tracked attempts in separate scopes stay distinct. A count is never a unique-PR total.

## Result times and later results

A closed-without-merge result is a combination of current conditions. It has no reliable result time, so its fact time stays unknown and the record time places it. The pull request closure time and the issue closure time are kept as separate source times.

The watcher reads later results. Each poll reads at most 20 attempts that are unknown or closed without delivery, in turn, with no age limit. When a pull request of such an attempt merged later, the watcher records the delivery on the original attempt, with the attempt's original type and resource lifetime. When the work item was deleted, replaced or moved during the read, nothing is recorded. The watcher never changes a work item for a later result. The metrics read itself never fetches GitHub.

The watcher also records the facts of each merged pull request, once per watched repository scope: lines added and deleted, changed files, commits, and who merged it (`pull_request_facts`). The merge marks them as due in its own transaction, and each poll reads at most 20 due pull requests. A failed read writes nothing and is tried again on a later poll. A value that GitHub did not report is unknown, never zero. `merge_path` says how a pull request was merged. Merges recorded before this version have no facts.

Synth City records each automatic-merge attempt (`github_merge_attempts`), with the repository and pull request it was for. The attempt is committed before each path's final checks, which stay the last before the call: on the superseded-revision path the authority fence and the HOLD read; on the ordinary path the acceptance, approval and settlement checks, run again after the record. Its outcome is:
- `merged` when GitHub confirmed the merge;
- `refused` when GitHub can't have merged: a final check or HOLD stopped it after the record, GitHub answered 4xx, it was rate-limited, or the request was never sent;
- `uncertain` when it may have reached GitHub: a 5xx, a timeout or lost connection after sending, or an unreadable answer. An attempt left unfinished by a crash reads the same way.

GitHub can prove whether Synth City made a merge, but not whether a person did, because the factory merges with one shared account. So `merge_path` is:
- `automatic` with a confirmed attempt;
- `outside` (merged outside Synth City's automatic merge, by a person, GitHub's auto-merge, a bot or a script) with no attempt or only refused ones;
- `unknown` with an uncertain or unfinished attempt, or for a pull request merged before attempt recording began.

Attempts are matched by repository and pull request, so every team watching a repository sees the same classification, and a scope moved to another repository can't borrow its old attempts. A team that recorded the facts while an attempt was still open stores `unknown`, which was accurate then. When the attempt finishes as merged or refused, a one-time `pull_request_merge_path` event settles that team's classification, and the original fact stays as it was observed. A team whose facts are written just after the attempt finishes settles its own `unknown` in the same write. The ordering relies on SQLite's single writer, the default and documented database. An uncertain outcome settles nothing.

`merged_by` stays beside it as a fact, and nothing is inferred from it.

The watcher completes a work item only through one guarded write. The write requires the same attempt and status that the watcher read, and no pending approval request, live revision or requested retry. If any of these changed, the item stays unchanged and no result is recorded.

## Waits on a decision

Each plan approval request and each continuation request to the Leader records a wait: a start event (`decision_wait_started`) with who asked and who must answer, and an end event (`decision_wait_ended`) when the request leaves pending. The end records the outcome (approved, rejected, superseded, expired or cancelled) and who answered, which stays empty when nobody is known to have answered. Both events are keyed by the request id. A request that is never answered stays open with its age. When its work item is deleted first, the wait is unknown, not open. Requests made before this version have no wait.

Two more kinds of wait are recorded the same way. A Mail context request or handoff waits on its recipient from the moment it is sent (`mail_wait_started`) until it is answered or acknowledged (`mail_wait_ended`), keyed by the message id. The Mail side of a plan approval, continuation or work item scope amendment is not a second wait, because its decision wait already counts it. When a member's pane shows a question, trust prompt or sign-in prompt, an input wait starts (`input_wait_started`) on the bound Supervisor, or on the Leader when there is none. It ends when the prompt clears or turns into a provider refusal (`input_wait_ended`). A provider refusal is not a wait for input.

A work item scope amendment request is a decision wait like a plan approval, keyed by its request id. An effort-limit episode is one too: it starts when Synth City sends the Leader the effort-limit notice (`effort_wait_started`, keyed by that notice's message id) and ends when the Leader extends the limit, or when the item ends (`effort_wait_ended`). The waits are rebuilt from the durable notices whenever a notice is sent, an extension is granted or the item ends, so an interruption between the notice and its start loses nothing. An extension ends the notice it answers, attributed to the Leader; a notice it did not see ends unattributed when found, and never before it started. The rebuild covers every attempt of the item, but only the current attempt's extensions end its notices. A rebuilt start names the owner notified with that notice, or no owner. An owner's Mail request to the Leader during that time is still its own Mail wait, so wait counts may include both; time is counted once per instant by the retrospective measures.

A request beyond the scope's effort ceiling waits on the Supervisor. Synth City's ceiling notice starts it (`ceiling_wait_started`, keyed by that notice's message id), in the notice's own transaction. That transaction checks the item and the ceiling again, so no notice is sent for a step that already fits. Only evidence that the notice was resolved ends it (`ceiling_wait_ended`), and never before it started:
- `ceiling_raised`: a change to `max_item_effort_minutes` that makes the step the notice asked for fit, when it did not fit before. A change that still leaves too little room, or comes after the item or attempt ended, leaves the wait open. The reason names the asked minutes and the room before and after.
- `work_stopped`: an end-stopped disposition.
- `item_terminal`: the item ended first.
- `attempt_ended`: a retry or a new attempt replaced the notice's attempt. The snapshot names the new attempt's launch once it is known, never its nonce; until then the reason says the launch is not yet known.

A split has no record of its own, so it counts as whichever of these recorded it. The end names whoever acted, for example the operator for a repository scope change; the answerer stays unknown, because an operator key or a Leader never proves the Supervisor's or the Director's answer. The notice is not a Mail request, so it is not counted twice.

The owner also waits for the operator at a recovery checkpoint hold and at a recorded observation pause. A hold starts a wait when the revision enters `decision_hold` or `ack_hold` (`hold_wait_started`, keyed by the continuation request id and the stage). A pause starts one when it becomes `paused` (`pause_wait_started`, keyed by the pause id). The release or the resume ends it (`hold_wait_ended` or `pause_wait_ended`), and the end records exactly the actor that action supplied:
- the operator, only when the operator-authenticated route supplied it;
- the Supervisor, only when its external-actor identity was supplied;
- nobody, when no actor was supplied. The operator key does not prove which person used it, so "operator" is never read as the Director.

A wait that nothing released is closed by the monitor, unattributed, at its first resolving transition: `hold_cleared` when the hold's revision ended on its own, or `item_terminal` when the item ended first. The end takes that transition's own time, never the poll time. For a hold that is the revision's recorded end, its approval request's supersession time, or the end-stopped time. When neither is recorded, it uses the item's last write, an approximate bound that is never later than the poll. Neither flow is a Mail request, so neither is counted twice.

## Review acceptance declarations

An operator can record a review acceptance from a trusted source with `POST /api/v1/factory/review-acceptances`. The declaration names:

- the work item, its attempt, the pull request and the exact commit version
- the reviewer, the reviewer kind (`human`), independence and the decision
- the review time and the source type and reference

The declaration must match an attempt of the item, the pull request of that attempt, and a version that the factory recorded for it. A changed or unrelated version is refused with 409 and a rejected record. The operator credential proves only who recorded the declaration. The factory never derives a human reviewer or independence from a credential or account type.

An accepted, independent declaration for a design item also records delivery of that exact version, before or after a merge. A declaration never changes work state, approvals or merges.

## Filters

Both reads use the same applied team and scope context keys. A context key belongs to one resource lifetime. When a team, scope or item is deleted, its key is retired; a later resource with the same numeric ID receives a new key, so old history never attaches to it. A retired key still addresses the retained history, but it has no current population. A key of the wrong kind, an unknown key, or a scope of another team also has no current population: present-state values are then unknown, never global. Reading the audit events never creates a key.

Each section names the filters, page and observation time of the data it shows. When a newer read is pending, fails or is cancelled, the section labels its data as retained. Paging waits while the rows on screen belong to another selection.

## Operator token

The event list uses the operator token for this browser tab. If no token is stored, the page asks for one once. A 401 response removes the refused token, and the next read asks for a replacement. A 503 response means the backend has no configured operator token. In both cases the safe metrics stay available.

## How a settings change was made

The operator key authorizes the write. Audit events from operator writes record the request's access path in `via`. The factory API does not expose this field.

| `via` | Meaning |
| --- | --- |
| `mail_tool` | The request carried a Mail session token that the server validated. The server reads the session from its own record and stores that session id. The event shows no member and no session text from the request. |
| `ui` | No usable Mail session. The request had a browser `Origin` header that matches a configured CORS origin. |
| `api` | Anything else: no `Origin` header, or an origin that is not configured. |

The server validates the session for `mail_tool`. The `ui` and `api` labels describe the request's `Origin` header, which callers can set. The labels supply attribution context. They grant no access and identify no person.

An unknown, closed or stale Mail session token leaves attribution at `ui` or `api` with no session ID. An offline mailbox or a missing live pane for a slot-bound session counts as stale. This attribution check applies regardless of the Mail token enforcement setting. The campaign settings audit also flags an invalid session in its context. Audit events omit the token itself.

On a Quick start installation, the controller and agents share an OS user. Agents running as that user can read the operator token. The Teams page and Director key prompt show this warning. Dedicated installations run the controller and agents under separate OS users.

## Limits

- Facts before the coverage marker are not available and are not reconstructed.
- Independent human review is recorded only through operator declarations from a trusted source. Without a declaration, reviewed-design counts stay at zero with explicit unknowns. Test declarations are synthetic and prove no live human review.
- The observed runtime provider comes only from the native process of the slot's current, fresh authenticated Mail session. Agents started through a `node` or `bun` wrapper, and absent, expired, ambiguous or changed sessions, stay unknown.
- Human benefit measurements and operator hands-on minutes are not measured (V14 NOT_PERFORMED).
- Cost stays unknown without measured usage attribution.
- Events are retained for the life of the database. No automatic purge exists.

See the [Factory API](/api/factory) for the route contracts.

## Campaigns

A campaign records one stretch of a team's work for its retrospective. It belongs to the team's lifetime, so a team recreated with the same id starts fresh. Its selector is `label:<name>` or `milestone:<title>`, and it covers every repository the team watches. A label is matched by name and a milestone by title, because each repository has its own milestones. A team has at most one `active` or `finishing` campaign at a time. Starting a campaign records a `campaign_started` event.

Membership is read from GitHub when it is needed, in all issue states:
- An issue deferred or superseded through the end-stopped action after the campaign started leaves the membership, even if it still matches. It is listed among the departures with its destination (the target milestone or the superseding pull request) and its reason. Departures come from the recorded event itself (the team lifetime, repository and issue it named), so deleting the local work item, or reusing its id, changes nothing.
- A repository that GitHub could not read, or where the milestone title doesn't name exactly one milestone, marks the membership incomplete, with the reason. It is never counted as zero members.
- Once the campaign's team is deleted, its repositories are no longer read, even if a new team takes the same id. The membership is incomplete (`team_retired`). A campaign can only be started for an existing team.

### Campaigns created on upgrade

The first start of v4.4 records each team that has a backlog milestone or a parent issue, the settings the team's campaign progress already reads. Later starts record none. So a team configured afterwards, or one with neither setting, gets no campaign until the Director starts one. Each recorded team then gets one `active` campaign at a later poll:
- **Selector:** `milestone:<backlog milestone>`, or the title of the parent issue's milestone when the parent is in one of the team's repositories. A parent outside them, or without a milestone, gives no campaign.
- **Start:** the first admission (work item) of an issue that matches the selector, in the team's own repositories. With none yet, the campaign starts when it is created.
- **When it waits:** while GitHub can't be read, or the membership is incomplete, the team waits for a later poll rather than getting a guessed start.
- **When it yields:** if the Director started a campaign first, the Director's is kept. A deleted team, or a new team that took its id, gets none. Once a team's outcome is recorded, a slower poll can't overwrite it. Any other storage refusal leaves the team waiting for a later poll.

The Director can adjust a campaign's start with `PATCH /api/v1/agent-teams/{preset_id}/campaign/start`, giving the new `started_at` and the `expected_started_at` they saw.
- It needs the operator token, and a Mail session is refused.
- Only an `active` campaign of the team's current lifetime changes, and only when the start is still the one the Director saw. Otherwise the answer is 409, or 404 when the team was deleted or its id reused.
- A start in the future is refused (422).
- Each change records its own `campaign_start_adjusted` event, even when a start is moved back to an earlier value.

### Measures recorded from v4.4 on

Check runs, the merge path, rate-limit windows, pull request facts and decision waits are recorded only from v4.4 on. A campaign that started before a measure's recording began reads unknown for that measure, never zero. Check runs, the merge path and rate-limit windows use their own recording markers. Pull request facts and decision waits use the first start of v4.4, so they may read unknown longer, but are never known too early. Every campaign measure reads this coverage first (`campaign_upgrade_service.recorded_coverage`), as the rate-limit measure already does with its own marker.

### Campaign snapshots

A campaign snapshot holds the campaign's measures from its start to an as-of time, each as a metric sample with its sample count, coverage and calculation version (`campaign-snapshot-v2`; v1 read a closed issue without a found delivery as closed without delivery). It also lists the coverage of each measure recorded from v4.4 on. The snapshot reads no clock, so the same records and membership give the same snapshot.
- **Measures in this version:** issues in the campaign, merged, closed without delivery, and deferred (from the membership and its departures), and the time spent rate-limited.
- **The cutoff:** only facts recorded by the as-of time count. A deferral or a delivery after it isn't counted. GitHub reports today's membership, so a membership read after the cutoff, or a member's departure or delivery after it, leaves the membership measures unknown with that reason. Merged counts recorded delivery facts, not the current status. A fact is read by the team, repository and issue it recorded, so deleting the work item doesn't hide it. A row recorded before that identity was kept is read through its work item while the item exists. A legacy item's auto-merge time counts as a merge.
- **Unknown, never zero:** an incomplete membership makes the membership measures unknown, with the repository and its reason. A closed issue counts as closed without delivery only with a recorded non-delivery by the cutoff. Otherwise it is unknown (`delivery_history_missing`), and so is every closed issue while a recorded merge can't be placed on an issue (`delivery_identity_unknown`). Merged and closed without delivery then keep their known count, with the unknown count and partial coverage, or read unknown when no closed issue is known. A campaign that started before the rate-limit recording, or without its marker, reads unknown for that measure.
- **Cache:** a snapshot is cached per campaign for 30 seconds, for the Overview panel's polls. Any new or changed record a measure reads ends the cache at once, including a newly watched repository or a status change. A change to the campaign itself ends it too. The rate-limit coverage is read again on every cached answer, so a window still waiting to be written shows as unknown. Retiring the team ends the cache too.
- **Frozen snapshots:** stored as versions in `campaign_snapshots`. Finishing stores version 1. A repeated or simultaneous finish returns that same version unchanged. Only the Director (the operator key) recomputes a frozen snapshot, and each recompute stores the next version, even when two run at once. Earlier versions are never changed. A recompute reads GitHub now, so for an earlier cutoff its membership measures read unknown (`membership_read_after_cutoff`).

The metrics window's samples have no calculation version.

Until #1395 merges, a delivery whose local work item was deleted, or an older item with only an automatic-merge time and no delivery record, may read as not delivered.

### Asking an agent about a campaign

The Director can ask the Leader, the Supervisor (when the team has a binding) or a verifier about a campaign. Each role gets its own question, so the answers add up and do not overlap:

| Role | Question |
|---|---|
| Leader | Coordination and flow: the queue, the assignments, the decisions, and what held items up. |
| Supervisor | The factory and the process: hand merges, quotas and rate limits, merge gates, effects across teams. |
| Verifier | Quality and rework: what was wrong in submissions, why items needed more rounds, and what would have prevented it. |

The Director can add one question of their own. The routes need the operator token. A Mail session is refused.
- `POST /api/v1/agent-teams/campaigns/{campaign_id}/asks` sends the ask. The body has `role` (`leader`, `supervisor` or `verifier`), `member_id` (the verifier's Mail member), an optional `director_question` and `acknowledge_verifier_warning`.
- `GET /api/v1/agent-teams/campaigns/{campaign_id}/asks` lists the asks with their replies. It stores nothing. For a live campaign it reads the campaign's issues from GitHub, to tell which version is current.
- `POST /api/v1/agent-teams/campaigns/{campaign_id}/asks/{request_id}/lesson` records an answered ask's diagnosis as a lesson, once.

**The version.** An ask names one snapshot version, by id. For a live campaign, the ask stores a new version with purpose `ask` and a negative number (-1, -2, ...), or reuses the one stored for the same records. The records are the database records of the campaign's own team, the live issues in each repository with each issue's state, and the rate-limit measure (its windows, gaps and coverage). An event of another team never changes them. A new ask reads GitHub once. That one observation gives the identity and builds the version, so the stored numbers and the identity cannot disagree. When the issues cannot be read completely, the ask stores a new version marked incomplete, and no later read counts it as current. Time that only grows with the clock (the open part of an interval, or a rate-limit window still open) moves without a new record, so it does not make a new version by itself. For a frozen campaign, it uses the latest final version and stores nothing. The freeze and the Director's recompute keep their numbers 1, 2, 3, and read final versions only. The campaign's own asks and every reply in their threads are not counted as campaign Agent Mail, in the size measures or in the records, so asking never makes a new version by itself. The request and its reply carry the same version id: Mail stores the request's id on the reply when it saves it, and the sender cannot name another. A reply without it does not count.

**The link.** The request names the campaign page at its version: `/insights/retrospectives/{campaign_id}?version={version_id}`. Each ask in the list has the same link in `page_link`. The page reads the `version` query value as the immutable id to show. A verifier's request lists the recorded review and rework measures first, each with its sample count and coverage, and then the other numbers. A measure that is not recorded reads unknown, never zero.

**The request and the reply.** The request is an Agent Mail message. The Leader and a verifier get a context request. The Supervisor gets a team notice that it reads in its inbox. The reply is the answer in the request's thread. It is stored in Mail, so reading the asks never writes. The first reply from the asked agent closes the request.

**One open request per agent per campaign.** A second ask of the same agent is refused with `campaign_ask_open` until the first is answered. Two callers at once cannot both succeed: the Mail delivery key `campaign-ask:{campaign}:{agent}:{n}` is unique. Asks add no waits on a decision.

**A verifier takes time from review.** In a live campaign, review is often the busiest lane. The ask is refused with `campaign_ask_verifier_warning` until `acknowledge_verifier_warning` is `true`. A frozen campaign needs no acknowledgement.

**Stale.** An ask is stale when the current version id differs from the one it names. For a frozen campaign, the current version is the latest final one, so a Director's recompute makes it stale. For a live campaign, the current version is the stored one for the records now; with none, the ask is stale. `version_changed` says the ids differ. `metrics_unchanged` is detail only: it is `true` when every reported measurement is the same in the newer version (the metrics, the repository counts, and the item time, time went, bottleneck and flow sections, with their counts and coverage; the version id is not compared, and neither are the times that the calculation generates from its cutoff: the cutoff itself, the end of a generated coverage window, and the start of the four-hour window; times that were recorded are compared, and so is every recorder's start; the text of the rate-limit coverage window is read with the rate-limit recorder's start only), and it never hides staleness. A reading that cannot be made (a version stored before records were fingerprinted, a team that no longer exists, issues that cannot be read from GitHub, or a finished campaign with no final version) is `unknown` with `unknown_reason`, and `stale` is empty.

### Claude Code cost

The Claude Code reader adds up each member's token use and its API-equivalent cost over a campaign window. It reads the agents' own session files.
- **Counting:** each assistant message counts once, at the time of its first row, and only when that time is inside the window. The four token kinds (input, cache creation, cache read, output) are summed by model, and each total carries its message count.
- **Whose usage:** a session counts for a member only when it is the session Synth City recorded for that member's launch. Every other session in the team's folders is listed as unattributed usage, never dropped. The record holds the current launch only, so the sessions of an earlier launch show as unattributed, while a resumed session keeps its id. Each team folder is read once, however its workspace is spelled, and that includes a Codex member's workspace, so an earlier Claude Code session there is unattributed too. Every assistant row must name the recorded session and a working directory inside the member's workspace. When two members claim one session, both read unknown and the session is listed once as unattributed. When two workspaces encode to the same project folder (for example `project.alpha` and `project-alpha`), the folder is read once and each transcript belongs to the workspace its rows ran in; a transcript whose rows fit neither, or both, is unattributed with `session_mismatch`. A project folder that can't be listed is reported in the report's `unlisted` list with `native_log_unlisted`, so its unattributed usage reads unknown, not none; members' own sessions in it are still read.
- **Prices:** cost uses the dated price table (`PricingService.PRICES_AS_OF`), so a page can say "at prices as of `<date>`". A model is priced only by its exact id. A request whose whole prompt (input, cache creation and cache read) is over 200,000 tokens is priced at the long-context rates for every kind, output included, when the model has them. A model without an entry shows its tokens, and its cost reads unknown (`price_unknown:<model>`), never zero. Today's newer models (Opus 5.5, Sonnet 5.5, Fable 5.1) have no entry yet.
- **Unknown, never zero:**
  - a member without a recorded session, with one that isn't a canonical UUID, or whose workspace path can't be resolved: `binding_unverified`;
  - a session two members claim: `competing_binding`;
  - a row that names another session, or a working directory outside the workspace or that can't be resolved: `session_mismatch`;
  - a missing session file: `no_native_session`;
  - a file that can't be read safely (not a regular file, a link, another owner, writable by others, larger than the bound, or a failed read): `native_log_unreadable`, for that session only;
  - a row in an unknown format, including one without a row type: `format_unknown`. A window with no assistant usage is a known zero;
  - a Codex member, read by a later item: `harness_not_read`.
