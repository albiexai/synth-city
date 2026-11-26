import type { ComponentProps } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { AgentTeamPreset, GithubWorkItem, TeamGithubScope } from '@/types/agentTeams'
import { AutonomyPanel } from './AutonomyPanel'

vi.mock('./BacklogCoordination', () => ({ BacklogCoordination: () => null }))
vi.mock('./WorkPublicationPanel', () => ({ WorkPublicationPanel: () => null }))
vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return { ...actual, fetchGithubRecoveryGateActive: vi.fn().mockResolvedValue({ active: false }) }
})

type PanelProps = ComponentProps<typeof AutonomyPanel>

const preset: AgentTeamPreset = {
  id: 1,
  name: 'Test team',
  created_at: '2026-09-29T12:00:00Z',
  updated_at: '2026-09-29T12:00:00Z',
  autonomy_enabled: false,
  slots: [],
}

const scope: TeamGithubScope = {
  id: 2,
  preset_id: 1,
  repo_owner: 'example',
  repo_name: 'project',
  repo_path: '/tmp/project',
  dispatch_label: 'synth-city-v43-ready',
  design_label: 'synth-city-v43-design',
  merge_policy: 'human',
  github_auth_mode: 'app',
  github_auth_configured: true,
  github_poll_token_configured: true,
  max_approval_rounds: 3,
  max_concurrent_dispatched: 3,
  max_verification_retries: 2,
  max_auto_merges_per_day: 5,
  base_ref: 'origin/HEAD',
  builds_out_of_tree: false,
  build_dir_template: 'build',
  build_command_hint: 'meson compile',
  max_build_parallelism: 4,
  continuation_enabled: false,
  max_continuation_revisions: 3,
  max_continuation_failed_heads: 8,
  max_failed_heads_per_revision: 1,
  max_scope_paths: 4,
  max_scope_commands: 8,
  enabled: true,
  created_at: '2026-09-29T12:00:00Z',
  updated_at: '2026-09-29T12:00:00Z',
}

function dispatchedItem(id: number): GithubWorkItem {
  return {
    id,
    scope_id: 2,
    repo_owner: 'example',
    repo_name: 'project',
    issue_number: 900 + id,
    issue_title: `Item ${id}`,
    issue_url: `https://github.com/example/project/issues/${900 + id}`,
    github_updated_at: '2026-09-29T12:00:00Z',
    issue_type: 'code',
    dispatch_status: 'dispatched',
    approval_round_count: 0,
    retry_count: 0,
    active_scope_revision: 0,
    attempt_phase: 'implementation',
    diagnostic_retry_count: 0,
    retry_allowed: false,
    created_at: '2026-09-29T12:00:00Z',
    updated_at: '2026-09-29T12:00:00Z',
  }
}

function renderPanel(overrides: Partial<PanelProps> = {}) {
  const props: PanelProps = {
    preset,
    scopes: [scope],
    workItems: [dispatchedItem(1), dispatchedItem(2)],
    loading: false,
    refreshing: false,
    lastRefreshedAt: null,
    loadError: null,
    onRefresh: vi.fn().mockResolvedValue(undefined),
    onToggleAutonomy: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  }
  return render(
    <MemoryRouter>
      <AutonomyPanel {...props} />
    </MemoryRouter>
  )
}

describe('AutonomyPanel item count', () => {
  it('shows loaded items against the team total when the page is truncated', () => {
    renderPanel({ workItemTotal: 137 })

    expect(screen.queryByText('2 of 137 items · 2 active (counts are for loaded items only)')).not.toBeNull()
  })

  it('shows the plain count when the page holds every item', () => {
    renderPanel({ workItemTotal: 2 })

    expect(screen.queryByText('2 items · 2 active')).not.toBeNull()
  })

  it('keeps the plain count for an older response without a total instead of showing zero', () => {
    renderPanel({ workItemTotal: null })

    expect(screen.queryByText('2 items · 2 active')).not.toBeNull()
  })

  it('reports an empty team as zero items', () => {
    renderPanel({ workItems: [], workItemTotal: 0 })

    expect(screen.queryByText('0 items · 0 active')).not.toBeNull()
  })
})

describe('AutonomyPanel workspace navigation', () => {
  it('links to the repository workspace section for each watched scope', () => {
    renderPanel({ scopes: [scope, { ...scope, id: 7, repo_name: 'other' }] })
    expect(screen.getByRole('link', { name: 'Workspaces for example/project' }))
      .toHaveAttribute('href', '/repositories/2#workspaces')
    expect(screen.getByRole('link', { name: 'Workspaces for example/other' }))
      .toHaveAttribute('href', '/repositories/7#workspaces')
  })
})
