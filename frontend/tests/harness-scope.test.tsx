import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useEffect } from 'react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ProjectProvider } from '../src/contexts/ProjectContext'
import { ScopeSelector, ScopedPage } from '../src/features/projects/ScopeSelector'
import { projectScopedSurface } from '../src/features/native-settings/projectScope'
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
