import { MemoryRouter } from 'react-router-dom'
import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { CampaignProgress } from '../src/features/factory/components/overview/CampaignProgress'
import type { ReadState } from '../src/features/factory/reads'
import type { AgentTeamPreset, AgentTeamSummary } from '../src/types/agentTeams'
import type { CampaignHealthResponse, HealthBottleneckWindow } from '../src/types/factory'

afterEach(cleanup)

const stamp = '2026-11-28T12:00:00Z'
const preset = {
  id: 9, name: 'v4.4', description: 'The v4.4 release', created_at: stamp, updated_at: stamp,
  autonomy_enabled: true, slots: [],
} as unknown as AgentTeamPreset

function summary(): AgentTeamSummary {
  return {
    preset_id: 9, description: 'The v4.4 release', parent_issue_url: null, supervisor: 'none', checked_at: stamp,
    valid_until: stamp, coverage_complete: true, hold_code: null, attention: [], human_actions: [],
    remaining_work: { state: 'known', unfinished_items: 0, reported_items: 0, confidence: 'unknown', reason: 'complete' },
    next_actors: [], next_actor_coverage_complete: true,
    campaign: {
      campaign_status: 'active', backlog_state: 'available', done_count: 3, latest_done: [], under_way_count: 1,
      under_way: [], queued_count: 2, queued: [], not_started_count: 0, not_started_groups: [], decisions_ahead: [],
    },
  } as unknown as AgentTeamSummary
}

const stage = (over = {}) => ({
  key: 'k', group: 'waiting', stage: 'decision', actor: 'the Leader', credited_seconds: 5400, share: 0.62, items: 9,
  median_seconds: 600, small_sample: false, coverage: 'full', ...over,
})
function window(over: Partial<HealthBottleneckWindow> = {}): HealthBottleneckWindow {
  return {
    window: 'last_four_hours', label: 'Last 4 hours', started_at: '2026-11-28T08:00:00Z', as_of: stamp, coverage: 'full',
    unknown_reasons: [], items: 9, credited_seconds: 8700, state: 'ranked', stages: [stage()], largest_observed: null,
    ...over,
  } as HealthBottleneckWindow
}
function health(over: Partial<CampaignHealthResponse> = {}): CampaignHealthResponse {
  return {
    schema_version: 1, campaign_id: 41, source: 'live', as_of: stamp,
    item_time: {
      items: 41, timed: 41, coverage: 'full', unknown_reasons: [],
      bands: [{ band: '0-100', items: 41, timed: 41, median_seconds: 2280, p85_seconds: 4260, small_sample: false,
        coverage: 'full', unknown_reasons: [] }],
    },
    item_time_reason: null,
    cost: { value_usd: null, coverage: 'unavailable', members_total: null, members_known: null, unknown_members: [],
      prices_as_of: null, reasons: ['cost_not_recorded'] },
    bottleneck: window(), bottleneck_reason: null,
    ...over,
  }
}
const read = (data: CampaignHealthResponse | null, error: Error | null = null): ReadState<CampaignHealthResponse> => ({
  data, error, refreshing: false, retrying: false, stopped: false,
})

function show(state: ReadState<CampaignHealthResponse> | null) {
  render(
    <MemoryRouter>
      <CampaignProgress presets={[preset]} summaries={[summary()]}
        health={state ? { 9: { campaignId: 41, state } } : undefined} />
    </MemoryRouter>,
  )
  return screen.queryByRole('group', { name: 'Campaign health' })
}

