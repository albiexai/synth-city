# Factory API

These five observational routes return versioned Albiex Synth City factory work and repository observations. Availability depends on the installed version. Two more routes read the audit ledger and the delivery metrics, and one protected route records review acceptance declarations; see [Audit events and delivery metrics](#audit-events-and-delivery-metrics). One protected route checks repository setup prerequisites without writing; see [Setup preflight](#setup-preflight).

## Route classification

The classes below describe behavior and access, not the HTTP method. "Operator token" means the `X-Synth-City-Operator-Token` header. "Mail session" means an authenticated `X-Synth-City-Session-Token`.

A route with **Operator token** access accepts only the operator token; a session token never opens it. A route that accepts both principals authenticates a non-empty session header first, also when both headers are sent. With no session header, or an empty one, it requires the operator token.

| Class | Route | Access | Behavior |
| --- | --- | --- | --- |
| Safe observation | GET `/api/v1/factory/overview`, `/api/v1/factory/work-items`, `/api/v1/factory/work-items/{item_id}`, `/api/v1/factory/repositories`, `/api/v1/factory/repositories/{scope_id}`, `/api/v1/factory/campaigns` | None | Reads local records and a bounded scheduler observation |
| Safe observation | GET `/api/v1/factory/metrics` | None | Reads ledger aggregates |
| Protected observation | GET `/api/v1/factory/audit-events` | Operator token | Reads ledger history |
| Protected observation | POST `/api/v1/factory/setup-preflight` | Operator token | Checks the checkout, GitHub access, labels, base branch and selected authentication presence; creates or changes no records |
| Protected observation | GET `/api/v1/agent-teams/github-work-items/{item_id}/scope-revisions` | Operator token, or a Mail session of the same team | Reads revision history; private commands only for the operator or the matched current owner |
| Protected declaration | POST `/api/v1/factory/review-acceptances` | Operator token | Records one audit declaration; never changes work state, approvals, merges, retries, leases or counters |
| Protected mutation | POST `/api/v1/agent-teams/github-work-items/{work_item_id}/retry` | Operator token, or the Mail session of the current Leader under the conditions below | Resets the item for retry after current state and eligibility checks |

A Mail session for retry qualifies only when all of these are true:

- The backend enforces Agent Mail capability tokens (`mail_capability_tokens_required=true`).
- The session comes from MCP and its mailbox is connected.
- The session belongs to the member of the team's current enabled Leader slot.

Otherwise the route returns 403 `current_leader_required`. Other team and recovery routes are in the [Agent Teams API](/api/agent-teams).

All paths below begin with `/api/v1/factory`. These GET routes read local factory records and a bounded scheduler observation. They do not dispatch, claim a lease, send or acknowledge Mail, launch a provider, change the active project, or fetch GitHub issues. They have no operator/session authentication dependency in this source. Protected recovery reads and mutations have their own authentication and state checks.

| Method and path | Response |
| --- | --- |
| GET `/overview` | Selected work counts, configured automation and observed intake |
| GET `/work-items` | Paginated safe work projections and complete filtered counts |
| GET `/work-items/{item_id}` | One safe work projection |
| GET `/repositories` | Paginated repository scopes with intake, poll and overlap observations |
| GET `/repositories/{scope_id}` | One repository scope observation |
| GET `/campaigns` | Paginated campaigns with their repositories and recorded issue counts (see [Campaigns list](#campaigns-list)) |

## Filters and pagination

Overview and both lists accept optional `team_id`, `scope_id` and `provider`. IDs must be positive integers no larger than 2^63−1. A scope selected with a team must belong to that team. `provider` must be a registered harness ID: `claude-code`, `codex-cli`, `copilot-cli`, `opencode-cli` or `pi-cli`.

For work counts/lists, provider matches the assigned owner's currently configured harness. An unassigned or invalid owner is not replaced with a default provider and does not match a provider filter. For repository selection, provider matches a configured slot in that scope's team roster; scopes have no assigned owner. These filters therefore describe different sets. Configured harness and observed session provider are separate fields.

The work list also accepts `category`: `all` (default), `queued`, `active`, `review`, `attention`, `finished` or `unknown`. Both lists accept `limit` from 1 to 100 (default 50) and optional `cursor`. Details accept their path ID, without list filters or pagination.

List records sort by updated timestamp descending, then ID descending. Treat the cursor as opaque; pass `next_cursor` back with the same filters and list type. A changed filter, malformed cursor or incompatible cursor requires a fresh request from the start. A response's count/list/authority reads share a database snapshot; subsequent pages can observe later changes. `updated_at` describes a persisted record update, not an execution heartbeat.

```text
GET /api/v1/factory/work-items?team_id=1&category=attention&limit=50
GET /api/v1/factory/repositories?provider=codex-cli&limit=50
```

The IDs in examples are synthetic. Repeated GitHub names and issues in different scopes remain independent records.

## Response envelopes

Successful responses include `schema_version: 1` and UTC ISO-8601 `generated_at`. Optional values are explicit null; arrays are empty arrays when no records match.

| Route | Additional top-level fields |
| --- | --- |
| Overview | `filters`, `counts`, `automation` |
| Work list | `filters`, `total`, `has_more`, `next_cursor`, `counts`, `items` |
| Work detail | `work_item`, optional `initial_path_comparison` |
| Repository list | `filters`, `total`, `has_more`, `next_cursor`, `repositories` |
| Repository detail | `repository` |

`total` and category counts describe the full selected dataset before pagination, including the selected category. Repository total counts scopes, not distinct GitHub names. `next_cursor` is null exactly when `has_more` is false.

Work projections contain an allowlisted `item`, team/repository context, category, nullable owner/approver/waiting/workspace, separate owner and approver session associations, policy, action observations and typed link hints. Dispatch nonces, assigned branch refs, host workspace paths, credentials/hashes, private commands and freeform recovery summaries are omitted. Last verified SHA is nullable and belongs only to the displayed PR; it is null for diagnostic execution. Treat titles and raw statuses as display text.

Work detail includes `initial_path_comparison` when a complete first-plan comparison is current. It contains the PR head and base SHAs, changed repository-relative files grouped as `in_scope`, `approved_companions`, and `outside_scope`, and an outside-scope note when applicable. Inside and outside refer to the work item scope.

## Status and action meaning

| Raw tracking status | Category |
| --- | --- |
| `pending` | queued |
| `dispatched`, `verifying` | active |
| `awaiting_human_review`, `ready_for_review` | review |
| `escalated`, `failed` | attention |
| `merged`, `completed` | finished |
| Any unrecognized state | unknown, with the raw state retained |

Finished counts describe tracking state, not measured delivery success or human acceptance. Operator-requested escalation remains attention and does not prove that the operating-system process stopped or that a terminal delivery outcome occurred. Authoritative manual and issue-update retry rules remain intact.

An action has `name`, `state`, `block_code`, `reason` and `required_actor`. Retry uses the existing retry predicate; it can be blocked by active continuation authority, pending approval or a preserved PR. The other five remedies report unknown eligibility in this API. A configured continuation flag is not permission to resume a prepared attempt. No action observation authorizes its viewer: protected mutations check the current principal and state again. Retry accepts the configured operator or an eligible authenticated current-Leader MCP session. Protected revision history also checks its caller and limits private commands by principal. Agent decisions and human PR review remain separate.

Session state can be bound, offline, ambiguous or unknown. Only a verified association provides a concrete team/slot/member/MCP-session target. Nullable Mail or offline launch hints identify context, not an instruction to send, claim or launch.

Owner and approver session associations include the [shared `agent_status`](agent-teams.md#shared-agent-status) when a member is known.
Factory reads reuse their loaded records and add one bulk native identity read.
These observations describe the read snapshot; concurrent changes can invalidate them.
They send no input and save no native identity.
The legacy association state and protected action predicates retain their separate meanings.

## Repository intake and overlap

Each repository projection is one repository scope. Team automation and scope enablement determine configured enablement. Effective intake is eligible, blocked or unknown based on that configuration plus normal/recovery-only/unknown runtime mode, running/stopped/unknown scheduler state and whether its job is scheduled. Runtime has its own observation timestamp, distinct from response generation. Configuration alone does not prove intake is running.

Polling freshness is fresh, stale, never_polled, suspended or unknown. Stale means an eligible scope's last poll is older than twice the configured interval. Blocked or paused intake suspends freshness; unknown runtime remains unknown. Overview stale/never-polled counts include eligible intake only.

Overlap warns when other enabled repository scopes, in any team, have the same GitHub repository (owner and name, case ignored) and the same dispatch label. It checks local scopes outside the selected filters and returns safe other scope IDs. A warning neither merges work records nor arbitrates dispatch ownership.

## Errors

New read errors use `detail: {code, message}`.

| HTTP status | Code | Meaning |
| --- | --- | --- |
| 422 | `invalid_filter` | Invalid IDs, category, provider, limit or incompatible filters |
| 422 | `invalid_cursor` | Malformed, wrong-version, wrong-list or filter-mismatched cursor |
| 404 | `resource_not_found` | Selected team, scope or work item is absent |
| 500 | `projection_failed` | Database/projection observations could not be loaded |

```json
{"detail":{"code":"invalid_cursor","message":"Refresh from start with the selected filters."}}
```

Preserve both code and message. A read failure is an error, not a zero count. Existing protected routes retain their own string or structured error details and their 401/403/409 semantics; this contract does not rewrite them.

## Synthetic response and source

The following overview comes from checked-in disposable fixtures, not a live factory capture:

```json
{
  "automation": {
    "configured_scopes": 3,
    "enabled_scopes": 2,
    "intake_blocked_scopes": 0,
    "intake_eligible_scopes": 2,
    "intake_unknown_scopes": 0,
    "never_polled_scopes": 1,
    "paused_scopes": 1,
    "runtime": {
      "mode": "normal",
      "observed_at": "2026-09-30T11:59:00Z",
      "reason_code": null,
      "scheduler_state": "running"
    },
    "stale_scopes": 0
  },
  "counts": {
    "active": 27,
    "attention": 26,
    "finished": 26,
    "queued": 14,
    "review": 26,
    "total": 132,
    "unknown": 13
  },
  "filters": {
    "provider": null,
    "scope_id": null,
    "team_id": null
  },
  "generated_at": "2026-09-30T12:00:00Z",
  "schema_version": 1
}
```

Complete nested schemas, nullable fields and bounded reason mappings are in `backend/app/models/factory_schemas.py` and `backend/tests/factory/fixtures/v1/`. The checked-in fixtures are synthetic examples of this versioned contract.

## Campaigns list

`GET /api/v1/factory/campaigns` lists the campaigns of every team for Insights, Retrospectives. It reads the database and the snapshot cache in memory. It never calls GitHub and never builds, refreshes or freezes a snapshot.

Query: `team_id`, `scope_id` (the same filters as the other routes), `status` (`live`, `finished` or `all`, the default), `limit` (1 to 100) and `cursor`. A `scope_id` keeps every campaign whose repositories include that repository's owner and name, in any team. A `scope_id` that does not belong to `team_id` returns 422. A bad `status` or `cursor` returns 422, and a `scope_id` or `team_id` that does not exist returns 404.

Each row has the team (`null` after the team was retired), the selector, the status (`active`, `finishing` or `finished`), the start and finish times, `merged` and `in_scope`, the `repositories` with their issue counts, and the `source` of the counts. A `team_id` selects the current lifetime of that team only: a campaign of an earlier team with the same id is not listed.

`merged` and `in_scope` keep the snapshot's own `coverage`, `unknown_count` and `reasons`. A value with `coverage` `partial` is a known count with some outcomes still unknown, for example a closed issue with no recorded delivery.

- **Count source.** The latest frozen snapshot, else the last live snapshot built in this process, else none. `source.kind` is `frozen` or `live`. `source.as_of` is the cutoff that the counts describe, taken from the snapshot itself. For a frozen snapshot, `source.frozen_at` is when that version was stored. The list never refreshes a count.
- **Unknown is never zero.** A number that is not recorded is `null` with a `reason`: `not_read_yet` (a live campaign with no snapshot yet, which still names the team's repositories), `incomplete_read` (a repository could not be read), `membership_read_after_cutoff`, `departure_after_cutoff` or `delivery_after_cutoff` (a gap at the cutoff leaves every repository count unknown, as it does the total), `repository_added_after_snapshot` (a live campaign covers a repository that the last snapshot does not list), `no_repositories_watched` (a live campaign whose team watches no repository now, or a frozen snapshot that recorded an empty repository list with a known zero total and full coverage; this is known, not missing evidence, so a repository filter neither keeps nor hides it), `snapshot_without_repository_counts` (a snapshot made before the counts existed), or `no_snapshot` (a finished campaign with no snapshot).
- **Before recording.** A campaign gets `before_recording` instead of `no_snapshot` only when the recording marker exists and the campaign began before it. A missing snapshot alone never says it. `finished_before_recording` counts only finished campaigns that ended before the marker.
- **Repository filter.** A campaign whose repositories are unknown is not kept by a `scope_id` filter. `hidden_unknown_repositories` counts the ones that the `status` filter would show. A live campaign covers exactly the repositories its team watches now, never an older snapshot's list: it stays under a repository that was added after its last snapshot, with an unknown count, and it lists none when the team watches none.

`GET /api/v1/factory/campaigns/{campaign_id}/bottlenecks` returns the same read-only ranking input for
the campaign page and Overview panel. It reads the latest frozen snapshot for a finished campaign. For a live
campaign it uses the campaign snapshot service and its short cache.

- `bottlenecks.whole_campaign` is labelled **Whole campaign**. `bottlenecks.last_four_hours` is labelled
  **Last 4 hours**, with a start no earlier than the campaign start. Each includes the actual start and cutoff.
- `stages` contains the waiting and process stages that meet both thresholds. A required check is ranked
  by repository, recorded app and name. Review is ranked per verifier. Time uses the shared stage credits
  after clipping to the window. It excludes active and unaccounted time. `share` is a fraction of recorded
  waiting and process credits.
- `items` on a stage counts distinct issues with credited time. Retries do not increase that count. The
  thresholds are 5 issues and 10% inclusive. Each stage includes its median credited seconds per issue.
  A complete read with no qualifying stage has state `below_thresholds` and names the largest observed stage,
  when one exists. That stage can have `small_sample` set.
- `coverage` and `unknown_reasons` describe the underlying evidence. Partial values describe recorded data.
  Both windows carry the campaign attribution gaps. Credited time for an unidentified actor contributes to
  the denominator and leaves actor coverage partial. Missing time is `null`. A partial read with no
  qualifying stage has state `unknown`. A fully recorded quiet window reports zero time and zero items.
- The optional `previous_top` query parameter takes the exact top stage's `key` from the caller's last complete
  whole-campaign view. Use an empty string when that view had no qualifying stage. Omit it on the first visit.
  `new_since_last_visit` is `null` without a comparison or when current coverage is incomplete. The client
  owns visit persistence and should keep it scoped to the campaign.

The response also has `total`, `counts` (`live` and `finished`, before the `status` filter), `has_more` and `next_cursor`.

## Audit events and delivery metrics

These two GET routes read the observation ledger. They never fetch GitHub facts and never change a record. The [audit and metrics guide](/guide/factory-audit-and-metrics) explains the numbers.

| Method and path | Authentication | Parameters |
| --- | --- | --- |
| GET `/audit-events` | Operator token header `X-Synth-City-Operator-Token` | `page` (default 1), `page_size` (1–100, default 25), `event_kind`, `team_context_key`, `scope_context_key`, `item_context_key`, `team_id`, `scope_id`, `item_id` |
| GET `/metrics` | None | `window_start`, `window_end` (required), `filter_scope` (`all` or `scoped`), `team_context_key`, `scope_context_key` |

Audit events accept pagination and context filters, without a time window. Metrics accept a time window and context filters, without pagination. Agent session tokens do not authorize audit reads. A missing backend operator token returns 503; a missing or invalid header returns 401.

An audit page has `items`, `total`, `page`, `page_size`, the applied keys and `snapshot_labels` (the observed snapshot keys in the page). Each item has its kind, source, record kind, fact source and fact time, actor kind and reference, live links and context keys, a typed safe `context_snapshot`, a sanitized reason, allowlisted before and after values, and the action, delivery and completion outcomes. Member and session IDs, operation identities and replay keys are not returned. `live_links_available` is false when every live link was removed by deletion.

A current-ID filter (`team_id`, `scope_id`, `item_id`) resolves the active context key of the current resource lifetime, without writing. An ID with no recorded lifetime returns no events. A context key also addresses its own history after deletion; a reused numeric ID has a new key.

A metrics window has `window_start`, `window_end`, `filter_scope`, `counting_unit_note`, `instrumentation_start`, `available_interval_start`, `available_interval_end`, `missing_intervals` and `metrics`. `instrumentation_start` is the installed coverage marker, not the first event. A `scoped` request without a key returns no metrics. Each sample has `name`, `counting_unit`, `value` (null when unknown), `sample_count`, `unknown_count`, `excluded_count`, `unknown_reasons`, `source` and `coverage`. Ledger samples say `full`, `partial` or `unavailable` coverage. Outcome samples count one current result per tracked attempt. Present-state samples name their population. A key without a current resource gives unknown present-state values.

### Review acceptance declarations

POST `/review-acceptances` records one review acceptance from a trusted source. It needs the operator token header `X-Synth-City-Operator-Token`. It is observational: it never changes work state, approvals, merges, retries, leases or counters.

| Field | Rule |
| --- | --- |
| `declaration_id` | 8–64 characters (`A-Z a-z 0-9 . _ : -`). The idempotency key. |
| `work_item_id` | An existing work item. |
| `attempt` | The attempt key `item:{id}:launch:{launch}:revision:{revision}` of that item. |
| `artifact` | `owner/repo/pull/N` in the item's repository. |
| `version` | The 40-character lowercase commit SHA that was reviewed. |
| `reviewer` | The attributed reviewer. `operator`, `shared-operator-credential` and `member:` references are refused. |
| `reviewer_kind` | `human`, as declared by the source. |
| `independent` | As declared by the source. |
| `decision` | `accepted` or `rejected`. |
| `occurred_at` | The review time, with a timezone, not in the future. |
| `source_kind`, `source_ref` | `github_review`, `signed_record` or `operator_attested`, and a safe source reference. |

Responses:

- 201 with `event_id`, `operation_id`, `counted` and `delivery_established` when the declaration is recorded.
- 200 with the stored result for an exact replay.
- 409 with `refusal` when the declaration does not bind: `attempt_unrelated`, `artifact_unrelated`, `version_changed`, `version_unrelated`, `work_item_not_found` or `replay_conflict`. The refusal is recorded as a rejected event.
- 422 for a field that breaks the contract, and 401 or 503 for the operator token, with no write.

An accepted, independent, human declaration for a design item also records delivery of that exact version. The recording credential is stored as the recording actor, separate from the reviewer.

## Setup preflight

POST `/api/v1/factory/setup-preflight` checks repository setup prerequisites for guided configuration. It needs the operator token header `X-Synth-City-Operator-Token`. It is an observation: it creates or changes no records, creates no labels and starts no issue intake. It reads the local checkout and GitHub.

| Request field | Rule |
| --- | --- |
| `repo_owner`, `repo_name` | 1–100 characters (`A-Z a-z 0-9 . _ -`) |
| `repo_path` | The primary checkout path, 1–2048 characters |
| `dispatch_label`, `design_label` | 1–100 characters |
| `dispatch_auth_mode` | `token` or `github_app` |
| `base_ref` | Default `origin/HEAD`, 1–255 characters |

The response has:

- `status`: `ready`, `blocked` or `unknown`.
- `observed_at` and `checked_at`.
- `checks`: one entry for each check, with `status` (`ready`, `blocked` or `unknown`), a short `code` and a `remedy`.
- `configuration_presence`: allowlisted setting names mapped to `true` or `false` only. It never contains values, key-file paths, hashes or raw environment output.
- `host_guidance`: static host-procedure steps.

A `ready` result describes this observation only. It does not verify model access or credential validity for later work. Activation in the guided flow needs a fresh `ready` result. A missing or invalid operator token is refused before any check runs.
