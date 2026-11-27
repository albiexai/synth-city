import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AgentTeamsPage } from '../src/features/agent-teams/AgentTeamsPage'
import { fetchAgentTeamPresets } from '../src/features/agent-teams/api'
import { MilestoneStates } from '../src/features/factory/components/MilestoneStates'
import { WorkPage } from '../src/features/factory/pages/WorkPage'
import { resetFactoryReads } from '../src/features/factory/reads'
import type { AgentTeamPreset } from '../src/types/agentTeams'
import type { DeferredIssue, MilestoneStatesResponse, WaitingIssue } from '../src/types/factory'
import { fixtureFetch, jsonResponse, settle } from './helpers/factory'

// #1337: items deferred by a milestone move and issues that wait for a team.
vi.mock('../src/features/agent-teams/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/features/agent-teams/api')>()
  return {
    ...actual,
    fetchAgentTeamPresets: vi.fn(),
    fetchTeamGithubScopes: vi.fn().mockResolvedValue({ scopes: [] }),
    fetchGithubWorkItems: vi.fn().mockResolvedValue({ items: [] }),
    fetchGithubRecoveryGateActive: vi.fn().mockResolvedValue({ active: false }),
    fetchAgentTeamActivity: vi.fn().mockResolvedValue({ preset_id: 1, checked_at: '', valid_until: '2000-01-01T00:00:00Z', slots: [] }),
  }
})
vi.mock('../src/features/agent-teams/BacklogCoordination', () => ({ BacklogCoordination: () => null }))
vi.mock('../src/features/agent-teams/HumanActionSummary', () => ({ HumanActionSummary: () => null }))
vi.mock('../src/features/agent-teams/AutonomyPanel', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/features/agent-teams/AutonomyPanel')>()
  return { ...actual, AutonomyPanel: () => <p>AUTONOMY-PANEL</p> }
})

const stamp = '2026-11-26T09:00:00Z'
const repository = { scope_id: 4, display_name: 'albiexai/synth-city', github: { owner: 'albiexai', name: 'synth-city' } }
const waiting = (extra: Partial<WaitingIssue> = {}): WaitingIssue => ({
  issue_number: 1400, milestone: 'v4.5', observed_at: stamp, repository, ...extra,
})
const deferred = (extra: Partial<DeferredIssue> = {}): DeferredIssue => ({
  deferred_at: stamp, issue_number: 1401, issue_title: 'Move the settings page', previous_branch: 'deck/slot-3/issue-1401',
  repository, target_milestone: 'v4.6', team: { id: 1, name: 'Alpha' }, work_item_id: 77, ...extra,
})
const body = (extra: Partial<MilestoneStatesResponse> = {}): MilestoneStatesResponse => ({
  deferred: [], deferred_truncated: false, filters: { team_id: null, scope_id: null, provider: null, category: 'all' },
  generated_at: stamp, schema_version: 1, waiting: [], waiting_truncated: false, ...extra,
} as MilestoneStatesResponse)

beforeEach(() => resetFactoryReads())
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

function serve(value: MilestoneStatesResponse | number) {
  return fixtureFetch((path) => {
    if (path === 'factory/milestone-states') {
      return typeof value === 'number' ? jsonResponse({ detail: 'x' }, value) : jsonResponse(value)
    }
    return jsonResponse({ detail: 'x' }, 404)
  })
}
const show = (props: { teamId?: number; scopeId?: number } = {}) =>
  render(<MemoryRouter><MilestoneStates {...props} /></MemoryRouter>)