describe('the Overview campaign health block (#1256)', () => {
  it('keeps the card as it was when there is no live campaign to read', () => {
    expect(show(null)).toBeNull()
    expect(screen.getByRole('article')).toHaveTextContent('3')
    expect(screen.getByRole('article')).toHaveTextContent('Your decisions ahead')
  })

  it('shows item time with its n, the unknown cost with its reason, and the labelled window', () => {
    const block = within(show(read(health()))!)
    expect(block.getByText('38 min')).toBeInTheDocument()
    expect(block.getByText(/0-100 lines · p85 1 h 11 min · n = 41/)).toBeInTheDocument()
    expect(block.getByText(/the cost is not recorded yet/)).toHaveTextContent('unknown')
    expect(block.getByText('Waiting for the Leader')).toBeInTheDocument()
    expect(block.getByText(/62% of waiting and process time · 9 items/)).toBeInTheDocument()
    expect(block.getByText(/^Last 4 hours/)).toBeInTheDocument()
    // The link works today: the Retrospectives list, filtered to this team's live campaigns.
    expect(block.getByRole('link', { name: /Live retrospective/ })).toHaveAttribute(
      'href', '/insights/retrospectives?team_id=9&status=live')
  })

  it('shows a recorded partial cost with its coverage and the members that are unknown', () => {
    const cost = { value_usd: 412.4, coverage: 'partial' as const, members_total: 7, members_known: 6,
      unknown_members: [{ member: 'B3', reason: 'harness_not_read' }], prices_as_of: '2026-11-15', reasons: [] }
    const block = within(show(read(health({ cost })))!)
    expect(block.getByText(/≈ \$412/)).toBeInTheDocument()
    expect(block.getByText('partial')).toBeInTheDocument()
    expect(block.getByText(/covers 6 of 7 members · prices as of 2026-11-15/)).toBeInTheDocument()
    expect(block.getByText(/B3 unknown: its harness is not read yet/)).toBeInTheDocument()
  })

  it('never shows a missing number as zero', () => {
    const block = within(show(read(health({
      item_time: null, item_time_reason: 'snapshot_without_item_time',
      bottleneck: null, bottleneck_reason: 'no_snapshot',
    })))!)
    expect(block.getByText(/the snapshot has no item time/)).toBeInTheDocument()
    expect(block.getByText(/no snapshot recorded/)).toBeInTheDocument()
    expect(block.queryByText(/^0\b/)).toBeNull()
  })

  it('says none above the thresholds and names the largest stage when nothing qualifies', () => {
    const largest = stage({ group: 'process', stage: 'required_check', actor: 'e2e', share: 0.08, items: 3,
      small_sample: true })
    const block = within(show(read(health({
      bottleneck: window({ state: 'below_thresholds', stages: [], largest_observed: largest }),
    })))!)
    expect(block.getByText('None above the thresholds')).toBeInTheDocument()
    expect(block.getByText('Largest: Required check: e2e')).toBeInTheDocument()
    expect(block.getByText(/8% of waiting and process time · 3 items · small sample/)).toBeInTheDocument()
  })

  it('does not say "none above the thresholds" when the ranking is unknown (V1 review 489)', () => {
    const largest = stage({ group: 'process', stage: 'required_check', actor: 'e2e', share: 0.3, items: 1, small_sample: true })
    const block = within(show(read(health({
      bottleneck: window({ state: 'unknown', coverage: 'partial', unknown_reasons: ['actor_unavailable'], stages: [],
        largest_observed: largest }),
    })))!)
    expect(block.getByText('Ranking unknown')).toBeInTheDocument()
    expect(block.getByText('Largest observed so far: Required check: e2e')).toBeInTheDocument()
    expect(block.queryByText(/None above the thresholds/)).toBeNull()
    expect(block.getByText(/Last 4 hours · partial · the actor is not known/)).toBeInTheDocument()
    // The complete-window control still says it.
    cleanup()
    const control = within(show(read(health({
      bottleneck: window({ state: 'below_thresholds', stages: [], largest_observed: largest }),
    })))!)
    expect(control.getByText('None above the thresholds')).toBeInTheDocument()
  })

  it('keeps the recorded member coverage next to an unknown dollar total (V1 review 489)', () => {
    const cost = { value_usd: null, coverage: 'unavailable' as const, members_total: 2, members_known: 0,
      unknown_members: [{ member: 'B3', reason: 'harness_not_read' }, { member: 'B4', reason: 'binding_unverified' }],
      prices_as_of: '2026-11-15', reasons: ['member_unknown'] }
    const block = within(show(read(health({ cost })))!)
    expect(block.getByText(/member unknown/)).toHaveTextContent('unknown')
    expect(block.getByText(/covers 0 of 2 members · prices as of 2026-11-15/)).toBeInTheDocument()
    expect(block.getByText(/B3 unknown: its harness is not read yet/)).toBeInTheDocument()
    expect(block.getByText(/B4 unknown: binding unverified/)).toBeInTheDocument()
    expect(block.queryByText(/≈/)).toBeNull()  // the amount stays unknown
  })

  it('labels a small sample and a partial window', () => {
    const state = read(health({
      item_time: { items: 3, timed: 3, coverage: 'partial', unknown_reasons: ['delivery_history_missing'], bands: [
        { band: 'over-400', items: 3, timed: 3, median_seconds: 3840, p85_seconds: 5000, small_sample: true,
          coverage: 'partial', unknown_reasons: [] }] },
      bottleneck: window({ coverage: 'partial', unknown_reasons: ['actor_unavailable'] }),
    }))
    const block = within(show(state)!)
    expect(block.getByText(/over-400 lines · p85 1 h 23 min · n = 3 · small sample · partial/)).toBeInTheDocument()
    expect(block.getByText(/Last 4 hours · partial · the actor is not known/)).toBeInTheDocument()
  })

  it('says it is reading, and says unknown (not zero) when the read failed', () => {
    expect(within(show(read(null))!).getByText(/Reading the campaign.s health/)).toBeInTheDocument()
    cleanup()
    const failed = within(show(read(null, new Error('down')))!)
    expect(failed.getByRole('note')).toHaveTextContent('could not be read')
    expect(failed.getByRole('note')).toHaveTextContent('unknown, not zero')
  })

  it('says a frozen version is frozen, and that a failed refresh may leave old numbers', () => {
    const block = within(show(read(health({ source: 'frozen' }), new Error('late')))!)
    expect(block.getByText(/Frozen version up to/)).toHaveTextContent('the last read failed')
  })
})
