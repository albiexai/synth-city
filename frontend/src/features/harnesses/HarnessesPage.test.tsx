import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { harnesses } from '@/features/factory/filterHelpers'
import { ProjectProvider } from '@/contexts/ProjectContext'
import { ProviderProvider } from '@/contexts/ProviderContext'
import { resetProviderOperations } from '@/hooks/useProviders'
import type { AgentProviderId, AgentProviderStatus, ProviderOperations } from '@/types/providers'
import { providerOperationKeys } from '@/types/providers'
import { HarnessesPage } from './HarnessesPage'

const providerIds: AgentProviderId[] = ['claude-code', 'codex-cli', 'copilot-cli', 'pi-cli', 'muse-cli']

function providerStatus(id: AgentProviderId): AgentProviderStatus {
  return {
    id,
    display_name: harnesses[id],
    binary_name: id,
    installed: true,
    binary_path: `/usr/bin/${id}`,
    version: '1.0.0',
    capabilities: {
      config: false, sessions: false, spawn: false, resume: false, fork: false, mcp: false, plugins: false,
      permissions: false, commands: false, agents: false, skills: false, hooks: false, memory: false,
      output_styles: false, statusline: false, usage: false, context: false, doctor: false, backup: false, plans: false,
    },
    capability_matrix: {},
    config_paths: {},
  }
}

function operatingCatalog(id: AgentProviderId, configuration: 'ready' | 'unknown', probeState: 'observed' | 'pending' = 'observed'): ProviderOperations {
  type Operation = ProviderOperations['operations'][(typeof providerOperationKeys)[number]]
  const operation: Operation = { state: 'unknown', reason: 'Not observed', conditions: [], evidence: [] }
  return {
    schema_version: 1,
    provider: id,
    provider_display_name: harnesses[id],
    operations: Object.fromEntries(providerOperationKeys.map(key => [key, operation])) as ProviderOperations['operations'],
    native_capabilities: {},
    native_surfaces: {},
    readiness: {
      configuration: { state: configuration, checks: [] },
      credentials: { state: 'unknown', reason: 'Credentials not checked', source: null, observed_at: null },
      session: { state: 'unknown', reason: 'No team slot selected', team_id: null, slot_id: null, member_id: null, session_id: null, observed_provider: null },
      observed_at: '2026-11-17T20:00:00.000Z',
      observation_started_at: null,
      probe_state: probeState,
      cache_ttl_seconds: 60,
      request_wait_seconds: 5,
      aggregate_probe_seconds: 10,
    },
  }
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

function stubApi(catalogs: Partial<Record<AgentProviderId, ProviderOperations>>) {
  const providers = providerIds.map(providerStatus)
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const { pathname } = new URL(input, 'http://localhost')
    if (pathname.endsWith('/providers')) return json({ providers, count: providers.length })
    const match = /\/providers\/([^/]+)\/operations$/.exec(pathname)
    const catalog = match ? catalogs[match[1] as AgentProviderId] : undefined
    if (catalog) return json(catalog)
    return json({ detail: 'not stubbed' }, 404)
  }))
}

const statusWords = ['Not installed', 'Needs setup', 'Not checked yet', 'Ready'] as const

function statusIn(text: string | null | undefined): string | null {
  return statusWords.find(word => text?.includes(word)) ?? null
}

async function listStatus(id: AgentProviderId): Promise<string | null> {
  let label: string | null = null
  await waitFor(() => {
    const heading = screen.getByRole('heading', { level: 3, name: new RegExp(`^${harnesses[id]}`) })
    label = statusIn(heading.textContent)
    expect(label).not.toBeNull()
  })
  return label
}

async function pageStatus(id: AgentProviderId): Promise<string | null> {
  let label: string | null = null
  await waitFor(() => {
    const heading = screen.getByRole('heading', { level: 2, name: harnesses[id] })
    label = statusIn(heading.parentElement?.textContent)
    expect(label).not.toBeNull()
  })
  return label
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <ProjectProvider>
        <ProviderProvider>
          <Routes>
            <Route path="/harnesses" element={<HarnessesPage />} />
            <Route path="/harnesses/:providerId" element={<HarnessesPage />} />
          </Routes>
        </ProviderProvider>
      </ProjectProvider>
    </MemoryRouter>,
  )
}

async function compareListAndPages(catalogs: Partial<Record<AgentProviderId, ProviderOperations>>) {
  stubApi(catalogs)
  const listed: Record<string, string | null> = {}
  const list = renderAt('/harnesses')
  for (const id of providerIds) listed[id] = await listStatus(id)
  list.unmount()

  const pages: Record<string, string | null> = {}
  for (const id of providerIds) {
    resetProviderOperations()
    const page = renderAt(`/harnesses/${id}`)
    pages[id] = await pageStatus(id)
    page.unmount()
  }
  return { listed, pages }
}

describe('harness status on the list and on each page', () => {
  beforeEach(() => {
    resetProviderOperations()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    resetProviderOperations()
  })

  it('reads the same status for ready operating data', async () => {
    const catalogs = Object.fromEntries(providerIds.map(id => [id, operatingCatalog(id, 'ready')]))
    const { listed, pages } = await compareListAndPages(catalogs)
    expect(pages).toEqual(listed)
  })

  it('reads the same status for not-yet-observed operating data', async () => {
    const catalogs = Object.fromEntries(providerIds.map(id => [id, operatingCatalog(id, 'unknown')]))
    const { listed, pages } = await compareListAndPages(catalogs)
    expect(pages).toEqual(listed)
  })

  it('shows the observed readiness shortly after a pending observation completes', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      let observed = false
      const providers = providerIds.map(providerStatus)
      vi.stubGlobal('fetch', vi.fn(async (input: string) => {
        const { pathname } = new URL(input, 'http://localhost')
        if (pathname.endsWith('/providers')) return json({ providers, count: providers.length })
        if (pathname.endsWith('/claude-code/operations')) {
          return json(observed ? operatingCatalog('claude-code', 'ready') : operatingCatalog('claude-code', 'unknown', 'pending'))
        }
        return json({ detail: 'not stubbed' }, 404)
      }))
      renderAt('/harnesses')
      await waitFor(() => expect(statusIn(screen.getByRole('heading', { level: 3, name: /^Claude Code/ }).textContent)).toBe('Not checked yet'))

      observed = true
      await vi.advanceTimersByTimeAsync(10_000)

      expect(statusIn(screen.getByRole('heading', { level: 3, name: /^Claude Code/ }).textContent)).toBe('Ready')
    } finally {
      vi.useRealTimers()
    }
  })

  it('retries a pending observation once per window and stops after unmount', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const providers = providerIds.map(providerStatus)
      const operationCalls = vi.fn(async () => json(operatingCatalog('claude-code', 'unknown', 'pending')))
      vi.stubGlobal('fetch', vi.fn(async (input: string) => {
        const { pathname } = new URL(input, 'http://localhost')
        if (pathname.endsWith('/providers')) return json({ providers, count: providers.length })
        if (pathname.endsWith('/claude-code/operations')) return operationCalls()
        return json({ detail: 'not stubbed' }, 404)
      }))
      const view = renderAt('/harnesses')
      await waitFor(() => expect(operationCalls).toHaveBeenCalledTimes(1))

      await vi.advanceTimersByTimeAsync(5_000)
      expect(operationCalls).toHaveBeenCalledTimes(2)

      view.unmount()
      await vi.advanceTimersByTimeAsync(20_000)
      expect(operationCalls).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })
})
