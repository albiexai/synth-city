import { MemoryRouter } from 'react-router-dom'
import { act, render as renderUi, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { RepositorySettings } from '../src/features/agent-teams/RepositorySettings'
import { clearOperatorToken, setOperatorToken } from '../src/features/agent-teams/operatorAuth'
import { fetchDeliveryPolicyHistory, updateTeamGithubDeliveryPolicy } from '../src/features/agent-teams/api'
import { ApiHttpError } from '../src/lib/api'
import type { DeliveryPolicy, DeliveryPolicyEvent, TeamGithubScope } from '../src/types/agentTeams'

// #1346 (part B2 of #1297): the editor for the other fields of the delivery policy.
vi.mock('../src/features/agent-teams/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../src/features/agent-teams/api')>()),
  updateTeamGithubDeliveryPolicy: vi.fn().mockResolvedValue({}),
  fetchDeliveryPolicyHistory: vi.fn(),
}))

const render = (ui: React.ReactElement) => renderUi(ui, { wrapper: MemoryRouter })
const check = (name: string, app_slug = 'github-actions') => ({ name, app_slug })
const policy = (over: Partial<DeliveryPolicy> = {}): DeliveryPolicy => ({
  required_checks: [check('Unit tests'), check('Lint')], owner_contact: 'native', owner_idle_seconds: 1800, owner_nudge_grace_seconds: 120,
  owner_observation_wait_seconds: 60, owner_observation_resume_seconds: 900, accepted_base_update: 'merge',
  broad_checks: 'hosted', checkpoint_delay_report_seconds: 600, caller_inventory_required: false, regression_proof_required: false, ...over,
})
const scope = (over: Partial<TeamGithubScope> = {}) => ({
  id: 2, preset_id: 1, repo_owner: 'example', repo_name: 'project', repo_path: '/tmp/project',
  dispatch_label: 'ready', design_label: 'design', merge_policy: 'human', github_auth_mode: 'app',
  github_auth_configured: true, github_poll_token_configured: true, max_approval_rounds: 3,
  max_concurrent_dispatched: 3, max_verification_retries: 2, max_auto_merges_per_day: 5, base_ref: 'origin/HEAD',
  builds_out_of_tree: false, max_build_parallelism: 4, continuation_enabled: false, max_continuation_revisions: 3,
  max_continuation_failed_heads: 8, max_failed_heads_per_revision: 1, max_scope_paths: 4, max_scope_commands: 8,
  enabled: true, created_at: '2026-09-29T12:00:00Z', updated_at: '2026-09-29T12:00:00Z',
  delivery_policy: policy(), delivery_policy_revision: 4, ...over,
} as TeamGithubScope)
const event = (id: number, revision: number, over: Partial<DeliveryPolicy>, extra: Partial<DeliveryPolicyEvent> = {}): DeliveryPolicyEvent => ({
  id, scope_id: 2, work_item_id: null, revision, policy: policy(over), reason: 'Set up', actor: null, created_at: '2026-11-26T08:00:00Z', ...extra,
})
const view = (over: Partial<TeamGithubScope> = {}, refresh = vi.fn().mockResolvedValue(undefined)) =>
  <RepositorySettings scope={scope(over)} autonomyEnabled={false} refresh={refresh} onRemoved={vi.fn()} />
const flush = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)) })

async function openEditor(over: Partial<TeamGithubScope> = {}, refresh = vi.fn().mockResolvedValue(undefined)) {
  const user = userEvent.setup()
  const rendered = render(view(over, refresh))
  await user.click(screen.getByRole('button', { name: 'Edit delivery policy for example/project' }))
  await screen.findByText('History')
  await flush()
  return { user, refresh, rerender: (next: Partial<TeamGithubScope>) => rendered.rerender(view(next, refresh)) }
}
const openOther = async (user: ReturnType<typeof userEvent.setup>, button: string) => {
  await user.keyboard('{Escape}')
  await user.click(screen.getByRole('button', { name: `${button} for example/project` }))
  await screen.findByText('History')
  await flush()
}
const field = (label: RegExp | string) => screen.getByLabelText(label)
const timer = (label: string) => screen.getByLabelText(new RegExp(`^${label} \\(seconds`))
const save = () => screen.getByRole('button', { name: 'Save' })
const type = async (user: ReturnType<typeof userEvent.setup>, input: HTMLElement, text: string) => { await user.clear(input); if (text) await user.type(input, text) }
const paste = async (user: ReturnType<typeof userEvent.setup>, input: HTMLElement, text: string) => { await user.clear(input); await user.click(input); await user.paste(text) }
const sent = () => vi.mocked(updateTeamGithubDeliveryPolicy).mock.calls[0][1]

