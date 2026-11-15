# Campaign retrospectives in Insights — design

Date: 2026-11-15. Revision 2 (after critique). Status: draft for critique, target release v4.3. Not approved for building.

Revision 2 incorporates five critiques on PR #943: the v4.1 Leader (coordination), the v4.1 reviewer (correctness),
the v4.1 harness builder (implementation), and two independent reviews (security; metrics and product).
"Changes in revision 2" at the end lists what moved and why.

## Intent

A factory should get better from one campaign to the next without anybody changing Synth City's code.
Synth City measures each campaign, shows where the work waited and on whom, and suggests process changes
that a Director applies directly in Synth City's configuration. The next retrospective checks whether each
applied change did what it was expected to do. This closes a self-improvement loop.

Why: the v4.1 retrospective was done by hand (SQL, GitHub and Mail counts). It showed that the changes that
worked were mostly configuration (Leader charter, auto-merge and its cap, continuation limits), made
mid-campaign, while the structural fixes were code. Directors need the first kind to be one action, not a patch,
and they need the signal during the campaign as well as after it.

## Goals

1. A campaign is a first-class record: a team, a selector (the label or milestone the team works from), a start and an end.
2. Finishing a campaign generates a retrospective: a frozen snapshot of measurements, charts and suggestions.
   An **interim view** of the same measurements is available while the campaign runs (not frozen, no verdicts).
3. Measurements show **where each item waited and who had to act**, not only totals.
4. Each suggestion is one of: **setting (measured)**, **investigation**, or **product defect**. Agent-written
   findings are shown separately as **hypotheses**.
5. The Director applies setting suggestions in Synth City, with a before/after view, a recorded expected effect,
   and an audit record.
6. The next retrospective gives each applied change a verdict: confirmed, not confirmed, too few items, or mixed
   with other changes, with keep or revert.
7. Agents can read retrospectives (through a restricted view) and **propose** changes with evidence.

## Non-goals (this version)

- Agents applying changes. Deferred to a later phase (see "Later phases"); the Director decided on 2026-11-15
  to ship "agents propose, the Director applies" first.
- An analyst-agent session launched by Synth City. Deferred; agents that already read the retrospective can propose.
- Comparing campaigns across teams or repositories. Measurements are stored in a versioned shape so this can come later.
- Exporting a retrospective as Markdown or a GitHub Discussion.

## Anti-goals (failure even if it works)

- A team that loosens its own limits.
- Numbers that cannot be traced to the events behind them.
- Agent judgement presented as measurement.
- Associations presented as proof that a change caused an improvement.
- Speed improved by worse work (smaller scope, skipped review, rubber-stamped plans) without the page showing it.
- Weakening Audit's existing guarantees (write-time redaction, events in the same transaction as the change).

## Decisions made with the Director

| Topic | Decision |
|---|---|
| Where it lives | Inside the Audit area, renamed **Insights**, with tabs Retrospectives and Audit log. |
| Campaign end | The Director finishes the campaign; Synth City prompts when the whole campaign scope is resolved. Finishing is Director-only. |
| Suggestions | Fixed rules on measurements always; agent findings optional, shown as hypotheses. |
| Layout | Option A: one report page read top to bottom. |
| Who applies | **First version: the Director only.** Agents read and propose. Trusted-agent Apply is a later phase. |
| Agent reads of audit | Yes, through a restricted agent view, limited to the agent's own team. |
| Interim view | Yes, during the campaign, without verdicts. |
| Loosening and charter edits | Charter edits and any target not in the target registry count as loosening (fail closed). This matters for agent proposals now and for agent Apply later. |
| Cross-campaign comparison | Deferred. |

## Approaches considered

1. **New Campaigns record with a Retrospective page under Teams.** Not chosen.
2. **Inside Audit, renamed Insights (chosen).** Reuses the event ledger, the filters and `factory_metrics_service`;
   evidence and verdict sit side by side.
3. **A generated report document.** Nothing can be applied; kept as a possible later export.

## The design

### Navigation

