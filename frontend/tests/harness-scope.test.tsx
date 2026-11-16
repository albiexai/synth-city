import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useEffect } from 'react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ProjectProvider } from '../src/contexts/ProjectContext'
import { ScopeSelector, ScopedPage, SurfaceScopeSelector } from '../src/features/projects/ScopeSelector'
import { DashboardProvider, useDashboard } from '../src/contexts/DashboardContext'
import { useProjectContext } from '../src/contexts/ProjectContext'
import { globalUserSurface, projectScopedSurface } from '../src/features/native-settings/projectScope'
import { apiClient, scopeUserHeaders } from '../src/lib/api'
import { fetchAgentTeamPresets } from '../src/features/agent-teams/api'
import type { HarnessScopeResponse } from '../src/types/harnessScope'

vi.mock('../src/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../src/lib/api')>()),
  apiClient: vi.fn(),
}))
vi.mock('../src/features/agent-teams/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../src/features/agent-teams/api')>()),
  fetchAgentTeamPresets: vi.fn(),
}))

// The dashboard reads these hooks. Their results are stable, as the real hooks' are.
const sessionsApi = { getDashboardStats: async () => ({}) }
const contextApi = { getActiveSessions: async () => ({ sessions: [] }) }
const plansApi = { getStats: async () => ({ total_plans: 0 }) }
vi.mock('../src/hooks/useSessionsApi', () => ({ useSessionsApi: () => sessionsApi }))
vi.mock('../src/hooks/useContextApi', () => ({ useContextApi: () => contextApi }))
vi.mock('../src/hooks/usePlansApi', () => ({ usePlansApi: () => plansApi }))
vi.mock('../src/contexts/ProviderContext', () => ({
  useProviderContext: () => ({ selectedProviderId: 'claude-code', selectedProvider: { display_name: 'Claude Code' } }),
}))

const twoUsers: HarnessScopeResponse = {
  default_user: 'agentuser',
  users: [
    { name: 'ctl', is_controller: true, is_default: false, claude_dir: '/home/ctl/.claude', exists: true, readable: true, writable: true },
    { name: 'agentuser', is_controller: false, is_default: true, claude_dir: '/home/agentuser/.claude', exists: true, readable: true, writable: true },
  ],
}
const oneUser: HarnessScopeResponse = { default_user: 'ctl', users: [{ ...twoUsers.users[0], is_default: true }] }

function serve(scope: HarnessScopeResponse) {
  vi.mocked(apiClient).mockImplementation((async (endpoint: string) => (endpoint === 'harness-scope' ? scope : { projects: [] })) as never)
}

function Search() {
  return <output aria-label="search">{useLocation().search}</output>
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <ProjectProvider><ScopeSelector /><Search /></ProjectProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(fetchAgentTeamPresets).mockResolvedValue({ presets: [] } as never)
  window.history.replaceState({}, '', '/')
})
afterEach(() => cleanup())

