# Changelog

All notable changes to Albiex Synth City will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [4.3.0] - 2026-11-18

Albiex Synth City 4.3.0 is a correctness release: it makes existing features work as they say. The factory now observes long Codex turns and split Claude Code replies, so team coverage and merge guards stop stalling; stopped work can be ended as deferred or superseded; the merge guard ignores other items' problems; GitHub rate limits are reported and backed off; and the harness pages say whose data they read. It also completes the OpenCode runtime-kit teardown and the Antigravity fail-closed checks, and verifies Muse first-session discovery across users.

**Not in this release (planned for 4.4):** routing work between teams by milestone; capability-aware routing; workspace provisioning that makes a new slot work on first launch; campaign retrospectives in Insights; TypeScript 7 and mcp 2.3. The Antigravity Agent Mail gate stays closed: `agy` still doesn't declare `call_mcp_tool` to the model (verified in #994).

### Added

- **A member that doesn't react to a wake is woken once more, and the Leader and the Supervisor are told if it still doesn't.** A delivered wake with no observed activity gets one re-wake, never a second. If the re-wake also gets no activity, the Leader and the Supervisor each receive one notice naming the member and its work item (#1059, #1060).
- **Provider refusals are recognised and shown.** A Codex pane showing a provider refusal or content-filter notice is recorded as a blocked state on the member. The Leader and the Supervisor are notified once, and Teams shows "Blocked: provider refusal" on the member card (#1062, #1091, #1092, #1064).
- **A stopped work item can be ended as deferred or superseded.** The operator route and the Leader tool `end-stopped` end an item with outcome `deferred` (naming the target milestone) or `superseded` (naming the delivering PR). It releases a verified clean workspace and clears the item's attention. A PR's final hold guard now ignores attention that belongs to other work items (#1219).

### Changed

- **Sessions, Usage, Context and Plans read the harness user's data, and say whose.** The four pages are scoped to the selected harness user, name the user and folder they read, and tell unreadable data apart from empty data (#1065, #1066, #1067).
- **The harness pages tell the truth about the agent user's install.** Claude Code's version is the agent user's, not the controller's; a missing CLI reads as "not installed" on both the Harnesses list and its pages; Memory has no project scope when no project is chosen; the project selector lists only the current harness's members' folders (#1068, #1069, #1070).
- **Codex session history is shown as not available yet,** and harness names and "Director key" wording are consistent across pages (#1165).
- **Agent Mail asks for the Director key when it's needed,** instead of reporting Agent Mail as uninstalled or opening the dialog on load.
- **Help text names "the backend environment file"** and links to the Installation guide, instead of the source path `backend/.env`.
- **Dependencies:** FastAPI 0.162 with Starlette 1.10 (#1160); jsdom 31 and `@testing-library/jest-dom` 7 (#1158); `actions/upload-artifact` and `actions/setup-python` v7 (#1157); a batch of minor and patch upgrades (#1058).
- **A Leader's extension request beyond the scope ceiling now reaches the Supervisor.** The request is still refused with `extension_exceeds_ceiling`. While the attempt is live (`dispatched` or `verifying`) and the team has a bound Supervisor, the factory also sends one "Effort ceiling reached" notice per item and attempt, which only the bound Supervisor's inbox shows. It names the request and the room left and grants no extension. Other refusals send nothing. The delivery policy text, the delivery practices and the extension tool's description now say the Leader extends within the scope ceiling and the Supervisor decides beyond it, instead of "ask the operator at the limit" ([#1102](https://github.com/albiexai/synth-city/issues/1102), split from [#1040](https://github.com/albiexai/synth-city/issues/1040)).
- **The Leader may extend an item's effort limit again, up to the scope ceiling, including while its PR is under review.** Each step is still at most the per-step cap (the smaller of 30 minutes and 25 percent of the original limit), and the limit plus all extensions may not pass `max_item_effort_minutes`. A step beyond the ceiling is refused with its own code, `extension_exceeds_ceiling`, naming the room left; `effort_extension_already_used` is gone. An extension is now accepted while the item is `verifying`. Before, every extension for a review correction was refused as `effort_extension_stale`, which is why two live extensions on #1059 needed the Supervisor. The item's effort view reports the room for the next step in `extension_cap_minutes`, which is no longer 0 after the first extension. The limit notice and the tool description say the Leader may extend again up to the ceiling. Each step keeps its own `effort_extension` audit event ([#1101](https://github.com/albiexai/synth-city/issues/1101), split from [#1040](https://github.com/albiexai/synth-city/issues/1040)).
- **The Leader's effort-limit extension states the total it saw and the evidence for it.** `synth_city_extend_effort_limit` now requires `expected_extension_minutes`, the extension total the Leader read in the item's effort (0 before any extension), and `evidence`, the work so far that supports the extension. A call whose total is no longer the recorded one is refused with `effort_extension_total_changed` and grants nothing, so a repeated or concurrent call never extends twice; before, a repeat with the same minutes returned the earlier result. Each granted extension records one `effort_extension` audit event with its reason and evidence. The one-extension rule and the per-step cap are unchanged ([#1100](https://github.com/albiexai/synth-city/issues/1100), split from [#1040](https://github.com/albiexai/synth-city/issues/1040)).
- **Running the frontend tests needs Node.js 22.22.2 or later in the 22 line, 24.15 or later in the 24 line, or 26 and later.** The test toolchain now uses jsdom 31 and `@testing-library/jest-dom` 7. Node.js 23 and 25 are not supported for the tests. `scripts/install.sh` still checks only for Node.js 22.12 or later, and that check is unchanged.

### Fixed

- **A Codex member in a long turn reads "working", not "unknown".** When a turn's start was more than 1 MiB back in its rollout log, the member read unknown, which made team coverage incomplete and stalled every merge guard. Synth City now reads earlier parts of the log, within an 8 MiB bound (#1201).
- **A Claude Code member that finished its turn reads "idle".** A final message written as several rows (thinking, then text) read as an unconfirmed completion (#1205).
- **The Autonomy tab shows a team's full work item count,** and its notification counter and the Audit coverage wording are correct (#1057).
- **Overview's Repository and Assigned-harness filters apply to Needs you, Progress and Teams,** including remaining work and next actors (#1141, #1142).
- **Team reads wait until the scope names are confirmed,** so the Overview no longer requests team summaries for an unknown scope (#1207).
- **The Work page shows the owner's agent status** when its session isn't bound to a verified pane, instead of a bare "unknown session" (#1073).
- **The Work and Repositories tables stay inside their box at 390 px** (#1166, #1230).
- **Detach removes a pane from the watch grid** and updates the watch count.
- **Backup URLs, unknown routes and Audit filters work,** and a missing asset returns a real 404 instead of a stale UI shell.
- **V1 can record a verdict on a submitted continuation revision** (no more 409 `amendment_context_unavailable`) (#1217).
- **The Leader can reject a pending initial plan on an escalated item** (#1190).
- **Antigravity native reads are bounded,** and activity needs overlapping snapshots; unseen screens and records fail closed (#1021, #1215).
- **The OpenCode runtime kit runs in its own PID namespace,** so `down` ends every process it started (#1018).
- **CI:** a failed-jobs rerun now passes the backend gate, because the gate uses the newest attempt's shard results (#1199); backend shards are rebalanced (#1029).
- **Completed work can release a clean workspace after its PR is closed unmerged.** The workspace HEAD must match the recorded PR's pushed head, branch and source repository. The Leader can read the exact lease identity before requesting release ([#1123](https://github.com/albiexai/synth-city/issues/1123)).
- **Concurrent Leader cancellation requests can finish when SQLite locks the owner notice.** The cancellation commits once. A bounded retry refreshes the cancellation context and settles the same keyed notice, so the caller that cancelled the revision can report success and the duplicate can report a replay. Owner and Supervisor notices each remain unique ([#1208](https://github.com/albiexai/synth-city/issues/1208)).
- **Guided repository setup can configure automatic merge.** Choosing automatic merge creates an enabled verifier for a new team or requires one for an existing team, and sets a daily cap of five. Repositories flags automatic scopes whose teams lack an enabled verifier separate from the Leader ([#1112](https://github.com/albiexai/synth-city/issues/1112)).
- **Teams explains why a coverage read is incomplete.** The summary shows safe source reasons and identifies an affected slot or work item when known. It distinguishes a session that needs a restart in resume mode from a temporary activity gap. The bounded unanswered-request read distinguishes timeouts, changing inputs and changed recipient bindings. A fresh summary leaves time for one hold-label operation. ([#1192](https://github.com/albiexai/synth-city/issues/1192))
- **Work progress, retry and Needs you agree on who acts next for a stopped attempt.** Safety HOLD and explicit operator recovery still take priority. During an ordinary intake pause, a retryable escalation points to the Leader. Needs you shows operator-owned stops and checkpoints. The protected actions keep their existing authorization and eligibility checks ([#1131](https://github.com/albiexai/synth-city/issues/1131)).
- **The factory backs off when its GitHub REST quota is exhausted.** Requests using the same polling credential pause until the reset or retry time. Operations health and repository scope reads show the remaining quota. The health endpoint and work item notes report `github_rate_limited` during a cooldown. Permission failures retain their auth diagnosis ([#1197](https://github.com/albiexai/synth-city/issues/1197)).
- **Overview › Needs you shows why an attempt escalated.** The inspection row shows the work item's known escalation reason even when its issue instructions are pending. When no known reason is available, the row keeps its existing detail and work-item link. The active factory contract schema is regenerated in both fixture copies ([#1140](https://github.com/albiexai/synth-city/issues/1140)).
- **A paused team with nothing left to do no longer raises "Supervisor check-in overdue".** The attention came from the check-in clock alone, so a paused team whose items were all done (deck-website-plan) still showed it in Overview › Needs you and on its team card. It is now suppressed while the team is paused and has no unfinished item in any of its scopes, whatever filter the summary is read with; the supervisor status itself still reads late or absent, and the operator read and the check-in result follow the same rule ([#1132](https://github.com/albiexai/synth-city/issues/1132)).
- **Escalated and failed items no longer show "State needs inspection" in the factory views.** The projection's list of known escalation reasons was stale (`ack_timeout`, `owner_idle`, `owner_unavailable` rather than the dispatcher's `leader_ack_timeout`, `owner_idle_timeout` and the rest), so most real reasons projected the waiting reason code `unknown`. The list is now the dispatcher's `ESCALATION_REASONS`, and every escalated or failed item maps to `abandoned_by_operator` or `attempt_escalated`; a missing or unrecognized reason gets the same fixed code and is never shown. The active v1-p04 contract fixtures are regenerated with `scripts/factory-fixtures.py` (P01 v1 is unchanged) ([#1181](https://github.com/albiexai/synth-city/issues/1181), part b of [#1139](https://github.com/albiexai/synth-city/issues/1139)).
- **An ended revision's prohibitions no longer look binding.** After WI286's PR merged, the owner context still showed its completed revision's `prohibited_actions` (including `release_workspace`) with nothing saying they no longer applied, so the workspace stayed leased and other items queued. Every revision response now carries `prohibitions_in_force`, true only while the revision is active or submitted; the owner context shows an ended revision with no prohibitions (the recorded terms stay in the revision history); and the guidance says an ended revision's prohibited actions no longer apply and releasing a merged item's workspace is never prohibited ([#1185](https://github.com/albiexai/synth-city/issues/1185)).
- **An escalated item is never left without a reason code.** Ending a revision returns the attempt to escalation with the revision's originating reason, and a scope amendment stores that as an empty string, so a handoff that superseded an active amendment left the item escalated with no reason, which the auto-merge note then showed as `unknown`. The new `restored_escalation_reason` falls back to `plan_blocked` (the attempt needs a fresh bounded proposal, as the Leader-cancel path already says), and a legacy empty row is named `escalation_reason_missing`. A static test keeps every escalation-reason write to a known code. The factory projection's stale reason list is [#1181](https://github.com/albiexai/synth-city/issues/1181) ([#1139](https://github.com/albiexai/synth-city/issues/1139), part a).
- **An escalated item whose revision was cancelled now settles when its PR merges at an accepted head.** The escalated-merge settlement (#1079, #1080) considered only active, submitted or exhausted revisions, so an item whose current revision had been superseded (cancelled through the Leader's tool) stayed escalated after its PR merged, and kept its workspace leased (WI233, PR #1152). A superseded revision has no submitted head, so it settles only when the merged head has an active acceptance for the same attempt and PR; the revision stays superseded ([#1164](https://github.com/albiexai/synth-city/issues/1164)).
- **A scope amendment that leaves out approved paths is refused when it is proposed, not at completion.** An amendment that named only its new path dropped the approved plan's paths, so completion later refused the work on them with `continuation_paths_out_of_scope` (#1055). An amendment now names its whole effective path set; an approved path it deliberately drops goes in the new optional `withdrawn_paths` of `synth_city_request_scope_amendment`. Otherwise the request is refused before anything is stored (`amendment_base_paths_omitted`, or `amendment_withdrawn_paths_invalid` for a wrong withdrawal). Completion still checks the allowed paths literally, so existing revisions and their withdrawals keep their meaning ([#1154](https://github.com/albiexai/synth-city/issues/1154), Supervisor option A).
- **The CI-watch capture no longer crashes on objects a rollback expired.** During verification, `github_ci_watch_service.capture` read `scope.preset_id` from the poll's scope after an earlier rollback in the same session had expired it, a synchronous lazy load in async code that raised `MissingGreenlet`. Capture now reloads only the expired attributes of the item and scope first (pending changes stay), so its callers' following reads work too; an expired object that is no longer in the session gets no watch instead of a lazy load. The team is read through the freshly loaded scope ([#1150](https://github.com/albiexai/synth-city/issues/1150), split from [#1144](https://github.com/albiexai/synth-city/issues/1144)).
- **Less write contention on the factory database.** Reading agent status (the Live sessions page's `GET /agent-bridge/sessions` and Mail status reads) no longer writes input-wait state; the poll and the team activity view still record it. SQLite's busy timeout is 15 s instead of 5 s, and a busy or locked error that outlasts it (any SQLite busy or locked code, including a shared-cache table lock) answers `503` with `Retry-After: 1` (`database_busy`) instead of a `500`. The controller's checkpoint backup copies 1024 pages at a time, releasing its read lock between steps; if writes keep restarting it, it stops after 20 steps without progress and removes the partial copy ([#1149](https://github.com/albiexai/synth-city/issues/1149), split from [#1144](https://github.com/albiexai/synth-city/issues/1144)).
- **A caught database failure in the poll no longer poisons the rest of the run.** About two dozen broad `except` blocks in the dispatch, watcher, verification, coordination and wake follow-up services logged a failure and kept using the same session. After a `database is locked` error or a failed flush, the session needed a rollback, so the next statement raised `PendingRollbackError` and the whole repository run rolled back (10 rolled-back polls seen live on 2026-11-17). Each of them now calls `recover_session`, which rolls the session back whenever it cannot be reused (any database error, or an inactive transaction) and keeps the stage's staged writes for a non-database failure such as a notification's HTTP error; a rollback that fails propagates. After a rollback it reloads the objects the session still holds, so the caller never reads an expired attribute (`MissingGreenlet`), and it runs first in every handler. A static test keeps new handlers from skipping it or placing it later. A poll failure finding, including one in the recovery-only loop, now names the stage that failed ([#1148](https://github.com/albiexai/synth-city/issues/1148), split from [#1144](https://github.com/albiexai/synth-city/issues/1144)).
- **A member's Mail tools are marked stale when a tool's parameters change, not only when tools are added or removed.** The catalog fingerprint a session reports, and the one the backend expects, covered tool names only. When #1100 added required parameters to `synth_city_extend_effort_limit` under the same name, a session started before the upgrade was still judged current, and its extension requests were refused (`extension_evidence_required`) with no hint to reload. The fingerprint now covers each public tool's input schema, computed the same way in the shim and the backend; the backend reads a committed snapshot (`backend/app/services/tool_catalog_schemas.json`, regenerated with `scripts/tool-catalog-schemas.py --write`) that a drift test keeps equal to the shim. The provider upgrade's shim check also compares the schemas. **Upgrade note:** after this change is deployed, every running member session shows a stale tool catalog once, until it is restarted in resume mode ([#1146](https://github.com/albiexai/synth-city/issues/1146)).
- **A finished team's remaining work no longer flips between known and unknown on repeated reads.** The team summary counted remaining work as known only when the whole summary was complete, including the human-action observation, which goes stale 120 seconds after a scope's last coordination poll. A paused team is polled only now and then, so a finished team's remaining work alternated between known (0) and unknown across reads seconds apart. Remaining work now depends only on what it is computed from: the unfinished items, their reports and an unchanged snapshot. A missing or expired item report, or a change during the read, still makes it unknown. Stale human-action, native or pause observations are still reported through `coverage_complete` ([#1107](https://github.com/albiexai/synth-city/issues/1107), split from [#1041](https://github.com/albiexai/synth-city/issues/1041)).
- **Campaign Progress counts escalated and failed items as needing attention, not as under way.** Progress listed an escalated item under "Under way" ("waiting for you") while the tiles counted it as needing attention, and a failed item landed in no Progress bucket at all. Both now count in a new "Needs attention" figure: in the campaign's totals, as an amber part of the bar and its label, and as a line with a link to Work filtered by the team and the attention category. Every item is in exactly one bucket, and the tiles and Progress share one definition (escalated or failed). `TeamCampaignSummary` gains `attention_count`; a response without it reads as none needing attention. The frozen factory contract schema is regenerated in both lanes ([#1129](https://github.com/albiexai/synth-city/issues/1129), split from [#1042](https://github.com/albiexai/synth-city/issues/1042)).
- **Every view uses one definition of a done work item: merged or completed.** Campaign Progress counted an item as done only when it was merged into the scope's current base branch, so a completed item, or a merge into another branch, fell into no campaign bucket at all (seen live: 13 of 13 done where the team had 14 finished items). The Finished tile, Work, work-item progress and the team summary's remaining work already counted merged and completed. All five now share one definition, and Campaign Progress counts every done item. A merge into another branch is counted as done and also reported in the new `done_on_other_branch_count`, so it stays visible; only a recorded base that differs counts there, so older merges without a recorded base are done but not claimed as another branch. Campaign Progress still counts only the team's campaign backlog while the Finished tile counts every item, so their totals can differ by population. The frozen factory contract schema is regenerated in both lanes ([#1106](https://github.com/albiexai/synth-city/issues/1106), split from [#1041](https://github.com/albiexai/synth-city/issues/1041)).
- **A superseded check run with an inconsistent status no longer blocks settlement when its re-run is green.** The check observer read every check run for the head, superseded attempts included, and applied the strict #1007 state check to each one before deciding which counted. A superseded attempt's row that GitHub left `in_progress` with conclusion `success` therefore refused the whole observation with `invalid_execution_state`, so accepted-revision settlement stayed blocked although every required check's latest run was green (seen on PR #1121, merged by hand as a recovery). The strict check now applies to the rows that count: the latest row of another application, and the current attempt's jobs that their Actions checks must equal. A superseded Actions row must still be proven to belong to an older attempt, and an inconsistent current run is still refused ([#1125](https://github.com/albiexai/synth-city/issues/1125)).
- **Overview counts a paused repository's blocked intake, as the Repositories page does.** Overview counted a paused repository (its scope switched off or its team's automation paused) only as paused and never evaluated its intake, so Automation details said "0 blocked" while the Repositories page showed every repository of a paused team as intake blocked (`team_paused`). Overview now evaluates intake for every repository, so its ready, blocked and not-known counts add up to the repositories set up and match the Repositories page. The paused and switched-on counts and the GitHub check freshness counts are unchanged. The Repositories header's "need attention" count shows the same blocked number, so it now includes paused repositories. The frozen factory contract fixture `overview.json` is regenerated in both lanes ([#1104](https://github.com/albiexai/synth-city/issues/1104), split from [#1041](https://github.com/albiexai/synth-city/issues/1041)).
- **Ready issues are dispatched in priority order even when an owner slot frees during a poll.** A poll ranks the waiting issues once and then checks each one's owner slot in turn. When the slot freed partway through, the next issue checked took it, so a newer, higher-numbered issue could start ahead of older, lower-numbered ones in the same lane (seen live: `priority:38` ahead of `priority:11`). A slot found busy for a higher-ranked issue now stays reserved for the rest of that poll, and the next poll gives it to the top-ranked issue; other slots are not held up ([#1081](https://github.com/albiexai/synth-city/issues/1081)).
- **Companion paths no longer use up a scope amendment's path limit.** The tests, fixtures, docs and generated contracts the checked-in companion map adds to a request were counted against the scope's path limit and an amendment's added-path cap, so a correction batch could be refused for path count only because of them (in v4.2 a scope's limits were raised by hand). Named paths keep the scope's limit; companions now have their own bound of 32 per request, counted only when the amendment's digest-verified trailer records them. Preflight and submission agree ([#1039](https://github.com/albiexai/synth-city/issues/1039)).
- **Automatic merge refuses a head with a failed commit status, not only failed check runs.** The merge gate waited for pending commit statuses but judged green from check runs alone, so a commit status that finished as failure or error could be merged on the ordinary path, the settled-revision promotion and the superseded-revision promotion. Every path now refuses it, a failed aggregate whose contexts are not listed fails closed, and the work item note names the failed status. Pending statuses are still waited for ([#1076](https://github.com/albiexai/synth-city/issues/1076)).
- **A verifying work item on a completed revision is no longer skipped forever after its PR merges.** When the current revision was completed but did not have the shape of a positive settlement (for example no completion time), the poll logged the item as inconsistent and skipped it on every poll, so its merged PR was never read. The poll now settles such a merge under the existing fences: at the submitted head or a head the reviewer accepted the item is merged and the revision keeps its completed history; any other head escalates for the operator. The stuck-item findings for a skipped item and for an item still verifying after its merge then clear on their own ([#1080](https://github.com/albiexai/synth-city/issues/1080), split from [#1038](https://github.com/albiexai/synth-city/issues/1038)).
- **An escalated work item whose PR merged now settles even when its revision wasn't submitted.** The escalated-merge reconciler saw only a submitted revision and hid exhausted ones, so an item escalated with an active or exhausted revision (for example after `continuation_revision_exhausted`) stayed escalated with a merged PR. It now settles under the same fences when the merged head is the submitted head or one the reviewer accepted: an active or submitted revision completes, and an exhausted revision stays exhausted. Any other merged head stays escalated for the operator, and a pending approval or handoff still blocks settlement. The settlement records the shared `merged_settlement` audit event ([#1079](https://github.com/albiexai/synth-city/issues/1079), split from [#1038](https://github.com/albiexai/synth-city/issues/1038)).
- **A dispatched work item whose PR merged while its revision was still active now settles.** The poll skipped such an item without reading its PR, so it stayed `dispatched` after the merge. The poll now reads the PR and, when it merged at a head the reviewer accepted, completes the revision and marks the item merged under the existing settlement fences. A merged head that nobody submitted or accepted still escalates for the operator. Every fenced settlement of a merged PR now records one `merged_settlement` audit event that names the state it settled from ([#1078](https://github.com/albiexai/synth-city/issues/1078), split from [#1038](https://github.com/albiexai/synth-city/issues/1038)).
- **A base update no longer counts as scope growth when a revision completes.** After GitHub's **Update branch** on a PR, `continuation_completed` returned `continuation_paths_out_of_scope` for every file the base merge brought in. Completion now counts an outside path only when the PR's own diff changes it, compared with its merge base with the current base; an outside edit in the PR is still refused, and an unreadable merge base is inconclusive. The Supervised factory playbook describes the update-branch route for attempts whose policy disables accepted base updates ([#1030](https://github.com/albiexai/synth-city/issues/1030)).
- **An accepted PR is no longer left for a human by a cause that has cleared.** A PR that fell back to human merge because of the daily cap, or because checks stalled, goes back to the automatic merge path once usage is below the cap or the same head's checks have finished green; every merge gate still applies. A check run again later on the same head no longer counts the earlier wait, so it no longer reports a false 60-minute stall ([#1037](https://github.com/albiexai/synth-city/issues/1037)).

## [4.2.0] - 2026-11-17

Albiex Synth City 4.2.0 makes the factory explain itself and gives the Leader standard ways to recover. The factory now reports stuck work items, silent background failures and automatic merges that did not run. Owners can check a request before they submit it. Work items record an effort limit, and the Leader has three standard recoveries. Claude Code members can't edit files while an unread stop or hold message waits. The backend CI suite runs as four parallel shards. This release also adds the Antigravity CLI provider, but Antigravity members can't yet be launched in a team.

**Not in this release (planned for 4.3):** live runtime checks for OpenCode 2 members; the Antigravity CLI screen fallback, wider activity classification and cross-user proof; the Mail tool-list roster badge and live notification; the access path on the remaining operator routes; CI shard rebalancing for a steady 10-minute backend check; the continuation scope check after a base update; campaign retrospectives in Insights; installation health; Agent Mail tool calls through `agy` ([#994](https://github.com/albiexai/synth-city/issues/994)); the dispatch nonce in branch names and publication URLs ([#1000](https://github.com/albiexai/synth-city/issues/1000)).

### Added

- **Antigravity CLI members (provider `antigravity-cli`, binary `agy`), not yet launchable in a team.** Synth City builds the `agy` launch and resume commands. A resume uses `agy --conversation <id> -i <prompt>` and keeps the member's bootstrap prompt. Before a team launch, Synth City adds the workspace to `trustedWorkspaces` in the agent user's Antigravity settings. Antigravity CLI appears in the provider lists, the team slot select, repository setup, Live sessions (a new session, or a resume by the exact conversation id) and the factory filters, with model and reasoning-effort options. See [Harnesses](https://albiex.org/docs/features/harnesses) ([#734](https://github.com/albiexai/synth-city/issues/734), [PR #950](https://github.com/albiexai/synth-city/pull/950), [#735](https://github.com/albiexai/synth-city/issues/735), [PR #954](https://github.com/albiexai/synth-city/pull/954), [#952](https://github.com/albiexai/synth-city/issues/952), [PR #973](https://github.com/albiexai/synth-city/pull/973), [#738](https://github.com/albiexai/synth-city/issues/738), [PR #989](https://github.com/albiexai/synth-city/pull/989), [#981](https://github.com/albiexai/synth-city/issues/981), [PR #1024](https://github.com/albiexai/synth-city/pull/1024)).
- **Agent Mail setup, readiness gates, read-only observation and a team guard for Antigravity CLI.** **Set up** and **Remove** on the Antigravity CLI harness page manage the Mail server (under the key `synth-city-mail`, so every tool name stays valid for `agy`) and three hook groups. For a team member, the hook denies `agy`'s question tool, even when Synth City can't be reached. Team readiness stays blocked for two reasons. First, Mail tool calls through `agy` aren't proven: in an isolated run, `call_mcp_tool` was not declared to the model. Second, the agent user must complete `agy`'s own onboarding; Synth City reads that signal read-only and never accepts the terms. The observer classifies only the activity shapes seen on `agy` 1.5.9 and reports everything else as unknown. An open question it observes is escalated to the Supervisor once. The team supervisor's guard fails closed on any Antigravity record shape it hasn't seen before. No team-member run and no real-model trial was done ([#736](https://github.com/albiexai/synth-city/issues/736), [PR #975](https://github.com/albiexai/synth-city/pull/975), [#991](https://github.com/albiexai/synth-city/issues/991), [PR #996](https://github.com/albiexai/synth-city/pull/996), [#992](https://github.com/albiexai/synth-city/issues/992), [PR #1001](https://github.com/albiexai/synth-city/pull/1001), [#993](https://github.com/albiexai/synth-city/issues/993), [PR #1004](https://github.com/albiexai/synth-city/pull/1004), [#995](https://github.com/albiexai/synth-city/issues/995), [PR #1005](https://github.com/albiexai/synth-city/pull/1005), [#737](https://github.com/albiexai/synth-city/issues/737), [PR #983](https://github.com/albiexai/synth-city/pull/983), [#984](https://github.com/albiexai/synth-city/issues/984), [PR #1020](https://github.com/albiexai/synth-city/pull/1020)).
- **Claude Code members can't edit while an unread stop or hold message waits.** The managed `PreToolUse` hook now asks Synth City before `Edit`, `Write`, `MultiEdit`, `NotebookEdit` and Mail tools that change state. While the member has an unread stop, hold or decision message from its Leader, its bound Supervisor or Synth City, the call is refused with a reason that names only the message id. Reading the inbox lifts the refusal, and read-only tools are never refused. If Synth City can't be reached, the call is allowed and the hook prints a warning. Installing again upgrades an older hook. Other harnesses aren't guarded ([#930](https://github.com/albiexai/synth-city/issues/930), [PR #951](https://github.com/albiexai/synth-city/pull/951)).
- **Check a request before submitting it.** The new owner-only tool `synth_city_preflight_request` checks a plan, scope-amendment or continuation request. It returns every mechanical problem at once, each with a code, a field and a message, and it writes nothing. A request that passes is not refused later for these rules, because submitting runs the same checks ([#937](https://github.com/albiexai/synth-city/issues/937), [PR #955](https://github.com/albiexai/synth-city/pull/955)).
- **A work item records its effort limit, notifies at the limit, and allows one Leader extension.** An approved plan's estimate becomes the item's effort limit. The new per-scope setting `max_item_effort_minutes` (default 120) is the ceiling; an estimate above it is refused. When the advisory elapsed working time reaches the limit, the Leader and the owner each get one notice. The limit stops nothing. With the new tool `synth_city_extend_effort_limit`, the Leader can extend the limit once ([#938](https://github.com/albiexai/synth-city/issues/938), [PR #967](https://github.com/albiexai/synth-city/pull/967)).
- **The factory reports stuck work items and silent background failures.** Four detectors open a finding while the condition lasts and close it when it clears: a merged item still verifying, a skipped item, a missing admission and a rolled-back poll. Each finding sends the Supervisor one notice. `/api/v1/health` reports `health`, `reasons` and `open_findings`, and `GET /api/v1/health/findings` lists the findings. The Overview shows a read-only "Needs a look: operations" card. See [Autonomous GitHub dispatch](https://albiex.org/docs/autonomy) ([#941](https://github.com/albiexai/synth-city/issues/941), [PR #956](https://github.com/albiexai/synth-city/pull/956)).
- **Automatic merge says why it did not run.** An accepted, ready, unheld PR that isn't merged because the daily cap was reached, or because its item was escalated, gets a reason in the work-item note. The Leader and the bound Supervisor each get one notice. The repository settings page shows "n of N in the last 24 hours" next to the per-scope cap. An item escalated only because of a completed diagnostic revision goes back to the normal automatic-merge gates. See [Autonomous GitHub dispatch](https://albiex.org/docs/autonomy) ([#939](https://github.com/albiexai/synth-city/issues/939), [PR #953](https://github.com/albiexai/synth-city/pull/953)).
- **A generated review-summary comment from recorded facts (first part of #940).** Synth City can render the Human review summary and the Work remaining block from recorded facts, and publish them as one PR comment that it owns. It never edits a PR or issue body. Publishing is off by default (`github_publish_summaries_enabled`). The Leader tools `synth_city_render_pull_summary` and `synth_city_publish_pull_summary` also work for PRs that Synth City doesn't track. Whether the GitHub App token can comment hasn't been checked live ([#940](https://github.com/albiexai/synth-city/issues/940), [PR #974](https://github.com/albiexai/synth-city/pull/974)).
- **Leader recovery.** The Leader can cancel a revision, finalize a merged item or release a clean workspace with `synth_city_leader_cancel_revision`, `synth_city_leader_finalize_merged` and `synth_city_leader_release_workspace`. Each recovery records an audit fact and sends the Supervisor one notice, and stops if a HOLD arrives while it is checking. A release records that it has started before it does anything outside Synth City. If the credential revocation may have run, a corrected request needs a new operation id ([#997](https://github.com/albiexai/synth-city/issues/997), [PR #1009](https://github.com/albiexai/synth-city/pull/1009), [#998](https://github.com/albiexai/synth-city/issues/998), [PR #1019](https://github.com/albiexai/synth-city/pull/1019)).
- **An authenticated route to the review binding, for the reviewer only.** `GET /github-work-items/{id}/review-binding` and the Mail tool `synth_city_get_review_binding` return the attempt binding a reviewer needs to record a head or revision review. Only the team's current verifier, or the reviewer named on the required-check watch for the PR's exact current head, may read it. Everyone else gets 403. The binding is private and must not be published ([#999](https://github.com/albiexai/synth-city/issues/999), [PR #1027](https://github.com/albiexai/synth-city/pull/1027)).
- **Isolated runtime kit: crash, relaunch and teardown.** `crash --role cli` stops only the recorded CLI, and `relaunch` resumes the exact saved OpenCode session after a crash. `down` now also stops processes in a kit service's own process group; any other unrecorded process still makes it refuse. A recorded kit run with OpenCode 2.2.7 and a stub model showed that the question tool is not available to the member. Teardown of a real OpenCode member is not yet covered. See the [isolated runtime kit guide](https://albiex.org/docs/guide/isolated-runtime-kit) ([#947](https://github.com/albiexai/synth-city/issues/947), [PR #961](https://github.com/albiexai/synth-city/pull/961), [#1010](https://github.com/albiexai/synth-city/issues/1010), [PR #1012](https://github.com/albiexai/synth-city/pull/1012), [#963](https://github.com/albiexai/synth-city/issues/963), [PR #1017](https://github.com/albiexai/synth-city/pull/1017)).
- **A settings change now records how it was made (#945, first part).** An audit event for a team settings change on `PATCH /api/v1/agent-teams/presets/{id}` stores `via`: `mail_tool` (a Mail session the server validated), `ui` or `api` (labels from the browser Origin header, not proof of the caller). A Mail session token that is unknown, closed or stale does not refuse the write; the event is recorded as unattributed with `session_invalid`. A campaign change on that route now records an event too, and the audit service can record an `external_actor` by actor id. The other operator routes follow in #1025 ([#945](https://github.com/albiexai/synth-city/issues/945)).
- **The team view flags members whose Mail tool list is out of date (first part of #960).** The Mail server reports a digest of the tools each session started with, and each member's status now carries `tool_catalog_state` (`current`, `stale` or `unknown`) judged over its connected sessions, with the reason "tool catalog stale: restart in resume mode" when stale. It is a display fact only and changes no wake, delivery or approval rule; restart a stale member in resume mode to load the new list. The roster badge and the `tools/list_changed` notification are separate follow-ups ([#960](https://github.com/albiexai/synth-city/issues/960)).

### Changed

- **The backend CI suite runs as four parallel shards behind the required check.** Each shard runs whole test files. The gate job keeps the required name `Full backend regression tests`, and fails unless every shard succeeded and every collected test ran exactly once. Two hosted runs took 10 min 14 s and 6 min 49 s, down from 21.6 to 28.5 minutes ([#959](https://github.com/albiexai/synth-city/issues/959), [PR #1011](https://github.com/albiexai/synth-city/pull/1011), [#1013](https://github.com/albiexai/synth-city/issues/1013), [PR #1028](https://github.com/albiexai/synth-city/pull/1028)).
- **The docs changelog page is generated from `CHANGELOG.md`.** It now lists every released version, newest first, and a check fails if the page and `CHANGELOG.md` drift apart ([#935](https://github.com/albiexai/synth-city/issues/935), [PR #949](https://github.com/albiexai/synth-city/pull/949)).

### Fixed

- **An item with a settled, accepted revision is promoted and merged automatically.** A `verifying` item whose revision was settled positively, and whose open PR head the reviewer accepted with green required checks, now goes through the normal merge gates and merge policy. When it can't be promoted, the item shows "Settled revision not promoted:" followed by the reason ([#957](https://github.com/albiexai/synth-city/issues/957), [PR #962](https://github.com/albiexai/synth-city/pull/962)).
- **A merged or closed item gets its remaining-work context after its workspace is released.** The Leader can again prepare the Remaining, Estimate and Next lines, built from the recorded verified head. The report context and the published marker carry a public `generation` reference instead of the dispatch nonce ([#976](https://github.com/albiexai/synth-city/issues/976), [PR #986](https://github.com/albiexai/synth-city/pull/986)).
- **Muse Code members are observed more reliably.** A member that runs as another OS user is now observed from its own process's data folder. An unreadable log shows as `native_log_unreadable`, with its own roster text. A member busy on a long turn now shows as working when the turn began before the last 1 MiB of its log (the scan reaches back up to 8 MiB). A read-only probe and runbook let the operator check first-session discovery for a member that runs as another user; that discovery isn't yet verified in production. See [Agent Teams](https://albiex.org/docs/features/agent-teams) ([#746](https://github.com/albiexai/synth-city/issues/746), [PR #964](https://github.com/albiexai/synth-city/pull/964), [#769](https://github.com/albiexai/synth-city/issues/769), [PR #966](https://github.com/albiexai/synth-city/pull/966), [#965](https://github.com/albiexai/synth-city/issues/965), [PR #1014](https://github.com/albiexai/synth-city/pull/1014)).
- **Settling an accepted revision no longer fails with an HTTP 500 when GitHub reports a check run inconsistently.** A run that GitHub gave as `in_progress` with a conclusion, or as `completed` with none, made the check observation refuse it (as intended, since reading it as green could pass a bad head), but the settlement route did not catch that and answered 500. It now answers 409 with `checks_not_observable:<reason>`, using only the observation's own fixed reason code, and leaves the attempt unchanged; the Leader settles again once the checks read consistently. Verification and the automatic-merge gate already kept such an item pending with a "checks are unavailable" note and charged no retry; tests now cover both shapes there too. The observation itself stays strict ([#1007](https://github.com/albiexai/synth-city/issues/1007)).
- **A refused summary render now says why.** `synth_city_render_github_summary` returned the same `invalid_github_summary` code for a mistyped expected body hash, an oversized body, bad or misplaced summary markers, a foreign section inside the block and bad summary text, so a body that rendered on a test call looked as if its content was refused. The code stays, and the answer now also carries a `reason` (`body_changed`, `body_invalid`, `rendered_body_too_large`, `summary_markers_ambiguous`, `summary_markers_invalid`, `summary_contains_foreign_section` or `invalid_summary_field`) with the names of the failing fields, never their values, and a matching suggestion. No guard was relaxed ([#987](https://github.com/albiexai/synth-city/issues/987)).
- **The factory status read counts shared Muse Code and Antigravity sessions like the team read does.** The factory snapshot looked for slots sharing a native session id among Codex and Claude Code slots only, while the team read also included Muse Code and Antigravity. For Antigravity the shared conversation was therefore not flagged in the factory view, where the team read reports it as unattributable; for Muse Code only the snapshot's inputs differed, because the Muse observer does not use the flag today. All three places now read one provider list ([#982](https://github.com/albiexai/synth-city/issues/982)).
- **The recorded estimate and effort limit are visible, and the Leader's extension stays under the scope ceiling.** The revision list, the work-item context (`synth_city_get_work_item_context`) and the request preflight now show the item's recorded estimate, approved limit, any extension, the scope ceiling and the room left for an extension. The extension is clamped so that the limit plus the extension never passes `max_item_effort_minutes` ([#968](https://github.com/albiexai/synth-city/issues/968)).
- **Auto-merge now runs when the last revision was superseded.** An item whose submitted revision a Leader had ended after a negative review stayed "dispatched" even after the reviewer accepted a later head, so the PR needed a hand merge. It is now verified at the accepted head and goes through the same gates as any other PR: the exact-head acceptance, green required checks, the hold label, the Leader approval and the merge policy. A reason is recorded when a gate stops it ([#972](https://github.com/albiexai/synth-city/issues/972)).
- **Harnesses pages show the agents' global scope and let you choose it.** On an install where the controller and the agents run as different users, Claude Code's global scope no longer reads the controller's own `~/.claude`. Set `harness_agent_user` to show the agent user's skills, agents, commands, hooks, MCP servers and memory. A **Global scope of** control switches users, and each page names the folder it reads and says when the controller cannot write there ([#969](https://github.com/albiexai/synth-city/issues/969)).

### Security

- **Unauthenticated team reads no longer return the dispatch nonce.** The work-item progress read and the preset work-item list now return `dispatch_nonce` as `null`. They identify the dispatch by `dispatch_generation`, a one-way public reference that no route accepts as authority. Routes that require authentication keep the nonce that operator actions need. The dispatch branch name and the publication URL still contain the nonce; that follow-up is planned for 4.3 ([#1000](https://github.com/albiexai/synth-city/issues/1000)) ([#988](https://github.com/albiexai/synth-city/issues/988), [PR #1002](https://github.com/albiexai/synth-city/pull/1002)).

## [4.1.0] - 2026-11-14

Albiex Synth City 4.1.0 makes the delivery factory steadier: owners can amend an approved plan, automatic merges require the reviewer's acceptance of the exact head, dispatch follows priority labels, and several stuck states now settle on their own.

**Not in this release (planned for 4.2):** live runtime acceptance for OpenCode 2 members, the Antigravity CLI harness, the Muse Code observation fixes, automatic dispatch-workspace provisioning, external actor token revocation, the backend CI speed-up and live Mail tool-list refresh.

### Added

- **OpenCode 2 members.** Albiex Synth City accepts OpenCode 2 or later only and refuses OpenCode 1 ([#730](https://github.com/albiexai/synth-city/issues/730), [PR #876](https://github.com/albiexai/synth-city/pull/876)). The OpenCode launch contract is reworked for OpenCode 2 ([#731](https://github.com/albiexai/synth-city/issues/731), [PR #879](https://github.com/albiexai/synth-city/pull/879)). The Install tab and the Harnesses page set up Agent Mail for OpenCode 2, and the install denies OpenCode's question tool ([#732](https://github.com/albiexai/synth-city/issues/732), [PR #884](https://github.com/albiexai/synth-city/pull/884)). See [Harnesses](https://albiex.org/docs/features/harnesses).
- **Claude Code model and reasoning effort per team member**, on every platform and not only on Amazon Bedrock ([#869](https://github.com/albiexai/synth-city/issues/869), [PR #889](https://github.com/albiexai/synth-city/pull/889)). See [Agent Teams](https://albiex.org/docs/features/agent-teams).
- **Dispatch priority labels** `priority:1` to `priority:9999` order ready issues, with the oldest issue first as the fallback ([#795](https://github.com/albiexai/synth-city/issues/795), [PR #888](https://github.com/albiexai/synth-city/pull/888)).
- **Required-check watch.** The assigned reviewer and the Leader are woken when a PR's required checks finish, and a team that has stopped moving is flagged ([#839](https://github.com/albiexai/synth-city/issues/839), [PR #878](https://github.com/albiexai/synth-city/pull/878)).
- **Healthy scope amendments and submitted-revision settlement.** An owner can amend an approved plan, and a submitted revision can be ended cleanly after review requires changes ([#859](https://github.com/albiexai/synth-city/issues/859), [PR #882](https://github.com/albiexai/synth-city/pull/882)). Approved plans carry companion paths: the tests, fixtures, docs and generated contracts that travel with the files they name ([#901](https://github.com/albiexai/synth-city/issues/901), [PR #907](https://github.com/albiexai/synth-city/pull/907)).
- **Auto-merge only after reviewer acceptance.** With the automatic merge policy a code PR merges only when the team's reviewer accepted its exact current head ([#904](https://github.com/albiexai/synth-city/issues/904), [PR #915](https://github.com/albiexai/synth-city/pull/915)). The review hold label still blocks automatic merge. See [Autonomous GitHub dispatch](https://albiex.org/docs/autonomy).
- **Isolated runtime kit** for provider live checks ([#902](https://github.com/albiexai/synth-city/issues/902), [PR #905](https://github.com/albiexai/synth-city/pull/905)). See the [isolated runtime kit guide](https://albiex.org/docs/guide/isolated-runtime-kit).

### Changed

- **Leader plan-approval wait.** A busy Leader's pending plan approval stays active for a bounded, activity-aware wait instead of escalating at once ([#843](https://github.com/albiexai/synth-city/issues/843), [PR #874](https://github.com/albiexai/synth-city/pull/874)).
- **Scope amendment rules.** Only approved amendments count toward the limit of three ([#906](https://github.com/albiexai/synth-city/issues/906), [PR #911](https://github.com/albiexai/synth-city/pull/911)). The approved plan's file list is read from `allowed_paths` or `paths` ([#908](https://github.com/albiexai/synth-city/issues/908), [PR #910](https://github.com/albiexai/synth-city/pull/910)). A healthy scope amendment is no longer refused for items with acknowledged plans or `origin/` base refs ([#890](https://github.com/albiexai/synth-city/issues/890), [PR #892](https://github.com/albiexai/synth-city/pull/892)). An active continuation on an escalated item can be settled or amended ([#887](https://github.com/albiexai/synth-city/issues/887), [PR #897](https://github.com/albiexai/synth-city/pull/897)).

### Fixed

- **Cross-user terminal lanes.** When agents run as another user and the attach fails, the lane shows one fixed message, and the message names the `!use_pty` rule when `sudo` cannot allocate its own pty ([#749](https://github.com/albiexai/synth-city/issues/749), [PR #877](https://github.com/albiexai/synth-city/pull/877)).

### Setup

- If the controller and the agents run as different operating-system users, the operator adds a command-scoped `!use_pty` rule to `sudoers` for the agent `tmux` command. Albiex Synth City does not change `sudoers`. See [Live sessions](https://albiex.org/docs/features/agent-bridge) and [Installation](https://albiex.org/docs/guide/installation).

### Evidence limits

- Several of these changes were checked with unit, component, mocked-render and hosted CI evidence. This entry makes no claim about a live check with a real OpenCode member or about OpenCode working and idle states; the Harnesses page lists those as not yet available.

## [4.0.0]

# Albiex Synth City v4.0.0 — formerly Claude Deck

**Albiex Synth City** is the new name of Claude Deck. Same project, same data, new name — now developed as `albiexai/synth-city` and documented at `albiex.org`. Old links redirect.

**Upgrading from v3.1:** stop the old version completely first, back up while stopped, then get v4.0.0 the normal source-based way (update source + dependencies, see the installation guide) and start it. Your database (`claude_registry.db` → `synth_city.db`), backups and attachments (`~/.claude-registry` → `~/.albiex-synth-city`), teams, settings and Agent Mail history move automatically on first start — no migration command, no manual renaming for a standard install. Read the [migration guide](https://github.com/albiexai/synth-city/blob/v4.0.0/docs/guide/migration-v4.md) before upgrading if you customised paths, environment variables, agent connections, or scripts.

**Breaking changes (complete list):**

- Old runtime names stop working; there are no aliases and no grace period.
- `CLAUDE_DECK_*` → `SYNTH_CITY_*` environment variables. Hand-set instance/supervisor variables must be renamed; startup warns (names only, never values) if old ones remain set.
- Agent Mail tools `deck_*` → `synth_city_*`; MCP server `claude-deck-mail` → `albiex-synth-city-mail`. Mail-setup connections the app can positively recognise are rewritten automatically (temporary failures retry on later starts; persistent failures are reported as incomplete, never as done); unrecognised or hand-written ones are left untouched, reported as needing attention, and must be re-added.
- API headers `X-Deck-*` / `X-Claude-Deck-Terminal-Token` → `X-Synth-City-*`; error codes `deck_unreachable` / `deck_http_error` → `synth_city_unreachable` / `synth_city_http_error`. Sessions must be relaunched and get fresh session credentials; the operator token is unchanged (re-enter it in the browser if the tab was closed) and authorisation checks are unchanged.
- Default database and data-folder paths renamed (above). Genuinely custom locations stay put; explicitly configured shipped defaults migrate automatically; overlapping custom locations may cause a safe stop instead of a move (see the migration guide).
- New queues/scopes default to labels `albiex-synth-city-ready` / `albiex-synth-city-design`; existing scopes keep working with their current labels.
- Release-download file names changed: `claude-deck-frontend-v…` → `albiex-synth-city-frontend-v…`; scripts that download releases must use the new names.
- Downgrade is restore-the-complete-pre-upgrade-set only (database set, data folder, provider configuration and browser state) — see the migration guide.

**Not changed:** your data model and history, backup file contents, the operator token configured on the server, and the internal identifiers that existing databases depend on. (Agent *session* credentials are the deliberate exception above: relaunched sessions register fresh ones.)

Full rename design and upgrade contract: [compatibility design](https://github.com/albiexai/synth-city/blob/v4.0.0/docs/plans/2026-11-06-rename-synth-city/compatibility.md). Report upgrade problems with your startup log — never post tokens or credentials.

## [3.1.0] - 2026-10-14

v3.1 makes the factory pages readable for a Director, and hardens the local API.

### Upgrade notes
- **Node.js 22.12 or later** is now required to build and test the frontend. Vite 8 and Vitest 5 need it. `scripts/install.sh` checks the version.
- **Agent Mail authentication is strict by default.** Agents need their Mail session credential, and external actors read and write only threads they created or are addressed in. When a Director key is configured, Agent Mail setup, member edits, actor creation and the UI's Mail reads need it. An install with no Director key keeps working as before and shows a recommendation to set one. An explicit opt-out restores the earlier unauthenticated reads; writes always need a key. See the Agent Mail guide.
- **Controller upgrades:** the maintenance upgrade now tolerates nullable columns that a startup migration adds, and accepts checkpoints from owners that work in a leased dispatch workspace. Its CLI prints the refusal reason code.

### Added
- **Overview:** answers what has happened, what is happening and what still needs to happen. It shows a campaign progress bar, a team summary card, the supervisor's status, unanswered release reminders, and items that wait for someone.
- **Teams:** a readable team page (header and members first, editing behind Edit team), a link from a team to its parent issue, and From Bridge and From Mail creation from the selected sessions only.
- **Audit:** a plain-language delivery summary, a readable activity feed, a "Needs a look" panel for refused agent requests, and Team, Repository and Window selects. Events unlock only when the Director chooses to.
- **Agent Mail:** "Waiting on someone" first, conversations grouped by team, a compact roster, the sender shown as Deck, Director or agent, and Mail setup on each harness page.
- **Live sessions:** a readable session list with Kill away from Watch, Team lanes that show every member four at a time, and the shared Director-key prompt for new sessions.
- **Harnesses:** breadcrumbs, a page per harness with grouped native pages, a compact list, and local projects folded into harness pages.
- **Factory signals:** one shared agent status across Agent Mail, Live sessions and Teams; an attention signal when required CI cannot be read; an optional supervisor check-in; unattended-launch results.
- **E2E journeys:** an isolated Playwright instance with deterministic seed data, and a required `e2e` CI job. Every lane has active journeys, including contrast, keyboard and a no-flicker check of the factory pages.

### Changed
- **Work and Repositories** are the single home of a work item and of repository setup. The Work list is compact and defaults to active work. Repository setup no longer asks for the key on entry or requires hand-typed paths.
- **Plain-language copy** on Director-facing pages: for example "Teams", "Director" and "updated" times.
- **Frontend dependencies:** Tailwind CSS 4, Vite 8 with plugin-react 6, lucide-react 1.x and Vitest 5.

### Fixed
- **Flicker:** the factory pages no longer show a "Refreshing…" line or flash "Previous" labels during each background refresh.
- **Delivery:** a pending check no longer uses up the merge retry budget; draft PRs are never promoted or auto-merged, and a configured review-hold label is honoured; a status report cannot move a merged item back to escalated.
- **Dispatch:** the Leader acknowledgement deadline starts when the plan request arrives; native owner contact renews in a leased dispatch workspace; workspace-release reminders are bounded; a merged item keeps no active continuation revision.
- **Factory pages** stop polling after a permanent 404 or 422, show local times in the right time zone, show a way back and plain errors on the Work item page, and keep the reader's place on Audit.
- **Accessibility:** WCAG AA contrast fixes in both themes (team cards, Autonomy, Agent Mail badges, Live sessions counts, Team lanes labels).

### Security
- **Local API hardening:** settings writes need the Director key when one is configured; Agent Mail authentication defaults as described in the upgrade notes; the Agent Mail install routes need the Director key.
- **Unattended team sessions:** interactive question prompts are blocked, and workspaces are pre-trusted for team sessions.


## [3.0.0] - 2026-10-09

### Added
- **Overview, Work and Repositories**: Show factory work counts, owners, waiting reasons, pull request context, and watched-scope intake, polling and overlap observations across teams.
- **Harnesses**: Show the versioned operations and readiness catalog for Claude Code, Codex CLI, GitHub Copilot CLI, OpenCode CLI and Pi, with guarded native pages. The catalog does not create an editor or grant authority.
- **Guided repository configuration**: `/repositories/new` keeps the draft, the saved configuration, the reviewed launch and the activation as separate steps.
- **Explicit Leader authority**: Teams sets the Leader through the team's `leader_slot_id`.
- **Audit history and metrics**: The factory audit ledger and delivery metrics record observed actions and outcomes. See `docs/guide/factory-audit-and-metrics.md`.
- **Agent Bridge**: Codex CLI sessions can launch on Amazon Bedrock with a provider-aware platform selector, a per-session `model_provider = "amazon-bedrock"` override, and optional AWS region, profile and model fields.
- **Version sync**: `scripts/bump-version.sh` also syncs `frontend/package-lock.json`, `backend/uv.lock` and the backend `app_version`.

### Changed
- **Product positioning**: Claude Deck is a self-hosted workspace to observe and intervene in bounded coding-agent attempts on GitHub issues, with mixed harnesses, visible policies and recovery controls. Native configuration and manual sessions stay available without automatic dispatch.
- **Version**: The runtime version response and the application package metadata report 3.0.0.
- **CI**: Product CI also runs for pull requests and pushes on `release/v3.0.0`.
- **Upgrade**: Existing databases upgrade in place. Back up `backend/claude_registry.db` first. Downgrade restores that backup. See `docs/guide/installation.md`.

### Fixed
- **Agent Teams**: Unsafe multi-slot Codex `resume --last` launches in the same repository are blocked. Codex hook session keys stay distinct per team slot.
- **Agent Mail**: Compose labels offline and not-wakeable recipients clearly before storing mail for later delivery.

### Evidence limits
- Finished is a tracking state. It is not independently reviewed delivery, reliability, cost or time saved.
- Cost is unknown. This release makes no performance, cost or time-saving claim.
- Native adapters stay provider-specific. Catalog support does not mean equal native capability across harnesses.
- The manual Bridge trial (V14) is not performed. Pilot participants, comparisons and human benefit measurements are not available.

## [2.0.1] - 2026-06-20

### Added
- **Claude Code Config**: Added coverage for current Claude Code settings such as advisor model, fallback model chains, Remote Control startup, push notifications, notification channel, theme, auto-compact, file checkpointing, and newer safety/privacy toggles.
- **Config editor helpers**: Added validated JSON editing for complex settings objects/arrays and a boolean map editor for plugin enablement.

### Changed
- **Dashboard**: Dashboard cards and navigation are now provider-aware so Codex selection does not show inaccessible Claude Code-only cards as if they were Codex pages.
- **Codex provider pages**: Codex MCP servers, plugins, feature flags, and configuration inventory now route to provider-appropriate pages instead of dead-end dashboard stats.
- **Claude Code settings**: Plugin marketplace settings, worktree symlink directories, sandbox Mach lookup settings, and managed marketplace policy values now use the JSON shapes documented by Claude Code.
- **Docs**: Refreshed release, Config, and Dashboard docs to describe the 2.0.1 stabilization fixes.

### Fixed
- **Usage costs**: Claude Code usage dashboards now calculate costs for current Claude model aliases such as Sonnet 5, Fable 5, Opus 4.6/4.7/4.8, Sonnet 4.6, and Haiku 4.5 instead of showing `$0.00` with non-zero token usage.
- **Usage cache**: Usage cache keys now include the pricing table version, avoiding stale zero-cost aggregates after pricing support changes.
- **Config defaults**: Corrected stale UI defaults/descriptions for auto-updates, sandbox auto-allow, thinking summaries, plan auto-mode, and turn duration.

## [2.0.0] - 2026-06-19

### Added
- **Agent Mail**: Durable local mailbox coordination for Claude Code and Codex CLI agents
  - Per-repository and Agent Team slot participants with role and charter metadata
  - Structured direct messages, context requests, handoffs, replies, read state, acknowledgements, and inbox load
  - Agent-facing MCP tools for team discovery, inbox checks, replies, context requests, and handoffs
  - One-click install and uninstall flows for Claude Code and Codex MCP/hooks configuration with best-effort backups
  - Best-effort tmux wake nudges for visible sessions through Agent Bridge
- **Agent Teams**: Saved rosters for launching or reusing local agent sessions
  - Manual team creation, team creation from Agent Mail participants, and team creation from visible Agent Bridge sessions
  - Team slots with provider, repository, display name, role, charter, bootstrap prompt, launch mode, provider options, and enabled state
  - Launch planning that checks provider availability, Agent Mail readiness, reusable tmux sessions, disabled slots, and launch option validity
  - Same-repository planner/reviewer and implementer workflows through distinct Agent Mail team-slot identities
- **External local orchestration**: Token-bound same-machine Agent Mail API for tools such as OpenClaw
  - External actor registration, member discovery, direct messages, broadcasts, context requests, handoffs, replies, request status, bounded waits, and acknowledgements
  - Agent Teams launch endpoints usable by local external automation after plan review
- **Agent Bridge session creation**: Searchable project picker and Codex model/profile selectors with custom-value fallback.

### Changed
- **Multi-agent workflow**: Agent Bridge, Agent Mail, and Agent Teams are now the supported surfaces for local session visibility, communication, and reusable rosters.
- **Agent Mail dashboard**: Connected, observed-only, not wakeable, and inbox-load states now describe delivery behavior more explicitly.
- **Agent Mail docs**: Setup documentation now explains required MCP/hooks configuration and the current tmux-only wake path.
- **Dependencies**: Merged low-risk package updates including React package alignment and small UI/runtime dependency updates.

### Removed
- **Presence**: Removed the Presence frontend route, backend route/service, hook integration, tests, and navigation. Existing inert SQLite tables may remain in local databases, but no active backend polling or UI surface uses them.

### Fixed
- **Agent Mail install feedback**: Install and uninstall actions now refresh status and show clearer results for Claude Code and Codex configuration.
- **Agent Mail roster accuracy**: Dashboard member counts now distinguish durable members from observed sessions and collapse bridge-observed/MCP sessions under the same member where appropriate.
- **Agent Mail hook output**: Claude Code and Codex hooks return provider-valid JSON so hook failures are not shown for normal mailbox checks.
- **Agent Bridge startup flow**: The development script now avoids transient frontend proxy noise by coordinating backend/frontend startup more carefully.

### Security
- **External Agent Mail boundary**: External actor creation is loopback-only, external endpoints require bearer tokens, and per-actor rate limits protect the local orchestration surface within Claude Deck's existing local trust model.

## [1.3.1] - 2026-06-11

### Fixed
- **Frontend runtime alignment**: Aligned `react` and `react-is` with the installed React DOM version to avoid production render failures from mixed React package versions.

### Changed
- **Documentation screenshots**: Refreshed README screenshots, including the Agent Bridge mixed-provider view, dashboard, config, and MCP visuals used for the release.

## [1.3.0] - 2026-06-08

### Added
- **Codex CLI support**: Provider-aware Codex support is now stable enough for everyday use
  - Agent Bridge discovers mixed Claude Code and Codex tmux sessions
  - Codex sessions can be spawned, resumed, forked, attached to, and killed from the UI
  - Provider switcher keeps Claude Code and Codex surfaces separate instead of showing unsupported pages
- **Codex Config**: Safe TOML editor for Codex settings
  - Structured General and Runtime cards for model, profile, reasoning effort, sandbox mode, approval policy, search, strict config, and alternate screen behavior
  - Dropdowns for known Codex enum values while keeping open-ended fields editable
  - Help tooltips for documented settings and feature flags
  - Feature flag inventory from `codex features list`, including editable overrides for flags such as goals, memories, hooks, multi-agent, shell tool, and network proxy
  - Profile diagnostics for active/default profile resolution, profile files, overrides, missing references, and malformed profiles
- **Codex MCP and Plugins**: Provider inventory and safe CLI-backed mutations
  - MCP inventory from `codex mcp list --json`
  - MCP add/remove through the Codex CLI with validation
  - Plugin inventory from `codex plugin list`
  - Plugin install/remove where the installed Codex CLI exposes safe commands
- **Codex Backup Export**: Redacted export-only backups for Codex config, profile files, rules, and provider inventory metadata
- **Projects**: Project discovery is easier, with directory browsing support when adding project paths

### Changed
- **Provider model**: Provider status, capabilities, diagnostics, and normalized errors now drive the UI for Claude Code and Codex CLI.
- **Documentation**: README and VitePress docs now describe the stable Codex support surface, the remaining provider boundaries, and the release-ready dependency updates.
- **Frontend toolchain**: Updated TypeScript to 6.0.3, `@vitejs/plugin-react` to 5.1.4, ESLint tooling, React DOM, PostCSS, Tailwind Merge, and Node types.

### Security
- **Codex privacy boundary**: Codex auth, history, model cache, SQLite state, prompt text, and raw cache payloads remain excluded from raw viewers and backups.
- **Codex restore policy**: Automatic Codex restore is refused because exports intentionally omit provider-owned local state.

## [1.2.0] - 2026-04-22

### Added
- **CC Bridge**: Live terminal bridge to Claude Code sessions running in tmux
  - Multi-terminal grid layout supporting up to 4 simultaneous panes (auto-layout: 1, 2-column, or 2x2 grid)
  - Per-pane read-only/interactive mode toggle, fullscreen, attach/detach, and close controls
  - Active terminal focus indicator — green glow on the focused pane
  - Session discovery via `tmux list-panes` with auto-refresh polling
  - Spawn new Claude Code sessions (plain, worktree, or resume mode) from the UI
  - Kill sessions with optional worktree cleanup
  - WebSocket-based PTY relay with xterm.js (WebGL rendering, web links)
- **Projects**: Discover projects from `~/.claude/projects/` session history
- **Dashboard**: Cache stats in context to avoid re-fetching on navigation
- **Documentation**: VitePress documentation site with guide, features, and API reference

### Fixed
- **CC Bridge**: Prevent orphaned `tmux attach-session` processes from accumulating on server reload/crash via `PR_SET_PDEATHSIG` and startup cleanup
- **CC Bridge**: Fix terminal not rendering in React StrictMode due to race condition in async attach flow

## [1.1.0] - 2026-03-03

### Added
- **CC Bridge**: Live terminal bridge to Claude Code sessions running in tmux
  - Multi-terminal grid layout supporting up to 4 simultaneous panes (auto-layout: 1, 2-column, or 2x2 grid)
  - Per-pane read-only/interactive mode toggle, fullscreen, attach/detach, and close controls
  - Active terminal focus indicator — green glow on the focused pane
  - Session discovery via `tmux list-panes` with auto-refresh polling
  - Spawn new Claude Code sessions (plain, worktree, or resume mode) from the UI
  - Kill sessions with optional worktree cleanup
  - WebSocket-based PTY relay with xterm.js (WebGL rendering, web links)
- **Projects**: Discover projects from `~/.claude/projects/` session history
- **Dashboard**: Cache stats in context to avoid re-fetching on navigation
- **Documentation**: VitePress documentation site with guide, features, and API reference

### Fixed
- **CC Bridge**: Prevent orphaned `tmux attach-session` processes from accumulating on server reload/crash via `PR_SET_PDEATHSIG` and startup cleanup
- **CC Bridge**: Fix terminal not rendering in React StrictMode due to race condition in async attach flow

## [1.0.0] - 2026-01-22

### Added
- Initial release of Claude Deck
- **Dashboard**: Overview of Claude Code configuration status and usage statistics
- **MCP Server Management**: Add, edit, remove, and configure MCP servers (global and project-scoped)
- **Commands Management**: Create and manage custom slash commands with argument support
- **Plugins Management**: Install, configure, and manage Claude Code plugins
- **Hooks Management**: Configure pre/post hooks for various Claude Code events
- **Permissions Management**: Manage allowed and denied permissions for tools
- **Backup & Restore**: Full backup and restore functionality for all configurations
- **Project Management**: Support for project-specific configurations
- **CLI Executor**: Execute Claude CLI commands from the web interface
- **Usage Tracking**: Track and visualize API usage and costs

### Technical
- FastAPI backend with async SQLAlchemy and SQLite
- React 18 frontend with TypeScript, Vite, and shadcn/ui
- RESTful API at `/api/v1/`
- CORS configured for local development

[Unreleased]: https://github.com/adrirubio/claude-deck/compare/v2.0.1...HEAD
[2.0.1]: https://github.com/adrirubio/claude-deck/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/adrirubio/claude-deck/compare/v1.3.1...v2.0.0
[1.3.1]: https://github.com/adrirubio/claude-deck/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/adrirubio/claude-deck/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/adrirubio/claude-deck/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/adrirubio/claude-deck/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/adrirubio/claude-deck/releases/tag/v1.0.0
