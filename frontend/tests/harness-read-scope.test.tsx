import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { BrowserRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SessionsPage } from '@/features/sessions/SessionsPage'
import { UsagePage } from '@/features/usage/UsagePage'
import { ContextPage } from '@/features/context/ContextPage'
import { PlansPage } from '@/features/plans/PlansPage'
import { clearNativeCatalogs, updateNativeCatalog, updateNativeMetadata } from '@/features/native-settings/surfaceRegistry'
import type { ActiveSessionContext, ContextAnalysis } from '@/types/context'
import type { AgentProviderStatus, ProviderOperations } from '@/types/providers'
import catalogs from './fixtures/provider-operations/v1/catalog.json'

const harness = vi.hoisted(() => ({ provider: 'claude-code' }))
const project = { path: '/fixture/project', name: 'Fixture project' }
vi.mock('@/contexts/ProviderContext', () => ({
  useProviderContext: () => ({ selectedProviderId: harness.provider }),
}))
vi.mock('@/contexts/ProjectContext', () => ({
  useProjectContext: () => ({ activeProject: null, projects: [project] }),
}))

const pages = [
  { name: 'sessions', Page: SessionsPage, endpoint: 'sessions/projects', empty: 'No sessions found', folder: 'projects' },
  { name: 'usage', Page: UsagePage, endpoint: 'usage/summary', empty: 'Total Cost', folder: 'projects' },
  { name: 'context', Page: ContextPage, endpoint: 'context/active', empty: 'No active sessions detected.', folder: 'projects' },
  { name: 'plans', Page: PlansPage, endpoint: 'plans', empty: 'No plans found', folder: 'plans' },
] as const

type Page = (typeof pages)[number]

function scopeFor(page: Page, user = 'worker', readable = true) {
  return {
    user,
    claude_dir: '/fixture/' + user + '/.claude',
    data_dir: '/fixture/' + user + '/.claude/' + page.folder,
    exists: true,
    readable,
  }
}

type TestScope = Omit<ReturnType<typeof scopeFor>, 'data_dir'> & { data_dir?: string | null }