describe('global scope control', () => {
  it('names the directory that the global scope reads, for the default agent user', async () => {
    serve(twoUsers)
    renderAt('/harnesses/claude-code/skills')

    expect(await screen.findByText('/home/agentuser/.claude')).toBeInTheDocument()
    expect(screen.getByText(/as the user agentuser/)).toBeInTheDocument()
    expect(screen.getByLabelText('Global scope of')).toHaveValue('agentuser')
  })

  it('switches to the controller user through the page URL, and back to the default with no value', async () => {
    serve(twoUsers)
    renderAt('/harnesses/claude-code/skills')
    const select = await screen.findByLabelText('Global scope of')

    await userEvent.selectOptions(select, 'ctl')
    expect(screen.getByLabelText('search')).toHaveTextContent('?scope_user=ctl')
    expect(await screen.findByText('/home/ctl/.claude')).toBeInTheDocument()

    await userEvent.selectOptions(screen.getByLabelText('Global scope of'), 'agentuser')
    expect(screen.getByLabelText('search')).toHaveTextContent('')
    expect(screen.getByLabelText('search').textContent).toBe('')
  })

  it('reads the user from the URL and falls back to the default for an unknown name', async () => {
    serve(twoUsers)
    renderAt('/harnesses/claude-code/skills?scope_user=ctl')
    expect(await screen.findByText('/home/ctl/.claude')).toBeInTheDocument()
    cleanup()

    renderAt('/harnesses/claude-code/skills?scope_user=nobody')
    expect(await screen.findByText('/home/agentuser/.claude')).toBeInTheDocument()
  })

  it('shows no user picker on a single-user install, only the directory', async () => {
    serve(oneUser)
    renderAt('/harnesses/claude-code/skills')

    expect(await screen.findByText('/home/ctl/.claude')).toBeInTheDocument()
    expect(screen.queryByLabelText('Global scope of')).not.toBeInTheDocument()
    expect(screen.queryByText(/as the user/)).not.toBeInTheDocument()
  })

  it('says when the controller cannot write or read the directory', async () => {
    serve({ ...twoUsers, users: [twoUsers.users[0], { ...twoUsers.users[1], writable: false }] })
    renderAt('/harnesses/claude-code/skills')
    expect(await screen.findByText(/cannot write to this directory/)).toBeInTheDocument()
    cleanup()

    serve({ ...twoUsers, users: [twoUsers.users[0], { ...twoUsers.users[1], readable: false, writable: false }] })
    renderAt('/harnesses/claude-code/skills')
    expect(await screen.findByText(/cannot read this directory/)).toBeInTheDocument()
    expect(screen.queryByText(/cannot write to this directory/)).not.toBeInTheDocument()
  })

  it('keeps working, with the project control only, when the scope read fails', async () => {
    vi.mocked(apiClient).mockImplementation((async (endpoint: string) => {
      if (endpoint === 'harness-scope') throw new Error('down')
      return { projects: [] }
    }) as never)
    renderAt('/harnesses/claude-code/skills')

    await waitFor(() => expect(apiClient).toHaveBeenCalledWith('harness-scope'))
    expect(screen.getByLabelText('Project')).toBeInTheDocument()
    expect(screen.queryByText(/Global scope reads/)).not.toBeInTheDocument()
  })
})

describe('scope header', () => {
  it('sends the chosen user from a Harnesses page URL only', () => {
    window.history.replaceState({}, '', '/harnesses/claude-code/skills?scope_user=ctl')
    expect(scopeUserHeaders()).toEqual({ 'X-Harness-Scope-User': 'ctl' })

    window.history.replaceState({}, '', '/sessions?scope_user=ctl')
    expect(scopeUserHeaders()).toEqual({})

    window.history.replaceState({}, '', '/harnesses/claude-code/skills')
    expect(scopeUserHeaders()).toEqual({})
  })

  it('offers the global user only on the surfaces whose backend carries the scope', () => {
    for (const surface of ['summary', 'config', 'mcp', 'plugins', 'commands', 'hooks', 'permissions', 'agents', 'skills', 'memory', 'output-styles']) {
      expect(globalUserSurface(surface), surface).toBe(true)
    }
    for (const surface of ['backup', 'plans', 'sessions', 'usage', 'statusline', null]) {
      expect(globalUserSurface(surface), String(surface)).toBe(false)
    }
  })

  it('treats a harness own page as project-scoped, so the tile counts follow the project', () => {
    expect(projectScopedSurface('/harnesses/claude-code')).toBe('summary')
    expect(projectScopedSurface('/harnesses')).toBeNull()
    expect(projectScopedSurface('/harnesses/claude-code/skills')).toBe('skills')
  })
})

describe('page remount', () => {
  it('opens a page again, so it reads again, when the global user changes', async () => {
    serve(twoUsers)
    const mounts: string[] = []
    function Page() {
      useEffect(() => { mounts.push('read') }, [])
      return <p>page</p>
    }
    render(
      <MemoryRouter initialEntries={['/harnesses/claude-code/skills']}>
        <ProjectProvider><ScopeSelector /><ScopedPage><Page /></ScopedPage></ProjectProvider>
      </MemoryRouter>,
    )
    await screen.findByText('/home/agentuser/.claude')
    expect(mounts).toHaveLength(1)

    await userEvent.selectOptions(screen.getByLabelText('Global scope of'), 'ctl')
    await waitFor(() => expect(mounts).toHaveLength(2))
  })
})