beforeEach(() => {
  setOperatorToken('director-key')
  vi.mocked(updateTeamGithubDeliveryPolicy).mockReset().mockResolvedValue({ scope_id: 2, revision: 5, policy: policy() })
  vi.mocked(fetchDeliveryPolicyHistory).mockReset().mockResolvedValue({ events: [event(7, 4, {})] })
})
afterEach(() => clearOperatorToken())

describe('the delivery policy section and form', () => {
  it('summarizes the choices, and says so when the server sends no policy', () => {
    const { unmount } = render(view())
    const section = screen.getByRole('region', { name: 'Delivery policy' })
    expect(within(section).getByText('hosted')).toBeInTheDocument()
    expect(within(section).getByText('merge')).toBeInTheDocument()
    expect(within(section).getByText('native')).toBeInTheDocument()
    unmount()
    render(view({ delivery_policy: undefined, delivery_policy_revision: undefined }))
    expect(within(screen.getByRole('region', { name: 'Delivery policy' })).getByText('Not available from this server')).toBeInTheDocument()
  })

  it('shows the current values, each range in its label, and a one-line explanation', async () => {
    await openEditor()
    expect(field('Broad checks')).toHaveValue('hosted')
    expect(field('Accepted base update')).toHaveValue('merge')
    expect(field('Owner contact')).toHaveValue('native')
    expect(timer('Owner idle time')).toHaveValue('1800')
    for (const label of ['Owner idle time (seconds, 60 to 86400)', 'Owner nudge grace (seconds, 30 to 3600)', 'Owner observation wait (seconds, 30 to 1800)',
      'Owner observation resume (seconds, 60 to 3600)', 'Checkpoint delay report (seconds, 60 to 86400)']) expect(screen.getByText(label)).toBeInTheDocument()
    expect(screen.getByText(/Empty: no wait/)).toBeInTheDocument()
    expect(field('Caller inventory required')).not.toBeChecked()
  })

  it('shows an unset timer as an empty box', async () => {
    await openEditor({ delivery_policy: policy({ owner_idle_seconds: null, owner_contact: 'reports', owner_observation_wait_seconds: null }) })
    expect(timer('Owner idle time')).toHaveValue('')
  })
})

