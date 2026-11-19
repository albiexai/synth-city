# Campaign retrospectives in Insights — design

Date: 2026-11-15, revised 2026-11-19. **Revision 4: the v4.4 slice.** Status: for Director approval; target release v4.4.
Not approved for building.

Revision 4 keeps the Director's intent from revision 3 and cuts the build to what v4.4 can carry. An independent review
(2026-11-19) sized revision 3 at **75–85 work items, the largest feature in v4.4**. This slice is **about 23 items**.
Everything cut is listed under "Later phases", nothing is dropped. Revision 2's mechanisms (exposure, preview token,
verdicts, the structured agent tool) are kept in **Appendix A** for those phases. The Director accepted every
simplification on 2026-11-19.

## Intent

The Director opens the page to understand **how the campaign and the team are performing**, and to find **what slows
them down**:
- the team: who waited for whom;
- the process: charters, review rules, limits, merge gates;
- the infrastructure: CI duration, flaky checks, quotas and rate limits.

The aim is campaigns that deliver quickly and efficiently. The page also gives a feel for **how big a campaign was**.

Why: in v4.3 the Supervisor reconstructed all of this by hand: item times, rework, about 30 hand merges, a builder idle
two hours on an unanswered decision, flaky tests, nine minutes of GitHub rate limiting.

## Goals (this version)

1. A campaign is a record: a team, a selector, a start and a finish.
2. **Recording first.** Data that isn't recorded can't be recovered later, so the recording items are the first v4.4
   items. That way the v4.4 campaign itself is measured.
3. A **live view** while the campaign runs, and a **frozen** one after the finish.
4. The core measure is **where the time went**: active work, process steps, and **waiting on someone**, shown first and broken down by who had to act. Around it: size, flow, CI, friction, cost.
5. **The top three bottlenecks**, each with its **kind of fix** and a way to act on it.
6. A **campaign health panel** on Overview.
7. **Ask the Leader, the Supervisor or a verifier** for their view of a snapshot; all of them are asked at the finish.

## Non-goals (this version, all in "Later phases")

