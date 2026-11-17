import { cleanup, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ProviderProvider } from '../src/contexts/ProviderContext'
import { HarnessesPage } from '../src/features/harnesses/HarnessesPage'
import { updateNativeMetadata } from '../src/features/native-settings/surfaceRegistry'
import { resetProviderOperations } from '../src/hooks/useProviders'
import { fetchAgentTeamPresets } from '../src/features/agent-teams/api'
import type { AgentProviderId, ProviderOperations } from '../src/types/providers'
import fixture from './fixtures/provider-operations/v1/catalog.json'
import { fixtureFetch, jsonResponse, operationsFixtureResponse, renderRoute, settle } from './helpers/factory'

// #1069: the Harnesses UI shows the chosen user's Claude Code version, or the exact unknown message, whether or not
// the controller has a claude of its own (installed describes the controller's binary).

vi.mock('../src/features/agent-teams/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../src/features/agent-teams/api')>()),
  fetchAgentTeamPresets: vi.fn(),
}))
vi.mock('../src/contexts/DashboardContext', () => ({
  DashboardProvider: ({ children }: { children: React.ReactNode }) => children,
  useDashboard: () => ({ stats: null, loading: false, error: null, lastFetched: null, refreshDashboard: () => undefined }),
}))
vi.mock('../src/contexts/ProjectContext', () => ({ useProjectContext: () => ({ projects: [], activeProject: null, knownFolders: [], setProjectPath: () => undefined, addProject: async () => undefined }) }))

const UNKNOWN = "version unknown (cannot read the agent user's install)"
const catalogs = fixture.providers as unknown as Record<AgentProviderId, ProviderOperations>
const ids = Object.keys(catalogs) as AgentProviderId[]

type Claude = { installed: boolean; version?: string | null; version_user?: string | null; version_source?: string | null; version_unknown_reason?: string | null }

function statusesWith(claude: Claude) {
  return ids.map(id => ({
    id, display_name: id, installed: true, version: '1.2.3', binary_path: null, config_paths: {},
    capabilities: { config: true, plugins: true, usage: true },
    capability_matrix: { ...catalogs[id].native_capabilities, config: { state: 'write_capable', reason: 'fixture' } },
    ...(id === 'claude-code' ? claude : {}),
  }))
}

async function openList(claude: Claude) {
  window.history.replaceState({}, '', '/harnesses')
  fixtureFetch(path => operationsFixtureResponse(path) ?? jsonResponse(path === 'providers' ? { providers: statusesWith(claude) } : {}))
  renderRoute(<ProviderProvider><HarnessesPage /></ProviderProvider>, '/harnesses', '*')
  await settle()
}

async function openPage(claude: Claude) {
  window.history.replaceState({}, '', '/harnesses/claude-code')
  fixtureFetch(path => operationsFixtureResponse(path) ?? jsonResponse(path === 'providers' ? { providers: statusesWith(claude) } : {}))
  renderRoute(<ProviderProvider><HarnessesPage /></ProviderProvider>, '/harnesses/claude-code', '/harnesses/:providerId')
  await settle()
}

const card = (name: string) => screen.getByRole('heading', { name: new RegExp(name) }).closest('article') as HTMLElement
const AGENT_KNOWN: Claude = { installed: true, version: '2.1.326', version_user: 'agentuser', version_source: 'native', version_unknown_reason: null }
const AGENT_UNKNOWN: Claude = { installed: true, version: null, version_user: 'agentuser', version_source: null, version_unknown_reason: UNKNOWN }

beforeEach(() => {
  resetProviderOperations(); updateNativeMetadata([]); localStorage.clear()
  vi.mocked(fetchAgentTeamPresets).mockResolvedValue({ presets: [] } as never)
})
afterEach(() => { cleanup(); resetProviderOperations(); vi.restoreAllMocks() })

describe('the harness card shows the chosen user\'s Claude Code version', () => {
  it('shows the agent user\'s version and not the controller\'s when the controller has a claude', async () => {
    await openList(AGENT_KNOWN); const claude = card('Claude Code')

    expect(within(claude).getByText('v2.1.326')).toBeInTheDocument()
    expect(within(claude).queryByText('v1.2.3')).toBeNull()
  })

  it('shows the agent user\'s version when the controller has NO claude (installed false)', async () => {
    await openList({ ...AGENT_KNOWN, installed: false }); const claude = card('Claude Code')

    expect(within(claude).getByText('v2.1.326')).toBeInTheDocument()
  })

  it('shows the exact unknown message when it cannot be read, with the controller installed or not', async () => {
    await openList(AGENT_UNKNOWN)
    expect(within(card('Claude Code')).getByText(UNKNOWN)).toBeInTheDocument()
    cleanup(); resetProviderOperations()

    await openList({ ...AGENT_UNKNOWN, installed: false })
    expect(within(card('Claude Code')).getByText(UNKNOWN)).toBeInTheDocument()
  })

  it('does not show the controller\'s version as the chosen user\'s when the user\'s version is unknown', async () => {
    await openList(AGENT_UNKNOWN); const claude = card('Claude Code')               // the other harnesses still say v1.2.3

    expect(within(claude).queryByText('v1.2.3')).toBeNull()
  })

  it('keeps today\'s output for the controller\'s own scope: its version when installed, none when not', async () => {
    await openList({ installed: true }); expect(within(card('Claude Code')).getByText('v1.2.3')).toBeInTheDocument()
    cleanup(); resetProviderOperations()

    await openList({ installed: false }); expect(within(card('Claude Code')).queryByText(/^v\d/)).toBeNull()
  })
})

describe('the harness page shows the chosen user\'s Claude Code version', () => {
  it('names the version and its user, and still says the controller has no claude when installed is false', async () => {
    await openPage({ ...AGENT_KNOWN, installed: false })

    expect(screen.getAllByText('Version 2.1.326 (agentuser)').length).toBeGreaterThan(0)
    expect(screen.getByText('Install it on this machine to use it in a team.')).toBeInTheDocument()
  })

  it('shows the exact unknown message in the page header and in Technical details, installed or not', async () => {
    await openPage({ ...AGENT_UNKNOWN, installed: false })

    // The header line is exactly the message. The details line is one text node that begins with its label, so an exact
    // match finds the header line only; the details line is checked by its full text.
    expect(screen.getAllByText(UNKNOWN)).toHaveLength(1)
    expect(screen.getByText(`Selected user's Claude Code: ${UNKNOWN}`)).toBeInTheDocument()
    cleanup(); resetProviderOperations()

    await openPage(AGENT_UNKNOWN)
    expect(screen.getAllByText(UNKNOWN)).toHaveLength(1)
    expect(screen.getByText(`Selected user's Claude Code: ${UNKNOWN}`)).toBeInTheDocument()
  })

  it('keeps today\'s header for the controller\'s own scope', async () => {
    await openPage({ installed: true })

    expect(screen.getByText('Version 1.2.3')).toBeInTheDocument()
    expect(screen.queryByText(/Selected user's Claude Code/)).toBeNull()
  })
})
