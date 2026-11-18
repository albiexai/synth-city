# Isolated runtime kit

The isolated runtime kit starts, proves and tears down one isolated runtime for a provider live check. A harness issue uses it instead of designing its own environment. Every check runs the CLI under test against a local stub model, inside its own network namespace, with Synth City's normal team launch and Agent Mail registration.

```bash
scripts/isolated-runtime-kit.sh up     --root /tmp/kit --provider claude-code --cli /path/to/cli
scripts/isolated-runtime-kit.sh status --root /tmp/kit
scripts/isolated-runtime-kit.sh exec   --root /tmp/kit [--timeout 120] -- COMMAND...
scripts/isolated-runtime-kit.sh down   --root /tmp/kit [--keep]
```

## What `up` starts

- **Two namespace anchors.** The outer anchor is PID 1 in the kit's PID namespace, created with `--pid --fork --mount-proc` and its own user and network namespaces. It brings up `lo` as the only link. The inner anchor maps back to the caller's uid and gid. Both start from an empty environment.
- **An isolated Synth City backend.** It has its own root, HOME, SQLite database and private tmux socket, a synthetic operator token, and an allowlisted environment. It doesn't build or serve the UI.
- **A stub model provider** (`scripts/runtime-kit/stub_provider.py`) on `127.0.0.1`. It answers the OpenAI-compatible chat API with a fixed reply, and its log keeps a hash of each request, never prompt text.
- **The CLI under test.** It's installed under its provider's binary name, for example `claude` for `claude-code`. Every other harness name resolves to a fake, so no host CLI can start, and `gh` is disabled. The kit then installs Agent Mail for the provider, creates a team preset with one slot and launches it through the normal launch API. That writes the real pane binding; the CLI then registers from its pane through the normal Agent Mail endpoint.

**Scripted stub replies.** Run on its own, the stub also takes `--script FILE`: a JSON list of 1 to 64 turns, at most 64 KiB, each exactly `{"text": "..."}` or `{"tool_call": {"name": "...", "arguments": {...}}}`. It serves them in order, one per chat completion, as plain JSON or as a `stream: true` event stream; a tool call comes back as an OpenAI `tool_calls` reply with `finish_reason: "tool_calls"`. After the last turn, the fixed reply resumes. A malformed script is refused (exit 2) before the stub listens, including one with a non-finite number (`NaN`, `Infinity`, `-Infinity`, or an exponent too large for a float, such as `1e400`) or nesting too deep to decode. With a script, each log entry adds the turn index and the reply kind (`text`, `tool_call` or `fallback`), still without prompt or argument text. A scripted reply proves only what the stub sends, not what a provider CLI does with it.

