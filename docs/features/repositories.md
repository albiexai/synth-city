# Repositories

A **repository scope** is one team's watch on one GitHub repository. It holds the repository, the base branch, the dispatch label, the primary checkout (the existing local clone), the dispatch workspaces, the required checks and the delivery policy. A team has one repository scope for each owner and name of a repository. Use the same spelling every time: the server keeps the case, so `example/repo` and `EXAMPLE/repo` count as two. A different base branch or label does not make a second scope. Different teams can watch the same repository, so one repository can have several scopes. All slots of a team work for all of its scopes.

[![Two teams and their repository scopes: team A watches repositories Y and X, team B watches repository X, and each team's slots work for all of its scopes](/images/operating-model/repository-scope.svg)](/images/operating-model/repository-scope.svg)

The API calls repository scopes `github-scopes`, for example `/api/v1/agent-teams/github-scopes/{scope_id}`. A repository scope is not a work item scope (the files that one work item may change) and not a configuration scope (user, project or local). The [Glossary](/guide/glossary) defines all three.

Repositories lists repository scopes across teams. The same GitHub repository can appear in several repository scopes. Use the team and scope ID when opening details or related work. Provider filtering here means a configured harness in the team's roster, while Work filtering means an assigned owner's harness.

Read configured enablement separately from effective intake. A team and scope can both be enabled while normal intake is blocked by recovery-only mode, a stopped scheduler or a missing scheduled job. Unavailable runtime evidence stays unknown. A runtime timestamp is separate from the stored last-poll time.

Fresh, stale and never-polled observations describe eligible polling. Paused or blocked intake displays suspended polling; unknown runtime is not silently classified as stale. An error while refreshing keeps the prior observation labelled rather than reporting zero configured scopes.

A same-repository/same-label overlap warning includes other enabled local scope IDs, even outside your current team filter. It warns about independent dispatch authorities; it does not deduplicate issues, merge attempts or prevent concurrent dispatch.

Open the scope's work with `scope_id` to browse its queue separately. Use existing Teams surfaces for configuration and existing authority checks. Navigation does not enable a scope or release a workspace. Team deletion remains server-guarded against enabled automation and in-use work, approvals, revisions and leases; it is not a way to silently stop or clear work.

Use [Teams](/features/agent-teams) for current repository settings and [Work](/features/work) for paused pagination/detail behavior. [Factory API](/api/factory) documents the safe observations and the protected setup check.

## Guided configuration

Select **Set up a repository** (`/repositories/new`) for a guided configuration. Each step is separate:

1. **Repository** — enter the owner, name, primary checkout, base branch, dispatch and design labels, and dispatch authentication. **Check access and labels** reads the checkout identity, repository access, labels, base branch and the presence of the selected authentication. It makes no changes. It needs the operator credential.
2. **Team and roles** — create an inactive team with an explicit Leader slot and worker slots, or use an existing team. An existing team keeps its roster, Leader, policy and activation.
3. **Routing** — set area labels and expertise for each new worker slot. Each new member can also start a charter from a [template](/features/charter-templates). A charter with a placeholder left blocks the save. Overlap advice uses the loaded data.
4. **Policy** — a new scope uses human merge, concurrency 1, one verification retry and no automatic merges. It is saved disabled, even when checks have gaps.
5. **Review** — confirm **Save configuration only**. Saving does not launch workers or activate the scope. Credentials are not stored in the draft.
6. **Saved setup** — review a current launch plan and launch the selected slots as a separate action. Activation is also separate: it needs a fresh ready check, no shared readiness blockers and acknowledgement of each listed overlap. Enabling the team also resumes its other enabled scopes.

An uncertain create or launch result is reconciled from fresh reads; the page does not repeat the request. Overlap detection is advisory; it is not an atomic ownership guarantee across scopes. Host credential and label procedures remain in [Autonomous GitHub dispatch](/autonomy).

## Review hold label and effort ceiling

Open a repository in [Teams](/features/agent-teams) and select **Edit** in **Review hold and effort limit**. The dialog edits two settings:

- **Review hold label.** The label that holds a pull request until a verifier accepts it. **No hold label (clear it)** removes it. An empty label is not valid.
- **Effort ceiling (minutes).** The most active minutes one item can use. The dialog shows the server's range and default, and refuses a value outside the range or a number that is not whole.

The dialog reads the history from the server and shows each change with its time, actor, reason and old and new value. A value that the server hides shows as "changed, value hidden". It needs the Director key to read and to save. A save sends only the fields that changed, with an optional reason, and the history reloads.

## Required checks

Open a repository in [Teams](/features/agent-teams) and select **Edit** in **Required checks**. The dialog edits the list of CI checks that must succeed on the exact head before a pull request can merge. The list is one field of the delivery policy. The other fields have their own dialog: see [Delivery policy](#delivery-policy).

- **A check** has a name and an app (the default is `github-actions`). The name and the app together are its identity. Add a check, remove it, or move it up or down.
- **Rules of the server, shown before a save.** A name is required and has at most 200 characters. An app uses lowercase letters, digits and hyphens, and has at most 100 characters. The same name and app cannot appear twice. The list has at most 32 checks. The list cannot be empty while the broad checks run on hosted CI.
- **Reason.** A reason is required. It is kept in the history with the new list.
- **A save replaces the whole policy.** The dialog sends the current policy with only the list changed, and the revision that you started from. If the policy changed on the server after that, nothing is saved. The dialog says so, and you can discard your changes to see the current list.
- **History.** The dialog reads the latest 100 changes of the delivery policy. It shows what each change added, removed or reordered, with the revision, the time, the reason and, for an attempt that took over the policy, the attempt.

The dialog needs the Director key to read the history and to save.

## Delivery policy

Select **Edit** in **Delivery policy**. The dialog edits the other fields of the delivery policy. The required checks keep their own dialog. A change applies to the next attempt. It does not grant authority.

- **Choices.** Broad checks (`local` or `hosted`), the accepted base update (`disabled`, `fast_forward` or `merge`) and the owner contact (`reports` or `native`).
- **Timers.** Each timer shows its range in seconds: owner idle time (60 to 86400), owner nudge grace (30 to 3600), owner observation wait (30 to 1800), owner observation resume (60 to 3600) and checkpoint delay report (60 to 86400). Each has a one-line explanation. A timer that the server allows to be unset (all but the resume time) is unset when its box is empty.
- **Switches.** Caller inventory required, and regression proof required.
- **Rules of the server, shown before a save.** Each number must be a whole number in its range. An observation wait needs native owner contact. Hosted broad checks need at least one required check. Add the check in **Required checks** first.
- **Reason, revision and history.** These work as in **Required checks**. A reason is required. A save replaces the whole policy and sends the revision that you started from. A stale revision is refused, and you can discard your changes. The history shows every field that changed, from the old value to the new value.

The dialog needs the Director key to read the history and to save.
