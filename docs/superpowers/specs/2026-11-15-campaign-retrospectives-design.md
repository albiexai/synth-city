# Campaign retrospectives in Insights — design

Date: 2026-11-15. Status: draft for critique, target release v4.3. Not approved for building.

## Intent

A factory should get better from one campaign to the next without anybody changing Synth City's code.
When a campaign ends, Synth City measures it, shows the results as charts, and suggests process changes
that a Director (or a permitted agent) can apply directly in Synth City's configuration. The next
retrospective then shows whether each applied change helped. This closes a self-improvement loop.

Why now: the v4.1 retrospective was done by hand (SQL, GitHub and Mail counts) and showed that the
changes that worked mid-campaign were mostly configuration (Leader charter, auto-merge and its cap,
continuation limits), while the structural fixes were code. Directors need the first kind to be one action, not a patch.

## Goals

1. A campaign is a first-class record with a name, a team, a start and an end.
2. Finishing a campaign generates a retrospective at once: a fixed snapshot of measurements, charts, and suggestions.
3. Each suggestion is one of three kinds: **setting (measured)**, **agent finding**, or **product defect**.
4. Setting and agent-finding suggestions can be applied in Synth City, with a before/after view, a confirmation and an audit record.
5. Product defects produce a drafted GitHub issue that a person submits.
6. The next retrospective reports on each applied change ("you applied X; Y changed from a to b").
7. Agents can read retrospectives and apply suggestions, with permission.

## Non-goals (this version)

- Comparing campaigns across different teams or repositories (deferred by the Director; measurements are stored in a
  versioned shape so it can be added later without recalculation).
- Automatic application of suggestions without a human or a permitted agent acting.
- Exporting a retrospective as Markdown or a GitHub Discussion (a later export of this feature).

## Anti-goals (failure even if it works)

- A team that loosens its own limits without a Director having granted that.
- Numbers that cannot be traced to the events behind them.
- Agent judgement presented as measurement.
- Weakening Audit's existing guarantees (redaction, transactional events) to make the feature work.

## Decisions made with the Director

| Topic | Decision |
|---|---|
| Where it lives | Inside the current Audit area (approach 2), which is renamed to cover both the audit log and retrospectives. |
| Campaign end | The Director (or a permitted agent) marks the campaign finished. Synth City prompts when every in-scope item is merged, closed or explicitly deferred. |
| Who writes suggestions | Both: fixed rules on the measurements always; an optional analyst-agent run adds labelled findings, each linked to its chart. |
| Layout | Option A: one report page read top to bottom (mockup: headline numbers, timeline with change markers, one chart per section, "What to change"). |
| Agent access | Agents may read and apply, with permission. |
| Loosening changes | Suggestions are tagged **tightening/neutral** or **loosening**. Two separate permissions; a Director may grant a trusted agent the loosening permission too. |
| Charter edits | Count as loosening, as does any unlisted target (fail closed). An agent without the loosening permission proposes them to the Director. |
| Cross-campaign comparison | Deferred. The first retrospective of a team compares the campaign's first and second halves; later ones compare to the team's previous campaign. |

## Approaches considered

1. **New Campaigns record with a Retrospective page under Teams.** Cleanest separation; more new surface. Not chosen.
2. **Inside Audit, renamed (chosen).** Reuses the event store, filters and the existing metrics panel; puts evidence and
   verdict side by side. Requires opening audit reads to agents, which the current route forbids
   (`backend/app/api/v1/factory.py` list_factory_audit_events: "Agent tokens do not authorize audit reads").
   The Director chose to reverse that rule, scoped as below.
3. **A generated report document.** Cheap and shareable but nothing can be applied and the numbers are not structured.
   Kept as a possible later export.

## The design

### Navigation and naming

