# Factory audit and delivery metrics

The audit ledger records what the factory actually did. The metrics page counts those records in a time window. Both are observations. They never approve a plan, raise a limit, retry work, release a workspace or replay an action.

Open **Insights** in the main navigation, then the **Audit log** tab (`/insights/audit`). The old address `/audit` still works: it opens the same tab and keeps its query and hash. The safe metrics load without an operator token. The event list needs the operator token for this browser tab.

## What the ledger records

Each event records:

- the event kind, its source and its occurrence and recording times
- the actor: the shared operator role, an authenticated member and session, or a code-owned scheduler reference
- the action outcome: `applied`, `rejected` or `uncertain`
- deletion-safe links to the team, scope, item, revision and request, and immutable context keys
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

The watcher also records the facts of each merged pull request, once per watched repository scope: lines added and deleted, changed files, commits, and who merged it (`pull_request_facts`). The merge marks them as due in its own transaction, and each poll reads at most 20 due pull requests. A failed read writes nothing and is tried again on a later poll. A value that GitHub did not report is unknown, never zero. `merge_path` is `automatic` only for a merge that Synth City's own automatic merge confirmed; otherwise it is `unknown`. Merges recorded before this version have no facts.

The watcher completes a work item only through one guarded write. The write requires the same attempt and status that the watcher read, and no pending approval request, live revision or requested retry. If any of these changed, the item stays unchanged and no result is recorded.

## Waits on a decision

Each plan approval request and each continuation request to the Leader records a wait: a start event (`decision_wait_started`) with who asked and who must answer, and an end event (`decision_wait_ended`) when the request leaves pending. The end records the outcome (approved, rejected, superseded, expired or cancelled) and who answered, which stays empty when nobody is known to have answered. Both events are keyed by the request id. A request that is never answered stays open with its age. When its work item is deleted first, the wait is unknown, not open. Requests made before this version have no wait.

Two more kinds of wait are recorded the same way. A Mail context request or handoff waits on its recipient from the moment it is sent (`mail_wait_started`) until it is answered or acknowledged (`mail_wait_ended`), keyed by the message id. The Mail side of a plan approval, continuation or scope amendment is not a second wait, because its decision wait already counts it. When a member's pane shows a question, trust prompt or sign-in prompt, an input wait starts (`input_wait_started`) on the bound Supervisor, or on the Leader when there is none. It ends when the prompt clears or turns into a provider refusal (`input_wait_ended`). A provider refusal is not a wait for input.

A scope-amendment request is a decision wait like a plan approval, keyed by its request id. An effort-limit episode is one too: it starts when Synth City sends the Leader the effort-limit notice (`effort_wait_started`, keyed by that notice's message id) and ends when the Leader extends the limit, or when the item ends (`effort_wait_ended`). The waits are rebuilt from the durable notices whenever a notice is sent, an extension is granted or the item ends, so an interruption between the notice and its start loses nothing. An extension ends the notice it answers, attributed to the Leader; a notice it did not see ends unattributed when found, and never before it started. The rebuild covers every attempt of the item, but only the current attempt's extensions end its notices. A rebuilt start names the owner notified with that notice, or no owner. An owner's Mail request to the Leader during that time is still its own Mail wait, so wait counts may include both; time is counted once per instant by the retrospective measures.

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

The operator key is still the only authority for a settings change. A recorded event now also says how the change reached the server. The field is `via`, and it is stored on the event. It is not part of the factory API response.

| `via` | Meaning |
| --- | --- |
| `mail_tool` | The request carried a Mail session token that the server validated. The server reads the session from its own record and stores that session id. The event shows no member and no session text from the request. |
| `ui` | No usable Mail session. The request had a browser `Origin` header that matches a configured CORS origin. |
| `api` | Anything else: no `Origin` header, or an origin that is not configured. |

Only `mail_tool` is a fact the server derives. `ui` and `api` are labels from a header that the caller sets. They do not prove who called. A caller that holds the operator key and sends no session token is recorded as `ui` or `api`. Treat the field as attribution for the honest path, not as a control against a determined key holder.

A Mail session token that is unknown, closed or stale does not refuse the write. Stale means an offline mailbox or, for a session bound to a team slot, a pane that is not alive. This attribution check applies whatever the Mail token enforcement setting is. The operator key authorizes it as before. The event is recorded as unattributed: it keeps the `ui` or `api` label, stores no session, and carries `session_invalid` in its context. The token itself is never stored.

Slice 1 applies this to `PATCH /api/v1/agent-teams/presets/{id}`. That route now also records one `policy_change` event for a campaign change (the milestone title and the number of planned decisions, never the decision text). Other operator routes keep their current actor until a later change. The event kind `external_actor` exists and records an external actor by its actor id, but no route uses it yet.

## Limits

- Facts before the coverage marker are not available and are not reconstructed.
- Independent human review is recorded only through operator declarations from a trusted source. Without a declaration, reviewed-design counts stay at zero with explicit unknowns. Test declarations are synthetic and prove no live human review.
- The observed runtime provider comes only from the native process of the slot's current, fresh authenticated Mail session. Agents started through a `node` or `bun` wrapper, and absent, expired, ambiguous or changed sessions, stay unknown.
- Human benefit measurements and operator hands-on minutes are not measured (V14 NOT_PERFORMED).
- Cost stays unknown without measured usage attribution.
- Events are retained for the life of the database. No automatic purge exists.

See the [Factory API](/api/factory) for the route contracts.
