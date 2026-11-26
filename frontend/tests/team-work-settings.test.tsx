import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { TeamWorkSettingsEditor } from '@/features/agent-teams/EditTeamPanel'
import type { TeamWorkSettingsResponse } from '@/types/agentTeams'

it('edits only changed overrides, including explicit false, and clears a timeout', async () => {
  let data: TeamWorkSettingsResponse = {
    defaults: { mail_request_unanswered_seconds: 1200, github_team_idle_timeout_seconds: 1800, github_publish_summaries_enabled: true },
    overrides: { mail_request_unanswered_seconds: null, github_team_idle_timeout_seconds: 90, github_publish_summaries_enabled: null },
    history: [],
    next_before_id: null,
  }
  const save = vi.fn(async (input) => (data = { ...data, overrides: { ...data.overrides, ...input } }))
  const load = vi.fn(async () => data)
  render(<TeamWorkSettingsEditor load={load} save={save} />)
  expect(load).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Load work settings' }))
  expect(await screen.findByText('In effect 90 s · Team override')).toBeVisible()
  fireEvent.change(screen.getByLabelText('Team idle timeout (seconds)'), { target: { value: '' } })
  fireEvent.change(screen.getByLabelText('Publish GitHub summaries'), { target: { value: 'false' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save work settings' }))
  await waitFor(() => expect(save).toHaveBeenCalledWith({
    github_team_idle_timeout_seconds: null, github_publish_summaries_enabled: false,
  }))
  expect(await screen.findByText('In effect 1800 s · Installation default')).toBeVisible()
  expect(screen.getByText('In effect Off · Team override')).toBeVisible()
})