describe('rules shown before the save', () => {
  const bad: [string, string, string][] = [
    ['Owner idle time', '59', '60 to 86400'], ['Owner idle time', '86401', '60 to 86400'], ['Owner nudge grace', '29', '30 to 3600'], ['Owner nudge grace', '3601', '30 to 3600'],
    ['Owner observation wait', '29', '30 to 1800'], ['Owner observation wait', '1801', '30 to 1800'], ['Owner observation resume', '59', '60 to 3600'],
    ['Owner observation resume', '3601', '60 to 3600'], ['Checkpoint delay report', '59', '60 to 86400'], ['Checkpoint delay report', '86401', '60 to 86400'],
    ['Owner idle time', '12.5', '60 to 86400'], ['Owner idle time', '-100', '60 to 86400'], ['Owner idle time', 'soon', '60 to 86400'],
  ]
  it.each(bad)('refuses %s = %s and names the range, without a request', async (label, value, range) => {
    const { user } = await openEditor()
    await type(user, timer(label), value)
    expect(screen.getByText(`Enter a whole number from ${range.replace(' to ', ' to ')}.`)).toBeInTheDocument()
    expect(timer(label)).toHaveAttribute('aria-invalid', 'true')
    await type(user, field('Reason'), 'Try')
    await user.click(save())
    expect(updateTeamGithubDeliveryPolicy).not.toHaveBeenCalled()
    expect(screen.getByText('Fix the problems in the form first.')).toBeInTheDocument()
  })

  const edges: [string, string, string][] = [
    ['Owner idle time', '60', 'owner_idle_seconds'], ['Owner idle time', '86400', 'owner_idle_seconds'], ['Owner nudge grace', '30', 'owner_nudge_grace_seconds'],
    ['Owner nudge grace', '3600', 'owner_nudge_grace_seconds'], ['Owner observation wait', '30', 'owner_observation_wait_seconds'], ['Owner observation wait', '1800', 'owner_observation_wait_seconds'],
    ['Owner observation resume', '60', 'owner_observation_resume_seconds'], ['Owner observation resume', '3600', 'owner_observation_resume_seconds'],
    ['Checkpoint delay report', '60', 'checkpoint_delay_report_seconds'], ['Checkpoint delay report', '86400', 'checkpoint_delay_report_seconds'],
  ]
  it.each(edges)('accepts the edge %s = %s and sends it as a number', async (label, value, key) => {
    const { user } = await openEditor()
    await type(user, timer(label), value)
    await type(user, field('Reason'), 'Edge')
    await user.click(save())
    expect(sent().policy).toMatchObject({ [key]: Number(value) })
  })

  it.each([['Owner idle time', 'owner_idle_seconds'], ['Owner nudge grace', 'owner_nudge_grace_seconds'], ['Owner observation wait', 'owner_observation_wait_seconds'],
    ['Checkpoint delay report', 'checkpoint_delay_report_seconds']])('sends an empty %s as null, because the server allows it to be unset', async (label, key) => {
    const { user } = await openEditor()
    await type(user, timer(label), '')
    await type(user, field('Reason'), 'Unset')
    await user.click(save())
    expect(sent().policy).toMatchObject({ [key]: null })
  })

  it('starts a new opening without the old draft', async () => {
    const { user } = await openEditor()
    await user.click(field('Caller inventory required'))
    expect(save()).toBeEnabled()
    await user.keyboard('{Escape}')
    await user.click(screen.getByRole('button', { name: 'Edit delivery policy for example/project' }))
    await screen.findByText('History')
    await flush()
    expect(field('Caller inventory required')).not.toBeChecked()
    expect(save()).toBeDisabled()
  })

  it('refuses an empty resume time, because the server needs one', async () => {
    const { user } = await openEditor()
    await type(user, timer('Owner observation resume'), '')
    expect(screen.getByText('Enter a whole number from 60 to 3600.')).toBeInTheDocument()
  })

  it('says before the save that an observation wait needs native owner contact', async () => {
    const { user } = await openEditor()
    await user.selectOptions(field('Owner contact'), 'reports')
    expect(screen.getByText(/An observation wait needs native owner contact/)).toBeInTheDocument()
    await type(user, field('Reason'), 'Contact')
    await user.click(save())
    expect(updateTeamGithubDeliveryPolicy).not.toHaveBeenCalled()
    await type(user, timer('Owner observation wait'), '') // clearing the wait removes the conflict
    expect(screen.queryByText(/needs native owner contact/)).not.toBeInTheDocument()
    await user.click(save())
    expect(sent().policy).toMatchObject({ owner_contact: 'reports', owner_observation_wait_seconds: null })
  })

  it('says before the save that hosted broad checks need a required check, and points to the other editor', async () => {
    const { user } = await openEditor({ delivery_policy: policy({ required_checks: [], broad_checks: 'local' }) })
    await user.selectOptions(field('Broad checks'), 'hosted')
    expect(screen.getByText(/Hosted broad checks need at least one required check\. Add one in Required checks first/)).toBeInTheDocument()
    await type(user, field('Reason'), 'Hosted')
    await user.click(save())
    expect(updateTeamGithubDeliveryPolicy).not.toHaveBeenCalled()
  })
})

describe('saving', () => {
  it('sends the whole policy with the revision and the trimmed reason, and keeps the required checks and untouched fields', async () => {
    const { user, refresh } = await openEditor()
    await user.selectOptions(field('Broad checks'), 'local')
    await user.selectOptions(field('Accepted base update'), 'fast_forward')
    await type(user, timer('Owner idle time'), '')
    await user.click(field('Caller inventory required'))
    await user.click(field('Regression proof required'))
    await type(user, field('Reason'), '  Tighten delivery  ')
    await user.click(save())
    expect(updateTeamGithubDeliveryPolicy).toHaveBeenCalledWith(2, {
      expected_revision: 4, reason: 'Tighten delivery',
      policy: policy({ broad_checks: 'local', accepted_base_update: 'fast_forward', owner_idle_seconds: null, caller_inventory_required: true, regression_proof_required: true }),
    }, 'director-key')
    expect(refresh).toHaveBeenCalled()
    expect(fetchDeliveryPolicyHistory).toHaveBeenCalledTimes(2) // on open and after the save
  })

  it('refuses a save with no change, and one with no reason or too long a reason', async () => {
    const { user } = await openEditor()
    await type(user, timer('Owner idle time'), '1801')
    await type(user, timer('Owner idle time'), '1800') // back to the original value
    await type(user, field('Reason'), 'Same')
    await user.click(save())
    expect(screen.getByText('No change to save.')).toBeInTheDocument()
    await user.click(field('Caller inventory required'))
    await type(user, field('Reason'), '')
    await user.click(save())
    expect(screen.getByText('Enter a reason for the change.')).toBeInTheDocument()
    await paste(user, field('Reason'), '😀'.repeat(1000)) // 1000 characters, 2000 UTF-16 units
    await user.click(save())
    expect(updateTeamGithubDeliveryPolicy).toHaveBeenCalledTimes(1)
  })

  it('refuses a reason of 1001 characters', async () => {
    const { user } = await openEditor()
    await user.click(field('Regression proof required'))
    await paste(user, field('Reason'), '😀'.repeat(1001))
    expect(screen.getByText('The reason has at most 1000 characters.')).toBeInTheDocument()
    await user.click(save())
    expect(updateTeamGithubDeliveryPolicy).not.toHaveBeenCalled()
  })

  it('does not enable Save until something changes', async () => {
    await openEditor()
    expect(save()).toBeDisabled()
  })
})