describe('the list', () => {
  it('shows a deferred issue with its milestone and a link to the issue', async () => {
    serve(body({ deferred: [deferred()] }))
    show()
    const section = await screen.findByRole('region', { name: 'Milestone moves' })
    expect(within(section).getByText('Deferred by a milestone move')).toBeInTheDocument()
    expect(within(section).getByText('v4.6')).toBeInTheDocument()
    expect(within(section).getByText(/Deferred to milestone/)).toBeInTheDocument()
    expect(within(section).getByText('Move the settings page')).toBeInTheDocument()
    expect(within(section).getByText('from Alpha')).toBeInTheDocument()
    expect(within(section).getByRole('link', { name: 'albiexai/synth-city#1401' }))
      .toHaveAttribute('href', 'https://github.com/albiexai/synth-city/issues/1401')
  })

  it('shows a waiting issue with its milestone and a link to the issue', async () => {
    serve(body({ waiting: [waiting()] }))
    show()
    const section = await screen.findByRole('region', { name: 'Milestone moves' })
    expect(within(section).getByText('Waiting for a team')).toBeInTheDocument()
    expect(within(section).getByText(/Waiting: no team for milestone/)).toBeInTheDocument()
    expect(within(section).getByText('v4.5')).toBeInTheDocument()
    expect(within(section).getByRole('link', { name: 'albiexai/synth-city#1400' }))
      .toHaveAttribute('href', 'https://github.com/albiexai/synth-city/issues/1400')
  })

  it('shows a waiting issue whose repository is not recorded with its number and no link', async () => {
    serve(body({ waiting: [waiting({ repository: null })] }))
    show()
    const section = await screen.findByRole('region', { name: 'Milestone moves' })
    const item = within(section).getByRole('listitem')
    expect(within(item).getByText('#1400')).toBeInTheDocument()
    expect(within(item).getByText('repository not recorded')).toBeInTheDocument()
    expect(within(item).queryByRole('link')).not.toBeInTheDocument()
    expect(item).not.toHaveTextContent('albiex')
    expect(within(item).getByText('v4.5')).toBeInTheDocument()
  })

  it('keeps the link of a waiting issue with a proven repository next to an unlinked one', async () => {
    serve(body({ waiting: [waiting({ repository: null, issue_number: 1500 }), waiting({ issue_number: 1501 })] }))
    show()
    const section = await screen.findByRole('region', { name: 'Milestone moves' })
    const items = within(section).getAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(within(items[0]).queryByRole('link')).not.toBeInTheDocument()
    expect(within(items[1]).getByRole('link', { name: 'albiexai/synth-city#1501' }))
      .toHaveAttribute('href', 'https://github.com/albiexai/synth-city/issues/1501')
  })

  it('shows two unlinked waiting issues with the same number', async () => {
    serve(body({ waiting: [waiting({ repository: null }), waiting({ repository: null })] }))
    show()
    const section = await screen.findByRole('region', { name: 'Milestone moves' })
    expect(within(section).getAllByRole('listitem')).toHaveLength(2)
  })

  it('shows both states together', async () => {
    serve(body({ deferred: [deferred()], waiting: [waiting()] }))
    show()
    const section = await screen.findByRole('region', { name: 'Milestone moves' })
    expect(within(section).getAllByRole('listitem')).toHaveLength(2)
  })

  it('says when the milestone of a deferral was not recorded', async () => {
    serve(body({ deferred: [deferred({ target_milestone: null })] }))
    show()
    expect(await screen.findByText('Deferred (milestone not recorded)')).toBeInTheDocument()
  })

  it('hides the team name when the list is for one team', async () => {
    serve(body({ deferred: [deferred()] }))
    show({ teamId: 1 })
    await screen.findByText('Move the settings page')
    expect(screen.queryByText('from Alpha')).not.toBeInTheDocument()
  })

  it('shows nothing when there is nothing to show', async () => {
    const { fetch } = serve(body())
    const { container } = show()
    await waitFor(() => expect(fetch).toHaveBeenCalled())
    await settle()
    expect(container).toBeEmptyDOMElement()
  })

  it('says when a list is cut', async () => {
    serve(body({ deferred: [deferred()], deferred_truncated: true, waiting: [waiting()], waiting_truncated: true }))
    show()
    expect(await screen.findByText('More deferred issues exist than this list shows.')).toBeInTheDocument()
    expect(screen.getByText('More waiting issues exist than this list shows.')).toBeInTheDocument()
  })

  it('shows a failed read as text with a way to try again', async () => {
    serve(404)
    show()
    expect(await screen.findByRole('alert')).toHaveTextContent('x')
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  })
})

describe('the request', () => {
  it('sends the team and the repository it was given', async () => {
    const { optionRequests: requests } = serve(body())
    show({ teamId: 3, scopeId: 9 })
    await waitFor(() => expect(requests).toHaveLength(1))
    expect(requests[0].path).toBe('factory/milestone-states')
    expect(requests[0].query.get('team_id')).toBe('3')
    expect(requests[0].query.get('scope_id')).toBe('9')
  })

  it('sends no filter by default', async () => {
    const { optionRequests: requests } = serve(body())
    show()
    await waitFor(() => expect(requests).toHaveLength(1))
    expect([...requests[0].query.keys()]).toEqual([])
  })
})

