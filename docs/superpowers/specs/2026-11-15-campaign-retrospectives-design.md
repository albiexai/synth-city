# Campaign retrospectives in Insights — design

Date: 2026-11-15, revised 2026-11-18. Revision 3 (redefined with the Director). Status: draft for approval,
target release v4.4. Not approved for building.

Revision 3 redefines the feature with the Director, using the v4.3 campaign as the worked example. The main
changes from revision 2: the feature is for **understanding a campaign and its bottlenecks**, with a **live view
during the campaign**; **Apply happens mid-campaign**, not after it; the page shows **cost** and **size KPIs**;
the Director can **ask the Leader or the Supervisor** for a view; and a **campaign health panel** goes on Overview.
"Changes in revision 3" at the end lists each change. Mechanisms from revision 2 that still apply (the snapshot
built from `build_metrics_window`, stages, guardrails, uncertainty, exposure, the target registry, preview and
confirm, append-only integrity) are kept.

## Intent

The Director opens the page to understand **how the campaign and the team are performing**, and to find
**what slows them down**:
- the team (who waited for whom);
- the process (charters, review rules, effort and amendment limits, merge gates);
- the infrastructure (CI duration, flaky tests, quotas and rate limits).

The aim is campaigns that deliver quickly and efficiently. The page also gives a feel for **how big a campaign
was**, and lets the Director compare it with earlier campaigns.

Why: in v4.3 the Supervisor reconstructed all of this by hand, from SQL, GitHub and Mail. Examples: item time,
rework rates, about 20 hand merges, a builder idle for two hours waiting on one decision, nine minutes of GitHub
rate limiting, a flaky test rerun on four PRs. The changes that helped (merge cap, model swaps, a Leader standing
rule) were made **during** the campaign, and they were judged within it.

## Goals

1. A campaign is a first-class record: a team, a selector (the label or milestone the team works from), a start
   and a finish.
2. A **live view** while the campaign runs shows the measurements, the suggestions, and the agents' views on request.
3. The measurements show **where work waited and who had to act**, together with **size, activity, flow, friction,
   cost and quality** KPIs (below).
4. **Apply is a mid-campaign action:** the Director applies a suggestion while the team that produced the evidence
   is still running, and only when the team can absorb the change.
5. Each applied change gets a **verdict within the same campaign**: items after it compared with items before it.
6. The Director can **ask the Leader or the Supervisor** for their view of a snapshot, at any time, and both are
   asked automatically at the finish.
7. **Finishing** freezes a retrospective that explains the campaign and lists **lessons for the next team or
   campaign**.
8. A simple **comparison** of finished campaigns side by side.

## Non-goals (this version)

