# Factory operating model

This page shows who does what when a Albiex Synth City team works on a GitHub backlog. Use it with the [Supervised factory playbook](./supervised-factory-playbook.md): this page explains the model, and the playbook gives the procedures.

Select a figure to open it at full size. Parts marked **Not implemented** describe planned behavior from proposal [#506](https://github.com/albiexai/synth-city/issues/506).

::: info Naming
This page calls the human role the **Director**. Agent Mail already uses that name. The UI calls the Director's token the **Director key**. The API and code still say *operator* for the same role: the operator token, and `require_operator` on the factory routes. A supervisor uses it only under delegation.
:::

## Repository scope

A **repository scope** is one team's watch on one GitHub repository. It holds the repository, the base branch, the dispatch label, the primary checkout, the dispatch workspaces, the required checks and the delivery policy. A team has one repository scope for each owner and name of a repository. Use the same spelling every time: the server keeps the case, so `example/repo` and `EXAMPLE/repo` count as two. A different base branch or label does not make a second scope. Different teams can watch the same repository. All slots of a team work for all of its scopes.

[![Two teams and their repository scopes: team A watches repositories Y and X, team B watches repository X, and each team's slots work for all of its scopes](/images/operating-model/repository-scope.svg)](/images/operating-model/repository-scope.svg)

The word "scope" has two other meanings in these pages. A **work item scope** is the files, commands and actions that the approved plan lets one work item use. A **configuration scope** is where a setting applies: user, project or local. The [Glossary](/guide/glossary) defines each term. The API calls repository scopes `github-scopes`.

## Director actions

Director actions require the operator token through the UI or operator API. These actions include creating, launching, or deleting teams, editing rosters and models, changing repository scopes and delivery policies, enabling autonomy, binding a Supervisor, and editing campaign metadata. Launch planning remains available to eligible Mail sessions. When an action requires typed confirmation, include its exact name in the `confirm` field.

The server returns `403 director_action_not_via_mail` when a Director action carries a current Agent Mail session, including when the operator token is absent. The audit event records `via = mail_tool` and the current session. It identifies the session member unless the operator credential is verified. `ui` and `api` describe the request path and provide no identity proof. Dedicated mode separates the controller and agents by OS user. In a shared-user installation, a process that can access the operator token can authenticate a direct API request, so this check cannot enforce role separation on its own.

## Kinds of actor

Each actor is one of three kinds.

| Kind | Actors | Job |
|---|---|---|
| **Human** | The Director | Owns the outcome and the backlog. Makes the retained decisions. |
| **AI agent** | Supervisor, Leader, implementers, verifiers, optional refinement helper | Work that needs judgment: supervision, planning, implementation and review. |
| **Non-agent system** | Synth City controller, maintenance script, GitHub | Work that a rule can do: dispatch, leases, Mail, attention signals, the maintenance operation and the record. |

When a rule can do a job, the job belongs in the controller, not in an agent prompt.

## Hierarchy of roles

[![Hierarchy of roles: the Director at the top; the supervisor and the refinement helper as optional staff roles; the Leader; implementers and verifiers; the Synth City controller and GitHub as non-agent systems, not ranks](/images/operating-model/hierarchy.svg)](/images/operating-model/hierarchy.svg)

- The line of command is Director → Leader → implementers and verifiers.
- The supervisor and the refinement helper are **staff roles** beside that line. Both are optional. Without a supervisor, the Leader reports directly to the Director.
- The supervisor uses the Director's powers only within the recorded delegation. It does not take the Leader's authority over plans and backlog assessments.
- Each team has one Leader, selected by `leader_slot_id`. The Director chooses the number of implementers and verifiers. Repository scope limits such as `max_concurrent_dispatched` limit concurrent work, not team size.
- The Synth City controller and GitHub are not ranks. The controller enforces the hierarchy with tokens, leases, limits and Mail identity. GitHub holds the backlog and the record.
- An external agent, such as OpenClaw, takes the supervisor role. It is not a second Director.

## The factory

[![The factory: the Synth City controller and the team inside the factory boundary; the Director, the supervisor, the refinement helper and GitHub outside it](/images/operating-model/factory.svg)](/images/operating-model/factory.svg)

The **factory** is the Synth City controller and the AI teams, working together on the Director's backlog. The Director, the supervisor, the AI helpers and GitHub work with the factory from outside it.

Test: if the Director must start, stop, upgrade or repair a part, the part is in the factory. The Director starts, stops, upgrades and repairs the Synth City controller and the teams. The supervisor helps the Director do that, from outside.

| Part | In the factory? | Reason |
|---|---|---|
| Supervisor (AI agent) | No | It helps the Director start, stop, upgrade and repair the factory, from outside. Its powers come from the Director's recorded delegation. |
| Synth City controller (non-agent) | Yes | Maintenance upgrades and repairs it. |
| Team (AI agents) | Yes | Synth City launches its sessions. The Director chooses the roster. |
| GitHub (non-agent) | No | It keeps running when the factory stops. It holds the backlog, and the result goes out there. |
| Director (human) | No | The Director owns the factory and the backlog and keeps the retained decisions. |
| Refinement helper (AI agent) | No | It is optional. It works for the Director, not for the factory. |

### The Director owns the backlog

The Director owns and refines the GitHub backlog:

- Write and order the issues.
- Set the acceptance conditions.
- Apply the dispatch label. The dispatch label is the Director's "ready" signal.

An optional AI agent can help. It drafts or splits issues for the Director, and the Director decides. The Leader assesses the refined backlog and selects the next eligible work. The Leader does not own the backlog.

## Two operating modes

[![Two operating modes: in normal running, Synth City flags a problem and the Leader or the Director acts; in factory maintenance, the supervisor prepares a fix, the owners hand off, the maintenance script upgrades the controller, and the supervisor checks the state and resumes](/images/operating-model/modes.svg)](/images/operating-model/modes.svg)

**Normal running.** Synth City code finds problems: stopped attempts, stalled owners, recovery decisions, review or merge requests and HOLD. The Leader acts on backlog work. The Director makes the retained decisions. A supervisor is optional.

**Factory maintenance.** Maintenance is a Synth City feature that the supervisor drives from outside the factory, helping the Director.

- Synth City supplies `scripts/factory-maintenance.py` and its checks: owner checkpoints through Mail, pause, backup, proof of termination, deploy and retained-state checks.
- The supervisor decides when to run it, prepares the reviewed fix and the request files, runs the operation and resumes the factory.
- Team sessions keep running through the controller restart. The upgrade is refused if a session binding or generation changes.
- A successful upgrade ends as `deployed_paused`. Resume enables autonomy again and releases the operation's holds. It does not start any agent again.
- While the controller is down, the Synth City UI is not available. The parent GitHub issue is the only live status surface.

See the [maintenance runbook](https://github.com/albiexai/synth-city/blob/master/docs/deploy/factory-maintenance-operations.md) and the playbook's [maintenance section](./supervised-factory-playbook.md#_9-stop-safely-for-factory-maintenance).

## Why the supervisor runs outside the controller

The supervisor works on the factory from outside it, and it does not run inside the controller process either.

- **It cannot upgrade its own host.** An upgrade stops the controller, proves termination and restarts it. An agent inside the controller stops when the controller stops.
- **A watchdog must not fail with the thing it watches.** If the controller stalls or crashes, an internal supervisor stops too, and nobody sees the failure.
- **A controller that repairs itself is unsafe.** An internal agent with permission to change and deploy Synth City makes the safety model much harder to understand.

| Duty | Owner | Kind |
|---|---|---|
| Own and refine the backlog | Director, optionally helped by an agent | Human |
| Detect stalls, stopped attempts, holds and required decisions | Synth City controller | Non-agent |
| Assess the refined backlog, approve plans, select the next eligible work | Team Leader | AI agent |
| Repair, upgrade and roll back the factory | Supervisor, with the maintenance script | AI agent and non-agent |
| Retained decisions: publication, budgets, models, security HOLD | Director | Human |

## One campaign, start to finish

| Actor | Refine | Prepare | Run | Decision | Maintenance | Finish |
|---|---|---|---|---|---|---|
| **Director** (human) | Write and order issues; apply the dispatch label | Write the outcome and delegation; authorize the start | Refine at any time; read the summary | Approve or decide on GitHub | Only for a security HOLD | Approve publication |
| **Supervisor** (AI agent, optional) | Draft issues for the Director | Run startup checks | Fix routine blockers within delegation | Prepare the exact request and evidence | Fix, hand off, run the reviewed upgrade | Reconcile records; disable dispatch |
| **Synth City controller** (non-agent) | Poll labelled issues | Store repository scopes, leases and Mail identity | Dispatch; bound attempts; show signals | List the human action | Record the operation; preserve authority and leases | Show the result |
| **Leader and team** (AI agents) | Optional readiness assessment | Mail canary | Assess, plan, implement, verify | Independent work continues | Owner checkpoint, then hold | Report results |
| **GitHub** (non-agent) | The Director's backlog | Parent issue and backlog | PRs and required CI | Exact instructions in the issue | Only live status surface | Final record |

## Supervisor heartbeat

<p>
  <a href="/docs/images/operating-model/supervisor-tick.svg"><img src="/images/operating-model/supervisor-tick.svg" alt="Supervisor tick: read, act if delegated, publish if changed, check in" width="48%"></a>
  <a href="/docs/images/operating-model/heartbeat-status.svg"><img src="/images/operating-model/heartbeat-status.svg" alt="The controller derives Healthy, Late or Absent from the last check-in and the clock" width="48%"></a>
</p>

The supervisor keeps no state between ticks. It reads Synth City and GitHub again on each tick, so a harness exit or reboot loses nothing.

At the end of each tick, the supervisor promises its next check (`next_check_at`). The promise must be later than the check-in and at most 24 hours ahead. The controller compares that promise with the clock. The supervisor never reports its own health.

| Status | Meaning |
|---|---|
| None | No supervisor is registered. This is not an error. |
| Healthy | The promised check has not passed. |
| Late | The promised check is overdue, but within the grace period. Work can still run. |
| Absent | No check-in yet, or the grace period has passed. |

The grace period is the team's override when one is set. Otherwise it is the larger of 30 minutes and twice the interval between the last check-in and its promise.

## Director's summary

The Overview shows one status line: whether the factory is running, paused or idle, with the team count when known. Each team's summary card on the Overview already shows its supervisor status, remaining work, attention items and the human actions it needs.

A single top-of-page summary would also answer three questions: Do I need to act? Is anyone watching? What work remains? That top-of-page layout is not implemented. Its planned form is shown below. See [#506](https://github.com/albiexai/synth-city/issues/506) and [#508](https://github.com/albiexai/synth-city/issues/508).

```text
Publish the next release                      Parent issue: #NNN
Outcome:     Release tagged, website live, final record posted
Supervisor:  Healthy. Checked 2 minutes ago. Next check in 8 minutes.
Attention:   None open
Remaining:   Publish the release and the website. Estimate: under 1 active hour.
Next actor:  You
Your action: Review the previews and approve publication on #NNN at SHA <sha>.
```
