# Start here: how Synth City works

Synth City runs teams of AI coding agents on your GitHub backlog. You decide what the factory works on, with which agents and under which rules. The factory does the work, and it shows you every step.

This page explains the ideas and the words. The reference pages give the details, and each section links to them. If a setting seems strange later, come back to the three ideas below. The [Glossary](/guide/glossary) defines each term.

## Three ideas

Most settings make sense once you know three ideas.

1. **Agree before you build.** A builder writes a plan first. The plan lists the files it will change, the commands it will run and the time it needs. The Leader approves it. The approved plan sets the **work item scope**: the agreed limits of the work. A verifier checks in review that the PR stayed inside the plan. Synth City also compares the changed files with the approved file list. For a continuation, it refuses any file that is not on the list. For a first plan that has a recorded file list, it holds automatic merge on a file outside the list, and on evidence that is not conclusive. This idea explains: Leader, plan approval, approval rounds, amendments, continuation.
2. **Trust the checks, not the agent.** An agent that says "done, tests pass" proves nothing. A PR merges only when CI is green and a different agent, a verifier, accepted that exact commit. This idea explains: verifier role, review hold label, required checks, merge policy.
3. **Every attempt has a budget.** Agents do not tire and do not give up. Without limits, a stuck item can loop for hours and use your quota. Each kind of retry has a counter. When a counter runs out, the item stops and asks for a person or the Supervisor. This idea explains: approval rounds, verification retries, effort limit, continuation limits, daily merge cap.

## One issue's journey

An issue goes through eight stages. Each stage uses words that the rest of the guide uses.

| Stage | What happens | Words you will meet |
|---|---|---|
| 1 | The issue gets the ready label. | Dispatch label, queue |
| 2 | The factory routes the issue to a builder by its area label and its difficulty. | Routing, area label, slot |
| 3 | The builder gets its own workspace folder. | Workspace, lease |
| 4 | The builder writes a plan. The Leader approves it. | Plan, work item scope, approval |
| 5 | The builder builds the plan. | Attempt |
| 6 | The builder opens a PR and adds the review hold label. | Review hold, head |
| 7 | A verifier reviews the exact commit. | Verifier, review |
| 8 | Checks are green, the verifier accepted the head and the hold is gone. The PR merges. | Required check, merge policy |

### Who does each action

Every action is done by a person, by an agent that uses a tool, by the Synth City backend (a program on the factory) or by GitHub.

| Actor | What it is |
|---|---|
| **Supervisor or Director** | A person, or an agent that acts as Supervisor. Works on GitHub and in the Synth City pages. |
| **Synth City backend** | The server process. Its watcher loop polls GitHub every 60 seconds by default. It updates its database, prepares folders and sends messages. It never writes code. |
| **Leader, builder, verifier** | Agent sessions that run in a terminal multiplexer. They use `git`, the GitHub command-line tool (`gh`) and the [Agent Mail](/features/agent-mail) tools. |
| **GitHub** | Runs CI (GitHub Actions). It closes an issue from a closing keyword in a PR description when the PR merges into the default branch. |

### Step by step

1. **A person adds the ready label** to an issue on GitHub. The label is the dispatch label of the repository scope. Area labels and a priority label are optional.
2. **The watcher reads the issue** on its next poll. It records a work item in its database, with status `pending`, and it routes the issue. The Leader slot and coordinator slots never get routed work. If the issue has a difficulty label, only builders whose capability tier allows that difficulty are eligible. An unset tier counts as high. The first eligible builder in roster order whose area labels match gets the issue. If no eligible builder has a matching area label, the issue stays pending. Expertise text does not route an issue by itself.
3. **The backend prepares a workspace** when the builder is free and a registered folder is free. It leases the folder to the work item and resets the folder to the base branch. It writes the builder's commit identity into the folder, so each commit shows which slot made it. It picks the branch name, writes the brief, sends the brief by Agent Mail and wakes the builder. A folder is created when the operator registers it, or the backend adopts an existing worktree of the same repository.
4. **The builder creates its branch**, reads the issue and writes a plan. It sends the plan with `synth_city_request_work_item_approval`. The Leader approves with `synth_city_approve_work_item`. The backend records the approval as work item scope revision 0 and delivers it. The builder acknowledges it before it edits any file.
5. **The builder builds.** It edits files, runs the focused tests, commits and pushes. Nothing stops it from touching other files while it works. The verifier checks that the work stayed inside the plan. Before a merge, the backend also compares the changed files with the approved file list. A continuation can touch only the listed files. A first plan with a recorded file list holds automatic merge on a file outside the list, and on inconclusive evidence. A first plan without a recorded file list gets no automatic comparison, so the verifier alone checks it.
6. **The builder opens a draft PR** with `gh pr create --draft`, adds the review hold label and reports `pr_opened`. With a GitHub App, the backend opens the draft PR instead, and it writes `Closes #<issue>` in the description. GitHub runs the CI workflows on the PR.
7. **The Leader sends the review** to a verifier by Agent Mail. The verifier reviews the exact head: the acceptance items, the tests, and that the changes stay inside the issue and the approved plan. It records `accepted` with `synth_city_record_head_review` and removes the hold label. If the builder pushes again, the verifier must review the new commit. The builder then marks the PR ready.
8. **The backend merges** under Auto merge policy. On its next poll it checks every gate again: the required checks have succeeded and no other check on the head has failed or is pending, the verifier's acceptance is for the current head, the Leader's approval is current, the PR has no file outside the plan's file list (when the plan has one), the PR is not a draft and the hold is gone. Then it calls the GitHub merge API with the exact commit it checked. If anyone pushed in between, GitHub refuses. No agent issues the merge. Under Human merge policy, the PR waits for a person.
9. **The issue closes.** Under Auto merge policy, the backend queues a closure for a merged code item. It reads the merge evidence from GitHub, posts a comment that names the PR and the merge commit, and closes the issue if it is still open. A person does not need to repeat this. A `Closes #<issue>` line in the PR description closes the issue only when the PR merges into the default branch of the repository. A PR that merges into a release branch does not close it that way. Under Human merge policy, the backend does not close the issue. GitHub closes it if the PR merges into the default branch with a closing keyword. Otherwise a person closes it, if it is still open, after the work item shows `merged`. The backend releases the folder lease. The next item that gets the folder starts with a fresh reset.