- The sidebar entry **Audit** (`/audit`) becomes **Insights** (`/insights`) with tabs **Retrospectives** (default)
  and **Audit log** (the current page, unchanged). `/audit` redirects to `/insights/audit` keeping query and hash.
- `/insights/retrospectives/:campaignId` shows one campaign. The team page links to its campaigns.

### Campaign record and membership

- Table `campaigns`: id, team context key (the immutable team lifetime key, not the numeric preset id), name,
  selector (`label:<name>` or `milestone:<title>`), started_at, finished_at, finished_by, status (`active`, `finished`).
  At most one active campaign per team. v4.1 ran on the label `release:v4.1`; 29 of its closed issues had no milestone.
- Membership is a set of **repository issue identities**, not work items, so issues that never started are included.
  It is computed from the selector and kept as an append-only history table (`campaign_membership_events`:
  joined, left, deferred, with time, actor and evidence). A work item links to a membership row when one exists.
- **Deferral** is a recorded Director action. A selector change seen on GitHub (label or milestone removed) is recorded
  as evidence to reconcile, shown to the Director, and not treated as an authorized deferral.
- The "looks done" prompt covers the whole scope: every member issue is merged, closed or deferred, and backlog
  coverage is known. Unknown coverage blocks the prompt and says why.
- Items carried over to the next campaign keep their earlier history in the earlier campaign; durations in the new
  campaign start when the item joined it.

### Measurements

- The snapshot is built by **`factory_metrics_service.build_metrics_window`** over the campaign window and team key,
  extended with the measures below. The Audit metrics panel and the retrospective therefore cannot disagree.
- Every measure is a `FactoryMetricSample`-shaped record: value, sample count, unknown and excluded counts, coverage,
  source, and calculation version. Imported or partial history shows as unknown, never as zero.
- Merge time comes from the existing immutable delivery fact (`record_merged_delivery`, `fact_time` = the PR's
  `merged_at`), and auto-merge from `auto_merged_at`. No mutable timestamp columns are added to `github_work_items`
  (their timestamps are overwritten per attempt). One new delivery fact is added: **PR first seen**.
- Counting units are explicit: distinct failed heads by event time (not the cumulative `failed_head_count`), with a
  deduplication key and retained source references.

**Stages (the core measure).** Each attempt and head is split into stages with a reason and a next responsible actor:
waiting for dispatch, dispatch to acknowledgement, plan approval wait, building, waiting for required checks,
waiting for review, waiting for a Director or Supervisor decision, merge wait. Sources: approval request and decision
times, check watches, head reviews, delivery facts, workspace release. Overlapping waits and unknown intervals stay
explicit. Elapsed time and declared effort are reported separately; active time is never derived by subtraction.

**Decision requests.** Replace "Supervisor latency" with a correlated decision-request lifecycle: request id, target
actor, decision needed, acknowledged_at, resolved_at, outcome evidence. Unrelated Mail and check-ins do not resolve a
request. (Today a check-in overwrites one column and is not kept as history; this needs a new event.)

**Quality guardrails.** Each speed measure is shown with guardrails: reverts and follow-up fix PRs within N days,
reopened issues, master CI failures after merge, `closed_without_delivery` and `unknown_outcomes` from the existing
service, and operator interventions.

**Conditions.** The snapshot records what else changed: issue mix and size (lines and files changed), model and
provider per slot over time, hours lost to quota or harness failures, and human-wait time. Throughput is shown per
active agent-hour as well as per wall-clock hour.

**Uncertainty.** Each tile shows n. Durations use medians and p85 with a bootstrap interval; rates use a Wilson
interval. A tile says "not distinguishable" unless the intervals separate and the change exceeds a minimum size, and
"too few to compare" below a minimum n. The cycle-time section shows individual items, not only a summary.

**Comparisons.** In a team's first retrospective, comparisons split at the change markers (before and after each
change), group items by dispatch time, and show items in flight at the split as their own group. The first
retrospective is labelled a baseline. Comparisons are always described as associations.

### Change markers and exposure

- Markers come from an allowlist of `policy_change` sources (Supervisor health transitions also use that kind and
  are excluded), plus two new events: a slot change event and an event for campaign-relevant preset fields
  (selector, parent issue, planned decisions; today only autonomy is audited).