- In-page one-click Apply with a preview token; within-campaign verdicts with keep or revert; exposure tracking.
- A structured agent findings tool with validated citations; proposals that become appliable suggestions.
- Guided restart sequences.
- Comparison between campaigns.
- Builder-utilisation charts; standing decisions as settings (wait for #1114 and #1115).
- An editable price table, a serving-provider slot setting, the subscription share, cost readers beyond Claude Code
  and Codex.
- Test-level flaky detection (it needs test names that GitHub check runs don't carry).

## Anti-goals

- A team that loosens its own limits.
- Numbers that can't be traced to the events behind them.
- Agent judgement presented as measurement, or measurements hidden because an agent disagrees.
- Associations presented as proof that a change caused an improvement.
- A cost shown as complete when some members' usage is unknown.
- Weakening Audit's existing guarantees.

## Decisions made with the Director

| Topic | Decision |
|---|---|
| Purpose | Understand performance and bottlenecks; size KPIs (2026-11-18) |
| Where it lives | Insights (Audit renamed), tabs Retrospectives and Audit log (2026-11-15) |
| Live view placement | A health panel on Overview; the full view in Insights (2026-11-18) |
| Cost | API-equivalent cost as the headline; unknown is never zero; every harness eventually (2026-11-18). This version: Claude Code and Codex. |
| Acting on a bottleneck | Four kinds of fix: setting, repository change, standing decision, Synth City defect (2026-11-19) |
| Apply | Mid-campaign. **This version:** a link to the settings UI with the value prefilled (2026-11-19) |
| Verdicts | **This version:** before-and-after medians with n at each change marker, no verdict label (2026-11-19) |
| Agents | Ask the Leader / Supervisor on a pinned snapshot; both asked at the finish. **This version:** the reply is free text, shown as judgement (2026-11-19) |
| Verifiers | Verifiers are asked too: automatically at the finish, on demand mid-campaign. Each role gets its own question, so views don't overlap (2026-11-19) |
| Finish | Synth City prompts; the Director finishes; the agents are asked; the snapshot freezes (2026-11-18) |
| Scope | The v4.4 slice in this revision; the rest in later phases (2026-11-19) |
| Grouping | **A campaign is one team's campaign**, across every repository that team watches. Filter and group by team and repository. A product release worked on by several teams is several campaigns, side by side. Grouping campaigns into a product release is deferred (2026-11-19) |

## The design

### 1. Recording (first in v4.4)

New append-only records, written by the paths that already see the facts:

1. **Check runs:** **every** check run on each PR head, not only the required ones, each marked required or not
   by the scope's delivery policy. Each record holds the check name, the app that reported it, the head, the
   conclusion, `started_at` and `completed_at`. GitHub returns them all in the same list, so there are no extra calls.
   - **For GitHub Actions checks,** also the Actions job's `created_at` (so queue time = `started_at − created_at`),
     the run attempt, and the workflow's name and file path (for example `.github/workflows/e2e.yml`).
   - Recorded **once, when the run completes**, not on every poll, so the shared GitHub quota hardly moves.
   - Source: the check runs that `github_check_observation.py` already reads. It already follows Actions checks to
     their job and keeps the workflow run id. Today it keeps no timestamps, ignores non-required checks, and
     `GithubRequiredCheckWatch.observed_at` is overwritten on every observation.
2. **PR facts at merge:** additions, deletions, changed files, commit count and `merged_by`, recorded as a delivery fact
   next to `record_merged_delivery`. `merged_by` gives the hand-merge count without a separate intervention event.
   Nothing under `backend/app` stores these today.
3. **Waits on a decision:** each request to the Leader, Supervisor or Director, with who asked, who must answer, and
   when it was asked and answered:
   - every structured decision flow: plan approvals, continuation decisions, effort extensions, scope amendments,
     Director and Supervisor decision requests;
   - Mail requests with a `request_status`;
   - the input-wait observations `agent_activity_service._record_input_waits` already writes, so a plain question
     that leaves a builder idle is counted.
4. **GitHub rate-limit windows:** start, reset and the source of each. #1197 already reports the current state; it
   needs keeping as history.

### 1b. Generic by construction (Director, 2026-11-19)

The same page must work for any repository a team watches: Synth City, Tizonia, or a project with different CI.
So:
- **Nothing is keyed to a check name or a workflow file.** "Required", "CI share" and the CI table come from each
  scope's own delivery policy (names and apps). No rule names `e2e`, `backend` or any Synth City job.
- **Any check app.** Durations and conclusions come from check runs, which every CI that reports to GitHub produces.
  Fields that only GitHub Actions provides (queue time, workflow name and path, run attempt) are **unknown** for other
  apps, never zero, and the page says why: "this check's CI doesn't report queue time".
- **Flaky checks, generically:** the same check name fails and then passes on the same head. That works for reruns
  through a new Actions run attempt and for other CI apps' new check runs alike.
- **No repository conventions are assumed.** The retrospective doesn't depend on shard layouts, gate jobs, docs-only
  skips or known-flakes lists. Those belong to a repository's own CI.
- **Not available through GitHub's API to non-admins,** so not shown as a number: the organisation's concurrency limit
  and plan. Their effect is **inferred** from queue times that rise when several PRs push together.
- **Workflow files aren't read** in this version. The workflow path on each run is enough for a repository-change
  suggestion to say which file to change. Reading triggers and shard counts is a later phase.

**Acceptance:** the snapshot tests run against two fixture scopes: one with GitHub Actions checks named like
Synth City's, and one with **different check names from a non-Actions app**. Both produce the same sections, with
unknowns where the second can't report. Before the campaign starts, the live page is checked once against a second
real repository with different CI (for example Tizonia).

### 2. Campaign record

- A `campaigns` row: the team's lifetime key, a selector (`label:` or `milestone:`), started_at, finished_at,
  finished_by, and a status (`active` or `finished`). At most one active campaign per team.
- **Repositories:** a campaign covers **every repository its team watches** (the team's scopes). The selector is matched
  in each one: a label by name, a milestone by title, since GitHub milestones are per repository.
- **Membership** is the set of issues matching the selector in those repositories, plus the deferral and supersede
  events that #1219 records. There is no separate membership history in this version.
- **On upgrade,** every team that has a backlog milestone or parent issue set (as `CampaignProgress` uses) gets an
  active campaign. Its start is the first admission of a matching item; the Director can adjust it.

### 3. Campaign snapshot

- A **campaign snapshot function**, separate from `build_metrics_window`. It reuses that module's coverage and sample
  helpers, so coverage rules stay shared. `build_metrics_window` itself is a time-window summary of single-value
  samples, so it can't carry stage breakdowns or series.
- Every number carries its sample count and coverage. A partial history shows as unknown, never zero.
- `FactoryMetricSample` gains a `calculation_version` field, so frozen snapshots say which calculation produced them.
- **Cached** per campaign, with a short lifetime, because the Overview panel polls.

**Measures in this version:**
- **Size:** issues in scope, merged, closed without delivery, deferred; PRs merged; lines and files changed (median
  and largest); team size; model and member changes.
- **Flow:** item time from dispatch to merge (median and p85, by size band); throughput over time, with change
  markers from the existing `policy_change` audit events.
- **Where the time went:** stages per attempt, derived from the existing work-lifecycle transitions, approval
  requests, delivery facts and the new records. Each stage belongs to one of three groups (Director, 2026-11-19):
  - **Waiting on someone:** waits on a Leader, Supervisor or Director decision, by who had to answer; waiting for
    dispatch. These are the bottlenecks to act on, and they are shown first.
  - **Process:** required checks (CI) and review. These are necessary steps; their speed is covered in the CI section.
  - **Active:** planning and building.

  The page shows one split bar with the three groups' shares, then each group's stages. Finer stages, such as
  acknowledgement and the merge gate, come later.
- **CI:** duration per required check (median and p85, slowest named); queue time against run time, where the CI
  reports it; CI's share of item time; reruns. A check is **flaky** when the same check name fails and then passes on
  the same head (see 1b). It is named per check, not per test, in this version. **Non-required checks** are listed
  separately with their runner time, because they compete for the same runners.
- **Friction:** hand merges (from `merged_by`); waits on decisions; GitHub rate-limit time; Synth City errors that
  blocked an agent (refusals, 409s, 500s, from the audit log).
- **Cost:** see below.
- **Activity:** Agent Mail messages, total and by sender role.

### 4. Cost

- **API-equivalent cost** = tokens × price, per member and model, over the campaign window.
- **Readers:** Claude Code (per-message usage in the transcripts) and Codex CLI (`token_count` events,
  `total_token_usage`). Usage is attributed to a member through the native session identity Synth City binds for
  activity observation. Unattributed usage is shown as such, never dropped.
- **Prices:** a dated table in code, extending `pricing_service.MODEL_PRICING` (today Claude-only and keyed by model).
  The page says "at prices as of <date>".
- The headline shows coverage: "≈ $X, covering 4 of 5 members". A member whose harness isn't read yet is **unknown**.
- **A one-item spike comes first:**
  - prove per-member, per-window token reads for both harnesses;
  - confirm the controller can read the agent user's transcripts.

  If the spike fails, cost ships as "unknown" with the reason, and the rest of the slice is unaffected.

### 5. Bottlenecks and how to act on them

The section has two parts that behave differently (Director, 2026-11-19): a deterministic **ranking**, and a
**diagnosis** that comes from a few rules or from an agent.

**Ranking (deterministic).**
- A bottleneck is a stage from "Where the time went" in the **waiting on someone** or **process** groups, for one
  responsible actor (for example "waiting for the Leader", "required check: e2e").
- Ranked by the time it accounts for: over the **whole campaign so far** on the live page, and over the **last 4
  hours** on the Overview panel.
- **Against flicker:** a bottleneck counts only with a minimum share (default 10% of waiting-and-process time) and a
  minimum number of items (default 5). When the top bottleneck has changed since the Director's last visit, the
  page says "new since your last visit".
- Every number shows its n, and the ranking is reproducible from the recorded data.

**Diagnosis: a few rules, then an agent.**
- **Rules** are code, each with a stated condition the page shows when it fires. They are used only where the
  evidence is unambiguous. The starter set for 4.4:

  | Rule | Condition (defaults configurable) | Kind of fix |
  |---|---|---|
  | Small correction batches wait on a decision | ≥ 5 waits on correction-batch decisions, all approved | standing decision |
  | Effort ceiling hit for factory reasons | ≥ 2 items over `max_item_effort_minutes` with time lost to Synth City errors or refusals | setting |
  | One required check dominates | one check ≥ 25% of item time across ≥ 10 items | repository change |
  | Flaky check | the same check name fails then passes on the same head ≥ 3 times | repository change (known-flakes policy, #1236) |
  | Queue grows under load | median queue time ≥ 2× its quiet-time value when 3+ PRs run checks at once | repository change (take non-required jobs off PRs) |

- **No rule fires → "Investigate".** The bottleneck shows its responsible actor and a button **"Ask the Leader /
  Supervisor / verifier about this"**. The request carries that bottleneck's evidence (the stage, its items and its
  longest waits). The agent's diagnosis appears under the bottleneck, marked as judgement, and can propose a fix of
  any of the four kinds. The Director acts on it through the same routes.
- Rules are kept few on purpose. From v4.5, the structured agent tool (Appendix A) lets agent-proposed fixes become
  first-class suggestions.

Each suggested fix names its kind and offers the matching action:

| Kind | Action on the page |
|---|---|
| **Synth City setting** | a link to the setting in the v4.4 settings UI (#1115), with the suggested value prefilled. The Director confirms there. The change appears as a change marker. |
| **Repository change** | **draft an issue**: the evidence and the proposed change, which the Director edits and files with the campaign's ready label. The Leader dispatches it like any other item. |
| **Standing decision** | shown as a **lesson** until #1114 and #1115 make standing decisions settings |
| **Synth City defect** | **copy evidence** for a bug report |

Each suggestion states **takes effect** (next poll, next attempt, or after a member restart) and **disruption** (none,
or which members restart). A restart change shows a **checklist** that links to the existing actions:
1. ask the members for a safe point and a handoff;
2. apply the change;
3. restart the members in resume mode;
4. check that each member is observed.

A suggestion that would invalidate an approved plan or an active item isn't offered mid-flight.

**Before and after:** at each change marker, the page shows the median item time and stage waits before and after the
change, with n. It never labels the change as confirmed or failed.

### 6. Asking the Leader, the Supervisor or a verifier

- Buttons **Ask the Leader**, **Ask the Supervisor** (only when the team has a Supervisor binding) and **Ask a verifier**
  (one per verifier slot, for example V1 and V2). They are Director-triggered, with an optional extra question.
- **Each role gets its own question,** written into the request, so the views add rather than overlap:
  - **Leader:** coordination and flow: queue, assignments, decisions, what held items up.
  - **Supervisor:** the factory and the process: hand merges, quotas and rate limits, merge gates, cross-team effects.
  - **Verifier:** quality and rework: what was wrong in submissions, why items needed further rounds (missing tests,
    unclear acceptance, scope drift, item size, builder or model patterns), and what would have prevented it.
- Synth City freezes the current snapshot and sends it by **Agent Mail**: a link to the snapshot, its key numbers
  (for a verifier, the review and rework numbers first), and the role's question.
- The agent replies in Mail. The page shows the reply under its role, "**Leader's view**" (or Supervisor's,
  V1's, V2's), "based on the snapshot of HH:MM", next to the measurements and marked as judgement. It's marked stale
  when the live numbers have moved past a threshold.
- One open request per agent per campaign. **Mid-campaign, asking a verifier is on demand only**, because review is
  often the busiest lane and the request takes the verifier away from it.

### 7. Finishing a campaign

1. Synth City prompts the Director when every member issue is merged, closed, deferred or superseded.
2. The Director presses **Finish**.
3. Synth City takes the final snapshot and asks the Leader, the Supervisor (if any) and **every verifier**, each with
   its role's question.
4. The snapshot **freezes** when all of them have answered, or after the Director's time limit (default 2 hours). It freezes
   **at once** when no member session is live. A missing view shows as "not received".
5. The frozen page is read-only. Its **lessons** list the changes made, with their before-and-after numbers, the
   suggestions not acted on, and the agents' views.

### 8. Navigation and the page

- **Navigation:**
  - The sidebar entry **Audit** (`/audit`) becomes **Insights** (`/insights`), with tabs **Retrospectives** and
    **Audit log**.
  - `/audit` redirects to `/insights/audit`, keeping the query and hash.
  - `/insights/retrospectives/:campaignId` is live while the campaign is active, frozen after.
  - **The list** has filters for **Repository** (the same control as Overview's), **Team** and **Status**, and groups
    rows by team, by repository or not at all. Each row lists the campaign's repositories, with the issue count in
    each. Filtering by a repository keeps every campaign that includes it.
  - **Inside a campaign** that spans several repositories:
    - a repository picker recalculates every section for the chosen repository;
    - a per-repository table shows issues, merged, item time, CI share and the top bottleneck;
    - cost and Agent Mail stay team-wide, because they belong to members rather than repositories, and the page
      says so;
    - arriving from a repository filter pre-selects that repository.
- **The page, top to bottom:**
  1. header;
  2. headline tiles (item time, merged against scope, cost with coverage, Agent Mail messages, hand merges);
  3. **where the time went** (a split bar of waiting on someone, process and active; waiting on someone listed first, by
     who had to act);
  4. flow, with change markers and before-and-after;
  5. CI;
  6. friction;
  7. size;
  8. the top three bottlenecks with actions (or lessons when frozen);
  9. agents' views, one per role (Leader, Supervisor, each verifier).

  Charts use Recharts, as in `frontend/src/features/usage`, and link to the Audit log filtered to their events.
- **The Overview health panel** extends the existing `CampaignProgress` card, one per team. It adds:
  - item time;
  - cost so far with coverage;
  - the top bottleneck (the largest "waiting on someone" stage or process step), with its responsible actor;
  - a link to the live view.

  It never applies anything.

### 9. Integrity

- Frozen snapshots are **append-only and immutable**. Only the Director recomputes one, which creates a new version.
- The new recording tables are append-only by design (no update paths). Database triggers on them come in a later
  phase.

## Work items (estimate)

| # | Item group | Items |
|---|---|---|
| 1 | Recording: check runs, PR facts at merge, waits on decisions, rate-limit windows | 5 |
| 2 | Campaign record, membership, creation on upgrade | 2 |
| 3 | Campaign snapshot service, with its cache | 4 |
| 4 | Cost spike, then the Claude Code and Codex readers | 3 |
| 5 | Insights rename and redirect; the live and frozen page, with the repository and team filters and the per-repository view | 5 |
| 6 | Overview health panel | 1 |
| 7 | Top three bottlenecks: deterministic ranking, five starter rules, Investigate-then-ask; their actions; before-and-after | 3 |
| 8 | Ask by Mail; the finish flow | 2 |
| | **Total** | **about 25 items, 18–20 active builder hours** |

Order: 1 first, then 2 and 3, with 4 in parallel, then 5 to 8.

## Later phases

- **v4.5:**
  - in-page Apply with a preview token and compare-and-set, with verdicts and keep or revert (Appendix A:
    "Change markers and exposure", "Apply");
  - the structured agent tool with validated citations, and agent proposals as suggestions (Appendix A:
    "Agent access");
  - comparison between campaigns, over frozen snapshots;
  - builder utilisation, from activity transitions;
  - standing decisions as settings, with a use count (after #1114 and #1115);
  - **product releases across teams:** a release record grouping several teams' campaigns (for example, a product
    release built by a product team and a website team). It gets a combined page, with totals across teams and each
    team's campaign underneath, and a Release filter on the list. About 4–6 more items (Director, 2026-11-19).
- **v4.6:**
  - guided restart sequences;
  - an editable price table and a serving-provider slot setting;
  - the subscription share;
  - cost readers for Muse, Antigravity, OpenCode, Copilot and Pi;
  - test-level flaky detection (it needs a test-result source per repository);
  - append-only triggers on every recording table;
  - membership history, with reconciliation of selector edits made on GitHub.

## Left to the builder

- Table and column names; migration shape.
- Thresholds and minimum sample sizes.
- The cache lifetime and the staleness threshold for an agent's view.
- The Overview panel's "top bottleneck" window (suggested: the last 4 hours).

## Changes in revision 4

| Change | Reason |
|---|---|
| Cut to the v4.4 slice (about 23 items), with later phases listed | Independent review sized revision 3 at 75–85 items; Director, 2026-11-19 |
| The body is self-contained; revision 2's mechanisms are in Appendix A | Review: revision 3 pointed to text that no longer existed |
| Recording comes first, with PR facts at merge, check-run timings, waits on decisions (including input waits) and rate-limit windows | Review: unrecorded data is lost; several "recorded" measures weren't |
| A separate campaign snapshot function with a cache | Review: `build_metrics_window` can't carry stages or series |
| Before-and-after medians instead of verdicts; Apply as a settings link; a restart checklist; Ask by free-text Mail | Review simplifications, accepted by the Director |
| Cost: Claude Code and Codex readers, a dated price table in code, a spike first | Review: per-member attribution is unproven |
| A flaky check is named per check, fail then pass on the same head | Review: GitHub check runs carry no test names |
| The finish freezes at once when no member session is live | Review |
| Section 2 becomes "Where the time went": one split bar of waiting on someone, process and active, with waiting on someone first and broken down by who had to act ("waited" mislabelled active work) | Director, 2026-11-19 |
| Top bottlenecks: deterministic ranking with anti-flicker thresholds; five starter rules with stated conditions; anything else is "Investigate" with an agent asked about that bottleneck's evidence | Director, 2026-11-19: static rules alone would be brittle |
| Verifiers' views (quality and rework) alongside the Leader's and Supervisor's: asked at the finish, on demand mid-campaign; a different question per role | Director, 2026-11-19 |
| Recording takes every check run on a head (required or not), Actions queue and run times and the workflow path; the page is generic by construction across repositories and CI apps, with a two-fixture acceptance test and a check on a second real repository | Director, 2026-11-19 |
| A campaign is one team's campaign across all its repositories; repository and team filters, grouping and a per-repository view; product releases across teams deferred | Director, 2026-11-19 |
| Corrected code claims: PR lines and commits weren't stored; item time was a median only; check runs kept no timestamps | Review, verified against the code |

## Appendix A: revision 2 mechanisms (for the later phases)

Kept verbatim from revision 2 (commit ab8d8fbc), as the starting point for the later phases above. Where it conflicts
with the body of revision 4, the body wins for v4.4.

#### Revision 2 design (verbatim)

#### Navigation

- The sidebar entry **Audit** (`/audit`) becomes **Insights** (`/insights`) with tabs **Retrospectives** (default)
  and **Audit log** (the current page, unchanged). `/audit` redirects to `/insights/audit` keeping query and hash.
- `/insights/retrospectives/:campaignId` shows one campaign. The team page links to its campaigns.

#### Campaign record and membership

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

#### Measurements

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

#### Change markers and exposure

- Markers come from an allowlist of `policy_change` sources (Supervisor health transitions also use that kind and
  are excluded), plus two new events: a slot change event and an event for campaign-relevant preset fields
  (selector, parent issue, planned decisions; today only autonomy is audited).
- The slot change event uses a bounded projection: changed field names, typed model and provider values, and opaque
  revision references for charter and launch configuration. Charter text and launch arguments are never stored in audit.
- **Exposure.** A saved change is not the same as work running under it. Active attempts keep their captured delivery
  policy until adoption, and charters load at the next resume restart. The snapshot records which policy and charter
  revisions each measured attempt ran with. Before/after comparisons only count attempts exposed to the change;
  "configured, not yet active" is shown.

#### Suggestions

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

#### Apply (Director only in this version)

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

#### Agent access (this version)

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

#### The page (option A)

1. Header: campaign, dates, team, selector, items in scope and merged.
2. Headline tiles with n, intervals and "not distinguishable" where it applies.
3. Timeline: throughput with change markers, and exposure shading.
4. Where work waited: stage breakdown with the responsible actor; decision-request times.
5. Quality guardrails next to the speed measures; conditions (issue mix, models, quota and human-wait time).
6. What to change: at most three suggestions with evidence; investigations; product defects.
7. Applied changes and their verdicts, with keep and revert.
8. Hypotheses (collapsed): agent findings with their citations.
Charts use Recharts (as in `frontend/src/features/usage`). Each chart links to the Audit log tab filtered to its events.

#### Integrity

- Append-only triggers (`BEFORE UPDATE/DELETE` abort) on audit events, snapshots, membership events, findings and proposals.
- Snapshots are immutable; only the Director recomputes, which creates a new version with fresh rule suggestions.
  Applied changes and findings link to the campaign, so they carry over.
- Rejection counts drawn from best-effort observations are labelled lower bounds.