describe('a policy that changed on the server', () => {
  it('shows the stale draft, blocks the save and lets the user discard', async () => {
    const { user, rerender } = await openEditor()
    await user.click(field('Caller inventory required'))
    rerender({ delivery_policy: policy({ broad_checks: 'local' }), delivery_policy_revision: 5 })
    expect(screen.getByText(/revision 4 to 5/)).toBeInTheDocument()
    expect(save()).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Discard my changes' }))
    expect(field('Broad checks')).toHaveValue('local')
    expect(field('Caller inventory required')).not.toBeChecked()
  })

  it('keeps a refused draft blocked after a 409, and unlocks when the read shows a newer revision', async () => {
    vi.mocked(updateTeamGithubDeliveryPolicy).mockRejectedValue(new ApiHttpError('delivery_policy_revision_changed', 409))
    const { user, refresh, rerender } = await openEditor()
    await user.click(field('Caller inventory required'))
    await type(user, field('Reason'), 'Conflict')
    await user.click(save())
    expect(await screen.findByText(/The server refused the save because the policy changed/)).toBeInTheDocument()
    expect(screen.getByText(/revision 4 is out of date/)).toBeInTheDocument()
    expect(field('Broad checks')).toBeDisabled()
    expect(save()).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Read again' }))
    expect(refresh).toHaveBeenCalledTimes(2) // after the 409, then Read again
    rerender({ delivery_policy: policy({ regression_proof_required: true }), delivery_policy_revision: 5 })
    expect(field('Broad checks')).toBeEnabled()
    expect(screen.getByText(/revision 4 to 5/)).toBeInTheDocument() // the old draft stays stale
    expect(save()).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Discard my changes' }))
    expect(field('Regression proof required')).toBeChecked()
  })

  it('shows another error without a lock', async () => {
    vi.mocked(updateTeamGithubDeliveryPolicy).mockRejectedValue(new Error('network down'))
    const { user } = await openEditor()
    await user.click(field('Caller inventory required'))
    await type(user, field('Reason'), 'Offline')
    await user.click(save())
    expect(await screen.findByText('network down')).toBeInTheDocument()
    expect(field('Broad checks')).toBeEnabled()
  })

  it('keeps a confirmed save when the dialog was reopened before the answer, and the new draft stays stale', async () => {
    let resolve!: (value: { scope_id: number; revision: number; policy: DeliveryPolicy }) => void
    vi.mocked(updateTeamGithubDeliveryPolicy).mockReturnValue(new Promise((res) => { resolve = res }))
    const { user, refresh } = await openEditor()
    await user.click(field('Caller inventory required'))
    await type(user, field('Reason'), 'Slow')
    await user.click(save())
    await user.keyboard('{Escape}')
    await user.click(screen.getByRole('button', { name: 'Edit delivery policy for example/project' }))
    await screen.findByText('History')
    await user.click(field('Regression proof required')) // a draft on revision 4
    await act(async () => resolve({ scope_id: 2, revision: 5, policy: policy({ caller_inventory_required: true }) }))
    expect(refresh).toHaveBeenCalled()
    expect(screen.getByText(/revision 4 to 5/)).toBeInTheDocument()
    expect(save()).toBeDisabled()
  })
})

