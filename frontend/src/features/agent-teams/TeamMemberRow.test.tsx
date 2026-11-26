import '@testing-library/jest-dom/vitest'
import { render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'

import type { AgentTeamSlot } from '@/types/agentTeams'
import { TeamMemberRow } from './TeamMemberRow'

const slot: AgentTeamSlot = {
  id: 1,
  preset_id: 1,
  position: 0,
  display_name: 'Builder',
  provider: 'claude-code',
  repo_id: 'fixture',
  repo_path: '/work/fixture',
  repo_name: 'fixture',
  launch_mode: 'plain',
  launch_options: {},
  enabled: true,
  created_at: '',
  updated_at: '',
}
const noop = () => undefined
const actions = { onLaunch: noop, onEdit: noop, onSetLeader: null, onMove: noop, onDelete: noop }

test('shows pending folder trust until a launch prepares it', () => {
  const props = { isLeader: false, first: true, last: true, status: null, actions }
  const { rerender } = render(
    <TeamMemberRow {...props} slot={{ ...slot, workspace_trust_status: 'pending' }} />,
  )
  expect(screen.getByText('Folder trust pending')).toBeVisible()
  rerender(<TeamMemberRow {...props} slot={{ ...slot, workspace_trust_status: 'ready' }} />)
  expect(screen.queryByText('Folder trust pending')).not.toBeInTheDocument()
})
