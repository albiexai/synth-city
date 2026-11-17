import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { CCBridgePage } from './CCBridgePage'
import type { CCSession } from './types'

const mocks = vi.hoisted(() => ({ sessions: [] as unknown[] }))
vi.mock('./useCCSessions', () => ({
  useCCSessions: () => ({ sessions: mocks.sessions, loading: false, error: null, refresh: vi.fn() }),
}))
vi.mock('./TerminalView', () => ({
  TerminalView: ({ target, onDetach }: { target: string; onDetach?: () => void }) => (
    <div>
      <p>Terminal {target}</p>
      <button type="button" onClick={onDetach}>Detach</button>
    </div>
  ),
}))
vi.mock('./TeamLanesView', () => ({ TeamLanesView: () => null }))
vi.mock('./NewSessionDialog', () => ({ NewSessionDialog: () => null }))
vi.mock('./KillSessionDialog', () => ({ KillSessionDialog: () => null }))
vi.mock('@/hooks/useSystemStatus', () => ({ useSystemStatus: () => null }))
vi.mock('@/contexts/ProviderContext', () => ({
  useProviderContext: () => ({ selectedProviderId: 'claude-code', providers: [{ id: 'claude-code', display_name: 'Claude Code', installed: true, capabilities: { spawn: true } }] }),
}))

function session(id: number): CCSession {
  return {
    provider: 'claude-code', provider_display_name: 'Claude Code', tmux_target: `t:0.${id}`, session_name: `s${id}`, window_name: 'w',
    pane_id: `%${id}`, cwd: '/work/demo', pid: '1', status: 'running', team_slot_name: `Member ${id}`, team_preset_id: 1,
    team_preset_name: 'Alpha', agent_status: null,
  } as unknown as CCSession
}

afterEach(cleanup)

describe('CCBridgePage Detach', () => {
  it('removes a detached pane from the grid and drops the watch count', () => {
    mocks.sessions = [session(1), session(2)]
    render(<MemoryRouter><CCBridgePage /></MemoryRouter>)

    fireEvent.click(screen.getAllByRole('button', { name: /^Watch / })[0])
    expect(screen.queryByText('Terminal t:0.1')).not.toBeNull()
    expect(screen.queryByText('Watching · 1')).not.toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Detach' }))

    expect(screen.queryByText('Terminal t:0.1')).toBeNull()
    expect(screen.queryByText('Watching · 1')).toBeNull()
    expect(screen.getAllByRole('button', { name: /^Watch / })).toHaveLength(2)
  })
})
