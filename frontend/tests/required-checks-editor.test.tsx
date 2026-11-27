import { MemoryRouter } from 'react-router-dom'
import { act, fireEvent, render as renderUi, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { RepositorySettings } from '../src/features/agent-teams/RepositorySettings'
import { clearOperatorToken, setOperatorToken } from '../src/features/agent-teams/operatorAuth'
import { fetchDeliveryPolicyHistory, updateTeamGithubDeliveryPolicy } from '../src/features/agent-teams/api'
import { ApiHttpError } from '../src/lib/api'
import type { DeliveryPolicy, DeliveryPolicyEvent, TeamGithubScope } from '../src/types/agentTeams'

// #1345 (part B1 of #1297): the editor for the required checks of the delivery policy.
vi.mock('../src/features/agent-teams/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../src/features/agent-teams/api')>()),
  updateTeamGithubDeliveryPolicy: vi.fn().mockResolvedValue({}),
  fetchDeliveryPolicyHistory: vi.fn(),
}))

const render = (ui: React.ReactElement) => renderUi(ui, { wrapper: MemoryRouter })
const check = (name: string, app_slug = 'github-actions') => ({ name, app_slug })
const policy = (checks = [check('Unit tests'), check('Lint')], over: Partial<DeliveryPolicy> = {}): DeliveryPolicy => ({
  required_checks: checks, owner_contact: 'native', owner_idle_seconds: 1800, owner_nudge_grace_seconds: 120,
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
const event = (id: number, revision: number, checks: ReturnType<typeof check>[], extra: Partial<DeliveryPolicyEvent> = {}): DeliveryPolicyEvent => ({
  id, scope_id: 2, work_item_id: null, revision, policy: policy(checks), reason: 'Set up', actor: null, created_at: '2026-11-26T08:00:00Z', ...extra,
})
const view = (over: Partial<TeamGithubScope> = {}, refresh = vi.fn().mockResolvedValue(undefined)) =>
  <RepositorySettings scope={scope(over)} autonomyEnabled={false} refresh={refresh} onRemoved={vi.fn()} />

async function openEditor(over: Partial<TeamGithubScope> = {}, refresh = vi.fn().mockResolvedValue(undefined)) {
  const user = userEvent.setup()
  const rendered = render(view(over, refresh))
  await user.click(screen.getByRole('button', { name: 'Edit required checks for example/project' }))
  await screen.findByText('History')
  await flush()
  return { user, refresh, rendered, rerender: (next: Partial<TeamGithubScope>) => rendered.rerender(view(next, refresh)) }
}
const flush = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)) })
const deferred = <T,>() => { let resolve!: (value: T) => void; let reject!: (error: Error) => void; const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej }); return { promise, resolve, reject } }
const name = (n: number) => screen.getByLabelText(`Check ${n} name`)
const app = (n: number) => screen.getByLabelText(`Check ${n} app`)
const save = () => screen.getByRole('button', { name: 'Save' })
const reason = () => screen.getByLabelText('Reason')
const type = async (user: ReturnType<typeof userEvent.setup>, input: HTMLElement, text: string) => { await user.clear(input); if (text) await user.type(input, text) }

beforeEach(() => {
  setOperatorToken('director-key')
  vi.mocked(updateTeamGithubDeliveryPolicy).mockReset().mockResolvedValue({ scope_id: 2, revision: 5, policy: policy() })
  vi.mocked(fetchDeliveryPolicyHistory).mockReset().mockResolvedValue({ events: [event(7, 4, [check('Unit tests'), check('Lint')])] })
})
afterEach(() => clearOperatorToken())

