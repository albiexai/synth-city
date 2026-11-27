import { MemoryRouter } from 'react-router-dom'
import { act, render as renderUi, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { RepositorySettings } from '../src/features/agent-teams/RepositorySettings'
import { clearOperatorToken, setOperatorToken } from '../src/features/agent-teams/operatorAuth'
import { fetchScopeSettings, updateTeamGithubScope } from '../src/features/agent-teams/api'
import type { TeamGithubScope } from '../src/types/agentTeams'

// #1297 part A: the editors for the review hold label and the effort ceiling, with the server's ranges and history.
vi.mock('../src/features/agent-teams/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../src/features/agent-teams/api')>()),
  updateTeamGithubScope: vi.fn().mockResolvedValue({}),
  fetchScopeSettings: vi.fn(),
}))

const render = (ui: React.ReactElement) => renderUi(ui, { wrapper: MemoryRouter })
const scope = {
  id: 2, preset_id: 1, repo_owner: 'example', repo_name: 'project', repo_path: '/tmp/project',
  dispatch_label: 'ready', design_label: 'design', merge_policy: 'human', github_auth_mode: 'app',
  github_auth_configured: true, github_poll_token_configured: true, max_approval_rounds: 3,
  max_concurrent_dispatched: 3, max_verification_retries: 2, max_auto_merges_per_day: 5, base_ref: 'origin/HEAD',
  builds_out_of_tree: false, max_build_parallelism: 4, continuation_enabled: false, max_continuation_revisions: 3,
  max_continuation_failed_heads: 8, max_failed_heads_per_revision: 1, max_scope_paths: 4, max_scope_commands: 8,
  enabled: true, created_at: '2026-09-29T12:00:00Z', updated_at: '2026-09-29T12:00:00Z',
  review_hold_label: 'review-hold', max_item_effort_minutes: 120,
} as TeamGithubScope
// A range that differs from the backend's 15 to 1440, so a pass proves the range comes from the server.
const definitions = {
  review_hold_label: { type: 'string' },
  max_item_effort_minutes: { type: 'integer', minimum: 20, maximum: 600, default: 90 },
  initial_paths_required: { type: 'boolean', default: false },
  max_scope_amendments: { type: 'integer', minimum: 0, maximum: 32, default: 3 },
  max_scope_amendment_requests: { type: 'integer', minimum: 0, maximum: 128, default: 8 },
  max_amendment_added_paths: { type: 'integer', minimum: 0, maximum: 256, default: 16 },
  max_companion_paths: { type: 'integer', minimum: 0, maximum: 128, default: 32 },
}
const entry = (id: number, changes: Record<string, { before: unknown; after: unknown }>, extra = {}) => ({
  event_id: id, occurred_at: '2026-11-26T08:00:00Z', actor_kind: 'operator', actor_reference: 'director', via: null,
  reason: 'Raised for the audit', changes, redacted_fields: [], ...extra,
})
const page = (history: ReturnType<typeof entry>[], next: number | null = null) => ({ definitions, required: [], history, next_before_id: next })

async function openEditor(overrides: Partial<TeamGithubScope> = {}, refresh = vi.fn().mockResolvedValue(undefined)) {
  const user = userEvent.setup()
  const view = render(<RepositorySettings scope={{ ...scope, ...overrides }} autonomyEnabled={false} refresh={refresh} onRemoved={vi.fn()} />)
  await user.click(screen.getByRole('button', { name: 'Edit review hold and effort limit for example/project' }))
  await screen.findByText(/20 to 600 minutes/)
  return { user, refresh, view }
}
const deferred = <T,>() => { let resolve!: (value: T) => void; let reject!: (error: Error) => void; const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej }); return { promise, resolve, reject } }
const withScope = (over: Partial<TeamGithubScope>) => <RepositorySettings scope={{ ...scope, ...over }} autonomyEnabled={false} refresh={vi.fn()} onRemoved={vi.fn()} />
const flush = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)) })
const effort = () => screen.getByLabelText('Effort ceiling (minutes)')
const save = () => screen.getByRole('button', { name: 'Save' })

beforeEach(() => {
  setOperatorToken('director-key')
  vi.mocked(updateTeamGithubScope).mockClear()
  vi.mocked(fetchScopeSettings).mockReset().mockResolvedValue(page([entry(7, { max_item_effort_minutes: { before: 60, after: 120 } })]))
})
afterEach(() => clearOperatorToken())