**Passing a script to the kit.** `up --stub-script FILE` starts the kit's stub with that script; `status`, `exec` and `down` refuse the option (exit 2).
- **Before anything is created,** `up` opens `FILE` without following a link and without blocking, requires a regular file of at most 64 KiB, and runs the stub's own script check on those bytes. Any failure refuses (exit 2) before the root, its marker or its state exist.
- **A private copy.** `up` then writes the same bytes to `state/stub-script.json` (mode 0600, a new file in the root's own plain state directory) and their SHA-256 to `state/stub-script.sha256`, and starts the stub with `--script` naming that copy. If a write fails, `up` removes only a file it can still prove it created; a replaced file, or one whose identity it can't read, is kept, and `up` fails as incomplete, naming it.
- **Proved by `status`.** Before it writes anything, `status` refuses (exit 2) unless both files are absent, or both are regular files whose digest matches. A run with a script adds `stub-script.sha256` to the evidence and its manifest, and `summary.json` gains `stub_script_sha256` (null without a script). The digest is checked again after capture; a change publishes nothing.
- `down` removes the copy with the root.

`up` prints one JSON line with the namespaces, links, uid, pane, registration and binding, stub request count and recorded host PIDs. Pane and registration PIDs are local to the kit.

## Commands

| Command | Deadline | What it does |
|---|---|---|
| `up` | 90 s (`RUNTIME_KIT_UP_DEADLINE`) | Preflight (10 s), stub (5 s), backend health (30 s), launch and registration (30 s). A failure stops every process and keeps the root for its logs. |
| `status` | 15 s | Checks the instance, every recorded process, the backend's health and the registration before it writes anything. It then builds the evidence and the summary in a new attempt directory and publishes them; see [Evidence](#evidence) for the commit point and exit codes. |
| `exec` | 120 s by default, at most 600 s | Runs a command inside the namespaces with the kit's environment. |
| `down` | 30 s | Stops every process of the instance, including ones started later inside its namespace, proves none remains, checks the evidence manifest, and removes the root (`--keep` keeps it). If it can't prove cleanup, it fails as incomplete and keeps the root. |
| `crash --role cli` | 15 s | Kills only the recorded CLI, for a crash test. It opens a pidfd first, proves the CLI's full recorded identity, sends `SIGKILL` through that pidfd, and writes `state/crash.json` only after the pidfd shows the exit. Any other role, a failed proof, a CLI that is already gone, or an existing crash marker is refused before any signal. An exit it can't prove records nothing and is incomplete. |
| `relaunch` | 75 s | After a `crash`, relaunches the slot through the isolated backend so the member resumes its exact saved session. It needs an `opencode-cli` slot saved for resume with a `ses_…` session id. It records the new pane under the recorded tmux server and waits for that pane's own registration: an open, connected MCP session of the kit provider and slot, bound to the new CLI's exact pid and start time. Another pane's leftover row never ends the wait. It then checks that the saved session is unchanged, and only then clears the crash marker. The tmux server is set to outlive its sessions, so a crashed pane doesn't stop it. |

**Exit codes:** 0 means success, 1 a failure, a deadline or an incomplete proof, and 2 a refusal.

## Ownership and cleanup

Each `up` creates one instance with a random id. `state/instance.json` (a private file) records that id, the root directory's identity and the kit's namespaces. `status`, `exec` and `down` refuse (exit 2) before any signal, write or deletion when:
- the record is missing or malformed, or isn't a private file of the caller;
- it was copied from another root;
- it names a host namespace.

**Ownership comes only from records.** The kit records every process it starts with its pid, start time, a SHA-256 of its command line, and its parent's pid and start time. Each process has one expected parent:
- the launcher is the parent of the outer anchor;
- the outer anchor is the parent of the inner anchor;
- the inner anchor is the parent of the tmux server, the stub and the backend (it starts them itself);
- the tmux server is the parent of the CLI pane.

A process counts as the kit's only when every recorded field matches and its parent is the recorded process of its expected parent role. It must also be in the kit's recorded namespaces. Before those are recorded, only the anchors may exist, and only in their own non-host namespaces. The kit resolves a namespace-local PID against the recorded parent and verifies that it still refers to the same process. When a process re-executes, only its recorded command line may change.

**Signals.** Every signal opens a pidfd first, proves the full recorded identity, and then signals through that pidfd. Without pidfd support, or on any mismatch, nothing is signalled. The launcher is never signalled; it exits with the outer anchor.

**`down`** first computes a read-only plan. It refuses before any signal if a recorded role fails its proof or the kit network contains a process whose PID namespace membership cannot be proved. For a current root its only signal target is the verified PID 1. The kernel stops the descendants of that init, including daemons in separate sessions. The kit proves cleanup before removing the root.

**Older roots.** Instances recorded before the PID namespace change use the group-based teardown from #1010. For those roots, `down` stops an unrecorded process only when all of these hold:
- its process group id is the pid of a proved service role (the CLI, the stub, the backend or the tmux server) that leads its own group;
- it is one of the caller's processes;
- it is in the kit's network namespace.

A process can join a group only inside its own session, and a live leader keeps its pid, so such a group holds only processes started inside the kit. An anchor's group never counts. Each member is stopped on its own: `down` opens a pidfd, proves the member again (pid, start time, command line, namespace, owner, and the same live, proved leader), and only then signals it through that pidfd, with TERM and then KILL after a fresh proof. Nothing is chosen by name, and no signal goes to a whole group.

**A root is never reused.** A root left by `down --keep` or by a failed `up` is refused by the next `up`; run `down` to remove it.

## Evidence

`status` publishes these files in `ROOT/evidence/` (a link to the latest complete attempt in `ROOT/evidence.d/`), with `MANIFEST.sha256`:
- `namespaces.json`, with the host and kit user, network, PID and mount namespace identities
- `links.txt`
- `processes.txt`
- `pane.json`
- `registration.json`
- `environment-names.json`, holding variable names only, never values
- `stub-requests.jsonl`
- `summary.json`

`down` refuses to finish if the manifest no longer matches.

**Publication.** `status` builds each attempt in a new `evidence.d/<16 hex>` directory and then replaces the `ROOT/evidence` link with one atomic rename. That rename is the commit point.
- **Before the commit point,** a failure removes only this run's own attempt directory and temporary link, after checking that each is its own direct, non-symlink entry. The published link and every earlier attempt stay unchanged; `status` exits 1 ("incomplete … the published evidence is unchanged").
- **After the commit point,** the new evidence is published and stays published. `status` then prints the summary. If printing fails, it exits 1 with "published, but the summary could not be printed". Exit 0 means published and printed.
- **Earlier attempts are kept.** `status` never deletes them and never follows a link to delete anything; `down` removes them with the whole root.
- `status` refuses (exit 2), writing nothing, if `evidence.d` or an attempt is a symlink or isn't a direct child of the root.

## Safety rules

The kit refuses, with exit 2, when:
- `SYNTH_CITY_URL` or a Synth City Mail credential variable is set;
- the root is not empty and has no `.deck-runtime-kit` marker;
- the root path is longer than 96 bytes, because the tmux socket path must fit in 108;
- no real tmux executable is found (a launcher script is not accepted);
- the network namespace has any link other than `lo`;
- the inner namespace doesn't map to the caller's uid and gid;
- the two-step entry lands in the wrong namespace or procfs shows a different PID from the process itself;
- a recorded process id now has another start time, so it may belong to another process.

**Namespace entry is two-step.** The kit proves both anchors, then enters the outer user, network, mount and PID namespaces. There it uses the inner anchor's PID from private procfs to enter the inner user namespace.

## OpenCode members

For `--provider opencode-cli`, `up` pins the member to the kit's stub model before any pane starts:
- **Stub-only config.** After the Agent Mail install has written the kit HOME's `opencode.json`, `up` adds exactly one provider, `kitstub` (OpenAI-compatible, `baseURL` `http://127.0.0.1:18781/v1`, one model `stub`). It sets `enabled_providers` to `["kitstub"]`, `model` to `kitstub/stub`, and turns autoupdate and sharing off. The install's Agent Mail entry (`mcp.servers.albiex-synth-city-mail`) and its question-deny rule are kept.
- **The slot model.** The kit's team slot launches with the model `kitstub/stub`, so the product's own per-member config (`OPENCODE_CONFIG_CONTENT`) also limits `enabled_providers` to `kitstub`.
- **Proof before launch.** `up` reads the config back and refuses (exit 2) before the launch unless all of these hold:
  - the only provider is `kitstub`, with the adapter `@ai-sdk/openai-compatible` and its `baseURL` on `127.0.0.1`;
  - `enabled_providers` is exactly `["kitstub"]` and the model is `kitstub/stub`;
  - autoupdate is off and sharing is disabled;
  - the install's Mail entry is usable: local, enabled, `codemode` off, provider `opencode-cli` and the kit's own backend URL. Its command must be exactly two items: an absolute path that resolves to the kit backend's own Python interpreter (an alias of it is accepted; a different program named `python3` is not), then this checkout's `backend/mcp_shim/agent_mail_server.py`;
  - the deny rule is present.

  It also refuses an `opencode.jsonc` (comments can't be merged safely), a missing or non-object config, a config that already names another provider, and any symlinked or non-directory component of `home/.config/opencode`. A refusal changes nothing, inside or outside the kit.
- **Any later change is refused.** `up` records the SHA-256 of the config it wrote in `state/opencode-config.sha256`. A config with any other bytes no longer counts as proved. `state/` must be a plain directory of the root and the proof a plain file. `up` stages the new config and proof completely and commits each by an atomic rename, proof first, so a hard-linked proof is replaced, never written through. If the config can't be committed, the earlier proof is restored and the config is left unchanged (exit 2). A failed or partial write removes its own temporary file. If the earlier proof can't be restored, or a temporary file can't be removed, `up` stops as incomplete (exit 1) and names what is not as it was, including every temporary file it kept; it never reports that as an unchanged refusal. `up` keeps that outcome: an incomplete setup ends `up` with exit 1, keeping the root, and only an unchanged refusal ends it with exit 2.
- **The provider is proved from records.** `status` uses the OpenCode proof when the provider recorded by `up`, the launched slot's provider and the registered session's provider all agree. If the marker is missing or any of them disagree, `status` refuses before writing anything.
- **Evidence.** `status` proves the config again and publishes `opencode-config.json` in the manifest. It holds the provider ids, `enabled_providers`, the model, the stub's host and port, and whether the Mail entry is usable and the deny rule present; nothing else. At the end of capture, `status` proves the provider and the config once more. A change during capture fails `status`, and the published evidence stays unchanged.

**Limits.** The fake CLI is not OpenCode, so the product's OpenCode 2 gate refuses its Agent Mail registration and `up` with the fake stops at registration. That the stub-only config holds for a real OpenCode member, and that no hosted fallback happens, is proved only by a run of the real OpenCode CLI inside the kit; that run is a separate, approved phase.

### OpenCode 2.2.7 teardown in v4.3

On 2026-11-18, one real OpenCode 2.2.7 member ran on `release/v4.3`
at `7701cf88`. The official Linux x64 archive matched the release's
`SHA256SUMS` and GitHub asset SHA-256
`89a6cee227b54bc011179c528a5e63176032462b1a5e21723b616801cddc2ca7`.
The kit used only the loopback stub and bound the connected MCP session
to the launched slot and pane. [OpenCode v2.2.7](https://github.com/anomalyco/opencode/releases/tag/v2.2.7)

- **Teardown passed.** Standard `up` and `status` succeeded. Before
  `down`, the standalone CLI, private `serve --stdio` process and Mail
  shim formed a live parent-child chain. The server and shim each led
  their own session and process group. Standard `down` stopped and
  removed the kit. An independent process check found zero processes
  in its PID namespace and none of the six recorded processes. The
  verified status manifest SHA-256 was
  `5bf21c3569e7f02a967af4d09dd0100817007c8c604c305768397a33f78cdd9c`.
- **Q4 remains incomplete.** The session store recorded a user message,
  an assistant message and a successful idle record, in that sequence.
  The tool execution and permission-event order still needs evidence.
- **The Mail round trip remains incomplete.** The stub emitted the
  scripted Mail tool call. The member's stored turn contained no matching
  tool execution or authenticated response. Registration is the observed
  Mail evidence from this run.
- **Q6 remains pending.** The run stopped before the planned crash and
  relaunch when the Mail evidence was inconclusive. [Issue #1195](https://github.com/albiexai/synth-city/issues/1195)

## Using it for a live check

1. Write the check as commands run with `exec`, or let the CLI under test act from its pane.
2. Run `up`, then `status`, and keep the printed summary and the evidence manifest as the check's evidence.
3. Run `down` and keep its result as proof of cleanup.

The end-to-end test `backend/tests/test_isolated_runtime_kit.py` runs a fake provider CLI (`scripts/runtime-kit/fake_cli.sh`) through these steps. Its tests that start a namespace, backend or CLI are **opt-in**: they run only with `SYNTH_CITY_KIT_RUNTIME=1` and otherwise skip with "kit runtime tests are opt-in", so a broad `-k` filter or a full local run never starts a runtime by accident. Without the flag, every kit command in the tests also runs with a `PATH` whose `setsid`, `unshare` and `nsenter` refuse, so even a regressed refusal can't start an anchor. The model and refusal tests run everywhere.

## Limits

- **Linux only.** The kit needs unprivileged user, network and PID namespaces with a private procfs, plus `tmux`, `unshare`, `nsenter`, `ip`, `timeout` and `setsid`. The model and refusal tests run without namespace privileges.
- **Not on the hosted `ubuntu-latest` runner as configured.** The runner denies the namespace setup (`unshare: write failed /proc/self/uid_map: Operation not permitted`). The log shows the denial, not its cause. There, each namespace-dependent test skips with `capability unavailable: uid_map`, and the namespace path is proved by one local run in the owner's leased workspace, with evidence on the PR for review.
- **OpenCode evidence.** The #1018 fake-provider regression and the real 2.2.7 teardown above cover the separate session leaders observed in #963. The complete Q4 event order, Q6 crash and relaunch capture, and Mail round trip remain in [#1195](https://github.com/albiexai/synth-city/issues/1195).
- **One OS user.** Factory members share one OS user, so ownership is proved by the instance id and namespace, never by the user id alone.
- **Local checks only.** The stub model doesn't model a real provider; a real-provider check is a separate decision with its own approval.
- **One kit per root.** The ports (18780 for the backend, 18781 for the stub) belong to the kit's own network namespace, so separate kits don't collide.