describe('the required checks section', () => {
  it('lists the checks and the policy revision, and says so when none is named', () => {
    const { unmount } = render(view())
    const section = screen.getByRole('region', { name: 'Required checks' })
    expect(within(section).getByText('Unit tests')).toBeInTheDocument()
    expect(within(section).getAllByText(/github-actions/)).toHaveLength(2)
    expect(within(section).getByText('4')).toBeInTheDocument()
    unmount()
    render(view({ delivery_policy: policy([], { broad_checks: 'local' }) }))
    expect(screen.getByText(/None named\. A head with no check runs is not green/)).toBeInTheDocument()
  })

  it('opens the editor with the current checks and the history', async () => {
    await openEditor()
    expect(name(1)).toHaveValue('Unit tests')
    expect(app(2)).toHaveValue('github-actions')
    expect(fetchDeliveryPolicyHistory).toHaveBeenCalledWith(2, 'director-key')
    expect(screen.getByText(/Revision 4/)).toBeInTheDocument()
  })

  it('refuses to edit when the server sends no delivery policy', async () => {
    await openEditor({ delivery_policy: undefined, delivery_policy_revision: undefined })
    expect(screen.getByText(/did not send the delivery policy/)).toBeInTheDocument()
    expect(save()).toBeDisabled()
  })
})

describe('the server rules, shown before a save', () => {
  it('refuses an empty name', async () => {
    const { user } = await openEditor()
    await type(user, name(1), '')
    expect(screen.getByText('Enter a check name.')).toBeInTheDocument()
    await type(user, reason(), 'Why')
    await user.click(save())
    expect(await screen.findByText('Fix the problems in the list first.')).toBeInTheDocument()
    expect(updateTeamGithubDeliveryPolicy).not.toHaveBeenCalled()
  })

  it('refuses an app that is not lowercase letters, digits and hyphens, and an empty app', async () => {
    const { user } = await openEditor()
    await type(user, app(1), 'My App')
    expect(screen.getByText(/lowercase letters, digits and hyphens/)).toBeInTheDocument()
    await type(user, app(1), '')
    expect(screen.getByText('Enter an app.')).toBeInTheDocument()
  })

  it('refuses names and apps that are too long', async () => {
    await openEditor()
    fireEvent.change(name(1), { target: { value: 'x'.repeat(201) } })
    expect(screen.getByText(/at most 200 characters/)).toBeInTheDocument()
    fireEvent.change(name(1), { target: { value: 'Unit tests' } })
    fireEvent.change(app(1), { target: { value: 'a'.repeat(101) } })
    expect(screen.getByText(/at most 100 characters/)).toBeInTheDocument()
  })

  it('judges an app by its characters, not by UTF-16 units', async () => {
    await openEditor()
    fireEvent.change(app(1), { target: { value: '\u{1F9EA}'.repeat(60) } }) // 120 UTF-16 units, 60 characters
    expect(screen.getByText(/lowercase letters, digits and hyphens/)).toBeInTheDocument()
    expect(screen.queryByText(/at most 100 characters/)).not.toBeInTheDocument()
    fireEvent.change(app(1), { target: { value: 'a'.repeat(101) } })
    expect(screen.getByText(/at most 100 characters/)).toBeInTheDocument()
  })

  it('refuses a duplicate name and app, but accepts the same name with another app', async () => {
    const { user } = await openEditor()
    await type(user, name(2), 'Unit tests')
    expect(screen.getByText('This name and app are already in the list.')).toBeInTheDocument()
    await type(user, app(2), 'other-app')
    expect(screen.queryByText('This name and app are already in the list.')).not.toBeInTheDocument()
  })

  it('refuses more than 32 checks and stops the Add button at 32', async () => {
    const many = Array.from({ length: 32 }, (_, index) => check(`Check ${index}`))
    const { user, rerender } = await openEditor({ delivery_policy: policy(many) })
    expect(screen.getByRole('button', { name: 'Add a check' })).toBeDisabled()
    rerender({ delivery_policy: policy([...many, check('One too many')]) })
    expect(screen.getByRole('button', { name: 'Add a check' })).toBeDisabled()
    await type(user, name(1), 'Renamed')
    expect(screen.getByText(/at most 32 required checks/)).toBeInTheDocument()
  })

  it('refuses an empty list while the broad checks run on hosted CI', async () => {
    const { user } = await openEditor({ delivery_policy: policy([check('Only check')]) })
    await user.click(screen.getByRole('button', { name: 'Remove check 1' }))
    expect(screen.getByText(/needs at least one check/)).toBeInTheDocument()
    await type(user, reason(), 'Why')
    await user.click(save())
    expect(await screen.findByText('Fix the problems in the list first.')).toBeInTheDocument()
    expect(updateTeamGithubDeliveryPolicy).not.toHaveBeenCalled()
  })

  it('allows an empty list when the broad checks run locally', async () => {
    const { user } = await openEditor({ delivery_policy: policy([check('Only check')], { broad_checks: 'local' }) })
    await user.click(screen.getByRole('button', { name: 'Remove check 1' }))
    expect(screen.queryByText(/needs at least one check/)).not.toBeInTheDocument()
    expect(screen.getByText(/No required checks\. A head with no check runs is not green/)).toBeInTheDocument()
  })

  it('needs a reason and a change', async () => {
    const { user } = await openEditor()
    await user.click(save()) // nothing was touched: the Save button waits for a change
    expect(updateTeamGithubDeliveryPolicy).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Move check 2 up' }))
    await user.click(save())
    expect(await screen.findByText('Enter a reason for the change.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Move check 1 down' })) // back to the saved order
    await type(user, reason(), 'Why')
    await user.click(save())
    expect(await screen.findByText('No change to save.')).toBeInTheDocument()
    expect(updateTeamGithubDeliveryPolicy).not.toHaveBeenCalled()
  })
})