function mockReads(page: Page, source: () => TestScope | undefined, options: {
  denied?: string
  cost?: () => number
  reply?: (endpoint: string) => Response | Promise<Response> | undefined
} = {}) {
  const fetcher = vi.fn(async (request: string, init?: RequestInit) => {
    const url = new URL(request, window.location.origin)
    const endpoint = url.pathname.replace(/^\/api\/v1\//, '')
    const scope = source()
    if (endpoint === page.endpoint && options.denied) {
      return Response.json({ detail: options.denied }, { status: 403 })
    }
    if (endpoint === page.endpoint && url.searchParams.has('scope_user')) {
      throw new Error('scope_user belongs in the scope header')
    }
    const reply = options.reply?.(endpoint)
    if (reply) return reply
    if (endpoint === 'sessions/projects') {
      return Response.json({ projects: [{ folder: 'fixture-project', name: project.name, session_count: 0 }], total_sessions: 0, scope })
    }
    if (endpoint === 'sessions') return Response.json({ sessions: [], total: 0 })
    if (endpoint === 'context/active') return Response.json({ sessions: [], scope })
    if (endpoint === 'plans') return Response.json({ plans: [], total: 0, scope })
    if (endpoint === 'plans/stats') return Response.json({ source: harness.provider, total_plans: 0, total_size_bytes: 0 })
    if (endpoint === 'usage/summary') {
      return Response.json({ summary: {
        total_cost: options.cost?.() ?? 0,
        total_tokens: 0, total_input_tokens: 0, total_output_tokens: 0,
        total_cache_read_tokens: 0, total_cache_creation_tokens: 0,
        session_count: 0, project_count: 0, models_used: [],
      }, scope })
    }
    if (endpoint.startsWith('usage/')) return Response.json({ data: [], total_cost: 0 })
    throw new Error('Unexpected endpoint ' + endpoint + String(init?.method ?? ''))
  })
  vi.stubGlobal('fetch', fetcher)
  return fetcher
}

function renderPage({ Page, name }: Page, search = '') {
  window.history.replaceState({}, '', '/harnesses/' + harness.provider + '/' + name + search)
  return render(<BrowserRouter><Page /></BrowserRouter>)
}

beforeEach(() => {
  harness.provider = 'claude-code'
  localStorage.clear()
  const providers = ['claude-code', 'codex-cli'] as const
  updateNativeMetadata(providers.map(id => ({
    id, display_name: id, installed: true,
    capability_matrix: catalogs.providers[id].native_capabilities,
  })) as AgentProviderStatus[])
  for (const id of providers) updateNativeCatalog(id, catalogs.providers[id] as ProviderOperations)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  clearNativeCatalogs()
  updateNativeMetadata([])
  window.history.replaceState({}, '', '/')
})

describe.each(pages)('$name source', (page) => {
  it('names the user and folder returned by the API', async () => {
    const scope = scopeFor(page)
    mockReads(page, () => scope)
    renderPage(page)

    expect(await screen.findByText("Reading worker's", { exact: false })).toHaveTextContent(scope.data_dir)
    expect(await screen.findByText(page.empty)).toBeInTheDocument()
  })

  it('shows unreadable instead of an empty or zero data view', async () => {
    const scope = scopeFor(page, 'worker', false)
    mockReads(page, () => scope)
    renderPage(page)

    expect(await screen.findByRole('alert')).toHaveTextContent("Cannot read worker's " + scope.data_dir)
    expect(screen.queryByText(page.empty)).not.toBeInTheDocument()
  })

  it('displays the existing scoped read denial without implying empty data', async () => {
    const detail = 'The controller cannot read /fixture/worker/.claude. It belongs to worker.'
    mockReads(page, () => undefined, { denied: detail })
    renderPage(page)

    expect(await screen.findByRole('alert')).toHaveTextContent('The controller cannot read this data folder.')
    expect(screen.queryByText(detail)).not.toBeInTheDocument()
    expect(screen.queryByText(page.empty)).not.toBeInTheDocument()
  })

  it('keeps legacy responses without scope usable', async () => {
    mockReads(page, () => undefined)
    renderPage(page)

    expect(await screen.findByText(page.empty)).toBeInTheDocument()
    expect(screen.queryByText(/^Reading /)).not.toBeInTheDocument()
  })
})

it('names the actual Codex home and keeps a configured Claude Plans path undisclosed', async () => {
  harness.provider = 'codex-cli'
  const page = pages[3]
  const scope = scopeFor(page)
  mockReads(page, () => ({ ...scope, data_dir: '/fixture/codex-home' }))
  const view = renderPage(page)
  expect(await screen.findByText("Reading worker's", { exact: false })).toHaveTextContent('/fixture/codex-home')
  expect(await screen.findByText('No Codex plan snapshots found')).toBeInTheDocument()
  view.unmount()

  harness.provider = 'claude-code'
  mockReads(page, () => ({ ...scope, data_dir: null }))
  renderPage(page)
  expect(await screen.findByText("Reading worker's configured plans folder")).toBeInTheDocument()
  expect(screen.queryByText(scope.claude_dir, { exact: false })).not.toBeInTheDocument()
})

it('verifies the current Usage source before applying a cache for the same project', async () => {
  const page = pages[1]
  let scope = scopeFor(page)
  let cost = 0.7
  const fetcher = mockReads(page, () => scope, { cost: () => cost })
  const first = renderPage(page)
  expect(await screen.findByText('$0.7000')).toBeInTheDocument()
  await waitFor(() => expect(Object.keys(localStorage).some(key => key.startsWith('usage_cache_'))).toBe(true))
  first.unmount()

  scope = scopeFor(page, 'controller')
  cost = 0.9
  fetcher.mockClear()
  renderPage(page)
  expect(screen.queryByText('$0.7000')).not.toBeInTheDocument()
  expect(await screen.findByText("Reading controller's", { exact: false })).toHaveTextContent(scope.data_dir)
  expect(await screen.findByText('$0.9000')).toBeInTheDocument()
  await waitFor(() => expect(fetcher.mock.calls.some(([url]) => url.includes('/usage/daily'))).toBe(true))
})

it('drops the old Usage breakdown while another source loads and when its read fails', async () => {
  const page = pages[1]
  let scope = scopeFor(page)
  let finishBreakdown!: (response: Response) => void
  const pendingBreakdown = new Promise<Response>(resolve => { finishBreakdown = resolve })
  mockReads(page, () => scope, { reply: endpoint => {
    if (endpoint !== 'usage/sessions') return
    if (scope.user === 'other-worker') return pendingBreakdown
    return Response.json({ total_cost: 1, data: [{
      session_id: 'original-session',
      project_path: '/fixture/old-project',
      input_tokens: 1,
      output_tokens: 1,
      total_cost: 1,
      last_activity: '2026-11-18',
      models_used: [],
    }] })
  } })
  renderPage(page)
  fireEvent.keyDown(await screen.findByRole('tab', { name: 'Sessions' }), { key: 'Enter' })
  expect(await screen.findByText('old-project')).toBeInTheDocument()

  scope = scopeFor(page, 'other-worker')
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  expect(await screen.findByText("Reading other-worker's", { exact: false })).toBeInTheDocument()
  expect(screen.queryByText('old-project')).not.toBeInTheDocument()

  finishBreakdown(Response.json({ detail: 'Breakdown failed' }, { status: 500 }))
  expect(await screen.findByText('Breakdown failed')).toBeInTheDocument()
  expect(screen.queryByText('old-project')).not.toBeInTheDocument()
  expect(screen.queryByText('No session data available')).not.toBeInTheDocument()
})

const activeSession: ActiveSessionContext = {
  session_id: 'original-session',
  project_folder: 'old-project',
  project_name: 'Old Context project',
  model: 'claude-sonnet',
  current_context_tokens: 100,
  max_context_tokens: 1000,
  context_percentage: 10,
  is_active: true,
  last_activity: '2026-11-18',
}

const contextAnalysis: ContextAnalysis = {
  ...activeSession,
  snapshots: [],
  content_categories: [],
  file_consumptions: [],
  tool_consumptions: [],
  cache_efficiency: { total_cache_read: 0, total_cache_creation: 0, total_uncached: 100, hit_ratio: 0 },
  avg_tokens_per_turn: 100,
  estimated_turns_remaining: 9,
  context_zone: 'green',
  total_turns: 1,
}

it.each(['displayed', 'pending'] as const)('clears %s Context analysis when the source changes', async state => {
  const page = pages[2]
  let scope = scopeFor(page)
  let finishAnalysis!: (response: Response) => void
  const pendingAnalysis = new Promise<Response>(resolve => { finishAnalysis = resolve })
  const fetcher = mockReads(page, () => scope, { reply: endpoint => {
    if (endpoint === 'context/active') {
      return Response.json({ sessions: scope.user === 'worker' ? [activeSession] : [], scope })
    }
    if (endpoint === 'context/old-project/original-session') {
      return state === 'pending' ? pendingAnalysis : Response.json({ analysis: contextAnalysis })
    }
  } })
  renderPage(page)
  fireEvent.click(await screen.findByRole('button', { name: /Old Context project/ }))
  if (state === 'displayed') expect(await screen.findByText('Cache Efficiency')).toBeInTheDocument()
  else expect(await screen.findByText('Analyzing session...')).toBeInTheDocument()

  scope = scopeFor(page, 'other-worker')
  if (state === 'displayed') {
    const hidden = vi.spyOn(document, 'hidden', 'get')
    try {
      hidden.mockReturnValue(true)
      fireEvent(document, new Event('visibilitychange'))
      hidden.mockReturnValue(false)
      fireEvent(document, new Event('visibilitychange'))
    } finally {
      hidden.mockRestore()
    }
  } else {
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  }
  expect(await screen.findByText("Reading other-worker's", { exact: false })).toBeInTheDocument()
  expect(screen.getByText(page.empty)).toBeInTheDocument()
  expect(screen.queryByText('Old Context project')).not.toBeInTheDocument()
  expect(screen.queryByText('Cache Efficiency')).not.toBeInTheDocument()
  expect(screen.queryByText('Analyzing session...')).not.toBeInTheDocument()

  await act(async () => { finishAnalysis(Response.json({ analysis: contextAnalysis })) })
  expect(screen.queryByText('Cache Efficiency')).not.toBeInTheDocument()
  expect(fetcher.mock.calls.filter(([url]) => url.includes('/context/old-project/original-session'))).toHaveLength(1)
})

it.each([
  { page: pages[0], endpoint: 'sessions/projects', status: 500, expected: 'Could not read session projects.' },
  { page: pages[0], endpoint: 'sessions', status: 500, expected: 'Could not read sessions.' },
  { page: pages[0], endpoint: 'sessions', status: 403, expected: 'The controller cannot read this data folder.' },
  { page: pages[2], endpoint: 'context/active', status: 500, expected: 'Could not read active sessions.' },
])('shows a read failure for $endpoint with status $status', async ({ page, endpoint, status, expected }) => {
  const detail = 'The controller cannot read /fixture/private/.claude.'
  mockReads(page, () => scopeFor(page), { reply: path => (
    path === endpoint ? Response.json({ detail }, { status }) : undefined
  ) })
  renderPage(page)

  expect(await screen.findByRole('alert')).toHaveTextContent(expected)
  expect(screen.queryByText(detail)).not.toBeInTheDocument()
  expect(screen.queryByText(page.empty)).not.toBeInTheDocument()
})

it.each([pages[0], pages[1]])('preserves the scoped user when filtering $name by project', async (page) => {
  const scrollIntoView = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView')
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
  try {
    const fetcher = mockReads(page, () => scopeFor(page))
    renderPage(page, '?scope_user=worker')
    await screen.findByText(page.empty)
    fireEvent.click(screen.getAllByRole('combobox')[0])
    fireEvent.click(await screen.findByRole('option', { name: /Fixture project/ }))

    const projectValue = page.name === 'sessions' ? 'fixture-project' : project.path
    const projectParam = page.name === 'sessions' ? 'project_folder' : 'project_path'
    expect(new URLSearchParams(window.location.search).get('scope_user')).toBe('worker')
    expect(new URLSearchParams(window.location.search).get('project')).toBe(projectValue)
    await waitFor(() => expect(fetcher.mock.calls.some(([url, init]) =>
      new URL(url, window.location.origin).searchParams.get(projectParam) === projectValue
      && new Headers(init?.headers).get('X-Harness-Scope-User') === 'worker',
    )).toBe(true))
  } finally {
    if (scrollIntoView) Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', scrollIntoView)
    else Reflect.deleteProperty(HTMLElement.prototype, 'scrollIntoView')
  }
})
