import { useState } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { act, cleanup, render as renderUi, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SupervisorBindingDialog } from '../src/features/agent-teams/SupervisorBindingDialog'
import { TeamHeader } from '../src/features/agent-teams/TeamHeader'
import { clearOperatorToken, setOperatorToken } from '../src/features/agent-teams/operatorAuth'
import { updateTeamSupervisor } from '../src/features/agent-teams/api'
import { fetchAuditEvents } from '../src/features/factory/auditApi'
import { resetFactoryReads } from '../src/features/factory/reads'
import type { AgentTeamPreset, SupervisorObservation } from '../src/types/agentTeams'
import { fixtureFetch, jsonResponse, settle } from './helpers/factory'

// #1298: bind, change the grace and remove the supervisor of a team, with the history of each change.
vi.mock('../src/features/agent-teams/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../src/features/agent-teams/api')>()),
  updateTeamSupervisor: vi.fn(),
}))
vi.mock('../src/features/factory/auditApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../src/features/factory/auditApi')>()),
  fetchAuditEvents: vi.fn(),
}))

const none: SupervisorObservation = { status: 'none', actor_id: null, address: null, checked_at: null, next_check_at: null, grace_seconds: 1800, binding_version: 4, note: null, attention: null }
const bound: SupervisorObservation = { ...none, status: 'healthy', actor_id: 7, address: { actor_id: 7, address: 'night@example', display_name: 'Night supervisor' }, grace_seconds: 1800 }
const event = (id: number, before: Record<string, unknown>, after: Record<string, unknown>, extra = {}) => ({
  id, occurred_at: '2026-11-26T08:00:00Z', recorded_at: '2026-11-26T08:00:00Z', event_kind: 'policy_change',
  source: 'agent_team_supervisor.registration', record_kind: 'event', actor_kind: 'operator', actor_reference: 'director',
  sanitized_reason: 'supervisor_registered', before_values: before, after_values: after, live_links_available: true, ...extra,
})
const bind12 = event(5, { supervisor_actor_id: null, supervisor_grace_seconds: null }, { supervisor_actor_id: 12, supervisor_grace_seconds: null })
const auditPage = (items: ReturnType<typeof event>[], total = items.length, page = 1) => ({ items, total, page, page_size: 100, snapshot_labels: [] })
const deferred = <T,>() => { let resolve!: (value: T) => void; let reject!: (error: Error) => void; const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej }); return { promise, resolve, reject } }
const flush = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)) })

function Harness({ observation, refresh }: { observation: SupervisorObservation | null; refresh: () => Promise<void> }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button onClick={() => setOpen(true)}>Edit supervisor binding for Alpha</button>
      <SupervisorBindingDialog presetId={1} presetName="Alpha" observation={observation} open={open} onOpenChange={setOpen} refresh={refresh} onReturnFocus={() => undefined} />
    </>
  )
}
async function openEditor(observation: SupervisorObservation | null = bound, refresh = vi.fn().mockResolvedValue(undefined)) {
  const user = userEvent.setup()
  const view = renderUi(<Harness observation={observation} refresh={refresh} />, { wrapper: MemoryRouter })
  await user.click(screen.getByRole('button', { name: 'Edit supervisor binding for Alpha' }))
  await screen.findByRole('dialog', { name: 'Supervisor binding' })
  await flush()
  return { user, refresh, view }
}
const actor = () => screen.getByLabelText('Supervisor actor id')
const grace = () => screen.getByLabelText('Grace (seconds)')
const useDefault = () => screen.getByRole('checkbox', { name: 'Use the default grace' })
const save = () => screen.getByRole('button', { name: 'Save' })
const retype = async (user: ReturnType<typeof userEvent.setup>, field: HTMLElement, text: string) => { await user.clear(field); if (text) await user.type(field, text) }

beforeEach(() => {
  setOperatorToken('director-key')
  vi.mocked(updateTeamSupervisor).mockReset().mockResolvedValue(bound)
  vi.mocked(fetchAuditEvents).mockReset().mockResolvedValue(auditPage([bind12]))
})
afterEach(() => { cleanup(); clearOperatorToken() })

