# Installation, 4.5 slice: "install it properly" — design

Date: 2026-11-24. Status: **draft for the Director's review**. Not approved for building.
Programme: the installation programme (#979). Earlier documents: `2026-11-16-installation-health-design.md` (project 1 design and the decisions A–Q of 24 Nov) and the three guides in `2026-11-16-installation-guides/`. **Where this document and the earlier ones differ, this document wins.**

## 1. Intent

Someone who is not the author of Synth City (the first one is Pablo, on Ubuntu, with Claude Code and Codex) can **install Synth City properly, keep it healthy and upgrade it**, in either mode, from the documentation and one command. Today a single-user install is one script with few checks, and a dedicated factory takes about twelve manual steps known only from the reference factory's history.

**Success, measured at the 4.5 release:**
1. **Quick start:** on a clean Ubuntu 24.04 machine, `install.sh` then `start.sh` gives a working Synth City with Claude Code and Codex verified, in under 15 minutes of the user's time, and `synth-city doctor` reports no failed required check.
2. **Dedicated:** on a clean Ubuntu 24.04 machine (and on WSL 2), `synth-city install --dedicated --execute`, the two guided harness logins and the docs give a factory whose doctor reports no failed required check, and a three-member team (Leader, builder, verifier) takes one issue from ready label to merge. **Pablo does this on his own computer** before the release.
3. **The reference factory adopts it:** the Synth City factory gets a layout file describing its current names, its doctor passes, and it is upgraded to 4.5.0 with `synth-city upgrade` (not the private script). How far its folders and units are converted is decided in section 9.
4. **Upgrade and rollback** work on a disposable dedicated install in CI: **4.5.0-rc.1 → 4.5.0-rc.2 → rollback → rc.2**, and **adoption** works from a 4.4.0 git-checkout install (adopt, then upgrade to the 4.5 candidate). *(Author's call: the `releases/` + `current` structure and the archive exist only from 4.5, so a 4.4.0 → 4.5.0 archive upgrade can't be tested; adoption covers the 4.4 starting point.)*

**Non-goals (4.6 or later):** team kits beyond three roster presets; the Quick start → Dedicated migration; one agent user per team (the layout allows it, nothing uses it); a `.deb` package; a public reverse proxy; macOS and native Windows dedicated installs.

**Anti-goals:**
- A check that says `pass` when it could not read its evidence (unknown is never pass).
- A secret in any output: evidence shows presence, owner and mode, never values.
- The web app gaining any route to root, or any one-click change to the security boundary.
- An installer that leaves a half-installed machine without saying exactly what was done and what remains.
- A second copy of the layout's facts (paths, names) in scripts, units or docs that can drift again.

## 2. Scope: four workstreams

```text
   W1 Health ───────────────┐
   (layout, checks, doctor, │  the shared base
    page, readiness)        │
                            ├──► W2 Dedicated installer + W3 upgrades
                            │
                            └──► W4 Quick start
   Docs run through all four.
```

| Workstream | Delivers | From decisions |
|---|---|---|
| **W1 Health** | The layout file; the check engine; `synth-city doctor`; `/health` degraded; the Installation page with settings in force; readiness blockers and warnings | Project 1 design; A, B, G |
| **W2 Dedicated installer** | Release archive; `synth-city install --dedicated`; the `agent-config` helper (full allowlist); guided and verified harness logins; roster presets | E, F, G, H, I, K, M, C |
| **W3 Upgrades** | `synth-city upgrade <version>` and `synth-city rollback`; adoption of an existing installation | L, G |
| **W4 Quick start** | `start.sh`; harness check and offer in `install.sh`; optional user service; the doctor at the end | N, O, P, Q |
| **Docs** | Installation guide (both modes), operator guide, remote access, upgrade guide | the three guides; J |

## 3. W1 Health

### 3.1 The layout file

The layout is **configuration, not code**. One file, `/etc/synth-city/layout.toml` (Dedicated; root-owned, world-readable, **no secrets**) or `~/.config/albiex-synth-city/layout.toml` (Quick start, generated with defaults from the current user).

**It is a new model, `InstallationLayout`, not an extension of `InstallationProfile`.** *(Author's call, reversed after the builder check.)* `InstallationProfile` (`maintenance_operations.py:69`) is strict, requires campaign-maintenance fields that a fresh or Quick start install doesn't have (`supervisor_unit`, `supervisor_state`, `arming_file`, `hold_files`, `protected_files`), and is loaded only from a **private** JSON file (`read_json(private=True)`). That privacy check stays. The maintenance operations keep their profile; a profile may name the layout file and take its paths from it, so the two don't drift.

Fields (names are the builder's call): `mode` (`quick_start` / `dedicated`); controller user, agent user, shared group; install root and releases folder; state folder; **settings file**; tmux socket; unit names; work root; `teams_agent_user` (a map team → agent user, with one default); thresholds; WSL flag (detected, stored for the doctor). Readers: the backend, the doctor, the installer, the upgrade. Units are **generated** from it at install time and never read it at run time.

**Settings file.** Today the backend reads `.env` only from its working directory (`backend/app/config.py:10`). H1 adds a configured settings path: the backend reads the file named by `SYNTH_CITY_SETTINGS_FILE` (set in the unit, or by the layout), and falls back to `./.env`. Sources stay distinguishable (default, settings file, environment), so decision A's "source" and "restart needed" keep working. The unit must **not** use `EnvironmentFile=` for these settings (that would make every setting look like the environment).

### 3.2 Checks, doctor, page, `/health`

As the project 1 design (check catalogue, fixes, reuse, the decisions after its builder check), plus:
- **Settings in force** (A): the Installation page shows each server setting's value and source (default, settings file, environment), secrets as set/not set, and a restart-needed marker. The 4.4 gap-filler #1280, if it shipped, becomes this section.
- **Readiness** (B): `activation-readiness` and the doctor share the checks. **Blockers** (no override): operator token set; GitHub can read the repository; the base branch exists and a CI workflow runs on PRs into it; a usable dispatch workspace; the ready label exists; builders' lane labels match; for auto merge, an enabled verifier and non-empty required checks. **Warnings** (acknowledged once by the Director, recorded with evidence in the audit log): no supervisor bound; no recent backup; auto merge or a cap above 10 on a team's first campaign; an Admin or Owner token for the agents.
- **New checks from the decisions:** the backend listens only on loopback (J); on WSL 2, systemd is enabled, with a warning that a WSL shutdown stops the factory (M); the Codex login expiry (I); the user service, when installed (P).

## 4. W2 Dedicated installer

### 4.1 Release archive (F)

`release.yml` already publishes the **frontend only** (`albiex-synth-city-frontend-v<version>.zip` and `.tar.gz`, with `SHA256SUMS.txt`; `release.yml:100-105`). I1 **adds** `albiex-synth-city-v<version>.tar.gz`: the source tree at the tag, the built frontend (`frontend/dist`), `backend/requirements.txt` (locked), and no `node_modules`, listed in the same `SHA256SUMS.txt`. The frontend-only assets stay for one release, then are dropped (noted in the changelog). The installer and the upgrade verify the checksum.

### 4.2 `synth-city install --dedicated` (E)

A Python command in the product (backend console entry point or `scripts/synth-city`; builder's call), run by an administrator with `sudo` rights.
- **Plan, then execute:** without `--execute` it prints the plan (each step, its commands, what exists already). With `--execute` it runs the steps in order, each root step through `sudo` on its own with the command printed. Running it again is safe: a step that is already done is reported and skipped.
- **Steps:**
  1. check the system (Ubuntu version, WSL, systemd, Python, git, tmux, free disk);
  2. create the users and group, and the folders with owner, group, mode and setgid;
  3. unpack the release archive into `releases/<version>`, create the Python environment, point `current` at it;
  4. write `layout.toml`; write the settings file with a fresh operator token (mode 600, controller-owned; the token is never printed);
  5. install `libexec/agent-tmux`, `libexec/agent-config` and `bin/tmux`; write the two command-scoped `sudoers` rules through `visudo -c` validation;
  6. generate and enable the two units (tmux server as the agent user; controller with `CAP_SYS_PTRACE`, `CAP_DAC_READ_SEARCH`, `UMask=0007`);
  7. install the harness binaries into the tools folder **only after confirmation** (as O), or use existing ones;
  8. the guided logins as the agent user, then one real model call per harness (I);
  9. install Agent Mail into the agent user's harnesses through `agent-config`;
  10. run the doctor and print the result and the remaining human steps (for example, the GitHub account, section 4.5).
- **Failure:** the installer stops at the first failed step, prints what was done, what failed and how to continue, and is safe to run again. No rollback of users or groups (those are harmless and reusable).
- **Tested in CI** on a hosted Ubuntu runner with real `sudo`, as `Hosted real-sudo attach` is: a full install, a second run (idempotent), the doctor, and a tiny team launch with a stub harness.

### 4.3 The `agent-config` helper (H)

4.4 ships the trust-only version with #1262. 4.5 extends its allowlist to Claude Code MCP servers and hooks, Codex MCP servers and hooks, and the team-kit files the roster presets need; Agent Mail install and repair use it. The rules stay: run as the agent user only; one typed request on standard input; allowlisted entries only; keep every other entry; refuse links; atomic writes with a backup; one-line result; audit; never a shell, never a credential.

### 4.4 Roster presets (C)

Guided setup offers **Minimal** (Leader, builder, verifier), **Small** (Leader, two builders, verifier) and **Large** (the 4.4 line-up). Each preset sets roles, harness choices, lane labels, the 4.4 charter templates (#1113) and cautious scope defaults (`merge_policy: human`, a daily cap of 5). It asks which harnesses the operator has and suggests models; the Large preset warns about quota. Presets are data files in the repository, so they are reviewed like code. Nothing else of team kits is in 4.5.

### 4.5 GitHub identity

The installer asks which GitHub account the factory uses and documents the recommendation (Guide 3, section 4b): a **machine user** with a fine-grained token on the factory's repositories, or a GitHub App. It does not create accounts or tokens. It logs the agent user's `gh` in with the token the operator pastes (read from standard input, never an argument) and stores `GITHUB_TOKEN` in the settings file. The doctor warns about an Admin or Owner token (B).

### 4.6 Several teams (K)

One shared agent user. `teams_agent_user` exists in the layout with a single default; the installer writes it; nothing else uses it in 4.5.

## 5. W3 Upgrades (L)

**A new operation in the same command, `synth-city upgrade <version>` and `synth-city rollback`.** *(Author's call, after the builder check.)* It is **not** `scripts/factory-maintenance.py upgrade`: that operation deploys **reviewed commits mid-campaign** (expected and target heads, a candidate git checkout, an `ACCEPT` review file, a runtime proof, a checkpoint message per active owner; `maintenance_operations.py:792-916`) and requires an **active supervisor unit** with a fresh tick (`safety()`, lines 264-272), which neither a fresh install nor the reference factory runs. It stays as it is, for mid-campaign fixes. The new operation **reuses its parts**: the hold check, the database backup, stopping and starting the controller, the database checks. The checked-in `scripts/product-factory/productctl` is campaign-specific (preset 1, one repository, hard-coded paths) and is **not** extended; its useful commands move into `synth-city` over time.

Steps:
1. **preflight**: autonomy off on every team, **zero dispatched or verifying items**, no hold file. *(A release upgrade waits for a quiet factory; it does not checkpoint running owners. The mid-campaign operation remains for that.)*
2. database backup to the state folder;
3. download the archive and verify its checksum;
4. unpack into `releases/<version>`; create its Python environment; run the migrations;
5. switch `current`; restart the units;
6. re-apply the managed harness configuration (Agent Mail MCP and hooks) through `agent-config`;
7. run the doctor and print the result.

**Rollback**: `synth-city rollback` switches `current` back and restores the backup taken by that upgrade. It refuses when the database has changed since the upgrade (new work items or Mail), says so, and stops.

**Adoption:** `synth-city install --adopt` writes a `layout.toml` describing an existing installation (for the reference factory: `deckproductctl`, `deckproduct`, `deckproduct-work`, `/opt/claude-deck-product`, its current units and drop-ins, `controller/backend/.env` as the settings file), checks it with the doctor, and changes nothing else. What an adopted installation's first upgrade converts is decided in section 9.

## 6. W4 Quick start

- **`start.sh` (N):** builds the frontend once if `frontend/dist` is missing or older than the source (or uses the archive's), then runs the backend serving the UI on one port. `dev.sh` stays.
- **`install.sh` (O):** adds checks for tmux, git, `gh` and each supported harness; offers the official install command for a missing harness and runs it only after confirmation; guides the login and verifies it with one real model call; writes the Quick start `layout.toml`; ends with the doctor.
- **`--service` (P):** installs `systemd --user` units for `start.sh`, explains and prints `loginctl enable-linger <user>`, off by default.
- **Versions (Q):** as M; on 22.04 the installer explains the Python 3.11 step and stops if Python is older.

## 7. Docs

- **Installation** (`docs/guide/installation.md`): rewritten for both modes from Guides 1 and 2, with the current situation replaced by the 4.5 commands.
- **Operating the factory**: Guide 3 as a docs page, updated for the 4.4 and 4.5 changes.
- **Remote access** (J): SSH tunnel and `tailscale serve`; why the port must not be public.
- **Upgrades and rollback**: `synth-city upgrade` and `synth-city rollback`, and what they do.
- The guides in `2026-11-16-installation-guides/` stay as the record of the situation before 4.5.

## 8. Items (first cut, to be refined before filing)

Sizes follow the 4.4 sweet spot (150–400 changed lines per item). L = likely to need splitting.

| Key | Item | Workstream | Depends on | Size |
|---|---|---|---|---|
| H1 | Layout file: `InstallationLayout`, TOML loader, Quick start defaults, WSL detection; the backend's configured settings path | W1 | — | M |
| H2 | Check engine: registry, states, evidence, freshness cache, `unknown` never `pass` | W1 | H1 | M |
| H3 | First checks: layout, permissions, services, secrets | W1 | H2 | M |
| H4 | Checks: git and GitHub (incl. CI on the base branch), harnesses (incl. Codex expiry), teams | W1 | H2 | L |
| H5 | `synth-city doctor` CLI (`--json`, exit codes) | W1 | H2 | S |
| H6 | `/health` degraded with the #941 findings | W1 | H2 | S |
| H7 | Installation page: checks by area, evidence, copyable fixes, one-click owned fixes | W1 | H3, H4 | L |
| H8 | Settings in force on the page (absorbs #1280) | W1 | H1 | S–M |
| H9 | Readiness blockers and acknowledged warnings, shared with the doctor | W1 | H3, H4 | M |
| I1 | Release archive and checksum in `release.yml` | W2 | — | S |
| I2 | Installer core: plan/execute, steps 1–4, idempotence, failure report | W2 | H1 | L |
| I3 | Installer: helpers, sudoers rules (validated), units | W2 | I2 | M |
| I4 | `agent-config` full allowlist; Agent Mail install and repair through it | W2 | #1262 | M |
| I5 | Harness install (confirmed), guided logins, verification call | W2 | I2 | M |
| I6 | GitHub identity step | W2 | I2 | S |
| I7 | CI: full dedicated install on a hosted runner, re-run, doctor, stub team | W2 | I3, I5 | M |
| I8 | Roster presets (three data files) in guided setup | W2 | #1113 | M |
| U1 | `synth-city upgrade`: preflight, backup, archive, migrations, switch, restart, doctor | W3 | H1, I1 | M–L |
| U1b | `synth-city rollback`, with the database-changed refusal | W3 | U1 | M |
| U1c | Re-apply managed harness configuration after an upgrade (through `agent-config`) | W3 | U1, I4 | S |
| U2 | `install --adopt` for an existing installation (units, drop-ins, settings in place) | W3 | H1, H5 | M |
| U3 | CI: rc.1 → rc.2 → rollback → rc.2 on a disposable install; adopt a 4.4.0 git-checkout install, then upgrade | W3 | U1b, U2, I7 | M |
| Q1 | `start.sh` (release build) | W4 | — | S |
| Q2 | `install.sh`: checks, harness offer, verified login, layout, doctor | W4 | H1, H5 | M |
| Q3 | `--service` user units | W4 | Q1 | S |
| D1 | Installation guide (both modes) | Docs | I2, Q2 | M |
| D2 | Operating the factory (docs page) | Docs | — | M |
| D3 | Remote access and upgrade guides | Docs | U1b | S |

**28 items.** Order: H1 and I1 and Q1 first (no dependencies); then the engine and the installer core in parallel; upgrades after the archive; docs last but D2 any time.

**4.4 items this slice builds on:** #1262 (minimal `agent-config`), #1113 (charter templates), #1280 (settings in force, optional), #948 (workspace provisioning). *(Author's call:)* 4.5 starts after 4.4.0 is released. An item that didn't ship in 4.4 moves into 4.5 as its own item, ordered before the items that depend on it; #1280 is simply absorbed by H8.

## 9. Open points for the Director

1. **The reference factory on release day.** Adoption writes a layout file and changes nothing. The question is what its first `synth-city upgrade` converts. Its controller is a git checkout with about 25 `*-candidate` folders beside it; the unit runs `venv-3.1` through a drop-in; `.env` is inside `controller/backend`; the units were written by hand.
   - **(a) Keep its layout for good:** adopted names, units, drop-ins and settings path stay; the upgrade only unpacks into `releases/` and repoints the drop-in. Lowest risk on release day.
   - **(b) Convert at the first upgrade:** regenerate its units and move its settings to `/etc/synth-city/env` on release day. It ends up like a fresh install, but it changes the live security boundary during the release.
   - **(c) Keep at release, convert later** in a quiet window after 4.5.0, with its own backup and rollback. *(Recommended.)*
2. **Pablo as the acceptance test (success 2).** Confirm, and the details: **when** (a release candidate in the last third of the campaign), **which repository** (one of his own, with a CI workflow on its base branch; or a small fixture repository we prepare), **which GitHub account** (his own for the test, or a machine user, which section 4.5 recommends), and **which merge policy** (`human`, the presets' default, so he merges the one PR himself; or `auto`, which also proves the verifier path).
3. **Where the installer starts.** Recommended: the docs give two commands, download and verify the archive, then run `scripts/synth-city install` from it. No `curl … | sudo bash`.

## 10. Left to the builder

- Module layout, the console entry point, TOML library choice.
- Thresholds (disk, backup age, check freshness), with defaults in the layout file.
- The page's exact placement (its own sidebar entry, per the project 1 design).
- How the CI runner simulates WSL (it can't; the WSL checks get unit tests and a manual check on the reference factory).
