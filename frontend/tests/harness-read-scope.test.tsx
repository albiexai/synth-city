import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { BrowserRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SessionsPage } from '@/features/sessions/SessionsPage'
import { UsagePage } from '@/features/usage/UsagePage'
import { ContextPage } from '@/features/context/ContextPage'
import { PlansPage } from '@/features/plans/PlansPage'
import { clearNativeCatalogs, updateNativeCatalog, updateNativeMetadata } from '@/features/native-settings/surfaceRegistry'
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

function mockReads(page: Page, source: () => TestScope | undefined, options: { denied?: string; cost?: () => number } = {}) {
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
