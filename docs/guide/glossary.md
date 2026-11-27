# Glossary

The words that the Synth City pages use, in alphabetical order. Each term links to the page that explains it. For the whole picture, read [Start here: how Synth City works](/guide/how-it-works) first.

| Term | Meaning | Learn more |
|---|---|---|
| **Amendment** | Adding files to the work item scope of an approved plan while the item is healthy. The Leader approves it. | [Autonomous GitHub Dispatch](/autonomy) |
| **Approval** | The Leader's decision on a plan. A prose reply is not an approval. | [Agent Mail](/features/agent-mail) |
| **Area label** | A GitHub label on an issue. Routing gives the issue to the first eligible builder, in roster order, whose area labels include it. | [Teams](/features/agent-teams) |
| **Attempt** | One try at an issue, from dispatch to merge or stop. A retry is a new attempt. | [Autonomous GitHub Dispatch](/autonomy) |
| **Backend** | The Synth City server process. It polls GitHub, keeps the database, prepares folders and sends messages. It never writes code. | [Architecture](/guide/architecture) |
| **Builder** | An agent in a slot with the implementer role. It writes the plan and the code. | [Teams](/features/agent-teams) |
| **Configuration scope** | Where a Claude Code setting applies: user, project or local. A page can also show a managed or plugin scope. It is not a repository scope. | [Config](/features/config) |
| **Continuation** | A new, bounded revision for an escalated item, on the same branch and PR. | [Autonomous GitHub Dispatch](/autonomy) |
| **Delivery policy** | The rules of a repository scope for delivery: the required checks, base updates and owner contact. | [Repositories](/features/repositories) |
| **Director** | The person who decides the roster, the models, the budget and the releases. | [Factory Operating Model](/guide/operating-model) |
| **Dispatch** | Giving a ready issue to a builder, with a workspace and a brief. | [Autonomous GitHub Dispatch](/autonomy) |
| **Dispatch label** | The GitHub label that marks an issue as ready. Adding it queues the issue. | [Autonomous GitHub Dispatch](/autonomy) |
| **Escalated** | Stopped, waiting for a decision. The work is kept. | [Autonomous GitHub Dispatch](/autonomy) |
| **Head** | The latest commit on the branch of a PR. Reviews and checks apply to one exact head. | [Supervised Factory Playbook](/guide/supervised-factory-playbook) |
| **Lease** | The exclusive right of a work item to a workspace folder. | [Work](/features/work) |
| **Leader** | The agent that approves plans, routes reviews and coordinates a team. A team names its Leader slot explicitly. | [Teams](/features/agent-teams) |
| **Member** | The agent in a slot. The slot is the seat, and the member is the agent in it. | [Teams](/features/agent-teams) |
| **Merge policy** | Human policy leaves a PR for a person. Auto policy lets the backend merge an eligible PR after every gate passes. | [Autonomous GitHub Dispatch](/autonomy) |
| **Operator** | The person who changes teams, repository scopes, autonomy and recovery. The operator token marks these changes. | [Autonomous GitHub Dispatch](/autonomy) |
| **Plan** | The list of files, commands and actions that a builder proposes for an attempt. The Leader approves it. | [Supervised Factory Playbook](/guide/supervised-factory-playbook) |
| **Primary checkout** | The existing local clone of the repository that a repository scope names. The backend makes the dispatch workspaces beside it. Dispatched work builds in a dispatch workspace. | [Supervised Factory Playbook](/guide/supervised-factory-playbook) |
| **Queue** | The issues that have the dispatch label and wait for a builder and a workspace. | [Work](/features/work) |
| **Repository scope** | One team's watch on one GitHub repository, with its base branch, dispatch label, primary checkout, dispatch workspaces, required checks and delivery policy. A team has at most one repository scope for each repository. Different teams can watch the same repository. | [Repositories](/features/repositories) |
| **Required check** | A CI job that must succeed on the exact head before merge. The delivery policy names it. Other checks on the head must not fail or stay pending either. | [Repositories](/features/repositories) |
| **Review** | A verifier's check of one exact head: the acceptance items, the tests and the agreed plan. | [Supervised Factory Playbook](/guide/supervised-factory-playbook) |
| **Review hold** | A GitHub label that blocks automatic merge until the accepting verifier removes it. | [Repositories](/features/repositories) |
| **Routing** | Choosing the builder for an issue. Only builders whose capability tier allows the difficulty of the issue are eligible. The first eligible builder, in roster order, whose area labels match gets it. | [Teams](/features/agent-teams) |
| **Slot** | One seat in a team: its harness, model, folder, role and charter. The agent in the slot is the member. | [Teams](/features/agent-teams) |
| **Supervisor** | A person or agent outside the team who watches it and handles incidents. | [Teams](/features/agent-teams#supervisor-binding) |
| **Team** | A group of slots with one Leader. It can watch repository scopes. Its slots work for all of its scopes. | [Teams](/features/agent-teams) |
| **Verifier** | An agent that reviews the exact head of a PR. It is a different agent from the builder. | [Factory Operating Model](/guide/operating-model) |
| **Watcher** | The backend loop that reads GitHub on a schedule and records the work items. | [Autonomous GitHub Dispatch](/autonomy) |
| **Work item** | The record of one issue in the backend, with its status, owner and workspace. | [Work](/features/work) |
| **Work item scope** | The files, commands and actions that the approved plan lets one work item use. Amendments and continuations change it. It is not a repository scope. | [Autonomous GitHub Dispatch](/autonomy) |
| **Work item scope revision** | One version of the work item scope of an attempt. Revision 0 is the first plan. Continuations are revisions 1, 2 and so on. | [Teams](/features/agent-teams) |
| **Workspace** | A dispatch workspace: a folder where one work item builds. The backend leases it to one item at a time. | [Work](/features/work) |
