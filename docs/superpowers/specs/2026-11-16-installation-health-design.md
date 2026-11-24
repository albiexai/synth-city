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

## Additions from the operator guide (Director, 2026-11-24)

Proposed by the Supervisor in Guide 3's open questions (`2026-11-16-installation-guides/`), accepted by the Director for 4.4 or 4.5. Tracked on #979.

### A. Settings in force (read-only)

The Installation page has a **Settings** section that shows every server setting that is in force:
- its **value and source**: the code default, `backend/.env`, or the process environment;
- **secrets as "set" or "not set"** only (`OPERATOR_TOKEN`, `GITHUB_TOKEN`, the App key path's readability), never their values or fingerprints in the UI;
- a **restart needed** marker when `backend/.env` differs from what the running process loaded.

It is never editable from the web app: the page shows the command to copy instead (the "boundary-changing" fix kind above). A smaller read-only version may ship earlier as a 4.4 gap-filler (#1280); this page absorbs it.

### B. Autonomy readiness: blockers and warnings

`GET …/github-scopes/{id}/activation-readiness` and the check engine share one set of checks, so the readiness view and `synth-city doctor` never disagree. Two levels:

| Level | Checks | Override |
|---|---|---|
| **Blocker**: dispatch would be unsafe or fail without any message | the operator token is configured; `GITHUB_TOKEN` (or the App) can read the repository; the base branch exists **and** a CI workflow runs on PRs into it; at least one dispatch workspace is registered and usable; the ready label exists on GitHub; every enabled builder's area labels match at least one label in use (or none are set); with `merge_policy: auto`, at least one enabled verifier and a non-empty `required_checks` | none |
| **Warning**: a judgement call | no supervisor bound for a team with autonomy; no backup newer than the threshold; `merge_policy: auto` or a daily cap above 10 on a team's first campaign; the agents' GitHub token has Admin or Owner rights | the Director acknowledges once; the acknowledgement is recorded in the audit log with the check's evidence |

Evidence for each blocker: CI not triggering on a new release branch (#499 in 3.0, #556 in 3.1); items silently `pending` without workspaces; `no_eligible_owner` from unmatched lane labels; operator protection off when no token is configured.

### C. Team kits (project 3): roster presets

Guided setup offers three starting rosters as the first team kits: **Minimal** (Leader, one builder, one verifier: the smallest roster that can auto-merge), **Small** (Leader, two builders, one verifier), **Large** (the 4.4 line-up: four builders, two verifiers). Each carries the 4.4 charter templates (#1113), lane labels and cautious scope defaults (`merge_policy: human`, a low daily cap). Presets fix **roles and harnesses, not models**: guided setup asks which harnesses and subscriptions the operator has, suggests models, and warns about quota for the large roster.

### D. Team-level overrides of timing settings (4.4, #1115)

Rule: settings about the **machine** stay per installation; settings about the **repository** stay per scope; settings about **how a team works** become an installation default with an optional team override, as the Leader ack windows already are (a `null` override restores the default). First candidates: owner idle and nudge timers, the Mail unanswered time, team idle, wake follow-up, publishing summaries. Move only those with an observed need; the 4.4 retrospectives show which ones cost time.

## Decisions for the Dedicated installer and upgrades (Director, 2026-11-24)

From Guide 2's open questions (`2026-11-16-installation-guides/`), proposed by the Supervisor and accepted by the Director. They shape project 2 (Dedicated installer) and the upgrade path. Tracked on #979.

### E. Installer shape: a Python command

`synth-city install --dedicated`, in the product, sharing the **layout file** and the **check engine** with `synth-city doctor`: installing is "apply the fixes until the doctor passes".
- It shows a plan first and runs only with `--execute`. Running it again is safe.
- Each root step (users, group, units, sudoers) runs through `sudo` on its own, with the exact command printed.
- It is tested in CI on a hosted runner with real `sudo`, as `Hosted real-sudo attach` already is.
- **Not** a shell script: the checked-in `productctl` and units drifted from the live factory. **Not** a `.deb` in the first version: packaging, an apt repository and signing cost more than they give while the installer is changing, and a package cannot do the harness logins anyway.

### F. Source: a built release archive

For Dedicated installs, `release.yml` attaches a **release archive** to each GitHub release. The archive holds the source, the built frontend and the locked requirements, with a published checksum. The host needs no Node for Synth City itself. `git clone` of a tag stays the way for Quick start and development.
Each release is unpacked into its own folder; a `current` symlink selects the one in use (see L).

### G. Names, paths and the layout file

| Item | Default |
|---|---|
| Controller user, agent user, shared group | `synthctl`, `synthagent`, `synth-work` |
| Program | `/opt/synth-city/releases/<version>/`, `current` → the running release |
| Layout file | `/etc/synth-city/layout.toml` (root-owned, world-readable, no secrets) |
| Server settings | `/etc/synth-city/env` (mode 600, owned by the controller user) |
| State | `/var/lib/synth-city/` (database, backups, maintenance records) |
| Work folders | `/home/synthctl/work/<campaign>/` (configurable) |

The units are **generated** from the layout file at install time; the doctor and `productctl` read the layout file at run time. An existing installation **adopts** a layout file that describes its current names, so the reference factory (`deckproductctl`, `deckproduct`, `/opt/claude-deck-product`) needs no renaming.

### H. The `agent-config` helper and its sudoers rule: confirmed

One root-owned helper at a fixed path, run by the controller **as the agent user** through one command-scoped `sudoers` rule (`synthctl ALL=(synthagent) NOPASSWD: /opt/synth-city/libexec/agent-config`).
- It takes **one typed request on standard input**, never file paths or values in its arguments.
- It writes only an **allowlist**: Claude Code folder trust (`~/.claude.json` projects), Claude Code MCP servers and hooks (`~/.claude/settings.json`), Codex folder trust and MCP servers (`~/.codex/config.toml`), Codex hooks (`~/.codex/hooks.json`). It edits in place, keeps every other entry, and writes a backup and an audit line for each change.
- It refuses symbolic links, never runs a shell, and never reads or prints credentials.
- A **minimal first version** (folder trust only) ships in **4.4** with #1262; 4.5 extends it to the rest of the allowlist (Agent Mail repair, team kits).

Rejected: group-write on the agent's configuration (the harnesses rewrite those files and their permissions, and it widens the controller's reach), and a root helper (it widens the boundary).

### I. Harness logins: a guided, verified step

The installer cannot log in for the operator. It shows the exact commands to run **as the agent user** (`sudo -iu synthagent claude`, `sudo -iu synthagent codex login`). Both work over SSH with a browser link or a device code. Then it **verifies** with one real model call per harness as the agent user. The doctor records the Codex login expiry and warns before it. The installer never copies a person's credentials into the agent's home.

### J. Remote access: SSH tunnel by default; Tailscale documented; no public proxy yet

Synth City's trust model is local: the operator token protects changes, but **reads are not authenticated** (source, Agent Mail, live sessions, which can attach to agent terminals). The supported access is an **SSH tunnel** to `127.0.0.1`; **`tailscale serve`** is documented as the second option. A public reverse proxy is not supported until Synth City has an authentication model for remote operators (the deferred external-operator design). The doctor warns when the backend listens on anything other than loopback.

### K. Several teams: one agent user first, one per team later

A harness login belongs to an **OS user**. Teams that use **different subscriptions**, or have different owners or trust levels, need different agent users. The first version supports **one shared agent user**. The layout file maps **team → agent user** from the start, so per-team agent users can follow without a migration. Teams with one owner (for example Synth City and Tizonia) share the agent user.

### L. Upgrades: part of the installer project

`productctl upgrade <version>` builds on the existing `scripts/factory-maintenance.py upgrade` operation, not a new design:
1. preflight: autonomy off, zero dispatched items, no active owner checkpoints (#654);
2. back up the database to `/var/lib/synth-city/backups/`;
3. unpack and verify the release archive; run the migrations;
4. switch `current`; restart the units;
5. re-apply the managed harness configuration (Agent Mail hooks) through `agent-config`;
6. run the doctor; report.

Rollback: switch `current` back and restore the backup. The private v4.3 upgrade script is the checklist to cover.

### M. Supported systems

Ubuntu **24.04 LTS** and **26.04 LTS**, including on **WSL 2**, tested in CI on the versions GitHub's hosted runners provide. On WSL 2 the doctor checks that systemd is enabled (`/etc/wsl.conf`) and warns that a WSL shutdown stops the factory. Ubuntu 22.04 is supported for Quick start only, with the documented Python 3.11 workaround.

## Left to the builder

- The registry's module layout and the doctor command's packaging (backend console script vs. `productctl doctor`).
- Thresholds (disk, backup age, check-in grace), with defaults.
- How checks that need another user's view run under the controller's capabilities, within the anti-goals above.
