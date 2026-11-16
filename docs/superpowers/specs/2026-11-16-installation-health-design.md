# Installation health — design

Date: 2026-11-16. Status: draft, accepted by the Director for now (2026-11-16), to be refined later. Not approved for building. Project 1 of 4 in the installation programme.

## The programme (context)

Synth City has two installation modes (Director decision, 2026-11-16):

- **Quick start**: one OS user; Synth City, the harnesses and the agents share that user's configuration. For trying it and
  for supervised use. This is today's `scripts/install.sh`.
- **Dedicated**: separate controller and agent users, with system services. For unattended teams. This is how the
  local factory runs (`/opt/claude-deck-product`, controller user `deckproductctl`, agent user `deckproduct`), but it was
  assembled by hand and its knowledge is not in the product.

Four projects, in this order (Director decision): **1. installation health** (this document), **3. team kits**
(layered: installation base → team → role/slot; plus whatever the repositories carry), **2. Dedicated installer**,
**4. Quick start → Dedicated migration**. Documentation runs through all four.

## Intent

A Director, or a supervising agent, can see at any time whether the installation is correct, which requirement is
wrong, and exactly how to fix it, without knowing the layout's history. The same checks verify the installer's
result (project 2) and the team kits (project 3).

Evidence for the need: in one day the local factory broke on (a) Claude Code members blocked because a new Agent
Mail hook (`PreToolUse`, from #930) was not installed after an upgrade, (b) the harness pages showing the controller
user's home as the global scope (#969), and earlier on trust, `safe.directory`, `sudoers` and capability issues that
were only known from operator notes.

## Goals

1. One **check engine**: a registry of checks, each with an id, area, severity (`required`, `recommended`), the modes it
   applies to (`quick_start`, `dedicated`), a state (`pass`, `warn`, `fail`, `unknown`), evidence, and a fix.
2. Two front ends over the same engine:
   - **`synth-city doctor`** command: works when the web app is down; exit code non-zero when a required check fails;
     `--json` output; run by the installer and by `productctl` before and after restarts and upgrades.
   - **Installation** page: its own sidebar entry; checks grouped by area; each shows the evidence and the fix.
3. **`/health`** reports `degraded` with a reason code when a required check fails (alongside the #941 findings).
4. **Fixes**: one-click for actions Synth City already owns (with preview, confirm and audit); a copyable `sudo`
   command for anything that changes the machine's security boundary. The page re-checks after a fix.
5. Agents (for example a supervising agent) can read the results; with the "apply process changes" permission they
   can run only the Synth-City-owned fixes.

## Non-goals

- Creating users, groups, units, `sudoers` rules or capabilities (that is the installer, project 2).
- Managing team kit content (project 3); the engine only gains kit checks then.
- Any privileged helper. The web app never gains a route to root.

## Anti-goals

- A check that reports `pass` when it could not read its evidence (unreadable means `unknown`, never `pass`).
- Printing secrets in evidence: evidence shows presence, mode, owner and fingerprints, never values.
- A one-click fix that changes the machine's security boundary.

## The installation layout is configuration, not code

The checks read the layout from one configuration source (mode; controller and agent users; shared group; install
root; state and workspace roots; tmux socket; service unit names), with the Quick start defaults derived from the
current user. Reason: the repository's `scripts/product-factory/productctl` hard-codes `/opt/albiex-synth-city-product`,
while the live factory is `/opt/claude-deck-product`; scripts and the real installation have already drifted.

## Check catalogue (first version)

| Area | Checks |
|---|---|
| **Layout** | mode detected; controller and agent users and shared group exist (dedicated); workspace roots exist with the expected owner, group and setgid; Python, Node, git and tmux versions meet the minimums |
| **Permissions** | the command-scoped `!use_pty` `sudoers` rule for the agent tmux wrapper exists; the command-scoped rule for the `agent-config` helper exists (see Decisions); `sudo -l` allows the controller only those two; the controller can reach the agent tmux socket; service capabilities (`cap_sys_ptrace`, `cap_dac_read_search`) and `kernel.yama.ptrace_scope` permit cross-user reads |
| **Services** | units active and enabled (controller, tmux, supervisor timer); database writable; free disk space above a threshold; a backup newer than a threshold |
| **Secrets** | the operator token is in the backend `.env` with mode 600 and the right owner; it is **not** present in the tmux server's global environment |
| **Git and GitHub** | `safe.directory` covers each workspace for both users; GitHub authentication works for the configured mode; each scope's repository is fetchable; each scope's base branch exists; the CI workflows trigger on that base branch |
| **Each installed harness** | binary present and version supported; login valid (including the Codex auth expiry); Agent Mail MCP installed and **all** its hooks current; workspace trust for each team workspace (Claude Code trusts the primary checkout); per-slot model configuration resolvable; interactive question tools blocked for unattended members; Antigravity onboarding completed by the user (never accepted by Synth City) |
| **Teams** | each slot's workspace exists and is trusted; members registered in Mail; each scope's activation readiness; a supervisor bound and checked in within its grace period |

Severity: everything a team launch would fail on is `required`; everything that degrades supervision or recovery
(backup age, supervisor check-in, CI triggers) is `recommended`.

## Fixes

| Kind | Examples | How |
|---|---|---|
| Synth-City-owned (one click) | install or repair Agent Mail for a harness in the **agent user's** home (`/agent-mail/install/<harness>/apply`, writing through `agent-config`); re-register a member; (project 3) re-sync a team kit | preview of the change → confirm → existing route → audit event → re-check |
| Boundary-changing (copy a command) | `sudoers` rule; capabilities; unit files; users and groups; `safe.directory` for the other user; harness login | the exact command with the configured names filled in; the page re-checks |

## Reuse

- Per-harness readiness: `provider_readiness_service.agent_mail_ready_reason` and the Agent Mail install status
  (`/agent-mail/install/status`) become checks rather than being re-implemented.
- Scope readiness: `GET /agent-teams/github-scopes/{id}/activation-readiness`.
- `/health` and `operations_health_service` (#941) gain the degraded reason for failed required checks.
- The UI uses the existing design tokens and `CLICKABLE_CARD`; no new chart or table library.

## Decisions after the builder check

**Director decision (2026-11-16): writes into the agent user's configuration go through a scoped helper.**
- In Dedicated mode the checks read the **agent user's** harness configuration (Claude Code, Codex, Muse, OpenCode,
  Antigravity homes of the agent user), never the controller's. Today `agent_mail_install_service` reads and writes the
  controller's home (`Path.home()`), so its status is wrong in exactly the case that matters.
- A second command-scoped `sudoers` rule, in the same model as the existing `agent-tmux` rule, lets the controller run
  one Synth City helper, **`agent-config`, as the agent user (never as root)**. The helper accepts only
  Synth-City-managed writes into managed areas: Agent Mail install or repair now; team-kit provisioning in project 3.
  It refuses arbitrary paths, symlinks and anything outside its allow-list, and every call is audited.
- Until the installer (project 2) creates the rule, the page shows the rule as a copyable command (a Permissions
  check) and the one-click fixes that need it are disabled with that reason.
- The agent-home resolution is shared with #969 (harness pages show the agent user's global scope); whichever lands
  first provides it.

**Author's calls (open to the Director's correction):**
1. **Layout source**: extend the existing `InstallationProfile` (`backend/app/services/maintenance_operations.py`), which
   already models the controller, database, `.env`, units, state directory and OS users. It must be readable by the web
   app, by `doctor` and by `productctl`. Acceptance is against the live factory's current naming
   (`/opt/claude-deck-product`); the rename to the post-cutover naming belongs to the installer (project 2).
   `productctl` changes only as far as running `doctor`.
2. **`/health`**: checks run periodically (default every 10 minutes) and on demand, and cache results with a timestamp.
   A result older than its freshness limit is `unknown`, never `pass`. A failed `required` check opens a #941
   `OperationsFinding` (open/resolve lifecycle, one notice to the bound Supervisor) and clears when the check passes.
3. **In use**: Teams checks cover presets with autonomy on or live member sessions; harness checks cover harnesses used
   by an enabled slot or listed in the profile. Idle and historical teams do not degrade `/health`.
4. **Agent access (first version)**: a bound Supervisor (external actor) reads redacted results through a Mail tool.
   Agent-run fixes wait for the permission model of the retrospectives design (v4.3), which is not built yet.

## Left to the builder

- The registry's module layout and the doctor command's packaging (backend console script vs. `productctl doctor`).
- Thresholds (disk, backup age, check-in grace), with defaults.
- How checks that need another user's view run under the controller's capabilities, within the anti-goals above.
