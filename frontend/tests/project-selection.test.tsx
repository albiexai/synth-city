import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Link, MemoryRouter, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ProjectProvider, useProjectContext } from '../src/contexts/ProjectContext'
import { ProjectSelector } from '../src/features/projects/ProjectSelector'
import { apiClient } from '../src/lib/api'
import { fetchAgentTeamPresets } from '../src/features/agent-teams/api'

vi.mock('../src/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../src/lib/api')>()),
  apiClient: vi.fn(),
}))
vi.mock('../src/features/agent-teams/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../src/features/agent-teams/api')>()),
  fetchAgentTeamPresets: vi.fn(),
}))

const TEAM_FOLDER = '/work/release/harness-live'
const TRACKED_FOLDER = '/work/other/tracked-app'

function trackedProject(isActive: boolean) {
  return { id: 7, name: 'tracked-app', path: TRACKED_FOLDER, is_active: isActive, last_accessed: '', created_at: '' }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(apiClient).mockResolvedValue({ projects: [trackedProject(true)] })
  vi.mocked(fetchAgentTeamPresets).mockResolvedValue({
    presets: [{ id: 1, slots: [{ id: 2, display_name: 'F3 implementer', provider: 'claude-code', repo_path: TEAM_FOLDER }] }],
  } as never)
})

function Probe() {
  const { activeProject, error } = useProjectContext()
  const { search } = useLocation()
  return (
    <>
      <output aria-label="active project">{activeProject?.path ?? 'none'}</output>
      <output aria-label="project error">{error ?? 'no error'}</output>
      <output aria-label="search">{search}</output>
    </>
  )
}

function renderAt(path: string, extra?: React.ReactNode) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <ProjectProvider><Probe />{extra}</ProjectProvider>
    </MemoryRouter>,
  )
}

describe('per-page project choice', () => {
  it('reads the project from the page URL, not from the server-wide active flag', async () => {
    renderAt(`/harnesses/claude-code/mcp?project=${encodeURIComponent(TEAM_FOLDER)}`)

    await waitFor(() => expect(screen.getByLabelText('active project')).toHaveTextContent(TEAM_FOLDER))
  })

  it('shows no project and no error for a missing value, even if the server marks one active', async () => {
    renderAt('/harnesses/claude-code/mcp')

    await waitFor(() => expect(apiClient).toHaveBeenCalled())
    expect(screen.getByLabelText('active project')).toHaveTextContent('none')
    expect(screen.getByLabelText('project error')).toHaveTextContent('no error')
  })

  it('shows no project and no error for an unknown value', async () => {
    renderAt(`/harnesses/claude-code/mcp?project=${encodeURIComponent('/not/a/known/folder')}`)

    await waitFor(() => expect(apiClient).toHaveBeenCalled())
    expect(screen.getByLabelText('active project')).toHaveTextContent('none')
    expect(screen.getByLabelText('project error')).toHaveTextContent('no error')
  })

  it('ignores ?project= on pages that are not project-scoped', async () => {
    renderAt(`/sessions?project=${encodeURIComponent(TEAM_FOLDER)}`)

    await waitFor(() => expect(apiClient).toHaveBeenCalled())
    expect(screen.getByLabelText('active project')).toHaveTextContent('none')
  })
})