describe('the supervisor binding editor', () => {
  it('shows the bound actor and the grace, or that no supervisor is bound', async () => {
    await openEditor()
    expect(screen.getByText(/Night supervisor/)).toBeInTheDocument()
    expect(actor()).toHaveValue('7'); expect(grace()).toHaveValue('1800')
    cleanup()
    await openEditor(none)
    expect(screen.getByText('No supervisor is bound.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove the supervisor' })).not.toBeInTheDocument()
  })

  it('binds an actor by its id and sends only the actor id', async () => {
    const { user, refresh } = await openEditor(none)
    await retype(user, actor(), '12'); await user.click(save())
    await waitFor(() => expect(updateTeamSupervisor).toHaveBeenCalledWith(1, { actor_id: 12, expected_binding_version: 4 }, 'director-key'))
    expect(refresh).toHaveBeenCalled()
  })

  it.each(['0', '1.5', 'abc', '-3', ''])('refuses the actor id "%s" with no request', async (text) => {
    const { user } = await openEditor(none)
    await retype(user, actor(), text); await user.click(save())
    expect(await screen.findByText('Enter the actor id as a whole number, 1 or more.')).toBeInTheDocument()
    expect(updateTeamSupervisor).not.toHaveBeenCalled()
  })

  it('changes only the grace: the current actor id and the new grace are sent', async () => {
    const { user } = await openEditor()
    await retype(user, grace(), '900'); await user.click(save())
    await waitFor(() => expect(updateTeamSupervisor).toHaveBeenCalledWith(1, { actor_id: 7, grace_seconds: 900, expected_binding_version: 4 }, 'director-key'))
  })

  it.each(['0', '1.5', 'abc', ''])('refuses the grace "%s" with no request', async (text) => {
    const { user } = await openEditor()
    await retype(user, grace(), text); await user.click(save())
    expect(await screen.findByText('Enter the grace as whole seconds, 1 or more.')).toBeInTheDocument()
    expect(updateTeamSupervisor).not.toHaveBeenCalled()
  })

  it('returns the grace to the default with an explicit null', async () => {
    const { user } = await openEditor()
    await user.click(useDefault()); await user.click(save())
    await waitFor(() => expect(updateTeamSupervisor).toHaveBeenCalledWith(1, { actor_id: 7, grace_seconds: null, expected_binding_version: 4 }, 'director-key'))
  })

  it('changes only the actor: the grace is not sent', async () => {
    const { user } = await openEditor()
    await retype(user, actor(), '9'); await user.click(save())
    await waitFor(() => expect(updateTeamSupervisor).toHaveBeenCalledWith(1, { actor_id: 9, expected_binding_version: 4 }, 'director-key'))
  })

  it('says "No value changed." when the same grace is typed again', async () => {
    const { user } = await openEditor()
    await retype(user, grace(), '1800'); await user.click(save())
    expect(await screen.findByText('No value changed.')).toBeInTheDocument()
    expect(updateTeamSupervisor).not.toHaveBeenCalled()
  })

  it('says "No value changed." and sends nothing', async () => {
    const { user } = await openEditor()
    await user.click(save())
    expect(await screen.findByText('No value changed.')).toBeInTheDocument()
    expect(updateTeamSupervisor).not.toHaveBeenCalled()
  })

  it('removes the supervisor only after a confirmation, with a null actor id', async () => {
    const { user } = await openEditor()
    await user.click(screen.getByRole('button', { name: 'Remove the supervisor' }))
    expect(screen.getByText('Remove this supervisor? Its grace setting is cleared.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Keep the supervisor' }))
    expect(updateTeamSupervisor).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Remove the supervisor' }))
    await user.click(screen.getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(updateTeamSupervisor).toHaveBeenCalledWith(1, { actor_id: null, expected_binding_version: 4 }, 'director-key'))
  })

  it('shows a server refusal as words, not as a code', async () => {
    vi.mocked(updateTeamSupervisor).mockRejectedValueOnce(new Error('supervisor_actor_missing'))
    const { user } = await openEditor(none)
    await retype(user, actor(), '99'); await user.click(save())
    expect(await screen.findByText('No external actor has this id.')).toBeInTheDocument()
    vi.mocked(updateTeamSupervisor).mockRejectedValueOnce(new Error('supervisor_binding_changed'))
    await user.click(save())
    expect(await screen.findByText('The binding changed while you edited. Review the current binding, then save again.')).toBeInTheDocument()
  })

  it('lists only supervisor binding events, in words, with the reason and the actor', async () => {
    vi.mocked(fetchAuditEvents).mockResolvedValue(auditPage([
      event(9, { supervisor_actor_id: 12, supervisor_grace_seconds: 900 }, { supervisor_actor_id: null, supervisor_grace_seconds: null }, { sanitized_reason: 'supervisor_removed' }),
      event(8, { supervisor_actor_id: 12, supervisor_grace_seconds: null }, { supervisor_actor_id: 12, supervisor_grace_seconds: 900 }),
      event(7, { supervisor_status: 'absent' }, { supervisor_status: 'healthy' }, { source: 'agent_team_supervisor.clock' }),
      bind12,
    ]))
    await openEditor()
    expect(screen.getByText('Supervisor: actor 12 → none')).toBeInTheDocument()
    expect(screen.getByText('Grace: 900 seconds → default')).toBeInTheDocument()
    expect(screen.getByText('Grace: default → 900 seconds')).toBeInTheDocument()
    expect(screen.getByText('Supervisor: none → actor 12')).toBeInTheDocument()
    expect(screen.getByText('Supervisor removed')).toBeInTheDocument()
    expect(screen.getAllByText('Binding saved').length).toBe(2)
    expect(screen.queryByText(/healthy|absent/)).not.toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: 'History' })).getAllByRole('listitem')).toHaveLength(3)
    expect(screen.getAllByText(/director/).length).toBeGreaterThan(0)
    expect(fetchAuditEvents).toHaveBeenCalledWith(expect.objectContaining({ teamId: 1, eventKind: 'policy_change', operatorToken: 'director-key' }))
  })

  it('pages older history with the next page number, each event once', async () => {
    vi.mocked(fetchAuditEvents).mockResolvedValueOnce(auditPage([bind12], 150, 1))
    const { user } = await openEditor()
    vi.mocked(fetchAuditEvents).mockResolvedValueOnce(auditPage([bind12, event(2, { supervisor_actor_id: null, supervisor_grace_seconds: null }, { supervisor_actor_id: 3, supervisor_grace_seconds: null })], 150, 2))
    await user.click(screen.getByRole('button', { name: 'Load older history' }))
    expect(await screen.findByText('Supervisor: none → actor 3')).toBeInTheDocument()
    expect(screen.getAllByText('Supervisor: none → actor 12')).toHaveLength(1)
    expect(fetchAuditEvents).toHaveBeenLastCalledWith(expect.objectContaining({ page: 2 }))
    expect(screen.queryByRole('button', { name: 'Load older history' })).not.toBeInTheDocument()
  })

  it('reloads the history after a save', async () => {
    const { user } = await openEditor(none)
    vi.mocked(fetchAuditEvents).mockResolvedValue(auditPage([event(10, { supervisor_actor_id: null, supervisor_grace_seconds: null }, { supervisor_actor_id: 21, supervisor_grace_seconds: null }), bind12]))
    await retype(user, actor(), '21'); await user.click(save())
    expect(await screen.findByText('Supervisor: none → actor 21')).toBeInTheDocument()
  })

  it('does not call an unreadable history empty', async () => {
    vi.mocked(fetchAuditEvents).mockRejectedValue(new Error('audit unavailable'))
    await openEditor()
    expect(screen.getByText('The history is not available.')).toBeInTheDocument()
    expect(screen.queryByText('No changes recorded yet.')).not.toBeInTheDocument()
  })

  it('says "No changes recorded yet." only after a successful read with no events', async () => {
    vi.mocked(fetchAuditEvents).mockResolvedValue(auditPage([]))
    await openEditor()
    expect(screen.getByText('No changes recorded yet.')).toBeInTheDocument()
  })

  it('keeps a typed actor id when the binding refreshes in the background, and lets the untouched grace follow', async () => {
    const { user, view, refresh } = await openEditor()
    await retype(user, actor(), '31')
    view.rerender(<Harness observation={{ ...bound, grace_seconds: 600 }} refresh={refresh} />)
    await flush()
    expect(actor()).toHaveValue('31'); expect(grace()).toHaveValue('600')
  })

  it('disables the grace field while the default is chosen, and enables it again when it is not', async () => {
    const { user } = await openEditor()
    await user.click(useDefault())
    expect(grace()).toBeDisabled()
    await user.click(useDefault())
    await retype(user, grace(), '700')
    expect(useDefault()).not.toBeChecked(); expect(grace()).toBeEnabled()
    expect(grace()).toHaveValue('700')
  })

  it('locks the fields while a save runs, and unlocks them after', async () => {
    const slow = deferred<SupervisorObservation>()
    vi.mocked(updateTeamSupervisor).mockReturnValueOnce(slow.promise)
    const { user } = await openEditor()
    await retype(user, grace(), '900'); await user.click(save())
    await waitFor(() => expect(updateTeamSupervisor).toHaveBeenCalledTimes(1))
    for (const field of [actor(), grace(), useDefault(), screen.getByRole('button', { name: 'Remove the supervisor' })]) expect(field).toBeDisabled()
    slow.resolve(bound)
    await waitFor(() => expect(grace()).toBeEnabled())
  })

  it('does not let a save from an older session touch the new one', async () => {
    const old = deferred<SupervisorObservation>()
    vi.mocked(updateTeamSupervisor).mockReturnValueOnce(old.promise)
    const { user } = await openEditor()
    await retype(user, grace(), '900'); await user.click(save())
    await waitFor(() => expect(updateTeamSupervisor).toHaveBeenCalledTimes(1))
    await user.keyboard('{Escape}')
    await user.click(screen.getByRole('button', { name: 'Edit supervisor binding for Alpha' }))
    await screen.findByRole('dialog', { name: 'Supervisor binding' }); await flush()
    await retype(user, actor(), '41')
    const reads = vi.mocked(fetchAuditEvents).mock.calls.length
    old.reject(new Error('supervisor_binding_changed')); await flush()
    expect(actor()).toHaveValue('41')
    expect(screen.queryByText(/binding changed/)).not.toBeInTheDocument()
    expect(fetchAuditEvents).toHaveBeenCalledTimes(reads)
  })

  it('asks for each older page once, even after a double click, and drops an answer from an older load', async () => {
    vi.mocked(fetchAuditEvents).mockResolvedValueOnce(auditPage([bind12], 150, 1))
    const { user } = await openEditor()
    const slow = deferred<ReturnType<typeof auditPage>>()
    vi.mocked(fetchAuditEvents).mockReturnValueOnce(slow.promise)
    await user.dblClick(screen.getByRole('button', { name: 'Load older history' }))
    expect(fetchAuditEvents).toHaveBeenCalledTimes(2)
    slow.resolve(auditPage([bind12], 150, 2)); await flush()
    expect(screen.getAllByText('Supervisor: none → actor 12')).toHaveLength(1)
  })

  it('forgets the old page cursor when it reopens, and offers older history only after a successful read', async () => {
    vi.mocked(fetchAuditEvents).mockResolvedValueOnce(auditPage([bind12], 150, 1))
    const { user } = await openEditor()
    expect(screen.getByRole('button', { name: 'Load older history' })).toBeInTheDocument()
    await user.keyboard('{Escape}')
    vi.mocked(fetchAuditEvents).mockRejectedValueOnce(new Error('audit unavailable'))
    await user.click(screen.getByRole('button', { name: 'Edit supervisor binding for Alpha' }))
    await screen.findByText('The history is not available.')
    expect(screen.queryByRole('button', { name: 'Load older history' })).not.toBeInTheDocument()
  })

  it('does not let an older save that succeeds reset the new draft', async () => {
    const old = deferred<SupervisorObservation>()
    vi.mocked(updateTeamSupervisor).mockReturnValueOnce(old.promise)
    const { user } = await openEditor()
    await retype(user, grace(), '900'); await user.click(save())
    await waitFor(() => expect(updateTeamSupervisor).toHaveBeenCalledTimes(1))
    await user.keyboard('{Escape}')
    await user.click(screen.getByRole('button', { name: 'Edit supervisor binding for Alpha' }))
    await screen.findByRole('dialog', { name: 'Supervisor binding' }); await flush()
    await retype(user, actor(), '41')
    const reads = vi.mocked(fetchAuditEvents).mock.calls.length
    old.resolve(bound); await flush()
    expect(actor()).toHaveValue('41')
    expect(fetchAuditEvents).toHaveBeenCalledTimes(reads)
  })

  it('keeps the new session locked while its own save runs, when an older save ends first', async () => {
    const old = deferred<SupervisorObservation>(), current = deferred<SupervisorObservation>()
    vi.mocked(updateTeamSupervisor).mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise)
    const { user } = await openEditor()
    await retype(user, grace(), '900'); await user.click(save())
    await waitFor(() => expect(updateTeamSupervisor).toHaveBeenCalledTimes(1))
    await user.keyboard('{Escape}')
    await user.click(screen.getByRole('button', { name: 'Edit supervisor binding for Alpha' }))
    await screen.findByRole('dialog', { name: 'Supervisor binding' }); await flush()
    await retype(user, grace(), '950'); await user.click(save())
    await waitFor(() => expect(updateTeamSupervisor).toHaveBeenCalledTimes(2))
    old.resolve(bound); await flush()
    expect(grace()).toBeDisabled()
    current.resolve(bound)
    await waitFor(() => expect(grace()).toBeEnabled())
  })

  it('drops an older history page that arrives after a newer first page', async () => {
    vi.mocked(fetchAuditEvents).mockResolvedValueOnce(auditPage([bind12], 150, 1))
    const { user } = await openEditor()
    const slow = deferred<ReturnType<typeof auditPage>>()
    vi.mocked(fetchAuditEvents).mockReturnValueOnce(slow.promise)
    await user.click(screen.getByRole('button', { name: 'Load older history' }))
    vi.mocked(fetchAuditEvents).mockResolvedValue(auditPage([event(10, { supervisor_actor_id: 12, supervisor_grace_seconds: null }, { supervisor_actor_id: 12, supervisor_grace_seconds: 900 }), bind12]))
    await retype(user, grace(), '900'); await user.click(save())
    expect(await screen.findByText('Grace: default → 900 seconds')).toBeInTheDocument()
    slow.resolve(auditPage([event(3, { supervisor_actor_id: null, supervisor_grace_seconds: null }, { supervisor_actor_id: 3, supervisor_grace_seconds: null })], 150, 2)); await flush()
    expect(screen.queryByText('Supervisor: none → actor 3')).not.toBeInTheDocument()
  })

  it('fails closed: with no binding version from the server, every write is off and none is sent', async () => {
    const { user } = await openEditor({ ...bound, binding_version: undefined })
    expect(screen.getByText(/Changes are off: this server does not report a binding version/)).toBeInTheDocument()
    expect(save()).toBeDisabled(); expect(useDefault()).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Remove the supervisor' })).toBeDisabled()
    await user.click(save())
    expect(updateTeamSupervisor).not.toHaveBeenCalled()
    expect(screen.getByText(/Night supervisor/)).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'History' })).toBeInTheDocument()
  })

  it('fails closed when the binding could not be read, and still shows the history', async () => {
    await openEditor(null)
    expect(screen.getByText('The supervisor binding could not be read.')).toBeInTheDocument()
    expect(save()).toBeDisabled(); expect(useDefault()).toBeDisabled()
    expect(updateTeamSupervisor).not.toHaveBeenCalled()
    expect(await screen.findByText('Supervisor: none → actor 12')).toBeInTheDocument()
  })

  it('marks a draft stale when the binding version changes during the edit, and resets it on request', async () => {
    const { user, view, refresh } = await openEditor()
    await retype(user, grace(), '900')
    view.rerender(<Harness observation={{ ...none, binding_version: 5 }} refresh={refresh} />)
    await flush()
    expect(screen.getByText('The binding changed while you edited. Review the current binding.')).toBeInTheDocument()
    expect(save()).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Review the current binding' }))
    expect(screen.queryByText('The binding changed while you edited. Review the current binding.')).not.toBeInTheDocument()
    expect(screen.getByText('No supervisor is bound.')).toBeInTheDocument()
    expect(actor()).toHaveValue(''); expect(save()).toBeEnabled()
    await retype(user, actor(), '12'); await user.click(save())
    await waitFor(() => expect(updateTeamSupervisor).toHaveBeenCalledWith(1, { actor_id: 12, expected_binding_version: 5 }, 'director-key'))
  })

  it('keeps a stale draft stale when the operator keeps typing', async () => {
    const { user, view, refresh } = await openEditor()
    await retype(user, grace(), '900')
    view.rerender(<Harness observation={{ ...bound, binding_version: 5 }} refresh={refresh} />)
    await flush()
    await user.type(grace(), '5')
    expect(screen.getByText('The binding changed while you edited. Review the current binding.')).toBeInTheDocument()
    expect(save()).toBeDisabled()
  })

  it('follows a changed version without a draft, and sends the new version', async () => {
    const { user, view, refresh } = await openEditor()
    view.rerender(<Harness observation={{ ...bound, grace_seconds: 600, binding_version: 6 }} refresh={refresh} />)
    await flush()
    expect(screen.queryByText(/changed while you edited/)).not.toBeInTheDocument()
    await retype(user, grace(), '300'); await user.click(save())
    await waitFor(() => expect(updateTeamSupervisor).toHaveBeenCalledWith(1, { actor_id: 7, grace_seconds: 300, expected_binding_version: 6 }, 'director-key'))
  })

  it('does not let a removal confirmation survive a changed binding', async () => {
    const { user, view, refresh } = await openEditor()
    await user.click(screen.getByRole('button', { name: 'Remove the supervisor' }))
    expect(screen.getByRole('button', { name: 'Remove' })).toBeInTheDocument()
    view.rerender(<Harness observation={{ ...bound, actor_id: 12, address: { actor_id: 12, address: 'new@example', display_name: 'New supervisor' }, binding_version: 5 }} refresh={refresh} />)
    await flush()
    expect(screen.queryByRole('button', { name: 'Remove' })).not.toBeInTheDocument()
    expect(screen.getByText('The binding changed, so the removal was not confirmed.')).toBeInTheDocument()
    expect(updateTeamSupervisor).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Remove the supervisor' }))
    await user.click(screen.getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(updateTeamSupervisor).toHaveBeenCalledWith(1, { actor_id: null, expected_binding_version: 5 }, 'director-key'))
  })

  it('on a conflict from the server, refreshes the binding and drops the draft', async () => {
    vi.mocked(updateTeamSupervisor).mockRejectedValueOnce(new Error('supervisor_binding_changed'))
    const { user, refresh } = await openEditor()
    await retype(user, grace(), '900'); await user.click(save())
    expect(await screen.findByText('The binding changed while you edited. Review the current binding, then save again.')).toBeInTheDocument()
    await waitFor(() => expect(refresh).toHaveBeenCalled())
    expect(grace()).toHaveValue('1800')
  })

  it('does not call a history empty while older pages are not read', async () => {
    vi.mocked(fetchAuditEvents).mockResolvedValueOnce(auditPage([], 150, 1))
    const { user } = await openEditor()
    expect(screen.queryByText('No changes recorded yet.')).not.toBeInTheDocument()
    expect(screen.getByText('No supervisor binding change in the events loaded so far. Load older history to look further.')).toBeInTheDocument()
    vi.mocked(fetchAuditEvents).mockResolvedValueOnce(auditPage([bind12], 150, 2))
    await user.click(screen.getByRole('button', { name: 'Load older history' }))
    expect(await screen.findByText('Supervisor: none → actor 12')).toBeInTheDocument()
    expect(screen.queryByText(/events loaded so far/)).not.toBeInTheDocument()
  })
})

describe('the Edit supervisor button on the team header', () => {
  beforeEach(() => resetFactoryReads())
  afterEach(() => vi.unstubAllGlobals())

  it('opens the editor from the Supervisor line of the team', async () => {
    const summary = { preset_id: 1, description: null, parent_issue_url: null, supervisor: 'none', supervisor_observation: bound, checked_at: '2026-11-26T08:00:00Z', valid_until: '2026-11-26T08:01:00Z' }
    fixtureFetch((path) => (path === 'agent-teams/presets/1/summary' ? jsonResponse(summary) : jsonResponse({ detail: 'x' }, 404)))
    const preset = { id: 1, name: 'Alpha', description: null, autonomy_enabled: false, leader_slot_id: null, slots: [] } as unknown as AgentTeamPreset
    const user = userEvent.setup()
    renderUi(<MemoryRouter><TeamHeader preset={preset} onEdit={() => undefined} onPlan={() => undefined} planDisabled onAutonomy={() => undefined} /></MemoryRouter>)
    await settle()
    await user.click(await screen.findByRole('button', { name: 'Edit supervisor binding for Alpha' }))
    expect(await screen.findByRole('dialog', { name: 'Supervisor binding' })).toBeInTheDocument()
  })
})
