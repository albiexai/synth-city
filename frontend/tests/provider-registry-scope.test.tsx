import { act, cleanup, render, screen } from '@testing-library/react'
import { BrowserRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Header } from '../src/components/layout/Header'
import { ProviderProvider } from '../src/contexts/ProviderContext'
import { ThemeProvider } from '../src/contexts/ThemeContext'
import { updateNativeMetadata } from '../src/features/native-settings/surfaceRegistry'
import { resetProviderOperations } from '../src/hooks/useProviders'
import { jsonResponse, settle } from './helpers/factory'

// #1069: the provider registry follows the selected scope, and the header never shows another user's Claude Code version
// (nor the controller's version from /status) as the selected user's, not even while the new registry loads.
// A real BrowserRouter is used on purpose: the request header is built from window.location, and only the history API
// changes both the router and window.location together, as in the live app.

type Scope = 'agent' | 'controller'

const claude = (extra: object) => ({
  id: 'claude-code', display_name: 'Claude Code', binary_name: 'claude', installed: true, binary_path: null, version: null,
  config_paths: {}, capabilities: {}, capability_matrix: {}, ...extra,
})
const AGENT = claude({ version: '2.1.326', version_user: 'agentuser', version_source: 'native', version_unknown_reason: null })
const AGENT_UNKNOWN = claude({
  version: null, version_user: 'agentuser', version_source: null,
  version_unknown_reason: "version unknown (cannot read the agent user's install)",
})
const CONTROLLER = claude({ version: '9.9.9' })

/** A backend: the registry answers by scope header, /status always carries the CONTROLLER's version. */
function install(hold: Scope[], agent: object = AGENT) {
  const calls: { path: string; scope: string | null }[] = []
  const waiting: Record<Scope, Array<() => void>> = { agent: [], controller: [] }
  const fetch = vi.fn((input: string | URL | Request, options?: RequestInit) => {
    const path = new URL(String(input), 'http://fixture.test').pathname.replace('/api/v1/', '')
    const header = (options?.headers as Record<string, string> | undefined)?.['X-Harness-Scope-User'] ?? null
    calls.push({ path, scope: header })
    if (path === 'status') {
      return Promise.resolve(jsonResponse({ claude_code_version: '9.9.9', active_sessions: 0, providers: { 'claude-code': CONTROLLER } }))
    }
    if (path === 'providers') {
      const scope: Scope = header === 'controller' ? 'controller' : 'agent'
      const body = jsonResponse({ providers: [scope === 'controller' ? CONTROLLER : agent] })
      if (!hold.includes(scope)) return Promise.resolve(body)
      return new Promise<Response>(resolve => { waiting[scope].push(() => resolve(body)) })   // answers only when released
    }
    return Promise.resolve(jsonResponse({}))
  })
  vi.stubGlobal('fetch', fetch)
  return { calls, release: (scope: Scope) => { waiting[scope].splice(0).forEach(answer => answer()) } }
}

function mount() {
  localStorage.setItem('theme', 'dark')            // a stored theme, so the theme provider needs no matchMedia
  return render(<BrowserRouter><ThemeProvider><ProviderProvider><Header /></ProviderProvider></ThemeProvider></BrowserRouter>)
}

/** The header tooltip starts with the count, then shows one version line per harness. */
const title = () => screen.getByRole('link', { name: /Open Harnesses/ }).getAttribute('title') ?? ''

function versionLine() {
  const [count, version] = title().split('\n')
  expect(count).toBe('1 of 1 supported harness installed for this factory')
  return version
}

function go(url: string) {
  act(() => { window.history.pushState({}, '', url); window.dispatchEvent(new PopStateEvent('popstate')) })
}

beforeEach(() => { resetProviderOperations(); updateNativeMetadata([]); localStorage.clear(); window.history.replaceState({}, '', '/harnesses') })
afterEach(() => { cleanup(); vi.unstubAllGlobals(); resetProviderOperations(); updateNativeMetadata([]); localStorage.clear(); window.history.replaceState({}, '', '/') })

describe('the provider registry follows the selected scope', () => {
  it('fetches again at once under the new scope and shows no Claude version while it loads', async () => {
    const server = install(['controller'])
    mount(); await settle()
    expect(versionLine()).toBe('Claude Code v2.1.326 (agentuser)')

    go('/harnesses?scope_user=controller'); await settle()

    const lastRegistryRequest = server.calls.filter(call => call.path === 'providers').at(-1)
    expect(lastRegistryRequest?.scope).toBe('controller')                        // a request for the new scope, with its header
    expect(title()).not.toMatch(/agentuser|2\.1\.326|9\.9\.9/)                   // neither the old user's version nor the controller's
    server.release('controller'); await settle()
    expect(versionLine()).toBe('Claude Code v9.9.9')                                   // the controller's own scope, once it has arrived
  })

  it('ignores a slow answer for the scope the user has already left', async () => {
    const server = install(['agent'])                                            // the agent registry answers late
    mount(); await settle()

    go('/harnesses?scope_user=controller'); await settle()
    expect(versionLine()).toBe('Claude Code v9.9.9')
    server.release('agent'); await settle()                                      // the late answer for the old scope arrives now

    expect(versionLine()).toBe('Claude Code v9.9.9')
    expect(title()).not.toMatch(/agentuser|2\.1\.326/)
  })

  it('goes back to the agent user\'s version when the user switches back, without showing the controller\'s meanwhile', async () => {
    const server = install(['agent'])
    mount(); server.release('agent'); await settle()
    go('/harnesses?scope_user=controller'); await settle()
    expect(versionLine()).toBe('Claude Code v9.9.9')

    go('/harnesses'); await settle()                                             // the agent registry is held again: still loading

    expect(title()).not.toMatch(/agentuser|2\.1\.326|9\.9\.9/)
    server.release('agent'); await settle()
    expect(versionLine()).toBe('Claude Code v2.1.326 (agentuser)')
  })
})

describe('the header takes Claude Code\'s version from the registry, never from /status', () => {
  it('shows the exact unknown message, not the controller\'s /status version, when the user\'s version cannot be read', async () => {
    install([], AGENT_UNKNOWN)

    mount(); await settle()

    expect(versionLine()).toBe("Claude Code version unknown (cannot read the agent user's install)")
    expect(title()).not.toContain('9.9.9')
  })
})