describe('ProjectSelector', () => {
  it('lists team members\' folders without typing a path and writes the choice to the URL only', async () => {
    const user = userEvent.setup()
    renderAt('/harnesses/claude-code/mcp', <ProjectSelector />)

    const select = await screen.findByLabelText('Project')
    await waitFor(() => expect(screen.getByRole('option', { name: /harness-live · F3 implementer/ })).toBeInTheDocument())
    expect(screen.getByRole('option', { name: /tracked-app/ })).toBeInTheDocument()

    await user.selectOptions(select, TEAM_FOLDER)

    await waitFor(() => expect(screen.getByLabelText('active project')).toHaveTextContent(TEAM_FOLDER))
    expect(screen.getByLabelText('search')).toHaveTextContent(`project=${encodeURIComponent(TEAM_FOLDER)}`)
    const calls = vi.mocked(apiClient).mock.calls.map(([endpoint, options]) => `${(options as { method?: string } | undefined)?.method ?? 'GET'} ${endpoint}`)
    expect(calls.filter(call => call !== 'GET projects')).toEqual([])
  })

  it('keeps the choice of one page out of another page', async () => {
    const first = renderAt(`/harnesses/claude-code/mcp?project=${encodeURIComponent(TEAM_FOLDER)}`)
    await waitFor(() => expect(screen.getByLabelText('active project')).toHaveTextContent(TEAM_FOLDER))
    first.unmount()

    renderAt('/harnesses/claude-code/commands')
    await waitFor(() => expect(apiClient).toHaveBeenCalled())
    expect(screen.getByLabelText('active project')).toHaveTextContent('none')
  })

  it('offers "Add a folder…" as the fallback', async () => {
    const user = userEvent.setup()
    renderAt('/harnesses/claude-code/mcp', <ProjectSelector />)

    await user.selectOptions(await screen.findByLabelText('Project'), 'Add a folder…')

    expect(screen.getByLabelText('Folder path')).toBeInTheDocument()
  })
})

const CODEX_FOLDER = '/work/release/codex-live'
const SHARED_FOLDER = '/work/release/shared'
const UNLABELED_FOLDER = '/work/release/unlabeled'
const MOVED_FOLDER = '/work/release/harness-moved'

function trackedAt(path: string, name: string) {
  return { id: 8, name, path, is_active: false, last_accessed: '', created_at: '' }
}