- The slot change event uses a bounded projection: changed field names, typed model and provider values, and opaque
  revision references for charter and launch configuration. Charter text and launch arguments are never stored in audit.
- **Exposure.** A saved change is not the same as work running under it. Active attempts keep their captured delivery
  policy until adoption, and charters load at the next resume restart. The snapshot records which policy and charter
  revisions each measured attempt ran with. Before/after comparisons only count attempts exposed to the change;
  "configured, not yet active" is shown.

### Suggestions

- **Rules** live in a code registry. Each rule declares: the eligible population, denominator, the blocking evidence
  that must be present, exclusions, the expected effect, and the guardrail that can veto it. A rule offers a setting
  change only when its evidence identifies that setting as the constraint; otherwise it offers an investigation
  (with the next responsible actor) or a product-defect draft.
- First rules, all backed by recorded data: auto-merge cap (fires only when items waited on the cap for X hours with
  no rise in reverts); plan rejection rate; manual merge share. Thresholds are "suggest only" until two campaigns of data exist.
- A test asserts that every setting rule targets an entry in the target registry.
- Suggestions are ranked by the stage time they account for; at most three are shown. When none clears the bar, the
  page says so and names the largest wait.
- **Agent findings** (from the Leader, Supervisor or another team agent, via a Mail tool) appear in a collapsed
  **Hypotheses** block. Each must cite item or event ids that the server validates; the page shows "based on n items".
  An agent finding can carry a proposed setting change only if it references a measured chart.
- **Product defects** offer "copy evidence" (a prepared issue body); a person files the issue.

### Apply (Director only in this version)

- A **target registry** lists every appliable target as `(model.column, unit, tightening direction, effect timing)`,
  using real columns: `max_auto_merges_per_day`, `max_build_parallelism`, `max_approval_rounds`,
  `max_continuation_revisions`, `max_continuation_failed_heads`, `max_failed_heads_per_revision`, `review_hold_label`,
  slot charter, and so on. A target not in the registry cannot be applied; the suggestion becomes a product defect.
  (Effort ceilings and the scope-amendment limit are not settings today; the latter is a code constant.)
- **Preview and confirm.** Preview returns the canonical patch, the before values, the classification of every changed
  field, the affected attempts, and a preview token. Confirm must present the token; the server checks that the
  current values still equal the previewed before values and applies the change, or refuses with no write.
- **Classification** is per field, and a patch takes its least restrictive component. `review_hold_label`: only
  null → label is tightening; replacing or clearing it is loosening (replacement releases every PR held by the old label).
- **Transaction.** Apply owns the transaction: the configuration change, its existing audit facts, the
  `process_change_applied` event and the suggestion state commit together. Shared helpers that commit internally
  (`agent_team_service.update_slot`, `update_scope_policy`) get a non-committing variant. An idempotency key prevents
  double application after a lost response.
- **Effect timing.** Each target states whether it affects the next campaign, future attempts, or current attempts.
  Retrospective changes default to future work. Apply never rewrites an approved plan, renews a lease, retries an
  item, restarts a member or lifts a hold. Charter changes are written to the slot; the result shows the member's
  current copy and "pending until restart" per member.
- **Expected effect.** The confirm dialog records the measure expected to move, its direction and rough size
  (pre-filled from the suggestion), and a review point (for example "after 15 items or the next retrospective").
- **Verdicts.** The next retrospective shows each applied change with confirmed, not confirmed, too few items, or
  mixed (other changes within the same window are listed), with keep and revert. Revert uses the same preview and confirm.
- Actor attribution needs #945 (v4.2): every write records who made it and through which path.

### Agent access (this version)

- Mail tools: `synth_city_list_retrospectives`, `synth_city_get_retrospective`, `synth_city_propose_process_change`,
  `synth_city_submit_retrospective_finding`.
- **Agent view of audit.** Agents read a separate projection, not the operator read: an allowlist of event kinds and
  fields; no free-text reasons written by other actors; no refusal details from guards.