describe('the review hold and effort limit editors', () => {
  it('shows the current values in the repository settings, and "none" for a cleared label', () => {
    const view = render(<RepositorySettings scope={scope} autonomyEnabled={false} refresh={vi.fn()} onRemoved={vi.fn()} />)
    const section = screen.getByRole('region', { name: 'Review hold and effort limit' })
    expect(within(section).getByText('review-hold')).toBeInTheDocument()
    expect(within(section).getByText('120 minutes')).toBeInTheDocument()
    view.rerender(<RepositorySettings scope={{ ...scope, review_hold_label: null }} autonomyEnabled={false} refresh={vi.fn()} onRemoved={vi.fn()} />)
    expect(within(screen.getByRole('region', { name: 'Review hold and effort limit' })).getByText('No hold label')).toBeInTheDocument()
  })

  it('reads the ranges and the history from the server with the Director key', async () => {
    await openEditor()
    expect(fetchScopeSettings).toHaveBeenCalledWith(2, 'director-key', undefined)
    expect(screen.getByText(/20 to 600 minutes/)).toHaveTextContent('default 90')
    expect(screen.getByText('Effort ceiling: 60 → 120')).toBeInTheDocument()
    expect(screen.getByText(/director/)).toBeInTheDocument()
    expect(screen.getByText('Raised for the audit')).toBeInTheDocument()
  })

  it.each(['19', '601', '30.5', 'abc', ''])('refuses the effort value "%s" and sends no request', async (value) => {
    const { user } = await openEditor()
    await user.clear(effort())
    if (value) await user.type(effort(), value)
    await user.click(save())
    expect(await screen.findByRole('alert')).toHaveTextContent(/whole number of minutes from 20 to 600/)
    expect(updateTeamGithubScope).not.toHaveBeenCalled()
  })

  it('accepts the ends of the server range', async () => {
    const { user } = await openEditor()
    for (const value of ['20', '600']) {
      await user.clear(effort()); await user.type(effort(), value); await user.click(save())
      await waitFor(() => expect(updateTeamGithubScope).toHaveBeenLastCalledWith(2, expect.objectContaining({ max_item_effort_minutes: Number(value) }), 'director-key'))
    }
  })

  it('refuses an empty or blank label, and clearing the label sends an explicit null', async () => {
    const { user } = await openEditor()
    await user.clear(screen.getByLabelText('Review hold label'))
    await user.click(save())
    expect(await screen.findByRole('alert')).toHaveTextContent(/Enter a label, or choose to clear it/)
    await user.type(screen.getByLabelText('Review hold label'), '   ')
    await user.click(save())
    expect(updateTeamGithubScope).not.toHaveBeenCalled()
    await user.click(screen.getByLabelText('No hold label (clear it)'))
    await user.click(save())
    await waitFor(() => expect(updateTeamGithubScope).toHaveBeenCalledWith(2, { review_hold_label: null }, 'director-key'))
  })

  it('sends only the changed fields and the reason', async () => {
    const { user } = await openEditor()
    await user.clear(effort()); await user.type(effort(), '90')
    await user.type(screen.getByLabelText('Reason (optional)'), 'Shorter items')
    await user.click(save())
    await waitFor(() => expect(updateTeamGithubScope).toHaveBeenCalledWith(2, { max_item_effort_minutes: 90, reason: 'Shorter items' }, 'director-key'))
  })

  it('shows the effect of each safety bound increase before requiring typed confirmation', async () => {
    const { user } = await openEditor()
    const amendments = screen.getByLabelText('Approved amendments')
    const companions = screen.getByLabelText('Companion paths')
    await user.clear(amendments); await user.type(amendments, '4')
    await user.clear(companions); await user.type(companions, '40')
    expect(screen.getByText(/Allows up to 4 approved amendments per attempt/)).toBeInTheDocument()
    expect(screen.getByText(/Allows up to 40 companion paths per amendment/)).toBeInTheDocument()
    await user.click(save())
    expect(updateTeamGithubScope).not.toHaveBeenCalled()
    await user.type(screen.getByLabelText('Type safety_bounds to confirm'), 'safety_bounds')
    await user.click(save())
    await waitFor(() => expect(updateTeamGithubScope).toHaveBeenCalledWith(2,
      { max_scope_amendments: 4, max_companion_paths: 40, confirm: 'safety_bounds' }, 'director-key'))
  })

  it('saves a lower safety bound without a confirmation', async () => {
    const { user } = await openEditor({ max_scope_amendment_requests: 5 })
    const requests = screen.getByLabelText('Amendment requests')
    expect(requests).toHaveValue('5')
    await user.clear(requests); await user.type(requests, '0')
    expect(screen.queryByLabelText('Type safety_bounds to confirm')).not.toBeInTheDocument()
    await user.click(save())
    await waitFor(() => expect(updateTeamGithubScope).toHaveBeenCalledWith(2,
      { max_scope_amendment_requests: 0 }, 'director-key'))
  })

  it('edits the first-plan path requirement and shows it in history', async () => {
    const { user } = await openEditor()
    const required = screen.getByRole('checkbox', { name: 'Require paths in the first plan' })
    expect(required).not.toBeChecked()
    vi.mocked(fetchScopeSettings).mockResolvedValue(page([
      entry(9, { initial_paths_required: { before: false, after: true } }),
    ]))
    await user.click(required)
    await user.click(save())
    expect(await screen.findByText('First-plan paths required: false → true')).toBeInTheDocument()
    expect(updateTeamGithubScope).toHaveBeenCalledWith(2, { initial_paths_required: true }, 'director-key')
  })

  it('sends nothing when no value changed, and says so instead of keeping an old refusal on screen', async () => {
    const { user } = await openEditor()
    await user.clear(effort()); await user.type(effort(), '19'); await user.click(save())
    expect(await screen.findByRole('alert')).toHaveTextContent(/from 20 to 600/)
    await user.clear(effort()); await user.type(effort(), '120'); await user.click(save())
    expect(await screen.findByRole('alert')).toHaveTextContent('No value changed.')
    expect(screen.queryByText(/from 20 to 600/)).not.toBeInTheDocument()
    expect(updateTeamGithubScope).not.toHaveBeenCalled()
  })

  it('removes an old refusal as soon as a valid save starts', async () => {
    vi.mocked(updateTeamGithubScope).mockReturnValueOnce(new Promise(() => undefined)) // a save that is still pending
    const { user } = await openEditor()
    await user.clear(effort()); await user.type(effort(), '19'); await user.click(save())
    expect(await screen.findByRole('alert')).toHaveTextContent(/from 20 to 600/)
    await user.clear(effort()); await user.type(effort(), '90'); await user.click(save())
    await waitFor(() => expect(updateTeamGithubScope).toHaveBeenCalled())
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('shows a saved change in the history, after the settings refresh', async () => {
    const { user, refresh } = await openEditor()
    vi.mocked(fetchScopeSettings).mockResolvedValue(page([
      entry(9, { max_item_effort_minutes: { before: 120, after: 90 } }, { reason: 'Shorter items' }),
      entry(7, { max_item_effort_minutes: { before: 60, after: 120 } }),
    ]))
    await user.clear(effort()); await user.type(effort(), '90'); await user.type(screen.getByLabelText('Reason (optional)'), 'Shorter items'); await user.click(save())
    expect(await screen.findByText('Effort ceiling: 120 → 90')).toBeInTheDocument()
    expect(refresh).toHaveBeenCalled()
    expect(screen.getByLabelText('Reason (optional)')).toHaveValue('')
    expect(fetchScopeSettings).toHaveBeenCalledTimes(2)
  })

  it('pages the history with the cursor, and shows hidden values as hidden', async () => {
    vi.mocked(fetchScopeSettings).mockResolvedValueOnce(page([entry(7, {}, { redacted_fields: ['build_command_hint'], reason: null })], 7))
    const { user } = await openEditor()
    expect(screen.getByText('build_command_hint: changed, value hidden')).toBeInTheDocument()
    vi.mocked(fetchScopeSettings).mockResolvedValueOnce(page([entry(3, { review_hold_label: { before: null, after: 'review-hold' } })]))
    await user.click(screen.getByRole('button', { name: 'Load more history' }))
    expect(await screen.findByText('Review hold label: none → review-hold')).toBeInTheDocument()
    expect(fetchScopeSettings).toHaveBeenLastCalledWith(2, 'director-key', 7)
    expect(screen.queryByRole('button', { name: 'Load more history' })).not.toBeInTheDocument()
  })

  it('shows a server refusal as text and keeps the form', async () => {
    vi.mocked(updateTeamGithubScope).mockRejectedValueOnce(new Error('max_item_effort_minutes must be at most 600'))
    const { user } = await openEditor()
    await user.clear(effort()); await user.type(effort(), '90'); await user.click(save())
    expect(await screen.findByRole('alert')).toHaveTextContent('max_item_effort_minutes must be at most 600')
    expect(effort()).toHaveValue('90')
  })

  it('does not save when the server ranges could not be read', async () => {
    vi.mocked(fetchScopeSettings).mockRejectedValue(new Error('settings unavailable'))
    const user = userEvent.setup()
    render(<RepositorySettings scope={scope} autonomyEnabled={false} refresh={vi.fn()} onRemoved={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: 'Edit review hold and effort limit for example/project' }))
    expect(await screen.findByText(/settings unavailable/)).toBeInTheDocument()
    expect(save()).toBeDisabled()
    expect(screen.queryByText('No changes recorded yet.')).not.toBeInTheDocument()
    expect(screen.getByText('The history is not available.')).toBeInTheDocument()
  })

  it('keeps an unsaved ceiling and reason when the settings refresh in the background, and lets untouched fields follow the server', async () => {
    const { user, view } = await openEditor()
    await user.clear(effort()); await user.type(effort(), '222'); await user.type(screen.getByLabelText('Reason (optional)'), 'Audit')
    view.rerender(withScope({ review_hold_label: 'other-label', max_item_effort_minutes: 120 }))
    await flush()
    expect(effort()).toHaveValue('222')
    expect(screen.getByLabelText('Reason (optional)')).toHaveValue('Audit')
    expect(screen.getByLabelText('Review hold label')).toHaveValue('other-label')
    await user.clear(effort()); await user.type(effort(), '90'); await user.click(save())
    await waitFor(() => expect(updateTeamGithubScope).toHaveBeenCalledWith(2, { max_item_effort_minutes: 90, reason: 'Audit' }, 'director-key'))
  })

  it('keeps an edited label over a background change of the same field', async () => {
    const { user, view } = await openEditor()
    await user.clear(screen.getByLabelText('Review hold label')); await user.type(screen.getByLabelText('Review hold label'), 'mine')
    view.rerender(withScope({ review_hold_label: 'theirs' }))
    await flush()
    expect(screen.getByLabelText('Review hold label')).toHaveValue('mine')
  })

  it('loads one history page for each cursor, even after a double click, and shows each event once', async () => {
    vi.mocked(fetchScopeSettings).mockResolvedValueOnce(page([entry(7, { max_item_effort_minutes: { before: 60, after: 120 } })], 7))
    const { user } = await openEditor()
    const slow = deferred<ReturnType<typeof page>>()
    vi.mocked(fetchScopeSettings).mockReturnValueOnce(slow.promise)
    await user.dblClick(screen.getByRole('button', { name: 'Load more history' }))
    expect(fetchScopeSettings).toHaveBeenCalledTimes(2)
    slow.resolve(page([entry(7, { max_item_effort_minutes: { before: 60, after: 120 } }), entry(3, { max_item_effort_minutes: { before: 30, after: 60 } })]))
    expect(await screen.findByText('Effort ceiling: 30 → 60')).toBeInTheDocument()
    expect(screen.getAllByText('Effort ceiling: 60 → 120')).toHaveLength(1)
  })

  it('discards a history page that arrives after a newer load', async () => {
    vi.mocked(fetchScopeSettings).mockResolvedValueOnce(page([entry(7, { max_item_effort_minutes: { before: 60, after: 120 } })], 7))
    const { user } = await openEditor()
    const old = deferred<ReturnType<typeof page>>()
    vi.mocked(fetchScopeSettings).mockReturnValueOnce(old.promise)
    await user.click(screen.getByRole('button', { name: 'Load more history' }))
    vi.mocked(fetchScopeSettings).mockResolvedValue(page([entry(9, { max_item_effort_minutes: { before: 120, after: 90 } })]))
    await user.clear(effort()); await user.type(effort(), '90'); await user.click(save())
    expect(await screen.findByText('Effort ceiling: 120 → 90')).toBeInTheDocument()
    old.resolve(page([entry(3, { max_item_effort_minutes: { before: 30, after: 60 } })]))
    await flush()
    expect(screen.queryByText('Effort ceiling: 30 → 60')).not.toBeInTheDocument()
  })

  it('says "No changes recorded yet." only after a successful read that found no entries', async () => {
    vi.mocked(fetchScopeSettings).mockResolvedValue(page([]))
    await openEditor()
    expect(screen.getByText('No changes recorded yet.')).toBeInTheDocument()
  })

  it('keeps a typed replacement label when another client clears the stored label, and saves it', async () => {
    const { user, view } = await openEditor()
    await user.clear(screen.getByLabelText('Review hold label')); await user.type(screen.getByLabelText('Review hold label'), 'mine')
    view.rerender(withScope({ review_hold_label: null }))
    await flush()
    expect(screen.getByLabelText('Review hold label')).toBeEnabled()
    expect(screen.getByLabelText('Review hold label')).toHaveValue('mine')
    expect(screen.getByRole('checkbox', { name: 'No hold label (clear it)' })).not.toBeChecked()
    await user.click(save())
    await waitFor(() => expect(updateTeamGithubScope).toHaveBeenCalledWith(2, { review_hold_label: 'mine' }, 'director-key'))
  })

  it('locks the fields while a save is pending, and unlocks them when it ends', async () => {
    const slow = deferred<object>()
    vi.mocked(updateTeamGithubScope).mockReturnValueOnce(slow.promise as never)
    const { user } = await openEditor()
    await user.clear(effort()); await user.type(effort(), '90'); await user.click(save())
    await waitFor(() => expect(updateTeamGithubScope).toHaveBeenCalledTimes(1))
    for (const field of [effort(), screen.getByLabelText('Review hold label'), screen.getByLabelText('Reason (optional)'), screen.getByRole('checkbox', { name: 'No hold label (clear it)' })]) expect(field).toBeDisabled()
    slow.resolve({})
    await waitFor(() => expect(effort()).toBeEnabled())
  })

  it('does not let a save that ends after the dialog was reopened touch the new draft or the history', async () => {
    const slow = deferred<object>()
    vi.mocked(updateTeamGithubScope).mockReturnValueOnce(slow.promise as never)
    const { user } = await openEditor()
    await user.clear(effort()); await user.type(effort(), '90'); await user.click(save())
    await waitFor(() => expect(updateTeamGithubScope).toHaveBeenCalledTimes(1))
    await user.keyboard('{Escape}')
    await user.click(screen.getByRole('button', { name: 'Edit review hold and effort limit for example/project' }))
    await screen.findByText(/20 to 600 minutes/)
    await user.type(screen.getByLabelText('Reason (optional)'), 'second draft')
    slow.resolve({})
    await flush()
    expect(screen.getByLabelText('Reason (optional)')).toHaveValue('second draft')
    expect(fetchScopeSettings).toHaveBeenCalledTimes(2)
  })

  it('forgets the old history cursor on reopen, and offers more history only after that read succeeded', async () => {
    vi.mocked(fetchScopeSettings).mockResolvedValueOnce(page([entry(7, { max_item_effort_minutes: { before: 60, after: 120 } })], 7))
    const { user } = await openEditor()
    expect(screen.getByRole('button', { name: 'Load more history' })).toBeInTheDocument()
    await user.keyboard('{Escape}')
    vi.mocked(fetchScopeSettings).mockRejectedValueOnce(new Error('history unavailable'))
    await user.click(screen.getByRole('button', { name: 'Edit review hold and effort limit for example/project' }))
    expect(await screen.findByText(/history unavailable/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Load more history' })).not.toBeInTheDocument()
  })

  it('does not show the error of a save from an older session in the new one', async () => {
    const old = deferred<object>()
    vi.mocked(updateTeamGithubScope).mockReturnValueOnce(old.promise as never)
    const { user } = await openEditor()
    await user.clear(effort()); await user.type(effort(), '90'); await user.click(save())
    await waitFor(() => expect(updateTeamGithubScope).toHaveBeenCalledTimes(1))
    await user.keyboard('{Escape}')
    await user.click(screen.getByRole('button', { name: 'Edit review hold and effort limit for example/project' }))
    await screen.findByText(/20 to 600 minutes/)
    await act(async () => { old.reject(new Error('old save failed')) })
    await flush()
    expect(screen.queryByText(/old save failed/)).not.toBeInTheDocument()
  })

  it('keeps the new session locked while its own save runs, when an older save ends first', async () => {
    const old = deferred<object>(), current = deferred<object>()
    vi.mocked(updateTeamGithubScope).mockReturnValueOnce(old.promise as never).mockReturnValueOnce(current.promise as never)
    const { user } = await openEditor()
    await user.clear(effort()); await user.type(effort(), '90'); await user.click(save())
    await waitFor(() => expect(updateTeamGithubScope).toHaveBeenCalledTimes(1))
    await user.keyboard('{Escape}')
    await user.click(screen.getByRole('button', { name: 'Edit review hold and effort limit for example/project' }))
    await screen.findByText(/20 to 600 minutes/)
    await user.clear(effort()); await user.type(effort(), '95'); await user.click(save())
    await waitFor(() => expect(updateTeamGithubScope).toHaveBeenCalledTimes(2))
    old.resolve({})
    await flush()
    expect(effort()).toBeDisabled()
    current.resolve({})
    await waitFor(() => expect(effort()).toBeEnabled())
  })
})