describe('harness pages offer their own members\' folders', () => {
  beforeEach(() => {
    vi.mocked(fetchAgentTeamPresets).mockResolvedValue({
      presets: [{
        id: 1,
        slots: [
          { id: 2, display_name: 'F3 implementer', provider: 'claude-code', repo_path: TEAM_FOLDER },
          { id: 3, display_name: 'C1 builder', provider: 'codex-cli', repo_path: CODEX_FOLDER },
          { id: 4, display_name: 'F4 implementer', provider: 'claude-code', repo_path: SHARED_FOLDER },
          { id: 5, display_name: 'C2 builder', provider: 'codex-cli', repo_path: SHARED_FOLDER },
          { id: 6, display_name: 'Unlabeled member', repo_path: UNLABELED_FOLDER },
        ],
      }],
    } as never)
  })

  it('a Codex page offers its own folder and not the Claude Code folder', async () => {
    renderAt('/harnesses/codex-cli/mcp', <ProjectSelector />)

    await waitFor(() => expect(screen.getByRole('option', { name: 'codex-live · C1 builder' })).toBeInTheDocument())
    expect(screen.queryByRole('option', { name: /harness-live/ })).not.toBeInTheDocument()
  })

  it('a shared folder lists only the members of the page\'s own harness', async () => {
    const claude = renderAt('/harnesses/claude-code/mcp', <ProjectSelector />)
    await waitFor(() => expect(screen.getByRole('option', { name: 'shared · F4 implementer' })).toBeInTheDocument())
    expect(screen.queryByRole('option', { name: /C2 builder/ })).not.toBeInTheDocument()
    claude.unmount()

    renderAt('/harnesses/codex-cli/mcp', <ProjectSelector />)
    await waitFor(() => expect(screen.getByRole('option', { name: 'shared · C2 builder' })).toBeInTheDocument())
    expect(screen.queryByRole('option', { name: /F4 implementer/ })).not.toBeInTheDocument()
  })

  it('a member with no harness is offered on no harness page', async () => {
    const claude = renderAt('/harnesses/claude-code/mcp', <ProjectSelector />)
    await waitFor(() => expect(screen.getByRole('option', { name: 'shared · F4 implementer' })).toBeInTheDocument())
    expect(screen.queryByRole('option', { name: /unlabeled/ })).not.toBeInTheDocument()
    claude.unmount()

    renderAt('/harnesses/codex-cli/mcp', <ProjectSelector />)
    await waitFor(() => expect(screen.getByRole('option', { name: 'shared · C2 builder' })).toBeInTheDocument())
    expect(screen.queryByRole('option', { name: /unlabeled/ })).not.toBeInTheDocument()
  })

  it('a tracked project at another harness\'s member folder is hidden and does not resolve', async () => {
    vi.mocked(apiClient).mockResolvedValue({ projects: [trackedAt(TEAM_FOLDER, 'harness-live')] })
    renderAt(`/harnesses/codex-cli/mcp?project=${encodeURIComponent(TEAM_FOLDER)}`, <ProjectSelector />)

    await waitFor(() => expect(screen.getByRole('option', { name: 'codex-live · C1 builder' })).toBeInTheDocument())
    expect(screen.queryByRole('option', { name: /harness-live/ })).not.toBeInTheDocument()
    expect(screen.getByLabelText('active project')).toHaveTextContent('none')
  })

  it('a tracked project at another harness\'s member folder is hidden when its path has a trailing slash', async () => {
    vi.mocked(apiClient).mockResolvedValue({ projects: [trackedAt(`${TEAM_FOLDER}/`, 'harness-live')] })
    renderAt(`/harnesses/codex-cli/mcp?project=${encodeURIComponent(`${TEAM_FOLDER}/`)}`, <ProjectSelector />)

    await waitFor(() => expect(screen.getByRole('option', { name: 'codex-live · C1 builder' })).toBeInTheDocument())
    expect(screen.queryByRole('option', { name: /harness-live/ })).not.toBeInTheDocument()
    expect(screen.getByLabelText('active project')).toHaveTextContent('none')
  })

  it('a member folder moved since the last visit is listed after returning to the harness page in the app', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter initialEntries={['/harnesses/claude-code/mcp']}>
        <ProjectProvider>
          <ProjectSelector />
          <Link to="/sessions">away</Link>
          <Link to="/harnesses/claude-code/mcp">back</Link>
        </ProjectProvider>
      </MemoryRouter>,
    )
    await waitFor(() => expect(screen.getByRole('option', { name: 'harness-live · F3 implementer' })).toBeInTheDocument())

    vi.mocked(fetchAgentTeamPresets).mockResolvedValue({
      presets: [{ id: 1, slots: [{ id: 2, display_name: 'F3 implementer', provider: 'claude-code', repo_path: MOVED_FOLDER }] }],
    } as never)
    await user.click(screen.getByRole('link', { name: 'away' }))
    await user.click(screen.getByRole('link', { name: 'back' }))

    await waitFor(() => expect(screen.getByRole('option', { name: 'harness-moved · F3 implementer' })).toBeInTheDocument())
    expect(screen.queryByRole('option', { name: /harness-live/ })).not.toBeInTheDocument()
  })

  it('a harness page offers nothing when team membership cannot be read', async () => {
    vi.mocked(fetchAgentTeamPresets).mockRejectedValue(new Error('unavailable'))
    renderAt(`/harnesses/claude-code/mcp?project=${encodeURIComponent(TRACKED_FOLDER)}`, <ProjectSelector />)

    await waitFor(() => expect(fetchAgentTeamPresets).toHaveBeenCalled())
    await act(() => new Promise((resolve) => setTimeout(resolve, 0)))
    expect(screen.getByLabelText('active project')).toHaveTextContent('none')
    expect(screen.queryByRole('option', { name: /tracked-app/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /harness-live/ })).not.toBeInTheDocument()
  })

  it('a page that is not a harness page keeps every team folder', async () => {
    renderAt('/sessions', <ProjectSelector />)

    await waitFor(() => expect(screen.getByRole('option', { name: 'harness-live · F3 implementer' })).toBeInTheDocument())
    expect(screen.getByRole('option', { name: 'codex-live · C1 builder' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'unlabeled · Unlabeled member' })).toBeInTheDocument()
  })

  it('offers a tracked project outside every team folder on each harness page', async () => {
    for (const harness of ['claude-code', 'codex-cli']) {
      const view = renderAt(`/harnesses/${harness}/mcp`, <ProjectSelector />)
      await waitFor(() => expect(screen.getByRole('option', { name: /tracked-app/ })).toBeInTheDocument())
      view.unmount()
    }
  })
})