- The sidebar entry **Audit** (`/audit`, `frontend/src/components/layout/Sidebar.tsx`) becomes **Insights** (`/insights`), with two tabs:
  **Retrospectives** (default) and **Audit log** (the current page, unchanged). `/audit` redirects to `/insights/audit` keeping query and hash.
  *(Builder's call within this: the exact label; "Insights" is the proposal.)*
- Retrospective URL: `/insights/retrospectives/:campaignId`. The team page links to its campaigns' retrospectives.

### Campaign record

- New table `campaigns`: id, team_preset_id, name (e.g. "v4.1"), backlog_milestone (copied from the preset at start),
  started_at, finished_at, finished_by (actor), status (`active`, `finished`), retrospective_status.
- A campaign starts when the Director starts it (or, for an existing team, from its first dispatch after the previous campaign finished).
  At most one active campaign per team.
- "Looks done" prompt: when every work item in the campaign's scope is terminal or explicitly deferred, the team page and
  the Retrospectives tab show "Campaign looks done — finish it?". Finishing freezes the time span.

### Measurements (fixed rules, versioned snapshot)

On finish, a service computes a `campaign_metrics` snapshot (JSON with `schema_version`) from Synth City's own records, including the new timestamps in "Calls made after the builder check" item 2:

| Measure | Source |
|---|---|
| Merges per hour, open→merge and plan→merge times | `github_work_items` (new `pr_opened_at`, `merged_at`), `github_approval_requests` |
| Plan/continuation requests and rejections | `github_approval_requests.status` (`rejected`) |
| Revisions and failed heads per item | `github_attempt_scope_revisions` |
| Merged by hand vs auto-merged | `github_work_items.merged_by_kind` (new), `auto_merged_at` |
| Mail per member per hour | `mail_messages` by sender |
| Settings changes during the campaign | `factory_audit_events` with `event_kind = policy_change` (plus the new slot event below) |

Each measure is computed for the whole campaign and for the halves (first retrospective) or against the previous campaign.
Rejection *reasons* are free text today (`github_approval_requests.reason`). Version 1 shows reasons only when a structured
code exists (the #937 preflight will add codes); until then the chart shows counts by request kind and the analyst agent may group reasons, labelled as its finding.

### Change markers

- Add a `policy_change` audit event to `PATCH /agent-teams/slots/{id}` for charter, provider, model and launch-option changes
  (same transaction, same redaction), so every process change is on the timeline.
- Every applied suggestion produces a `process_change_applied` event linking campaign, suggestion and actor.

### Suggestions

- **Rules** live in code as a small registry: each rule reads the snapshot and emits zero or one suggestion with: kind,
  tightening/loosening, the exact target setting and value, the measured evidence, and the chart it belongs to.
  Initial rules from v4.1: auto-merge cap reached; high plan rejection rate; high manual merge share; Leader message rate; long Supervisor latency.
- **Analyst agent** (optional, per retrospective): the Director clicks "Ask an analyst". Synth City launches a one-off agent
  session with read access to the campaign's audit events, metrics snapshot and Mail; it submits findings through a new Mail
  tool `synth_city_submit_retrospective_finding` (text, evidence references, optional proposed setting change). Findings show as "agent finding".
- **Product defects**: a rule or the agent may classify a finding as needing code; the action prepares an issue draft (title, body with evidence) that a person submits.

### Apply

- One endpoint `POST /insights/process-changes` takes a suggestion id (or an ad-hoc change) and a `confirm` flag.
  Without confirm it returns the before/after diff and the classification. With confirm it applies the change through the
  existing route logic (scope, continuation, preset, slot charter), records `process_change_applied`, and marks the suggestion applied.
- Classification is computed by Synth City from the target and direction, not by the caller. Loosening: raising caps,
  effort ceilings, concurrency, auto-merge scope, approval rounds; changing model or roster; turning autonomy on;
  **any charter edit** (free text, so its direction cannot be computed; Director decision 2026-11-15); and **any target
  not on an explicit tightening/neutral list** (fail closed). The tightening/neutral list is explicit: lowering caps,
  ceilings, concurrency or approval rounds; adding a review-hold label; turning autonomy off.
- Charter changes take effect for running members after a resume restart; the Apply result says so.

### Permissions and agent access

- Two new per-team permissions on a member or external actor: `apply_process_changes` (tightening/neutral) and
  `apply_loosening_process_changes`. Both off by default; only a Director grants or withdraws them; each grant is audited.
- An agent without the loosening permission can **propose** a loosening change; it reaches the Director as a one-click approval with the evidence.
- Audit reads open to agent and external-actor tokens **for their own team only** (events whose team or scope belongs to
  that team). Operator reads are unchanged. Redaction is already applied on write (`factory_audit_service.py`).
- Mail tools: `synth_city_list_retrospectives`, `synth_city_get_retrospective`, `synth_city_apply_process_change`
  (same diff/confirm contract), `synth_city_submit_retrospective_finding`.

### The page (option A)

1. Header: campaign name, dates, team, PRs merged.
2. Four or five headline tiles with the comparison and better/worse/flat.
3. Timeline: throughput over the campaign with every change marker, labelled.
4. Sections, one chart each: plan rejections, revisions and failed heads, cycle time per item, Mail per member, hand vs auto merges.
5. "What to change": suggestions with tag, evidence, and Apply / Edit charters / Draft issue; applied ones show who and when.
6. "Since last campaign": the effect of each change applied after the previous retrospective.
Charts use Recharts (already used in `frontend/src/features/usage`). Each chart links to the Audit log tab filtered to its events.

## Constraints

- Existing audit guarantees stay: events in the same transaction as the change; secrets redacted on write.
- No new chart library; follow `CLICKABLE_CARD`, `MODAL_SIZES` and the existing design tokens.
- The snapshot is immutable once written; recomputation creates a new version, never edits the old one.

## Calls made after the builder check (author's calls, open to the Director's correction)

1. **Campaign membership.** Each work item gets a `campaign_id`, stamped when it is admitted while a campaign is active.
   An item carried over unfinished moves to the next campaign when that campaign starts, and its pre-move history stays
   attributed to the earlier campaign by time. **Deferral** is a new recorded action ("defer to next campaign") that
   records an audit event and removes the item from the "looks done" count. Moving the issue out of the milestone on
   GitHub is detected at poll time and recorded the same way. No new dispatch status is added.
2. **Timestamps.** Synth City records what it needs from now on: `pr_opened_at` and `merged_at` (with `merged_by_kind`:
   auto or hand) on work items, written where the PR is first seen and where `_mark_merged` runs. "Existing tables only"
   is withdrawn. A one-time import may fill these for the v4.1 campaign from GitHub; imported values are labelled
   `imported` in the snapshot and on the charts.
   **Supervisor latency** is measured as the time from a Mail message addressed to the Supervisor actor to that actor's
   next reply or check-in (`mail_messages`, `supervisor-checkin` events). Where it cannot be measured, the rule stays silent.
3. **Agent reads.** Reads of retrospectives and audit events need no extra permission, but they are limited to the
   agent's own team. For a team member, "own team" is the member's preset. For an external actor, it is the presets
   that the actor supervises (`agent_team_presets.supervisor_actor_id`). Applying changes needs the permissions above.
4. **Relation to the existing campaign summary.** `TeamCampaignSummary` stays the live view of the active campaign.
   The `campaigns` row keeps the milestone value current while the campaign is active: a change made through
   `synth_city_update_team_campaign` or the preset updates the active row and records a `policy_change`, and it does
   not start a new campaign. The value freezes at finish.
5. **Snapshot versions.** Only a Director can recompute. Recomputing creates a new version with fresh rule suggestions.
   Applied suggestions and agent findings are linked to the campaign, not to a version, so they carry over. The page
   and "Since last campaign" use the latest version.

## Left to the builder

- Exact table and column names, migration shape, and how a campaign starts for teams that existed before this feature.
- The rule thresholds (configurable per team, with defaults from v4.1).
- How the analyst session is launched (a dedicated slot type or a one-off bridge session), within the access rules above.
- The phasing of delivery (suggested: campaign record + metrics + page; then Apply and permissions; then agent access and the analyst).
