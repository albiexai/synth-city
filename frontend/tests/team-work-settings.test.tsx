import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { TeamWorkSettingsEditor } from '@/features/agent-teams/EditTeamPanel'
import type { TeamWorkSettingsResponse } from '@/types/agentTeams'

it('edits only changed overrides, including explicit false, and clears a timeout', async () => {
  let data: TeamWorkSettingsResponse = {
    defaults: {
      mail_request_unanswered_seconds: 1200, github_team_idle_timeout_seconds: 1800, github_publish_summaries_enabled: true,
      github_owner_idle_timeout_seconds: 900, github_nudge_grace_seconds: 180,
      wake_followup_enabled: true, wake_followup_delay_seconds: 120,
    },
    overrides: {
      mail_request_unanswered_seconds: null, github_team_idle_timeout_seconds: 90, github_publish_summaries_enabled: null,
      github_owner_idle_timeout_seconds: 300, github_nudge_grace_seconds: null,
      wake_followup_enabled: false, wake_followup_delay_seconds: 95,
    },
    history: [],
    next_before_id: null,
  }
  const save = vi.fn(async (input) => (data = { ...data, overrides: { ...data.overrides, ...input } }))
  const load = vi.fn(async () => data)
  render(<TeamWorkSettingsEditor load={load} save={save} />)
  expect(load).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Load work settings' }))
  expect(await screen.findByText('In effect 90 s · Team override')).toBeVisible()
  expect(screen.getByText('In effect 300 s · Team override')).toBeVisible()
  expect(screen.getByText('In effect 180 s · Installation default')).toBeVisible()
  const ownerIdle = screen.getByLabelText('Owner idle timeout (seconds)')
  const nudgeGrace = screen.getByLabelText('Nudge grace (seconds)')
  expect(ownerIdle).toHaveAttribute('min', '60')
  expect(ownerIdle).toHaveAttribute('max', '86400')
  expect(nudgeGrace).toHaveAttribute('min', '30')
  expect(nudgeGrace).toHaveAttribute('max', '3600')

  const wakeFollowup = screen.getByRole('combobox', { name: 'Wake follow-up' })
  const wakeDelay = screen.getByRole('spinbutton', { name: 'Wake follow-up delay (seconds)' })
  expect(screen.getByText('In effect Off · Team override')).toBeVisible()
  expect(wakeFollowup).toHaveValue('false')
  expect(wakeDelay).toHaveValue(95)
  expect(screen.getByText('In effect 95 s · Team override')).toBeVisible()
  expect(wakeDelay).toHaveAttribute('min', '30')
  expect(wakeDelay).toHaveAttribute('max', '3600')
  fireEvent.change(wakeFollowup, { target: { value: '' } })
  fireEvent.change(wakeDelay, { target: { value: '' } })
  fireEvent.change(ownerIdle, { target: { value: '' } })
  fireEvent.change(nudgeGrace, { target: { value: '75' } })
  fireEvent.change(screen.getByLabelText('Team idle timeout (seconds)'), { target: { value: '' } })
  fireEvent.change(screen.getByLabelText('Publish GitHub summaries'), { target: { value: 'false' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save work settings' }))
  await waitFor(() => expect(save).toHaveBeenCalledWith({
    github_team_idle_timeout_seconds: null, github_publish_summaries_enabled: false,
    github_owner_idle_timeout_seconds: null, github_nudge_grace_seconds: 75,
    wake_followup_enabled: null, wake_followup_delay_seconds: null,
  }))
  expect(await screen.findByText('In effect 1800 s · Installation default')).toBeVisible()
  expect(screen.getByText('In effect Off · Team override')).toBeVisible()
  expect(screen.getByText('In effect 900 s · Installation default')).toBeVisible()
  expect(screen.getByText('In effect 75 s · Team override')).toBeVisible()
  expect(screen.getByText('In effect On · Installation default')).toBeVisible()
  expect(screen.getByText('In effect 120 s · Installation default')).toBeVisible()

  fireEvent.change(wakeFollowup, { target: { value: 'false' } })
  fireEvent.change(wakeDelay, { target: { value: '45' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save work settings' }))
  await waitFor(() => expect(save).toHaveBeenLastCalledWith({
    wake_followup_enabled: false, wake_followup_delay_seconds: 45,
  }))
  expect(await screen.findByText('In effect 45 s · Team override')).toBeVisible()
  expect(wakeFollowup).toHaveValue('false')
})
