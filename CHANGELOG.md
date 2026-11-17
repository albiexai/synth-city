# Changelog

All notable changes to Albiex Synth City will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

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