describe('unscoped surfaces', () => {
  it('keep only the project picker, with no agent user and no directory line', async () => {
    serve(twoUsers)
    render(
      <MemoryRouter initialEntries={['/harnesses/claude-code/backup']}>
        <ProjectProvider><ScopeSelector showGlobalUser={false} /></ProjectProvider>
      </MemoryRouter>,
    )

    expect(await screen.findByLabelText('Project')).toBeInTheDocument()
    await waitFor(() => expect(apiClient).toHaveBeenCalled())
    expect(screen.queryByLabelText('Global scope of')).not.toBeInTheDocument()
    expect(screen.queryByText(/Global scope reads/)).not.toBeInTheDocument()
  })
})

describe('surface scope control', () => {
  it.each([
    ['claude-code', 'skills', true], ['claude-code', 'mcp', true], ['claude-code', 'summary', true],
    ['claude-code', 'backup', false], ['claude-code', 'plans', false], ['codex-cli', 'skills', false],
  ])('%s %s: global user picker %s', async (providerId, surface, shown) => {
    serve(twoUsers)
    render(
      <MemoryRouter initialEntries={[`/harnesses/${providerId}/${surface}`]}>
        <ProjectProvider><SurfaceScopeSelector providerId={providerId} surface={surface} /></ProjectProvider>
      </MemoryRouter>,
    )

    expect(await screen.findByLabelText('Project')).toBeInTheDocument()
    if (shown) expect(await screen.findByLabelText('Global scope of')).toBeInTheDocument()
    else {
      await waitFor(() => expect(apiClient).toHaveBeenCalledWith('harness-scope'))
      expect(screen.queryByLabelText('Global scope of')).not.toBeInTheDocument()
      expect(screen.queryByText(/Global scope reads/)).not.toBeInTheDocument()
    }
  })
})

describe('tile counts', () => {
  const PROJECT = '/work/p'
  function skillReads() {
    return vi.mocked(apiClient).mock.calls.filter(([endpoint]) => String(endpoint).startsWith('agents/skills')).map(([endpoint]) => String(endpoint))
  }
  function Controls() {
    const { setProjectPath, setScopeUser } = useProjectContext()
    useDashboard({ autoFetch: true })
    return (
      <>
        <button onClick={() => setProjectPath(PROJECT)}>choose project</button>
        <button onClick={() => setProjectPath(null)}>back to global</button>
        <button onClick={() => setScopeUser('ctl')}>choose controller</button>
      </>
    )
  }
  function mountDashboard(path = '/harnesses/claude-code') {
    vi.mocked(apiClient).mockImplementation((async (endpoint: string) => {
      if (endpoint === 'projects') return { projects: [{ id: 1, name: 'p', path: PROJECT, is_active: false, last_accessed: '', created_at: '' }] }
      return {}
    }) as never)
    return render(
      <MemoryRouter initialEntries={[path]}>
        <ProjectProvider><DashboardProvider><Controls /></DashboardProvider></ProjectProvider>
      </MemoryRouter>,
    )
  }

  it('read again on the first change from Global to a project, not only on later ones', async () => {
    mountDashboard()
    await waitFor(() => expect(skillReads()).toHaveLength(1))
    expect(skillReads()[0]).not.toContain('project_path')

    await userEvent.click(screen.getByRole('button', { name: 'choose project' }))
    await waitFor(() => expect(skillReads()).toHaveLength(2))
    expect(skillReads()[1]).toContain(`project_path=${encodeURIComponent(PROJECT)}`)

    await userEvent.click(screen.getByRole('button', { name: 'back to global' }))
    await waitFor(() => expect(skillReads()).toHaveLength(3))
    expect(skillReads()[2]).not.toContain('project_path')
  })

  it('read again when a project in the page URL hydrates after the first read', async () => {
    mountDashboard(`/harnesses/claude-code?project=${encodeURIComponent(PROJECT)}`)
    // The project list arrives after the first read, so the first read is Global and a second one follows.
    await waitFor(() => expect(skillReads().some((url) => url.includes('project_path='))).toBe(true))
  })

  it('read again when the global user changes, and not when nothing changes', async () => {
    mountDashboard()
    await waitFor(() => expect(skillReads()).toHaveLength(1))

    await userEvent.click(screen.getByRole('button', { name: 'choose controller' }))
    await waitFor(() => expect(skillReads()).toHaveLength(2))
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(skillReads()).toHaveLength(2)
  })
})