describe('the history', () => {
  it('shows each changed field from the one before, in its own words', async () => {
    vi.mocked(fetchDeliveryPolicyHistory).mockResolvedValue({ events: [
      event(13, 9, { owner_idle_seconds: null, regression_proof_required: true, caller_inventory_required: true }, { reason: 'Looser' }),
      event(12, 8, { accepted_base_update: 'disabled' }, { reason: 'Hosted' }),
      event(11, 7, { broad_checks: 'local', accepted_base_update: 'disabled' }, { reason: 'Same again' }),
      event(10, 6, { broad_checks: 'local', accepted_base_update: 'disabled' }, { reason: 'Start' }),
    ] })
    await openEditor()
    expect(screen.getByText('Owner idle time: 1800 s to not set')).toBeInTheDocument()
    expect(screen.getByText('Regression proof required: no to yes')).toBeInTheDocument()
    expect(screen.getByText('Broad checks: local to hosted')).toBeInTheDocument()
    expect(screen.getByText('Accepted base update: disabled to merge')).toBeInTheDocument()
    expect(screen.getByText('No field changed')).toBeInTheDocument()
    expect(screen.getByText('Required checks: Unit tests (github-actions), Lint (github-actions)')).toBeInTheDocument()
  })

  it('shows the whole policy for the oldest event, which has no earlier event to compare with', async () => {
    vi.mocked(fetchDeliveryPolicyHistory).mockResolvedValue({ events: [event(7, 4, { owner_idle_seconds: null, caller_inventory_required: true })] })
    await openEditor()
    const history = within(screen.getByRole('region', { name: 'History' }))
    for (const line of ['Broad checks: hosted', 'Accepted base update: merge', 'Owner contact: native', 'Owner idle time: not set', 'Owner nudge grace: 120 s',
      'Owner observation wait: 60 s', 'Owner observation resume: 900 s', 'Checkpoint delay report: 600 s', 'Caller inventory required: yes', 'Regression proof required: no']) expect(history.getByText(line)).toBeInTheDocument()
  })

  it('shows a saved change after the save, and says when the history cannot be read', async () => {
    const { user } = await openEditor()
    vi.mocked(fetchDeliveryPolicyHistory).mockResolvedValue({ events: [event(8, 5, { caller_inventory_required: true }, { reason: 'Inventory', actor: 'director' }), event(7, 4, {})] })
    await user.click(field('Caller inventory required'))
    await type(user, field('Reason'), 'Inventory')
    await user.click(save())
    expect(await screen.findByText('Caller inventory required: no to yes')).toBeInTheDocument()
    expect(screen.getByText(/director/)).toBeInTheDocument()
  })

  it('does not call an unreadable history empty', async () => {
    vi.mocked(fetchDeliveryPolicyHistory).mockRejectedValue(new Error('boom'))
    await openEditor()
    expect(screen.getByText('The history is not available.')).toBeInTheDocument()
    expect(screen.queryByText('No changes recorded yet.')).not.toBeInTheDocument()
    expect(field('Broad checks')).toBeEnabled()
  })
})

