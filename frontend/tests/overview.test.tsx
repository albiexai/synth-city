import { act, cleanup, fireEvent, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { OverviewPage } from '../src/features/factory/pages/OverviewPage'
import { resetFactoryReads } from '../src/features/factory/reads'
import type { AgentTeamPreset, AgentTeamSummary, TeamCampaignSummary } from '../src/types/agentTeams'
import overview from './fixtures/factory/v1-p04/overview.json'
import work from './fixtures/factory/v1-p04/work-items.json'
import { fixtureFetch, jsonResponse, renderRoute, settle } from './helpers/factory'

// #508: the Overview answers "Do I need to act? Is anyone watching? What remains?".
vi.mock('../src/features/agent-teams/WorkPublicationPanel', () => ({ WorkPublicationPanel: () => null }))

beforeEach(() => resetFactoryReads())
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const stamp = '2026-10-12T12:00:00Z'
const preset = (id: number, name: string, on: boolean): AgentTeamPreset => ({
  id, name, created_at: stamp, updated_at: stamp, autonomy_enabled: on, slots: [],
})
const emptyCampaign: TeamCampaignSummary = {
  backlog_state: 'available', done_count: 0, latest_done: [], under_way_count: 0, under_way: [],
  queued_count: 0, queued: [], not_started_count: 0, not_started_groups: [], decisions_ahead: [],
}
function summary(overrides: Partial<AgentTeamSummary> = {}): AgentTeamSummary {
  return {
    preset_id: 1, description: 'Release Deck v3.0.1.', parent_issue_url: 'https://github.com/o/r/issues/490',
    supervisor: 'none', checked_at: stamp, valid_until: '2026-10-12T12:00:15Z', coverage_complete: true,
    hold_code: null, attention: [], human_actions: [],
    remaining_work: { state: 'known', unfinished_items: 0, reported_items: 0, confidence: 'unknown', reason: 'complete' },
    next_actors: [], next_actor_coverage_complete: true, campaign: emptyCampaign, ...overrides,
  }
}

const counts = (over: Record<string, number> = {}) => ({
  active: 0, attention: 0, finished: 0, queued: 0, review: 0, total: 0, unknown: 0, ...over,
})
function overviewWith(automation: Record<string, unknown>, c = counts()) {
  const base = overview.normal.response
  return { ...base, counts: c, automation: { ...base.automation, ...automation } }
}

function serve(options: {
  presets: AgentTeamPreset[]
  summaries: Record<number, AgentTeamSummary>
  overview: unknown
  work?: unknown
}) {
  return fixtureFetch((path) => {
    if (path === 'factory/overview') return jsonResponse(options.overview)
    if (path === 'agent-teams/presets') return jsonResponse({ presets: options.presets })
    const match = /^agent-teams\/presets\/(\d+)\/summary$/.exec(path)
    if (match) return jsonResponse(options.summaries[Number(match[1])])
    if (path === 'factory/work-items') return jsonResponse(options.work ?? { ...work.empty.response })
    return jsonResponse({ rows: [], items: [], repositories: [], total: 0, has_more: false, generated_at: stamp })
  })
}

async function open(path = '/') {
  renderRoute(<OverviewPage />, path, '/')
  await settle()
  await settle()
}

describe('Needs you', () => {
  it('lists each Director action that a summary reports, with a link to its item', async () => {
    serve({
      presets: [preset(1, 'Release crew', true)],
      summaries: {
        1: summary({
          human_actions: [
            { scope_id: 1, issue_number: 47, work_item_id: 9, kind: 'review_pr', state: 'requested', source: 'leader', assessment_current: true, pull_request_number: 50 },
            { scope_id: 1, issue_number: 48, work_item_id: 10, kind: 'merge_pr', state: 'resolved', source: 'leader', assessment_current: true },
          ],
        }),
      },
      overview: overviewWith({}, counts({ review: 1 })),
    })
    await open()
    const heading = screen.getByRole('heading', { name: /Needs you/ })
    const list = heading.closest('section')!
    expect(within(list).getAllByRole('listitem')).toHaveLength(1)
    expect(within(list).getByText('Review the PR #50')).toBeInTheDocument()
    expect(within(list).getByRole('link', { name: /Open #47/ })).toHaveAttribute('href', '/work/9')
    // The first heading after the title is the action list.
    const headings = screen.getAllByRole('heading').map((item) => item.textContent)
    expect(headings[0]).toBe('Overview')
    expect(headings[1]).toMatch(/^Needs you/)
  })

  it('shows one row for one issue even when the summary reports an action and an attention code', async () => {
    serve({
      presets: [preset(1, 'Release crew', true)],
      summaries: {
        1: summary({
          attention: [{ code: 'human_review', work_item_id: 9, issue_number: 47 }],
          human_actions: [
            { scope_id: 1, issue_number: 47, work_item_id: 9, kind: 'review_pr', state: 'requested', source: 'leader', assessment_current: true },
          ],
        }),
      },
      overview: overviewWith({}, counts({ review: 1 })),
    })
    await open()
    expect(within(screen.getByRole('heading', { name: /Needs you/ }).closest('section')!).getAllByRole('listitem')).toHaveLength(1)
  })

  it('shows the attempt escalation reason even when action details are pending', async () => {
    const inspection = {
      scope_id: 1, issue_number: 47, work_item_id: 9, kind: 'inspect_attempt',
      state: 'context_pending', source: 'dispatch', assessment_current: true,
    } as const
    const withReason = { ...inspection, escalation_reason: 'leader_ack_timeout' }
    serve({
      presets: [preset(1, 'Release crew', true)],
      summaries: {
        1: summary({
          human_actions: [withReason, { ...inspection, issue_number: 48, work_item_id: 10 }],
          attention: [{ code: 'attempt_stopped', issue_number: 47, work_item_id: 9 }],
        }),
      },
      overview: overviewWith({}),
    })
    await open()
    const list = screen.getByRole('heading', { name: /Needs you/ }).closest('section')!
    const affectedLink = within(list).getByRole('link', { name: /Open #47/ })
    expect(affectedLink).toHaveAttribute('href', '/work/9')
    const affected = within(affectedLink.closest('li')!)
    expect(affected.getByText(/^on #47 · Leader acknowledgement timed out/)).toBeInTheDocument()
    expect(affected.queryByText(/details pending/)).not.toBeInTheDocument()
    expect(within(list).getByText(/^on #48 · details pending/)).toBeInTheDocument()
    expect(within(list).getAllByRole('listitem')).toHaveLength(2)
  })

  it('never shows an action that no summary reports', async () => {
    serve({
      presets: [preset(1, 'Release crew', true)],
      summaries: { 1: summary({ attention: [{ code: 'owner_stalled', work_item_id: 9, issue_number: 47 }] }) },
      // The work counts say 5 items need attention; the Needs you list still follows the summaries.
      overview: overviewWith({}, counts({ attention: 5 })),
    })
    await open()
    expect(screen.queryByRole('heading', { name: /Needs you/ })).not.toBeInTheDocument()
    expect(screen.getByText('Nothing needs you right now.')).toBeInTheDocument()
  })

  it('shows required CI that cannot be read as a row', async () => {
    serve({
      presets: [preset(1, 'Release crew', true)],
      summaries: {
        1: summary({
          required_ci_unreadable: [{
            code: 'required_ci_unreadable', label: 'Required CI cannot be read', since: stamp, reason: 'api_error',
            next_steps: ['Check GitHub access.'], work_item_id: 9, scope_id: 1, issue_number: 52,
          }],
        }),
      },
      overview: overviewWith({}),
    })
    await open()
    const list = screen.getByRole('heading', { name: /Needs you/ }).closest('section')!
    expect(within(list).getByText('Required CI cannot be read')).toBeInTheDocument()
    expect(within(list).getByRole('link', { name: /Open #52/ })).toHaveAttribute('href', '/work/9')
  })
})

describe('status sentence', () => {
  it('says idle and paused once when every repository is off and nothing is open', async () => {
    serve({
      presets: [preset(2, 'Quiet team', false)],
      summaries: { 2: summary({ preset_id: 2, description: null, parent_issue_url: null }) },
      overview: overviewWith({ configured_scopes: 3, enabled_scopes: 0, paused_scopes: 3 }),
    })
    await open('/?team_id=2')
    expect(screen.getAllByText(/idle and paused/i)).toHaveLength(1)
    expect(screen.getByText(/All 3 repositories are off\. Nothing is queued\./)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open Teams' })).toHaveAttribute('href', '/teams')
    expect(screen.getByText('Nothing needs you right now.')).toBeInTheDocument()
  })

  it('says paused, not idle, while items are still open', async () => {
    serve({
      presets: [preset(1, 'Release crew', false)],
      summaries: { 1: summary() },
      overview: overviewWith({ configured_scopes: 2, enabled_scopes: 0 }, counts({ review: 2 })),
    })
    await open()
    expect(screen.queryByText(/idle and paused/i)).not.toBeInTheDocument()
    expect(screen.getByText(/Factory paused\./)).toBeInTheDocument()
    expect(screen.getByText(/2 items are still open/)).toBeInTheDocument()
  })

  it('says running with the team and repository counts', async () => {
    serve({
      presets: [preset(1, 'A', true), preset(2, 'B', true)],
      summaries: { 1: summary(), 2: summary({ preset_id: 2 }) },
      overview: overviewWith({ configured_scopes: 3, enabled_scopes: 2 }, counts({ active: 1 })),
    })
    await open()
    expect(screen.getByText('Factory running.').closest('p')).toHaveTextContent('Factory running. 2 teams · 2 of 3 repositories on.')
  })

  it('keeps the automation detail behind a closed disclosure', async () => {
    serve({
      presets: [preset(1, 'A', true)],
      summaries: { 1: summary() },
      overview: overviewWith({}),
    })
    await open()
    const details = screen.getByText('Automation details').closest('details')!
    expect(details).not.toHaveAttribute('open')
    expect(within(details).getByText(/Intake: /)).toBeInTheDocument()
  })
})

describe('status tiles', () => {
  it('mutes zero values and hides Unknown at zero', async () => {
    serve({
      presets: [], summaries: {},
      overview: overviewWith({}, counts({ finished: 13 })),
    })
    await open()
    expect(screen.queryByRole('link', { name: /^Unknown/ })).not.toBeInTheDocument()
    const attention = screen.getByRole('link', { name: /^Needs attention/ })
    expect(within(attention).getByText('0')).toHaveClass('text-muted-foreground')
    const finished = screen.getByRole('link', { name: /^Finished/ })
    expect(within(finished).getByText('13')).not.toHaveClass('text-muted-foreground')
  })

  it('shows Unknown when it is above zero', async () => {
    serve({ presets: [], summaries: {}, overview: overviewWith({}, counts({ unknown: 2 })) })
    await open()
    expect(screen.getByRole('link', { name: /^Unknown/ })).toHaveAttribute('href', '/work?category=unknown')
  })
})

describe('empty sections', () => {
  it('shows no filter message and no pagination text when no filter is set', async () => {
    serve({ presets: [preset(1, 'A', true)], summaries: { 1: summary() }, overview: overviewWith({}) })
    await open()
    expect(screen.queryByText(/No work matches these filters/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Clear filters to inspect/)).not.toBeInTheDocument()
    expect(screen.queryByText(/0 loaded \/ 0 matching/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Refresh from start' })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: /Active work/ })).not.toBeInTheDocument()
  })

  it('states the last update time once', async () => {
    serve({ presets: [preset(1, 'A', true)], summaries: { 1: summary() }, overview: overviewWith({}) })
    await open()
    expect(screen.getAllByText(/^Updated/)).toHaveLength(1)
    expect(screen.queryByText(/^Observed:/)).not.toBeInTheDocument()
  })
})

describe('Progress', () => {
  const campaign: TeamCampaignSummary = {
    backlog_state: 'available',
    done_count: 6,
    latest_done: [{ title: 'Release notes', merged_at: '2026-10-12T11:35:00Z' }, { title: 'Website pages', merged_at: null }],
    under_way_count: 2,
    under_way: [
      { title: 'Release files', owner_short_name: 'R2', state_phrase: 'building it' },
      { title: 'Public docs', owner_short_name: null, state_phrase: 'waiting for you' },
    ],
    queued_count: 1,
    queued: [{ title: 'Announce the release' }],
    not_started_count: 3,
    not_started_groups: [{ area: 'Audit', count: 2 }, { area: 'Other', count: 1 }],
    decisions_ahead: [
      { text: 'Approve publication.', state: 'now' },
      { text: 'Approve promotion to the main branch', state: 'later' },
      { text: 'Review the design', state: 'done' },
    ],
  }
  const run = (over: Partial<TeamCampaignSummary> = {}, on = true) =>
    serve({
      presets: [preset(1, 'major-release-v3', on)],
      summaries: { 1: summary({ campaign: { ...campaign, ...over } }) },
      overview: overviewWith({}),
    })

  it('shows the header, an accessible bar, three columns and the decisions ahead', async () => {
    run()
    await open()
    const card = screen.getByRole('article', { name: /Progress: Release Deck v3.0.1/ })
    expect(within(card).getByText('of 12 done', { exact: false })).toBeInTheDocument()
    expect(within(card).getByText('major-release-v3', { exact: false })).toHaveTextContent('parent issue #490')
    expect(within(card).getByRole('img')).toHaveAttribute('aria-label', '6 done, 2 under way, 4 still to do, backlog known')
    for (const name of ['Done', 'Under way', 'Still to do']) expect(within(card).getByRole('heading', { name: new RegExp(`^${name}`) })).toBeInTheDocument()
    expect(within(card).getByText('R2 is building it')).toBeInTheDocument()
    expect(within(card).getByText('Waiting for you')).toBeInTheDocument()
    expect(within(card).getByText('3 not started')).toBeInTheDocument()
    expect(within(card).getByText('Audit 2')).toBeInTheDocument()
    expect(within(card).getByRole('link', { name: 'and 4 more ›' })).toHaveAttribute('href', '/work?team_id=1&category=finished')
    expect(within(card).getByText('Approve publication · now')).toBeInTheDocument()
    expect(within(card).getByText('Approve promotion to the main branch · later')).toBeInTheDocument()
    expect(within(card).queryByText(/Review the design/)).not.toBeInTheDocument()
    expect(within(card).getByRole('link', { name: 'Parent issue #490 ›' })).toHaveAttribute('href', 'https://github.com/o/r/issues/490')
  })

  it('counts escalated and failed items as needing attention, never under way (#1129)', async () => {
    run({ attention_count: 2 })
    await open()
    const card = screen.getByRole('article', { name: /Progress: Release Deck v3.0.1/ })
    // Every item has one bucket: the two needing attention join the campaign total.
    expect(within(card).getByText('of 14 done', { exact: false })).toBeInTheDocument()
    expect(within(card).getByRole('img')).toHaveAttribute(
      'aria-label', '6 done, 2 under way, 2 need attention, 4 still to do, backlog known')
    expect(within(card).getByRole('heading', { name: /^Needs attention/ })).toHaveTextContent('2')
    expect(within(card).getByRole('link', { name: 'View the 2 items that need attention ›' }))
      .toHaveAttribute('href', '/work?team_id=1&category=attention')
  })

  it('reads a response without attention_count as none needing attention', async () => {
    run({ attention_count: undefined })
    await open()
    const card = screen.getByRole('article', { name: /Progress: Release Deck v3.0.1/ })
    expect(within(card).getByText('of 12 done', { exact: false })).toBeInTheDocument()
    expect(within(card).queryByRole('heading', { name: /^Needs attention/ })).not.toBeInTheDocument()
  })

  it('says the team is paused when its autonomy is off', async () => {
    run({ under_way_count: 0, under_way: [] }, false)
    await open()
    expect(screen.getByText('Paused. Nothing is under way.')).toBeInTheDocument()
    expect(screen.getByText('3 not started · the team is paused')).toBeInTheDocument()
  })

  const unavailable = { backlog_state: 'backlog unavailable', not_started_count: null, not_started_groups: [] } as const

  it('shows the last successful observation time beside the available backlog counts once stale', async () => {
    run({ backlog_observed_at: stamp, backlog_stale: true })
    await open()
    const card = screen.getByRole('article', { name: /Progress:/ })
    const note = within(card).getByText(/backlog as of/i)
    expect(note.querySelector('time')).toHaveAttribute('dateTime', stamp)
    expect(within(card).getByText('of 12 done', { exact: false })).toBeInTheDocument()
  })

  it('omits the as-of note for a fresh observation', async () => {
    run({ backlog_observed_at: stamp, backlog_stale: false })
    await open()
    expect(screen.queryByText(/backlog as of/i)).not.toBeInTheDocument()
  })

  it('draws the bar over the known work when the backlog cannot be read, and says the backlog is unknown', async () => {
    run({ ...unavailable, backlog_observed_at: stamp, backlog_stale: true })
    await open()
    const card = screen.getByRole('article', { name: /Progress:/ })
    // 6 done, 2 under way and 1 queued are known; the total is not guessed.
    expect(within(card).getByText('of 9 known items done', { exact: false })).toBeInTheDocument()
    expect(within(card).getByText('backlog unknown')).toBeInTheDocument()
    expect(within(card).queryByText(/backlog as of/i)).not.toBeInTheDocument()
    const bar = within(card).getByRole('img')
    expect(bar).toHaveAttribute('aria-label', '6 done, 2 under way, 1 still to do, backlog unknown')
    // A dashed end marks the unknown remainder, so the bar never looks nearly complete.
    expect(within(bar).getByTestId('unknown-remainder')).toBeInTheDocument()
    expect(within(card).getByText(/The backlog cannot be read/)).toBeInTheDocument()
    expect(within(card).queryByText(/not started/)).not.toBeInTheDocument()
  })

  it('keeps the known work at 80% of the bar and leaves the rest to the unknown remainder', async () => {
    run({ ...unavailable, done_count: 4, under_way_count: 0, under_way: [], queued_count: 0, queued: [], campaign_status: 'active' })
    await open()
    const bar = screen.getByRole('img')
    const done = bar.querySelector('span.bg-primary') as HTMLElement
    expect(done.style.width).toBe('80%')
    expect(bar).toHaveAttribute('aria-label', '4 done, 0 under way, 0 still to do, backlog unknown')
  })

  it('draws no unknown remainder when the backlog is known, and fills the whole bar', async () => {
    run()
    await open()
    const bar = screen.getByRole('img')
    expect(within(bar).queryByTestId('unknown-remainder')).not.toBeInTheDocument()
    const widths = [...bar.querySelectorAll('span')].map((item) => Number.parseFloat((item as HTMLElement).style.width))
    expect(widths.reduce((sum, value) => sum + value, 0)).toBeCloseTo(100)
  })

  it('draws no bar when nothing is known and the backlog cannot be read', async () => {
    run({ ...unavailable, done_count: 0, latest_done: [], under_way_count: 0, under_way: [], queued_count: 0, queued: [], campaign_status: 'active' })
    await open()
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.getByText(/The backlog cannot be read/)).toBeInTheDocument()
    expect(screen.getByText('backlog unknown')).toBeInTheDocument()
  })

  // #1415: a card shows while the team's recorded campaign is active or finishing; finished ones are one line.
  const team = (id: number, name: string, over: Partial<TeamCampaignSummary>, extra: Partial<AgentTeamSummary> = {}) => ({
    preset: preset(id, name, id === 1),
    summary: summary({ preset_id: id, description: name, campaign: { ...campaign, ...over }, ...extra }),
  })
  const serveTeams = (teams: ReturnType<typeof team>[]) => {
    const options = {
      presets: teams.map(t => t.preset),
      summaries: Object.fromEntries(teams.map(t => [t.preset.id, t.summary])) as Record<number, AgentTeamSummary>,
      overview: overviewWith({}),
    }
    serve(options)
    return options
  }
  const finishedOnly = { under_way_count: 0, under_way: [], queued_count: 0, queued: [], not_started_count: 0,
    not_started_groups: [], decisions_ahead: [], campaign_status: 'finished' as const, finished_campaign_count: 1 }

  it('shows only the active campaign and one line for the finished ones', async () => {
    serveTeams([team(1, 'synth-city-v4.4', { campaign_status: 'active' }), team(2, 'synth-city-v4.3', finishedOnly),
      team(3, 'synth-city-v4.2', finishedOnly), team(4, 'major-release-v3', finishedOnly)])
    await open()
    expect(screen.getAllByRole('article', { name: /^Progress:/ })).toHaveLength(1)
    expect(screen.getByRole('article', { name: /Progress: synth-city-v4.4/ })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /^Progress/ })).toHaveTextContent('1 campaign')
    // Work filters one team at a time, so each contributing team gets its own link to its finished work.
    const history = screen.getByTestId('finished-campaigns')
    expect(history).toHaveTextContent(/^3 finished campaigns:/)
    for (const [id, name] of [[2, 'synth-city-v4.3'], [3, 'synth-city-v4.2'], [4, 'major-release-v3']] as const) {
      expect(within(history).getByRole('link', { name: `${name} ›` })).toHaveAttribute('href', `/work?team_id=${id}&category=finished`)
    }
  })

  it('counts every recorded finish, and a new campaign never hides an earlier one', async () => {
    serveTeams([team(1, 'synth-city-v4.4', { campaign_status: 'active', finished_campaign_count: 1 }),
      team(2, 'synth-city-v4.3', { ...finishedOnly, finished_campaign_count: 2 })])
    await open()
    expect(screen.getByRole('article', { name: /Progress: synth-city-v4.4/ })).toBeInTheDocument()
    expect(screen.getByTestId('finished-campaigns')).toHaveTextContent(/^3 finished campaigns:/)
    expect(screen.getByRole('link', { name: 'synth-city-v4.4 ›' })).toHaveAttribute('href', '/work?team_id=1&category=finished')
  })

  it('keeps the page filters on the finished-work link', async () => {
    // Teams with a Codex CLI slot, so the Assigned-harness filter keeps them (#1142).
    const codex = (entry: ReturnType<typeof team>) => ({ ...entry, preset: { ...entry.preset, slots: [
      { id: entry.preset.id * 10, display_name: 'B1', provider: 'codex-cli' } as unknown as AgentTeamPreset['slots'][number]] } })
    serveTeams([codex(team(1, 'synth-city-v4.4', { campaign_status: 'active' })), codex(team(2, 'synth-city-v4.3', finishedOnly))])
    await open('/?provider=codex-cli')
    expect(screen.getByRole('link', { name: '1 finished campaign ›' }))
      .toHaveAttribute('href', '/work?team_id=2&provider=codex-cli&category=finished')
  })

  it('hides a team without a campaign record whose only decision is already done', async () => {
    serveTeams([team(2, 'old-team', { ...finishedOnly, campaign_status: null, finished_campaign_count: 0,
      decisions_ahead: [{ text: 'Review the design', state: 'done' }] })])
    await open()
    expect(screen.queryByRole('article', { name: /Progress: old-team/ })).not.toBeInTheDocument()
  })

  it("links one finished campaign to its team's finished work", async () => {
    serveTeams([team(1, 'synth-city-v4.4', { campaign_status: 'active' }), team(2, 'synth-city-v4.3', finishedOnly)])
    await open()
    expect(screen.getByRole('link', { name: '1 finished campaign ›' }))
      .toHaveAttribute('href', '/work?team_id=2&category=finished')
  })

  it('shows a started campaign even before it has work, and reads the status, never the counts', async () => {
    serveTeams([team(1, 'synth-city-v4.4', { ...finishedOnly, done_count: 0, latest_done: [], campaign_status: 'finishing' }),
      team(2, 'synth-city-v4.3', { campaign_status: 'finished' })])
    await open()
    expect(screen.getByRole('article', { name: /Progress: synth-city-v4.4/ })).toBeInTheDocument()
    expect(screen.queryByRole('article', { name: /Progress: synth-city-v4.3/ })).not.toBeInTheDocument()
  })

  it('shows a team without a campaign record while it has work under way, but not for done work alone', async () => {
    serveTeams([team(1, 'synth-city-v4.4', { campaign_status: null }),
      team(2, 'old-team', { ...finishedOnly, campaign_status: null, finished_campaign_count: 0 })])
    await open()
    expect(screen.getByRole('article', { name: /Progress: synth-city-v4.4/ })).toBeInTheDocument()
    expect(screen.queryByRole('article', { name: /Progress: old-team/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /finished campaign/ })).not.toBeInTheDocument()
  })

  it("keeps a finished team's attention item under Needs you without bringing its card back", async () => {
    serveTeams([team(2, 'synth-city-v4.3', finishedOnly, { human_actions: [
      { scope_id: 1, issue_number: 47, work_item_id: 9, kind: 'review_pr', state: 'requested', source: 'leader',
        assessment_current: true, pull_request_number: 50 }] })])
    await open()
    expect(screen.getByRole('heading', { name: /^Needs you/ })).toBeInTheDocument()
    expect(screen.queryByRole('article', { name: /^Progress:/ })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: '1 finished campaign ›' })).toBeInTheDocument()
  })

  it('removes a card on the next refresh after its campaign finishes, and shows it again when one starts', async () => {
    vi.useFakeTimers()
    try {
      const options = serveTeams([team(1, 'synth-city-v4.4', { campaign_status: 'active' })])
      await open()
      expect(screen.getByRole('article', { name: /Progress: synth-city-v4.4/ })).toBeInTheDocument()
      options.summaries[1] = team(1, 'synth-city-v4.4', finishedOnly).summary
      await act(async () => { await vi.advanceTimersByTimeAsync(15000) })
      await settle()
      expect(screen.queryByRole('article', { name: /Progress: synth-city-v4.4/ })).not.toBeInTheDocument()
      expect(screen.getByRole('link', { name: '1 finished campaign ›' })).toBeInTheDocument()
      options.summaries[1] = team(1, 'synth-city-v4.4', { ...finishedOnly, campaign_status: 'active' }).summary
      await act(async () => { await vi.advanceTimersByTimeAsync(15000) })
      await settle()
      expect(screen.getByRole('article', { name: /Progress: synth-city-v4.4/ })).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('hides a team without a campaign record that has only backlog or planned decisions (the Supervisor, 2026-11-27)', async () => {
    serveTeams([team(1, 'synth-city-v4.4', { campaign_status: 'active' }),
      team(2, 'old-team', { ...finishedOnly, campaign_status: null, not_started_count: 4,
        not_started_groups: [{ area: 'Other', count: 4 }], decisions_ahead: [{ text: 'Approve publication.', state: 'later' }],
        dispatch_order: [] })])
    await open()
    expect(screen.queryByRole('article', { name: /Progress: old-team/ })).not.toBeInTheDocument()
  })

  it.each([
    ['under way', { under_way_count: 1 }], ['queued', { queued_count: 1 }], ['needing attention', { attention_count: 1 }],
    ['in the observed dispatch order', { dispatch_order: [{ title: 'Next', scope_id: 1, repository: 'o/r', issue_number: 7,
      priority: null, dispatch_position: 1 }] }],
  ])('shows a team without a campaign record while work is %s', async (_, over) => {
    serveTeams([team(2, 'old-team', { ...finishedOnly, campaign_status: null, ...over })])
    await open()
    expect(screen.getByRole('article', { name: /Progress: old-team/ })).toBeInTheDocument()
  })

  it('omits a campaign with nothing to show', async () => {
    serve({ presets: [preset(1, 'A', true)], summaries: { 1: summary() }, overview: overviewWith({}) })
    await open()
    expect(screen.queryByRole('heading', { name: /^Progress/ })).not.toBeInTheDocument()
  })
})

// #1141, #1142: the Repository (scope_id) and Assigned-harness (provider) filters narrow Needs you, Progress and the
// Teams cards. With a filter they all read the same separately keyed summary of each MATCHING team (one request for
// each team); without one the page makes the unfiltered reads it always made.
describe('Overview filters', () => {
  const action = (scope: number, issue: number, work: number) => ({
    scope_id: scope, issue_number: issue, work_item_id: work, kind: 'review_pr' as const, state: 'requested' as const,
    source: 'leader' as const, assessment_current: true,
  })
  const repository = (scope: number, team: number, name: string) => ({
    scope_id: scope, team: { id: team, name: `Team ${team}` }, github: { owner: 'o', name },
  })
  const crew = (id: number, name: string, providers: string[]): AgentTeamPreset => ({
    ...preset(id, name, true),
    slots: providers.map((provider, index) => ({ id: id * 10 + index, display_name: `${name} ${index}`, provider }) as unknown as AgentTeamPreset['slots'][number]),
  })
  // An active campaign, so a team shows by its recorded status, not by done work alone (#1415).
  const campaign = (done: number): TeamCampaignSummary => ({ ...emptyCampaign, done_count: done, campaign_status: 'active' })
  const everything = summary({ human_actions: [action(8, 47, 9), action(9, 937, 11)], campaign: campaign(3) })

  function serveFilters(
    filtered: (preset: number, query: URLSearchParams) => Response | Promise<Response>,
    options: { hasMore?: boolean } = {},
  ) {
    return fixtureFetch((path, query) => {
      if (path === 'factory/overview') return jsonResponse(overviewWith({}, counts({ review: 2 })))
      if (path === 'factory/repositories') {
        return jsonResponse({
          repositories: [repository(8, 1, 'one'), repository(9, 1, 'two'), repository(10, 2, 'three')],
          has_more: options.hasMore ?? false, total: 3, generated_at: stamp,
        })
      }
      if (path === 'agent-teams/presets') {
        return jsonResponse({ presets: [crew(1, 'Release crew', ['codex-cli', 'claude-code']), crew(2, 'Second crew', ['codex-cli'])] })
      }
      const match = /^agent-teams\/presets\/(\d+)\/summary$/.exec(path)
      if (match) {
        const id = Number(match[1])
        if (query.has('scope_id') || query.has('provider')) return filtered(id, query)
        return jsonResponse(id === 1 ? everything : summary({ preset_id: 2, campaign: campaign(5) }))
      }
      return jsonResponse({ rows: [], items: [], repositories: [], total: 0, has_more: false, generated_at: stamp })
    })
  }
  const summaryReads = (requests: { path: string; query: URLSearchParams }[]) =>
    requests.filter((request) => /^agent-teams\/presets\/\d+\/summary$/.test(request.path))
  const needsYouList = () => screen.getByRole('heading', { name: /Needs you/ }).closest('section')!
  const progress = () => screen.queryAllByRole('article', { name: /^Progress:/ })
  const teamCards = () => screen.queryAllByRole('region', { name: /^Team summary:/ })
  const note = () => screen.queryByText(/Filtered by repository or harness/)
  const mine = (id: number, rows = [action(8, 47, 9)], done = 1) =>
    summary({ preset_id: id, human_actions: rows, campaign: campaign(done) })

  it('makes the same reads as before when no repository or harness filter is set', async () => {
    const { requests } = serveFilters(() => jsonResponse(summary()))
    await open()
    const reads = summaryReads(requests)
    expect(reads.every((request) => request.query.toString() === '')).toBe(true)
    expect([...new Set(reads.map((request) => request.path))].sort()).toEqual([
      'agent-teams/presets/1/summary', 'agent-teams/presets/2/summary'])
    expect(within(needsYouList()).getAllByRole('listitem')).toHaveLength(2)
    expect(progress()).toHaveLength(2)
    expect(teamCards()).toHaveLength(2)
    expect(note()).not.toBeInTheDocument()
  })

  it('a Repository filter reads only the owning team, with one filtered request, and narrows all three sections', async () => {
    const { requests } = serveFilters((id, query) => {
      expect(query.toString()).toBe('scope_id=8')
      return jsonResponse(mine(id))
    })
    await open('/?scope_id=8')
    const reads = summaryReads(requests)
    // Only the team that owns the repository, once, and no unfiltered summary at all.
    expect(reads.map((request) => `${request.path}?${request.query}`)).toEqual([
      'agent-teams/presets/1/summary?scope_id=8'])
    const list = within(needsYouList())
    expect(list.getAllByRole('listitem')).toHaveLength(1)
    expect(list.getByRole('link', { name: /Open #47/ })).toHaveAttribute('href', '/work/9')
    expect(list.queryByRole('link', { name: /Open #937/ })).not.toBeInTheDocument()
    expect(progress()).toHaveLength(1)
    expect(progress()[0].querySelector('strong')).toHaveTextContent('1')
    expect(teamCards()).toHaveLength(1)
    expect(within(teamCards()[0]).getByRole('link', { name: 'Release crew' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Second crew' })).not.toBeInTheDocument()
    expect(note()).toBeInTheDocument()
  })

  it('an Assigned-harness filter keeps the teams with a slot of that harness and sends the provider', async () => {
    const { requests } = serveFilters((id, query) => {
      expect(query.toString()).toBe('provider=claude-code')
      return jsonResponse(mine(id, [action(9, 937, 11)], 2))
    })
    await open('/?provider=claude-code')
    expect(summaryReads(requests).map((request) => request.path)).toEqual(['agent-teams/presets/1/summary'])
    expect(within(needsYouList()).getByRole('link', { name: /Open #937/ })).toBeInTheDocument()
    expect(progress()[0].querySelector('strong')).toHaveTextContent('2')
    expect(teamCards()).toHaveLength(1)
    expect(screen.queryByRole('link', { name: 'Second crew' })).not.toBeInTheDocument()
  })

  it('a harness that no team has shows no team, no progress and no request, not a stale unfiltered card', async () => {
    const { requests } = serveFilters(() => jsonResponse(summary()))
    await open('/?provider=antigravity-cli')
    expect(summaryReads(requests)).toEqual([])
    expect(progress()).toHaveLength(0)
    expect(teamCards()).toHaveLength(0)
    expect(screen.getByText('No teams to show.')).toBeInTheDocument()
    expect(screen.getByText('Nothing needs you right now.')).toBeInTheDocument()
  })

  it('both filters together send both, and a team must match both', async () => {
    const { requests } = serveFilters((id, query) => {
      expect(query.toString()).toBe('scope_id=10&provider=codex-cli')
      return jsonResponse(mine(id, [action(10, 12, 20)], 4))
    })
    await open('/?scope_id=10&provider=codex-cli')
    expect(summaryReads(requests).map((request) => request.path)).toEqual(['agent-teams/presets/2/summary'])
    expect(teamCards()).toHaveLength(1)
    expect(within(teamCards()[0]).getByRole('link', { name: 'Second crew' })).toBeInTheDocument()
  })

  it('keeps every team when the repository list cannot prove which team owns the repository', async () => {
    const { requests } = serveFilters((id) => jsonResponse(mine(id)), { hasMore: true })
    await open('/?scope_id=8')
    expect([...new Set(summaryReads(requests).map((request) => request.path))].sort()).toEqual([
      'agent-teams/presets/1/summary', 'agent-teams/presets/2/summary'])
    expect(teamCards()).toHaveLength(2)
  })

  it('does not call a failed filtered read an empty list', async () => {
    serveFilters(() => jsonResponse({ detail: 'broken' }, 500))
    await open('/?scope_id=8')
    await expectNeedsYouAlert()
    expect(screen.queryByText('Nothing needs you right now.')).not.toBeInTheDocument()
  })

  it('says it is checking, not that nothing needs you, while the filtered read is pending', async () => {
    serveFilters(() => new Promise<Response>(() => undefined))
    await open('/?scope_id=8')
    expect(screen.getByText('Checking what needs you…')).toBeInTheDocument()
    expect(screen.queryByText('Nothing needs you right now.')).not.toBeInTheDocument()
  })

  // A failed refresh keeps the earlier data and sets the error (reads.ts), so these start from a loaded read.
  async function refresh() {
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Refresh' })) })
    await settle()
    await settle()
  }
  const empty = () => screen.queryByText('Nothing needs you right now.')
  // The team cards report their own failed reads as alerts too; this is the Needs-you one.
  const needsYouAlerts = () => screen.queryAllByRole('alert').filter((item) => /Could not read what needs you/.test(item.textContent ?? ''))
  async function expectNeedsYouAlert() {
    await screen.findAllByRole('alert')
    expect(needsYouAlerts()).toHaveLength(1)
  }

  it('shows the alert, not the empty state, when a refresh fails after an empty filtered read', async () => {
    let broken = false
    serveFilters((id) => (broken ? jsonResponse({ detail: 'broken' }, 500) : jsonResponse(summary({ preset_id: id }))))
    await open('/?scope_id=8')
    expect(empty()).toBeInTheDocument()
    expect(needsYouAlerts()).toHaveLength(0)
    broken = true
    await refresh()
    await expectNeedsYouAlert()
    expect(empty()).not.toBeInTheDocument()
  })

  it('keeps the loaded rows and shows the alert when a refresh fails after a non-empty read', async () => {
    let broken = false
    serveFilters((id) => (broken ? jsonResponse({ detail: 'broken' }, 500) : jsonResponse(mine(id))))
    await open('/?scope_id=8')
    expect(within(needsYouList()).getByRole('link', { name: /Open #47/ })).toBeInTheDocument()
    broken = true
    await refresh()
    await expectNeedsYouAlert()
    expect(within(needsYouList()).getByRole('link', { name: /Open #47/ })).toBeInTheDocument()
  })

  it('removes the alert and restores the plain state when a later refresh succeeds', async () => {
    let broken = false
    serveFilters((id) => (broken ? jsonResponse({ detail: 'broken' }, 500) : jsonResponse(summary({ preset_id: id }))))
    await open('/?scope_id=8')
    broken = true
    await refresh()
    await expectNeedsYouAlert()
    broken = false
    await refresh()
    await settle()
    expect(needsYouAlerts()).toHaveLength(0)
    expect(empty()).toBeInTheDocument()
  })

  it('removes the alert after recovery and still shows the rows of a non-empty read', async () => {
    let broken = false
    serveFilters((id) => (broken ? jsonResponse({ detail: 'broken' }, 500) : jsonResponse(mine(id))))
    await open('/?scope_id=8')
    broken = true
    await refresh()
    await expectNeedsYouAlert()
    broken = false
    await refresh()
    await settle()
    expect(needsYouAlerts()).toHaveLength(0)
    expect(within(needsYouList()).getByRole('link', { name: /Open #47/ })).toBeInTheDocument()
  })
})



it('shows the paused intake reason and operator beside the running team on Overview', async () => {
  serve({
    presets: [{ ...preset(1, 'Release crew', true), dispatch_paused: true,
      dispatch_pause_reason: 'Drain for checkpoint', dispatch_paused_by: 'operator' }],
    summaries: { 1: summary() },
    overview: overviewWith({}),
  })
  await open()
  const card = screen.getByRole('region', { name: 'Team summary: Release crew' })
  expect(within(card).getByText('Dispatch paused')).toBeInTheDocument()
  expect(within(card).getByText(/Drain for checkpoint.*Set by Operator/)).toBeInTheDocument()
  expect(within(card).getByText('Running')).toBeInTheDocument()
})

// #1256: the campaign health block reads each live campaign's health after the overview loaded.
describe('Campaign health on the Progress card', () => {
  const live = { ...emptyCampaign, campaign_status: 'active' as const, done_count: 3 }
  const health = {
    schema_version: 1, campaign_id: 41, source: 'live', as_of: stamp,
    item_time: { items: 5, timed: 5, coverage: 'full', unknown_reasons: [], bands: [
      { band: '0-100', items: 5, timed: 5, median_seconds: 2280, p85_seconds: 4260, small_sample: false, coverage: 'full',
        unknown_reasons: [] }] },
    item_time_reason: null,
    cost: { value_usd: null, coverage: 'unavailable', members_total: null, members_known: null, unknown_members: [],
      prices_as_of: null, reasons: ['cost_not_recorded'] },
    bottleneck: { window: 'last_four_hours', label: 'Last 4 hours', started_at: stamp, as_of: stamp, coverage: 'full',
      unknown_reasons: [], items: 6, credited_seconds: 3600, state: 'ranked', stages: [{
        key: 'k', group: 'waiting', stage: 'decision', actor: 'the Leader', credited_seconds: 3600, share: 0.5, items: 6,
        median_seconds: 600, small_sample: false, coverage: 'full' }], largest_observed: null },
    bottleneck_reason: null,
  }
  const serveHealth = (campaignRows: unknown[], healthResponse: () => Response) =>
    fixtureFetch((path, query) => {
      if (path === 'factory/overview') return jsonResponse(overviewWith({}))
      if (path === 'agent-teams/presets') return jsonResponse({ presets: [preset(1, 'v4.4', true)] })
      if (/^agent-teams\/presets\/1\/summary$/.test(path)) return jsonResponse(summary({ campaign: live }))
      if (path === 'factory/campaigns') {
        expect(query.get('status')).toBe('live')
        return jsonResponse({ campaigns: campaignRows })
      }
      if (path === 'factory/campaigns/41/health') return healthResponse()
      if (path === 'factory/work-items') return jsonResponse({ ...work.empty.response })
      return jsonResponse({ rows: [], items: [], repositories: [], total: 0, has_more: false, generated_at: stamp })
    })

  it('reads the live campaign of the team and shows its health block with the working link', async () => {
    const { requests } = serveHealth([{ id: 41, team: { id: 1, name: 'v4.4' }, status: 'active' }],
      () => jsonResponse(health))
    await open()
    const block = within(screen.getByRole('group', { name: 'Campaign health' }))
    expect(block.getByText('38 min')).toBeInTheDocument()
    expect(block.getByText('Waiting for the Leader')).toBeInTheDocument()
    expect(block.getByRole('link', { name: /Live retrospective/ })).toHaveAttribute(
      'href', '/insights/retrospectives?team_id=1&status=live')
    expect(requests.filter(r => r.path === 'factory/campaigns/41/health')).toHaveLength(1)
    // The card keeps its counts.
    expect(screen.getByRole('article', { name: /Progress: / })).toHaveTextContent('Your decisions ahead')
  })

  it('shows no health block for a team with no live campaign, and asks for no health read', async () => {
    const { requests } = serveHealth([], () => jsonResponse(health))
    await open()
    expect(screen.queryByRole('group', { name: 'Campaign health' })).not.toBeInTheDocument()
    expect(requests.some(r => r.path.endsWith('/health'))).toBe(false)
    expect(screen.getByRole('article', { name: /Progress: / })).toBeInTheDocument()
  })

  it('says the health could not be read when its read fails, and keeps the rest of the card', async () => {
    serveHealth([{ id: 41, team: { id: 1, name: 'v4.4' }, status: 'active' }],
      () => jsonResponse({ detail: 'gone' }, 404))
    await open()
    const block = within(screen.getByRole('group', { name: 'Campaign health' }))
    expect(block.getByRole('note')).toHaveTextContent('could not be read')
    expect(screen.getByRole('article', { name: /Progress: / })).toHaveTextContent('Your decisions ahead')
  })
})
