# Charter templates

::: v-pre

A charter is the text that tells one team member how to work. Albiex Synth City ships a charter template for each of three roles: Leader, builder and reviewer. The templates are Markdown files in `frontend/src/features/factory/charter-templates/`. A Director can edit any charter that a template produces.

Each template holds the same eight rules. Each rule has a one-line reason, so a reader knows why the rule exists. [Guided setup](/features/repositories#guided-configuration) offers the templates for a new team.

## The rules and why they exist

### Plan size limit

A plan must fit in the limit. The limit is `{{plan_size_limit}}` active minutes. The Leader decides each plan. If the work is bigger, the Leader asks for numbered split issues. A reviewer flags oversized work to the Leader and does not reject a plan.

Synth City checks the upper estimate at initial plan submission. An estimate above the repository scope's plan limit returns `plan_estimate_exceeds_limit` and suggests splitting the work into smaller issues. The limit defaults to 60 active minutes, capped by the scope's item effort ceiling.

Why: a plan that is too big hides scope and cannot be reviewed in one pass.

### Light review tier

A PR that changes only docs, UI or tests gets a focused review without mutation probes. A PR with the `security` label always gets a full review, whatever files it changes. The repository has no other definition of this tier. The wording comes from the verifier charter (Director, 2026-11-16).

Why: a cheap review for low-risk changes keeps the reviewers free for risky ones, and a security change is never low-risk.

### Focused local tests

A builder writes the failing tests first. The builder runs the focused tests for what changed. The full suites run on CI.

Why: the full suites take long, and CI already runs them in parallel with review.

### Test evidence block

Each PR handoff carries one block with the exact head, each test command, its pass/fail counts and the hosted CI run URL.

Why: the reviewer can trust the run without repeating it.

### Review hold

Each PR carries the hold label `{{hold_label}}` from creation. As **team practice**, only the reviewer who accepted the head removes it. The **enforced** gate is different: the system merges a PR only after a reviewer accepts the exact head and all required checks are green.

Why: the label shows the hold to people, and the exact-head acceptance is the check that the system enforces.

### Escalation order

A member takes assignments and plans to the Leader, incidents and gate problems to the Supervisor, and release decisions to the Director. A question about the roster, models or budget also goes to the Director.

Why: each person decides one kind of question, so a question reaches the right person at once.

### Process safety

All members share one operating system user. Nobody uses `pkill`, `killall` or `pgrep -f` to choose processes. A member stops only a process that the member started, after a check of its command line and parent.

Why: a pattern kill can stop another member's work.

### No interactive questions

Nobody uses an interactive question tool, such as `request_user_input` or `AskUserQuestion`.

Why: nobody is there to answer it, so the session waits for ever.

## Placeholders

A template has six placeholders. A value is filled in only when its source states it. A value that has no source stays as the literal text, for example `{{hold_label}}`. It is never blank and never guessed.

| Placeholder | Value | Source |
| --- | --- | --- |
| `{{release_branch}}` | The branch that PRs target | The scope's base ref without a leading `origin/`. The symbolic `origin/HEAD` names no release branch, so it gives no value. |
| `{{ready_label}}` | The label that marks an issue ready | The scope's dispatch label. |
| `{{hold_label}}` | The label that holds a PR | The scope's `review_hold_label`. It has no value when the scope sets none. |
| `{{lane_label}}` | The builder's lane | The area labels of the builder's slot, joined by commas. It has no value when the slot has none. |
| `{{plan_size_limit}}` | The plan limit in active minutes | The documented default of 60 minutes, clamped to the scope's `max_item_effort_minutes` when that is lower. |
| `{{supervisor_address}}` | Who to escalate to | The bound supervisor, as its display name and key. It has no value when no supervisor is bound. |

Repository operators can set `max_plan_estimate_minutes` from 15 minutes up to `max_item_effort_minutes`. A scope settings PATCH with `null` restores the inherited limit. The scope's settings history records changes. Existing charter text stays editable, and the charter template presentation has its own follow-up (#1363).

## Edit and append

Every charter stays editable. A template never replaces a charter that has text. It adds the template below the existing text.

## Start from a template in guided setup

In the Routing step, the Leader and each worker have a **Charter** field. In the Policy step, the verifier has one when the merge policy is automatic. Each field has a **Start from a template** button.

- An empty field is filled with the template of its role. Values come only from the checked repository settings. A value that was not checked stays as a placeholder.
- A field with text is never replaced. The page asks if it should append the template below your text.
- A warning names each placeholder that is still in the text of the field.
- A templated charter must be resolved before the team is created. **Save configuration** stays off while a placeholder is left. Replace each placeholder with a real value in the text, or clear the charter.
- Guided setup cannot fill `{{hold_label}}` or `{{supervisor_address}}`. Type the real value in each charter that has it.
- An existing team keeps its roster and charters. Guided setup shows no charter field for it.

:::