- Applying changes after a campaign has finished. Lessons are carried forward by the Director, for example into
  charter templates (#1113) or the next campaign's settings, not applied with one click.
- Agents applying changes. Agents read and propose; the Director applies.
- An analyst-agent session launched by Synth City.
- Statistical comparison between campaigns. Comparison is side by side, labelled as association.
- Export to Markdown or a GitHub Discussion.

## Anti-goals (failure even if it works)

- A team that loosens its own limits.
- Numbers that cannot be traced to the events behind them.
- Agent judgement presented as measurement, or measurements hidden because an agent disagrees with them.
- Associations presented as proof that a change caused an improvement.
- Speed improved by worse work (smaller scope, skipped review, rubber-stamped plans) without the page showing it.
- A cost shown as complete when some members' usage is unknown.
- Weakening Audit's existing guarantees (write-time redaction, events in the same transaction as the change).

## Decisions made with the Director

| Topic | Decision |
|---|---|
| Purpose | Understand performance and bottlenecks (team, process, infrastructure); size KPIs; simple comparison (2026-11-18) |
| Where it lives | Insights (Audit renamed), tabs Retrospectives and Audit log (2026-11-15) |
| Live view placement | **Option B:** a campaign health panel on Overview, full live view in Insights (2026-11-18) |
| Cost | API-equivalent cost as the headline, subscription share beside it; priced by the approved provider that serves each model; every harness counts; unknown is never zero (2026-11-18) |
| Apply | Mid-campaign only, both immediate and restart-type changes, with "takes effect" and "disruption" shown; restart changes as a guided sequence (2026-11-18) |
| Verdicts | Within the same campaign (2026-11-18) |
| After finish | For understanding; lessons for the next team or campaign; no Apply (2026-11-18) |
| Agents | Ask the Leader and Ask the Supervisor, Director-triggered, on a pinned snapshot; both asked at the finish; the Leader is not otherwise involved while the campaign runs (2026-11-18) |
| Finish | Synth City prompts; the Director finishes; the Leader and Supervisor are asked; the retrospective freezes when both answer or after a time limit (2026-11-18) |
| Who applies | The Director only (2026-11-15) |
| Agent reads of audit | A restricted agent view, limited to the agent's own team (2026-11-15) |
| Loosening | Charter edits and any target not in the registry count as loosening (2026-11-15) |

## The design

### Navigation and placement

- The sidebar entry **Audit** (`/audit`) becomes **Insights** (`/insights`), with tabs **Retrospectives** (default)
  and **Audit log** (the current page, unchanged). `/audit` redirects to `/insights/audit`, keeping the query and hash.
- `/insights/retrospectives/:campaignId` shows one campaign: **live** while it's active, **frozen** after the finish.
  The team page links to the team's campaigns.
- **Campaign health panel on Overview**, one per active campaign in the current filter:
  - item time (median), merged against scope, cost so far (with its coverage);
  - the **top bottleneck** (the stage with the most waiting time in the last N hours, with the responsible actor);
  - at most **one** suggestion, as a link;
  - a link to the live view.
  The panel never applies anything, and it only reads data the live view also shows.
- The panel **extends the existing `CampaignProgress` panel** on Overview (one panel per team, keeping its counts and
  merge bar), rather than adding a second panel *(Supervisor's call)*.
- `/insights/compare?campaigns=a,b,c` shows finished campaigns side by side.

### Campaign record and membership

Unchanged from revision 2:
- Table `campaigns` keyed on the team's immutable lifetime key, with a selector (`label:` or `milestone:`), status
  `active` or `finished`, and at most one active campaign per team.
- Membership over **repository issue identities** (including issues that never started), with an append-only
  history: joined, left, deferred.
- Deferral is a recorded Director action. A selector change seen on GitHub is evidence to reconcile, not an
  authorized deferral.

Note: the v4.4 milestone routing (#1223) and the end-as-deferred action (#1219) produce the deferral events this
history needs.

### Measurements

The snapshot is built by `factory_metrics_service.build_metrics_window`, **extended in place** with an optional
campaign argument (the campaign's window and team key) and the measures below. There is one function, so the Audit
metrics panel and the retrospective share coverage rules *(Supervisor's call)*. Every measure is a
`FactoryMetricSample`-shaped record: value, sample count, unknown and excluded counts, coverage, source. A
**`calculation_version` field is added** to `FactoryMetricSample` (it doesn't exist today), so snapshots record
which calculation produced them. Partial history shows as unknown, never as zero.
The revision 2 rules on uncertainty (n on every tile, medians and p85 with intervals, "not distinguishable",
"too few to compare") still apply.

Legend: **R** = recorded today; **P** = partly recorded; **N** = needs new recording.

**Size**
- issues in scope, merged, closed without delivery, deferred (R);
- PRs merged and lines changed, median and largest (R, from GitHub);
- splits and scope growth: issues added during the campaign, split parents and their parts (P: the "Split from #N"
  convention isn't a recorded link; needs a structured split record, see #1114);
- duration, team size, member and model changes (P: needs the slot change event).

**Activity**
- Agent Mail messages: total and by sender role (R, from `mail_messages`); per merged PR (P: through the work item
  a message is addressed to, where it has one; messages with no work item count as "not linked");
- decisions requested and who answered them, with their wait (N: the decision-request lifecycle from revision 2). It
  covers **every decision flow**: plan approval (`GithubApprovalRequest`), continuation decisions, effort extensions,
  scope amendments, and Director or Supervisor decision requests. The stage breakdown needs to know who held each
  wait;
- reviews per PR and rework rounds, i.e. items with three or more commits (R);
- CI runs, reruns and flaky tests (P, see CI below).

**Flow**
- item time from dispatch to merge: median, p85, by size band (R, from the delivery ledger);
- throughput over time, with change markers (R, plus the change markers);
- **where work waited:** stages per attempt, with the responsible actor (N, as revision 2: dispatch, acknowledgement,
  plan approval, building, required checks, review, waiting for the Leader / Supervisor / Director, merge gate);
- builder utilisation: working, idle, blocked per member over time (P: activity states exist but aren't kept as
  history; needs an activity transition record).

**CI durations (added)**
- duration per required check: median and p85, slowest check named;
- queue time against run time;
- CI's share of item time, with reruns counted separately;
- failures by test, with flaky tests and how often each was rerun.

The factory watches required checks (`github_required_check_watches`) but keeps no per-run timing. It needs a
check-run record (check name, run id, attempt, head, queued_at, started_at, completed_at, conclusion), taken from
the check runs the watcher already reads, and failed test names where CI reports them. N.

**Friction**
- hand interventions: Supervisor recovery merges, operator force-releases and overrides, with reasons (P: some are
  audit events, some are only PR comments; needs one recorded intervention event);
- blocks by cause: merge guard, effort limit, amendment refusals, stopped items, quota, and **Synth City errors**
  (refusals, 409s, 500s that blocked an agent) (P);
- quota and rate-limit time lost: GitHub rate-limit windows, harness usage limits, forced model swaps (P: #1197
  reports the GitHub rate limit; harness limits need recording).

**Cost and quality**
- cost so far (see Cost);
- quality guardrails, as revision 2: reverts and follow-up fixes within N days, reopened issues, CI failures on the
  release branch after merge, closed without delivery, unknown outcomes.

### Cost

**A. API-equivalent cost: the headline.**
- Cost = tokens × price, per member and model, summed over the campaign window.
- Price comes from a **price table keyed by approved provider and model**: the price of the provider that actually
  serves the model for that member, since the same model can cost differently through different providers.
- The serving provider is a **new slot setting**, chosen from a Director-managed list of approved providers. It is
  not `AgentTeamSlot.provider`, which names the harness. A slot without it has unknown cost *(Supervisor's call)*.
- The price table is a **setting with a source and a date**, editable by the Director, not a code constant. Every
  cost figure says "at prices as of <date>".
- Today's `pricing_service.MODEL_PRICING` is Claude-only and keyed by model alone. It becomes the default seed for
  the new table, not the source of truth.
- Token sources, per harness:
  - **Claude Code:** per-message usage in the transcripts (as the Usage page reads them).
  - **Codex CLI:** `token_count` events in the rollout log (`total_token_usage`: input, cached input, cache write,
    output, reasoning).
  - **Muse, Antigravity, OpenCode, Copilot, Pi:** one reader each, where the harness records usage.
- A member whose harness reports no usage is **unknown, never zero**. The headline says "≈ $X, covering 4 of 5
  members". Usage is attributed to a member through the **native session identity** Synth City binds for activity observation
  (the Claude Code session id, the Codex rollout id), not the Mail session key. Usage that matches no bound session is
  shown as unattributed, never dropped *(Supervisor's call; the builder verifies the identifier formats first)*.

**B. Subscription share, beside it.**
- The percentage of each plan's limit the campaign used, where the harness reports it. Codex writes `rate_limits`
  with `used_percent` and the window into its rollout log. Other harnesses where available.
- Attribution is only claimed when the account served nothing else in the window. Otherwise it's shown as "account
  shared; share not attributable".

Documents and UI name harnesses, providers and models. They never say which platform serves a particular factory.

### Suggestions and Apply (mid-campaign)

Rules and the target registry are as revision 2. At most three suggestions are shown, ranked by the stage time they
account for.

**Four kinds of fix** (Director, 2026-11-19). Each suggestion says which kind it is, because each kind travels by a
different route. CI is the worked example:

| Kind | Who changes it | What the page offers | CI example |
|---|---|---|---|
| **Synth City setting** | the Director | a suggestion with **Apply** (mid-campaign), or a lesson | `accepted_base_update: merge`; the required checks list |
| **Repository change**: code or configuration Synth City doesn't own | the team, through a normal issue and PR | a suggestion that **drafts an issue** (evidence plus proposed change). The Director edits and approves it; it's filed with the campaign's ready label and the Leader dispatches it like any other item | shard e2e across parallel jobs (#1235); run only affected journeys on PRs |
| **Standing decision**: a rule an agent applies | the Leader or the Supervisor, by rule | a suggestion that adds a **standing decision** to the scope's settings (#1114, #1115), with a count of each use on the page | "a failure of a known flaky test gets one full rerun"; "update a stale branch from the base" |
| **Synth City defect** | the Synth City developers | **copy evidence**, as a bug report | a gate that can't pass a partial rerun (#1199) |

A rule offers a setting or standing decision only when its evidence identifies that target as the constraint. It
offers a repository change when the constraint is in the repository (for example a required check's duration).
Otherwise it offers an investigation, naming the next responsible actor. Repository-change drafts carry the measured
evidence and the expected effect, so the next campaign's verdict can check them like an applied setting.

What changes in revision 3:

- **Apply is on the live view only.** A frozen retrospective shows its suggestions as **lessons**, without Apply.
- Each suggestion states up front:
  - **takes effect:** next poll, next attempt, or after a member restart;
  - **disruption:** none, restart of named members, or not offered (see below).
- **Immediate changes** (caps, limits, hold rules): preview and confirm, as in revision 2.
- **Restart changes** (a charter edit, a model or provider change for a slot): a **guided sequence**, not one click:
  1. ask the affected members to reach a safe point, with a handoff where the session will be new;
  2. apply the change;
  3. restart each member in resume mode where possible;
  4. check that each member is observed (activity known, tool catalog current) before the sequence is complete.
  Each step shows progress and can stop with a reason. The sequence is recorded as one change.
- **Not offered mid-flight:** a change that would invalidate an approved plan or an active item (for example
  lowering an effort ceiling below an active item's spend). The page says why, and offers it as a lesson.
- **Verdicts within the campaign.** For each applied change, the live view compares the items exposed to it (as
  revision 2 defines exposure) with the items before it: confirmed, not confirmed, too few items, or mixed (other
  changes overlapping are listed). Keep or revert is offered while the campaign runs. The frozen retrospective
  records the final verdicts.

### Asking the Leader or the Supervisor

- Buttons **Ask the Leader** and **Ask the Supervisor** on the live view (the second only when the team has a
  Supervisor binding). Director-triggered only, with an optional question.
- Synth City **freezes a snapshot** of what the Director sees (version, time, the measures and their item and event
  ids) and sends it by Agent Mail, using a server-prepared bundle. The agent does not rebuild the facts itself.
- The agent answers through a dedicated Mail tool, `synth_city_submit_campaign_view`, in a fixed shape:
  - **findings:** each with text and the item or event ids it relies on, which the server validates against the
    snapshot;
  - **proposed changes:** each pointing at a measured chart and a registry target. These appear as suggestions the
    Director can apply.
  - or **decline**, with a reason ("will answer at the next checkpoint").
- The page shows "**Leader's view** (or Supervisor's view), based on the snapshot of HH:MM" next to the measurements,
  marked as judgement. It is marked stale when the live numbers have moved past a threshold since the snapshot.
- Limits: one open request per agent per campaign; a time budget for the answer; the measurements are always shown
  next to an agent's view, never replaced by it.
- The agent read projection and team boundary are as revision 2.

### Finishing a campaign

1. Synth City **prompts** the Director when every member issue is merged, closed or deferred and backlog coverage
   is known (unknown coverage blocks the prompt and says why).
2. The Director presses **Finish** (Director-only).
3. Synth City takes the **final snapshot** and asks **the Leader and the Supervisor** (if any) for their view of it,
   as above.
4. The retrospective **freezes** when both have answered, or when a time limit set by the Director (default 2 hours)
   runs out. A missing view is shown as "not received".
5. The frozen retrospective is read-only. Its **lessons** list holds the changes that worked (with their verdicts),
   the suggestions not applied, and the agents' proposals, as "start the next team or campaign with these".
   The Director can copy a lesson into a charter template or a team's settings by hand. There is no Apply.

### The page

Live and frozen share one layout, read top to bottom:
1. Header: campaign, team, selector, dates, status (live or frozen), items in scope and merged.
2. Headline tiles: item time, merged against scope, cost so far (A, with coverage, and B), Agent Mail messages,
   hand interventions.
3. **Where work waited**: stage breakdown with the responsible actor; decision-request waits.
4. Flow: throughput over time with change markers; builder utilisation.
5. CI: durations per check, queue and run time, reruns, flaky tests.
6. Friction: blocks by cause (including Synth City errors); quota and rate-limit time.
7. Quality guardrails, next to the speed measures.
8. Size: issues, PRs, lines, splits and scope growth, team and model changes.
9. **What to change** (live: up to three suggestions with Apply; frozen: **lessons**).
10. **Applied changes and their verdicts.**
11. **Agents' views**: Leader's and Supervisor's views, with their snapshot times.

Charts use Recharts (as in `frontend/src/features/usage`). Each chart links to the Audit log tab filtered to its events.

**Comparison page:** a table of finished campaigns, one column each, for the size, activity, flow, cost and CI
KPIs, with n per cell. Labelled "side by side, not a controlled comparison".

### Integrity

As revision 2:
- append-only triggers on audit events, snapshots, membership events, agent views and proposals;
- immutable snapshots, where only the Director recomputes a frozen one, creating a new version;
- "lower bound" labels on counts drawn from best-effort observations.

The new records (check runs, activity transitions, interventions, decision requests, cost readings) are append-only too.

## Phases

1. **Recording:** check runs; activity transitions; the decision-request lifecycle; intervention events; slot and
   preset change events; the PR-first-seen delivery fact; cost readers for Claude Code and Codex; the price table.
2. **Live view and health panel:** campaign record and membership; the extended metrics window; the live page;
   the Overview panel.
3. **Mid-campaign Apply:** target registry; immediate changes with preview and confirm; verdicts within the campaign.
4. **Agents:** the snapshot bundle; Ask the Leader / Supervisor; `synth_city_submit_campaign_view`; the agent read view.
5. **Finish and compare:** the finish flow with the final agent views; frozen retrospectives and lessons; the
   comparison page.
6. **Restart changes:** the guided restart sequence for charter and model changes.

Later phases: more harness cost readers (Muse, Antigravity, OpenCode, Copilot, Pi) as each records usage;
trusted-agent Apply, as revision 2 describes.

## Left to the builder

- Table and column names and migration shape.
- **Teams that already exist when the feature ships** *(Supervisor's call)*: the upgrade creates an `active` campaign
  for each team that has a backlog milestone or parent issue set (the campaign settings `CampaignProgress` already
  uses). Its start is the first admission of a matching item. The Director can adjust the start or finish it. Teams
  without those settings get no campaign until the Director starts one.
- Rule thresholds (configurable per team) and minimum sample sizes.
- The exact allowlists for change markers and the agent audit view, within the rules above.
- The "top bottleneck" window for the Overview panel (default suggestion: the last 4 hours).
- The staleness threshold for an agent's view.

## Changes in revision 3

| Change | Reason |
|---|---|
| Purpose: understanding performance and bottlenecks; size KPIs; simple comparison brought back | Director, 2026-11-18 |
| Live view during the campaign carries suggestions, Apply and agents' views; Overview gets a health panel (option B) | Director |
| Apply moved to mid-campaign only; the frozen retrospective gives lessons instead | Director: after a campaign there may be no next one, or the team differs |
| Verdicts within the same campaign | Director; matches how v4.3's changes were judged |
| Restart changes as a guided sequence; "takes effect" and "disruption" on every suggestion | Director: "only if the team can absorb them"; v4.3 swap caused an observation gap |
| Cost: API-equivalent headline, subscription share beside it, by approved provider and model, every harness, unknown never zero | Director |
| KPI groups: size, activity, flow, CI durations, friction, cost and quality; "factory defects found" dropped; "Synth City errors" as a block cause | Director |
| Ask the Leader / Ask the Supervisor on a pinned snapshot, with a dedicated tool; both asked at the finish | Director |
| Finish flow: prompt, finish, agents asked, freeze on answers or time limit | Director |
| Analyst session, trusted-agent Apply and export stay later; statistical cross-campaign comparison stays out | unchanged |
| Four kinds of fix (setting, repository change, standing decision, Synth City defect), each with its own route; repository changes are drafted as issues for the team | Director, 2026-11-19, with CI as the worked example |
| Builder check: one metrics function; `calculation_version` added; serving provider as a slot setting; attribution by native session identity; all decision flows counted; Mail per PR is partly recorded; the panel extends `CampaignProgress`; campaigns created on upgrade for existing teams | independent builder review of revision 3 |