describe('saving', () => {
  it('sends the whole policy with only the list changed, the revision and the reason', async () => {
    const { user, refresh } = await openEditor()
    await user.click(screen.getByRole('button', { name: 'Add a check' }))
    await type(user, name(3), '  End-to-end tests ')
    await user.click(screen.getByRole('button', { name: 'Move check 3 up' }))
    await user.click(screen.getByRole('button', { name: 'Remove check 3' })) // the old second check is now last
    await type(user, reason(), 'Add the end-to-end job')
    vi.mocked(fetchDeliveryPolicyHistory).mockResolvedValue({ events: [
      event(8, 5, [check('Unit tests'), check('End-to-end tests')], { reason: 'Add the end-to-end job' }), event(7, 4, [check('Unit tests'), check('Lint')])] })
    await user.click(save())
    await waitFor(() => expect(updateTeamGithubDeliveryPolicy).toHaveBeenCalledTimes(1))
    expect(updateTeamGithubDeliveryPolicy).toHaveBeenCalledWith(2, {
      expected_revision: 4,
      policy: policy([check('Unit tests'), check('End-to-end tests')]),
      reason: 'Add the end-to-end job',
    }, 'director-key')
    expect(refresh).toHaveBeenCalled()
    // a saved change appears in the history, with what it added and removed
    expect(await screen.findByText('Added: End-to-end tests (github-actions)')).toBeInTheDocument()
    expect(screen.getByText('Removed: Lint (github-actions)')).toBeInTheDocument()
    expect(fetchDeliveryPolicyHistory).toHaveBeenCalledTimes(2)
  })

  it('sends a reordered list in its new order', async () => {
    const { user } = await openEditor()
    await user.click(screen.getByRole('button', { name: 'Move check 2 up' }))
    await type(user, reason(), 'Lint first')
    await user.click(save())
    await waitFor(() => expect(updateTeamGithubDeliveryPolicy).toHaveBeenCalled())
    expect(vi.mocked(updateTeamGithubDeliveryPolicy).mock.calls[0][1].policy.required_checks).toEqual([check('Lint'), check('Unit tests')])
  })

  it('sends one request for two clicks, and locks the fields while it runs', async () => {
    const pending = deferred<{ scope_id: number; revision: number; policy: DeliveryPolicy }>()
    vi.mocked(updateTeamGithubDeliveryPolicy).mockReturnValue(pending.promise)
    const { user } = await openEditor()
    await user.click(screen.getByRole('button', { name: 'Remove check 2' }))
    await type(user, reason(), 'Drop lint')
    await user.dblClick(save())
    expect(updateTeamGithubDeliveryPolicy).toHaveBeenCalledTimes(1)
    expect(name(1)).toBeDisabled()
    expect(reason()).toBeDisabled()
    await act(async () => { pending.resolve({ scope_id: 2, revision: 5, policy: policy() }) })
  })

  it('keeps an unsaved draft when the page refreshes the repository in the background', async () => {
    const { user, rerender } = await openEditor()
    await type(user, name(1), 'Renamed tests')
    rerender({ delivery_policy: policy(), delivery_policy_revision: 4, updated_at: '2026-11-27T10:00:00Z' })
    expect(name(1)).toHaveValue('Renamed tests')
  })

  it('shows a stale revision and keeps nothing saved when the policy changed under the draft', async () => {
    const { user, rerender } = await openEditor()
    await type(user, name(1), 'Renamed tests')
    rerender({ delivery_policy: policy([check('Other')]), delivery_policy_revision: 5 })
    expect(screen.getByText(/The policy changed on the server \(revision 4 to 5\)/)).toBeInTheDocument()
    expect(save()).toBeDisabled()
    expect(name(1)).toHaveValue('Renamed tests') // the draft stays visible until it is discarded
    await user.click(screen.getByRole('button', { name: 'Discard my changes' }))
    expect(name(1)).toHaveValue('Other')
    expect(updateTeamGithubDeliveryPolicy).not.toHaveBeenCalled()
  })

  it('explains a 409 from the server and reads the repository again', async () => {
    vi.mocked(updateTeamGithubDeliveryPolicy).mockRejectedValue(new ApiHttpError('delivery_policy_revision_changed', 409))
    const { user, refresh } = await openEditor()
    await user.click(screen.getByRole('button', { name: 'Remove check 2' }))
    await type(user, reason(), 'Drop lint')
    await user.click(save())
    expect(await screen.findByText(/the policy changed on the server, so nothing was saved/i)).toBeInTheDocument()
    await waitFor(() => expect(refresh).toHaveBeenCalled())
    expect(name(1)).toBeEnabled() // the dialog is usable again
  })

  it('shows another server error as it is', async () => {
    vi.mocked(updateTeamGithubDeliveryPolicy).mockRejectedValue(new Error('Hosted broad checks need required check identities.'))
    const { user } = await openEditor()
    await user.click(screen.getByRole('button', { name: 'Remove check 2' }))
    await type(user, reason(), 'Drop lint')
    await user.click(save())
    expect(await screen.findByText('Hosted broad checks need required check identities.')).toBeInTheDocument()
  })

  it('does not let a save from an older open of the dialog change the new one', async () => {
    const pending = deferred<{ scope_id: number; revision: number; policy: DeliveryPolicy }>()
    vi.mocked(updateTeamGithubDeliveryPolicy).mockReturnValue(pending.promise)
    const { user } = await openEditor()
    await user.click(screen.getByRole('button', { name: 'Remove check 2' }))
    await type(user, reason(), 'Drop lint')
    await user.click(save())
    await user.keyboard('{Escape}')
    await user.click(screen.getByRole('button', { name: 'Edit required checks for example/project' }))
    await screen.findByText('History')
    await flush()
    await type(user, name(1), 'Fresh draft')
    await act(async () => { pending.resolve({ scope_id: 2, revision: 5, policy: policy() }) })
    await flush()
    expect(name(1)).toHaveValue('Fresh draft')
    expect(save()).toBeEnabled()
  })
})