- **Team boundary.** The server derives the authorized team context keys from the authenticated principal: a member's
  current team lifetime; for an external actor, the presets it supervises now, and only events since the binding.
  Every caller filter is intersected with that boundary before pagination and totals. Unbound sessions are refused.
  Missing or conflicting provenance fails closed. Events that carry only a slot id get their team resolved at write time.
- **Proposals** reach the Director with the server-computed diff and server-linked evidence, shown apart from the
  agent's own text. At most one open proposal per team and target; a rate limit per agent; proposals expire at
  campaign finish; approval uses the same preview-token check and happens only in the UI.

### The page (option A)

1. Header: campaign, dates, team, selector, items in scope and merged.
2. Headline tiles with n, intervals and "not distinguishable" where it applies.
3. Timeline: throughput with change markers, and exposure shading.
4. Where work waited: stage breakdown with the responsible actor; decision-request times.
5. Quality guardrails next to the speed measures; conditions (issue mix, models, quota and human-wait time).
6. What to change: at most three suggestions with evidence; investigations; product defects.
7. Applied changes and their verdicts, with keep and revert.
8. Hypotheses (collapsed): agent findings with their citations.
Charts use Recharts (as in `frontend/src/features/usage`). Each chart links to the Audit log tab filtered to its events.

### Integrity

- Append-only triggers (`BEFORE UPDATE/DELETE` abort) on audit events, snapshots, membership events, findings and proposals.
- Snapshots are immutable; only the Director recomputes, which creates a new version with fresh rule suggestions.
  Applied changes and findings link to the campaign, so they carry over.
- Rejection counts drawn from best-effort observations are labelled lower bounds.

## Phases

1. Campaign record and membership; the new events (PR first seen, slot change, preset fields, decision requests);
   the extended metrics window with stages, guardrails, conditions and uncertainty; the page and the interim view.
2. Target registry, Director Apply with preview token, transaction and expected effect; verdicts on the next retrospective.
3. Agent read view, the team boundary, findings and proposals.

## Later phases (not in this version)

- **Trusted-agent Apply**, requiring: #945 attribution; a "never agent-applicable" class (reviewer charters, any
  provider, model or launch options, repository identity and auth, dispatch and design labels, the selector,
  supervisor binding, permission grants, the applier's own slot); grants bound to a slot and a campaign that expire
  at finish; rate limits and a Director notice on each change.
- **An analyst session** with a server-prepared data bundle, no shell, no GitHub write, a finding tool bound to the
  session, and proposals only.
- Cross-team comparison and export.

## Left to the builder

- Table and column names, migration shape, and how a campaign starts for teams that existed before this feature.
- Rule thresholds (configurable per team) and the minimum sample sizes.
- The exact allowlists for markers and the agent audit view, within the rules above.

## Changes in revision 2

| Change | Source |
|---|---|
| First version: Director applies; agents read and propose; trusted-agent Apply and analyst session moved to later phases | B2, security, metrics; Director decision |
| Snapshot reuses `build_metrics_window` and the delivery ledger; no new mutable timestamps | B2, V1, metrics |
| Membership by selector over issue identities, including unstarted issues; append-only history; GitHub selector changes are evidence, not deferral | B2, L1, security |
| Stage breakdown with responsible actor; decision-request lifecycle replaces Supervisor latency | L1, metrics, B2 |
| Uncertainty, conditions, guardrails, association language, split at change markers | metrics, L1, V1 |
| Exposure: only attempts running under a change count for it | V1 |
| Preview token with compare-and-set; per-field classification; hold-label rule; transaction owned by Apply; idempotency | V1, security |
| Target registry with real columns; non-settings become product defects | B2, security |
| Effect timing; Apply never touches plans, leases, retries, restarts or holds | L1 |
| Expected effect at Apply; verdicts with keep and revert; interim view | metrics; Director decision |
| Agent audit projection; server-derived team boundary on context keys | security, V1, B2 |
| Marker allowlist; slot and preset events with a bounded projection | B2, V1, security |
| Append-only triggers; lower-bound labels | security |
| Attribution prerequisite filed as #945 (v4.2) | security |