describe('both delivery policy editors share what the repository is known to hold (V1 review 436)', () => {
  it('uses a revision and policy that the delivery policy dialog saved, in the required checks dialog, before the read catches up', async () => {
    vi.mocked(updateTeamGithubDeliveryPolicy).mockResolvedValueOnce({ scope_id: 2, revision: 5, policy: policy({ caller_inventory_required: true }) })
    const { user } = await openEditor() // the read stays at revision 4
    await user.click(field('Caller inventory required'))
    await type(user, field('Reason'), 'Inventory')
    await user.click(save())
    await screen.findByText('Delivery policy saved').catch(() => undefined)
    await openOther(user, 'Edit required checks')
    await user.click(screen.getByRole('button', { name: 'Remove check 2' }))
    await type(user, field('Reason'), 'Drop lint')
    await user.click(save())
    expect(vi.mocked(updateTeamGithubDeliveryPolicy).mock.calls[1][1]).toMatchObject({ expected_revision: 5, policy: { caller_inventory_required: true, required_checks: [check('Unit tests')] } })
  })

  it('uses a revision and policy that the required checks dialog saved, in the delivery policy dialog', async () => {
    vi.mocked(updateTeamGithubDeliveryPolicy).mockResolvedValueOnce({ scope_id: 2, revision: 5, policy: policy({ required_checks: [check('Unit tests')] }) })
    const { user } = await openEditor()
    await openOther(user, 'Edit required checks')
    await user.click(screen.getByRole('button', { name: 'Remove check 2' }))
    await type(user, field('Reason'), 'Drop lint')
    await user.click(save())
    await flush()
    await openOther(user, 'Edit delivery policy')
    await user.click(field('Regression proof required'))
    await type(user, field('Reason'), 'Proof')
    await user.click(save())
    expect(vi.mocked(updateTeamGithubDeliveryPolicy).mock.calls[1][1]).toMatchObject({ expected_revision: 5, policy: { regression_proof_required: true, required_checks: [check('Unit tests')] } })
  })

  it('keeps the required checks dialog read-only after the delivery policy dialog got a 409 on the same revision', async () => {
    vi.mocked(updateTeamGithubDeliveryPolicy).mockRejectedValueOnce(new ApiHttpError('delivery_policy_revision_changed', 409))
    const { user } = await openEditor()
    await user.click(field('Caller inventory required'))
    await type(user, field('Reason'), 'Conflict')
    await user.click(save())
    await screen.findByText(/revision 4 is out of date/)
    await openOther(user, 'Edit required checks')
    expect(screen.getByText(/revision 4 is out of date/)).toBeInTheDocument()
    expect(screen.getByLabelText('Check 1 name')).toBeDisabled()
  })

  it('keeps the delivery policy dialog read-only after the required checks dialog got a 409 on the same revision', async () => {
    vi.mocked(updateTeamGithubDeliveryPolicy).mockRejectedValueOnce(new ApiHttpError('delivery_policy_revision_changed', 409))
    const { user } = await openEditor()
    await openOther(user, 'Edit required checks')
    await user.click(screen.getByRole('button', { name: 'Remove check 2' }))
    await type(user, field('Reason'), 'Conflict')
    await user.click(save())
    await screen.findByText(/revision 4 is out of date/)
    await openOther(user, 'Edit delivery policy')
    expect(screen.getByText(/revision 4 is out of date/)).toBeInTheDocument()
    expect(field('Broad checks')).toBeDisabled()
  })

  it('forgets a rejected revision when the settings show another repository', async () => {
    vi.mocked(updateTeamGithubDeliveryPolicy).mockRejectedValueOnce(new ApiHttpError('delivery_policy_revision_changed', 409))
    const { user, rerender } = await openEditor()
    await user.click(field('Caller inventory required'))
    await type(user, field('Reason'), 'Conflict')
    await user.click(save())
    await screen.findByText(/revision 4 is out of date/)
    rerender({ id: 3 })
    await openOther(user, 'Edit required checks')
    expect(screen.getByLabelText('Check 1 name')).toBeEnabled()
    expect(screen.queryByText(/is out of date/)).not.toBeInTheDocument()
  })

  it('keeps a draft in each dialog separate', async () => {
    const { user } = await openEditor()
    await user.click(field('Caller inventory required'))
    await openOther(user, 'Edit required checks')
    expect(save()).toBeDisabled() // no draft in this dialog, even though the other one has one
  })
})

describe('the one-line explanations match what the service does', () => {
  it('says that the observation wait starts after a nudge and the pause has a resume deadline', async () => {
    await openEditor()
    expect(screen.getByText(/After a nudge, how long to wait when native evidence is unknown, before the item pauses for the operator/)).toBeInTheDocument()
    expect(screen.getByText(/An unchanged pause can be resumed before this deadline\. At or after it, the resume is refused\./)).toBeInTheDocument()
  })

  it('says what the selected accepted base update does, and that GitHub Update branch stays possible when it is disabled', async () => {
    const { user } = await openEditor() // the policy holds merge
    expect(screen.getByText('The command merges the accepted base into the workspace.')).toBeInTheDocument()
    await user.selectOptions(field('Accepted base update'), 'disabled')
    expect(screen.getByText('The operator integration-update command is refused. Use GitHub Update branch by hand.')).toBeInTheDocument()
    await user.selectOptions(field('Accepted base update'), 'fast_forward')
    expect(screen.getByText('The command moves the workspace to the accepted base only when the history stays linear.')).toBeInTheDocument()
  })

  it('keeps the option labels short and explains the selected broad checks and owner contact choice', async () => {
    const { user } = await openEditor()
    expect(within(field('Accepted base update')).getAllByRole('option').map((option) => option.textContent)).toEqual(['Disabled', 'Fast forward', 'Merge'])
    expect(screen.getByText('Builders run focused checks. The required hosted jobs give the final evidence.')).toBeInTheDocument()
    expect(screen.getByText('Fresh activity in the owner session can renew owner contact.')).toBeInTheDocument()
    await user.selectOptions(field('Owner contact'), 'reports')
    expect(screen.getByText('The owner sends progress reports before the idle time ends.')).toBeInTheDocument()
  })
})