describe('the pages', () => {
  const listing = {
    counts: { total: 0, active: 0, attention: 0, finished: 0, queued: 0, review: 0, unknown: 0 }, items: [], total: 0,
    has_more: false, next_cursor: null, generated_at: stamp, filters: {}, schema_version: 1,
  }

  it('the Work page shows both states and passes the team of the URL', async () => {
    const { optionRequests: requests } = fixtureFetch((path) => {
      if (path === 'factory/milestone-states') return jsonResponse(body({ deferred: [deferred()], waiting: [waiting()] }))
      if (path === 'factory/work-items') return jsonResponse(listing)
      if (path === 'factory/repositories') return jsonResponse({ repositories: [{ scope_id: 4, team: { id: 1, name: 'Alpha' }, github: repository.github }], has_more: false, total: 1 })
      return jsonResponse({ detail: 'x' }, 404)
    })
    render(
      <MemoryRouter initialEntries={['/work?team_id=1']}>
        <Routes><Route path="/work" element={<WorkPage />} /></Routes>
      </MemoryRouter>,
    )
    await screen.findByRole('region', { name: 'Milestone moves' })
    expect(requests.find((r) => r.path === 'factory/milestone-states')?.query.get('team_id')).toBe('1')
  })

  it('the Work page links each stamped repository and leaves an unstamped row unlinked', async () => {
    const second = { scope_id: 5, display_name: 'second/project', github: { owner: 'second', name: 'project' } }
    fixtureFetch((path) => {
      if (path === 'factory/milestone-states') return jsonResponse(body({ waiting: [
        waiting(), waiting({ repository: second }), waiting({ repository: null }),
      ] }))
      if (path === 'factory/work-items') return jsonResponse(listing)
      return jsonResponse({ detail: 'x' }, 404)
    })
    render(<MemoryRouter initialEntries={['/work']}>
      <Routes><Route path="/work" element={<WorkPage />} /></Routes>
    </MemoryRouter>)

    const section = await screen.findByRole('region', { name: 'Milestone moves' })
    const items = within(section).getAllByRole('listitem')
    expect(items).toHaveLength(3)
    expect(within(items[0]).getByRole('link', { name: 'albiexai/synth-city#1400' }))
      .toHaveAttribute('href', 'https://github.com/albiexai/synth-city/issues/1400')
    expect(within(items[1]).getByRole('link', { name: 'second/project#1400' }))
      .toHaveAttribute('href', 'https://github.com/second/project/issues/1400')
    expect(within(items[2]).getByText('#1400')).toBeInTheDocument()
    expect(within(items[2]).queryByRole('link')).not.toBeInTheDocument()
  })

  it('the Teams page shows the list on the Autonomy tab for the selected team', async () => {
    const stampTeam = '2026-10-12T12:00:00Z'
    const team = {
      id: 1, name: 'alpha', description: 'd', created_at: stampTeam, updated_at: stampTeam, autonomy_enabled: false,
      leader_slot_id: null, slots: [],
    } as unknown as AgentTeamPreset
    vi.mocked(fetchAgentTeamPresets).mockResolvedValue({ presets: [team] })
    const { optionRequests: requests } = fixtureFetch((path) => {
      if (path === 'factory/milestone-states') return jsonResponse(body({ waiting: [waiting()] }))
      return jsonResponse({ detail: 'x' }, 404)
    })
    render(
      <MemoryRouter initialEntries={['/teams/1?tab=roster']}>
        <Routes><Route path="/teams/:teamId" element={<AgentTeamsPage />} /></Routes>
      </MemoryRouter>,
    )
    expect(screen.queryByRole('region', { name: 'Milestone moves' })).not.toBeInTheDocument()
    await userEvent.setup().click(await screen.findByRole('tab', { name: 'Autonomy' }))
    await screen.findByRole('region', { name: 'Milestone moves' })
    expect(screen.getByText('AUTONOMY-PANEL')).toBeInTheDocument()
    expect(requests.find((r) => r.path === 'factory/milestone-states')?.query.get('team_id')).toBe('1')
  })
})