describe('the history', () => {
  it('shows each change from the one before, an order change, an unchanged list and an attempt adoption', async () => {
    vi.mocked(fetchDeliveryPolicyHistory).mockResolvedValue({ events: [
      event(12, 8, [check('Lint'), check('Unit tests')], { reason: 'Reorder', actor: 'director' }),
      event(11, 7, [check('Unit tests'), check('Lint')], { work_item_id: 99, reason: 'Adopted' }),
      event(10, 6, [check('Unit tests'), check('Lint')], { reason: 'Other field' }),
      event(9, 5, [check('Unit tests')], { reason: 'Start' }),
    ] })
    await openEditor()
    expect(screen.getByText('Order changed')).toBeInTheDocument()
    expect(screen.getByText('Applied to attempt 99')).toBeInTheDocument()
    expect(screen.getByText('Added: Lint (github-actions)')).toBeInTheDocument()
    expect(screen.getByText('Required checks: Unit tests (github-actions)')).toBeInTheDocument()
    expect(screen.getByText(/director/)).toBeInTheDocument()
  })

  it('says that nothing was recorded only after a successful read', async () => {
    vi.mocked(fetchDeliveryPolicyHistory).mockResolvedValue({ events: [] })
    await openEditor()
    expect(screen.getByText('No changes recorded yet.')).toBeInTheDocument()
  })

  it('says that the history is not available when the read fails, and does not call it empty', async () => {
    vi.mocked(fetchDeliveryPolicyHistory).mockRejectedValue(new Error('boom'))
    await openEditor()
    expect(screen.getByText('The history is not available.')).toBeInTheDocument()
    expect(screen.queryByText('No changes recorded yet.')).not.toBeInTheDocument()
    expect(screen.getByText(/Could not read the history: boom/)).toBeInTheDocument()
    expect(name(1)).toBeEnabled() // the list can still be edited
  })
})