The required checks are the CI jobs that the delivery policy of the repository scope names. A merge needs every one of them to succeed on the exact head, and the named list protects against a required job that is missing. Other checks on the head also count: a check that failed, or that is still pending, can block automatic merge even when every named check succeeded. A gate job lets the policy name one check for a group of jobs. See [Repositories](/features/repositories) and the [Factory API](/api/factory) for the policy fields.

## When something goes wrong

The budgets take over.

| Situation | What happens | Setting |
|---|---|---|
| The Leader rejects a plan again and again | After the last allowed round, the item is escalated and waits for a decision. | Approval rounds |
| The work takes longer than the plan | The limit is advisory. Messages start at the limit. The Leader may extend it, up to the ceiling of the repository scope. | Effort limit, effort ceiling |
| An estimate is bigger than the plan limit | Synth City refuses the plan and suggests that you split the issue. | Plan size limit |
| Review or CI keeps failing | After the allowed retries, the item is escalated. | Verification retries |
| The work needs files outside the plan | While the item is healthy, the builder asks for an amendment. The Leader approves the new files. | Amendment limit |
| An escalated item can continue | A continuation is a new, bounded revision on the same branch. Its limits run out in the end. | Continuation limits |
| The limits run out | You end the item, or you file a follow-up issue. | None |

An escalated item keeps its work. A person, the Supervisor or the Leader decides what happens next. See [Autonomous GitHub Dispatch](/autonomy) for the recovery steps.

## The map: what you configure

Settings sit in layers. Each layer has its own place and its own owner.

| Layer | Where you change it | How often | Who may change it |
|---|---|---|---|
| Server settings (tokens, port, database, intervals) | `backend/.env`, then restart | Once, at install | The person who administers the host |
| GitHub: labels, milestone, base branch, CI workflows | GitHub | Once per campaign | Repository maintainers |
| Team and slots | The [Teams](/features/agent-teams) page, or the API | Once per campaign. Slots change when the roster changes. | The operator |
| Repository scope: repository, main checkout, base branch, labels, merge policy, limits | The [Repositories](/features/repositories) page (guided setup), or the API | Once per campaign, adjusted during it | The operator |
| Delivery policy, Supervisor binding, review hold label, effort limit | The repository settings and the API. See [Repositories](/features/repositories) and [Agent Teams](/features/agent-teams#supervisor-binding). | Rarely | The operator |
| Autonomy | The [Teams](/features/agent-teams) page | At every start and stop | The operator |

The **operator token** (`operator_token` in `backend/.env`) protects every change to teams, repository scopes, autonomy and recovery. The pages ask for it once for each browser tab. API calls send it in the header `X-Synth-City-Operator-Token`. Never paste it into Agent Mail, an issue or a chat.

Why a token, when it is your own machine? The agents can call the Synth City API too. That is how Agent Mail works. Without a token, any agent could switch its own team's autonomy on, change its limits or approve its own work. The token marks a change as the operator's change, not an agent's. In single-user mode the protection is weaker, because the agents run as you. A dedicated factory is stronger.

## Where to go next

- [Quick Start](/guide/quick-start): start the product and look at existing work.
- [Teams](/features/agent-teams): rosters, Leaders, charters, Supervisor binding.
- [Repositories](/features/repositories): repository scopes and guided setup.
- [Work](/features/work): owners, waiting reasons and PR context.
- [Autonomous GitHub Dispatch](/autonomy): enable autonomy and handle stuck work.
- [Factory Operating Model](/guide/operating-model): who may do what.
- [Supervised Factory Playbook](/guide/supervised-factory-playbook): run a campaign.
