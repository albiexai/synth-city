# Repositories

Repositories lists watched scopes across teams. The same GitHub repository can appear in more than one scope; keep the team and scope identity when opening details or related work. Provider filtering here means a configured harness in the team's roster, while Work filtering means an assigned owner's harness.

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