describe('answers that arrive late or out of order (V1 review 106)', () => {
  it('keeps the confirmed list and revision when the repository read still shows the old policy', async () => {
    vi.mocked(updateTeamGithubDeliveryPolicy).mockResolvedValue({ scope_id: 2, revision: 5, policy: policy([check('Unit tests')]) })
    const { user, refresh, rerender } = await openEditor() // revision 4: Unit tests, Lint
    await user.click(screen.getByRole('button', { name: 'Remove check 2' }))
    await type(user, reason(), 'Drop lint')
    await user.click(save())
    await waitFor(() => expect(refresh).toHaveBeenCalled())
    // the refresh answered with the old scope, so the dialog still gets revision 4 and the removed check
    expect(name(1)).toHaveValue('Unit tests')
    expect(screen.queryByLabelText('Check 2 name')).not.toBeInTheDocument()
    // the next edit starts from the confirmed revision, not from the old one
    await user.click(screen.getByRole('button', { name: 'Add a check' }))
    await type(user, name(2), 'End-to-end tests')
    await type(user, reason(), 'Add e2e')
    await user.click(save())
    await waitFor(() => expect(updateTeamGithubDeliveryPolicy).toHaveBeenCalledTimes(2))
    expect(vi.mocked(updateTeamGithubDeliveryPolicy).mock.calls[1][1].expected_revision).toBe(5)
    // when the repository read catches up, the repository's own policy is used again
    rerender({ delivery_policy: policy([check('Unit tests'), check('End-to-end tests')]), delivery_policy_revision: 6 })
    expect(name(2)).toHaveValue('End-to-end tests')
  })

  it('keeps a draft blocked after a 409 while the repository read still shows the old revision', async () => {
    vi.mocked(updateTeamGithubDeliveryPolicy).mockRejectedValue(new ApiHttpError('delivery_policy_revision_changed', 409))
    const { user, refresh } = await openEditor() // the mocked refresh changes nothing: revision 4 stays
    await user.click(screen.getByRole('button', { name: 'Remove check 2' }))
    await type(user, reason(), 'Drop lint')
    await user.click(save())
    expect(await screen.findByText(/The server refused the save because the policy changed/)).toBeInTheDocument()
    await waitFor(() => expect(refresh).toHaveBeenCalled())
    expect(save()).toBeDisabled()
    expect(screen.queryByLabelText('Check 2 name')).not.toBeInTheDocument() // the draft is still there
    await user.click(screen.getByRole('button', { name: 'Discard my changes' }))
    expect(name(2)).toHaveValue('Lint')
    expect(screen.queryByText(/The server refused the save/)).not.toBeInTheDocument()
    // a new draft is not blocked by the old conflict
    await type(user, name(1), 'Renamed tests')
    await type(user, reason(), 'Rename')
    expect(save()).toBeEnabled()
    expect(screen.queryByRole('button', { name: 'Discard my changes' })).not.toBeInTheDocument()
  })

  it('does not carry a conflict into the next opening of the dialog', async () => {
    vi.mocked(updateTeamGithubDeliveryPolicy).mockRejectedValue(new ApiHttpError('delivery_policy_revision_changed', 409))
    const { user } = await openEditor()
    await user.click(screen.getByRole('button', { name: 'Remove check 2' }))
    await type(user, reason(), 'Drop lint')
    await user.click(save())
    await screen.findByRole('button', { name: 'Discard my changes' })
    await user.keyboard('{Escape}')
    await user.click(screen.getByRole('button', { name: 'Edit required checks for example/project' }))
    await screen.findByText('History')
    await flush()
    await type(user, name(1), 'Renamed tests')
    await type(user, reason(), 'Rename')
    expect(save()).toBeEnabled()
  })

  it('keeps the confirmed list when the dialog is closed and opened again before the read catches up', async () => {
    vi.mocked(updateTeamGithubDeliveryPolicy).mockResolvedValue({ scope_id: 2, revision: 5, policy: policy([check('Unit tests')]) })
    const { user, refresh } = await openEditor()
    await user.click(screen.getByRole('button', { name: 'Remove check 2' }))
    await type(user, reason(), 'Drop lint')
    await user.click(save())
    await waitFor(() => expect(refresh).toHaveBeenCalled())
    await flush()
    await user.keyboard('{Escape}')
    await user.click(screen.getByRole('button', { name: 'Edit required checks for example/project' }))
    await screen.findByText('History')
    await flush()
    expect(screen.queryByLabelText('Check 2 name')).not.toBeInTheDocument()
  })

  it('keeps the draft blocked after a 409 when the refresh fails too', async () => {
    vi.mocked(updateTeamGithubDeliveryPolicy).mockRejectedValue(new ApiHttpError('delivery_policy_revision_changed', 409))
    const { user, refresh } = await openEditor({}, vi.fn().mockRejectedValue(new Error('offline')))
    await user.click(screen.getByRole('button', { name: 'Remove check 2' }))
    await type(user, reason(), 'Drop lint')
    await user.click(save())
    expect(await screen.findByRole('button', { name: 'Discard my changes' })).toBeInTheDocument()
    await waitFor(() => expect(refresh).toHaveBeenCalled())
    expect(save()).toBeDisabled()
  })
})

describe('the length limits count characters, as the server does', () => {
  const tubes = (count: number) => '\u{1F9EA}'.repeat(count) // one code point, two UTF-16 units

  it('accepts a check name of 200 emoji and refuses 201', async () => {
    await openEditor()
    fireEvent.change(name(1), { target: { value: tubes(101) } }) // 202 UTF-16 units, 101 characters
    expect(screen.queryByText(/at most 200 characters/)).not.toBeInTheDocument()
    fireEvent.change(name(1), { target: { value: tubes(200) } })
    expect(screen.queryByText(/at most 200 characters/)).not.toBeInTheDocument()
    fireEvent.change(name(1), { target: { value: tubes(201) } })
    expect(screen.getByText(/at most 200 characters/)).toBeInTheDocument()
  })

  it('accepts a reason of 1000 emoji, sends it whole, and refuses 1001', async () => {
    const { user } = await openEditor()
    await user.click(screen.getByRole('button', { name: 'Remove check 2' }))
    fireEvent.change(reason(), { target: { value: tubes(1001) } })
    expect(screen.getByText(/The reason has at most 1000 characters/)).toBeInTheDocument()
    await user.click(save())
    expect(await screen.findAllByText(/The reason has at most 1000 characters/)).not.toHaveLength(0)
    expect(updateTeamGithubDeliveryPolicy).not.toHaveBeenCalled()
    fireEvent.change(reason(), { target: { value: tubes(1000) } })
    expect(screen.getByText('The reason is required. It is kept in the history with the new list.')).toBeInTheDocument() // the live message is back to normal
    await user.click(save())
    await waitFor(() => expect(updateTeamGithubDeliveryPolicy).toHaveBeenCalledTimes(1))
    expect(vi.mocked(updateTeamGithubDeliveryPolicy).mock.calls[0][1].reason).toBe(tubes(1000))
  })
})
