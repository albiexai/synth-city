import { cleanup, screen, within } from '@testing-library/react'
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

  it('draws the bar over the known work when the backlog cannot be read, and says the backlog is unknown', async () => {
    run(unavailable)
    await open()
    const card = screen.getByRole('article', { name: /Progress:/ })
    // 6 done, 2 under way and 1 queued are known; the total is not guessed.
    expect(within(card).getByText('of 9 known items done', { exact: false })).toBeInTheDocument()
    expect(within(card).getByText('backlog unknown')).toBeInTheDocument()
    const bar = within(card).getByRole('img')
    expect(bar).toHaveAttribute('aria-label', '6 done, 2 under way, 1 still to do, backlog unknown')
    // A dashed end marks the unknown remainder, so the bar never looks nearly complete.
    expect(within(bar).getByTestId('unknown-remainder')).toBeInTheDocument()
    expect(within(card).getByText(/The backlog cannot be read/)).toBeInTheDocument()
    expect(within(card).queryByText(/not started/)).not.toBeInTheDocument()
  })

  it('keeps the known work at 80% of the bar and leaves the rest to the unknown remainder', async () => {
    run({ ...unavailable, done_count: 4, under_way_count: 0, under_way: [], queued_count: 0, queued: [] })
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
    run({ ...unavailable, done_count: 0, latest_done: [], under_way_count: 0, under_way: [], queued_count: 0, queued: [] })
    await open()
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.getByText(/The backlog cannot be read/)).toBeInTheDocument()
    expect(screen.getByText('backlog unknown')).toBeInTheDocument()
  })

  it('omits a campaign with nothing to show', async () => {
    serve({ presets: [preset(1, 'A', true)], summaries: { 1: summary() }, overview: overviewWith({}) })
    await open()
    expect(screen.queryByRole('heading', { name: /^Progress/ })).not.toBeInTheDocument()
  })
})

// #1141: Needs you honours the Repository (scope_id) and Assigned-harness (provider) filters. Only Needs you reads the
// filtered summaries; the team cards and Progress keep the unfiltered reads.
describe('Needs you filters', () => {
  const action = (scope: number, issue: number, work: number) => ({
    scope_id: scope, issue_number: issue, work_item_id: work, kind: 'review_pr' as const, state: 'requested' as const,
    source: 'leader' as const, assessment_current: true,
  })
  const repository = (scope: number, team: number, name: string) => ({
    scope_id: scope, team: { id: team, name: `Team ${team}` }, github: { owner: 'o', name },
  })
  const everything = summary({ human_actions: [action(8, 47, 9), action(9, 937, 11)] })

  function serveFilters(filtered: (preset: number, query: URLSearchParams) => Response | Promise<Response>) {
    return fixtureFetch((path, query) => {
      if (path === 'factory/overview') return jsonResponse(overviewWith({}, counts({ review: 2 })))
      if (path === 'factory/repositories') {
        return jsonResponse({
          repositories: [repository(8, 1, 'one'), repository(9, 1, 'two'), repository(10, 2, 'three')],
          has_more: false, total: 3, generated_at: stamp,
        })
      }
      if (path === 'agent-teams/presets') {
        return jsonResponse({ presets: [preset(1, 'Release crew', true), preset(2, 'Second crew', true)] })
      }
      const match = /^agent-teams\/presets\/(\d+)\/summary$/.exec(path)
      if (match) {
        const id = Number(match[1])
        if (query.has('scope_id') || query.has('provider')) return filtered(id, query)
        return jsonResponse(id === 1 ? everything : summary({ preset_id: 2 }))
      }
      return jsonResponse({ rows: [], items: [], repositories: [], total: 0, has_more: false, generated_at: stamp })
    })
  }
  const summaryReads = (requests: { path: string; query: URLSearchParams }[]) =>
    requests.filter((request) => /^agent-teams\/presets\/\d+\/summary$/.test(request.path))
  const needsYouList = () => screen.getByRole('heading', { name: /Needs you/ }).closest('section')!

  it('makes the same reads as before when no repository or harness filter is set', async () => {
    const { requests } = serveFilters(() => jsonResponse(summary()))
    await open()
    const reads = summaryReads(requests)
    expect(reads.length).toBeGreaterThan(0)
    expect(reads.every((request) => request.query.toString() === '')).toBe(true)
    expect(within(needsYouList()).getAllByRole('listitem')).toHaveLength(2)
  })

  it('a Repository filter drops the other repository from Needs you, and only there', async () => {
    const { requests } = serveFilters((id, query) => {
      expect(query.get('scope_id')).toBe('8')
      return jsonResponse(id === 1 ? summary({ human_actions: [action(8, 47, 9)] }) : summary({ preset_id: 2 }))
    })
    await open('/?scope_id=8')
    const list = within(needsYouList())
    expect(list.getAllByRole('listitem')).toHaveLength(1)
    expect(list.getByRole('link', { name: /Open #47/ })).toHaveAttribute('href', '/work/9')
    expect(list.queryByRole('link', { name: /Open #937/ })).not.toBeInTheDocument()
    const reads = summaryReads(requests)
    // The filtered read is for Needs you; the unfiltered reads for the cards and Progress are still made.
    const filtered = reads.filter((request) => request.query.has('scope_id'))
    expect(filtered.map((request) => request.path).sort()).toEqual([
      'agent-teams/presets/1/summary', 'agent-teams/presets/2/summary'])
    expect(filtered.every((request) => request.query.toString() === 'scope_id=8')).toBe(true)
    const plain = reads.filter((request) => request.query.toString() === '').map((request) => request.path)
    expect(plain).toContain('agent-teams/presets/1/summary')
    expect(plain).toContain('agent-teams/presets/2/summary')
  })

  it('an Assigned-harness filter sends the provider and shows only what that read returns', async () => {
    const { requests } = serveFilters((id, query) => {
      expect(query.get('provider')).toBe('codex-cli')
      return jsonResponse(id === 1 ? summary({ human_actions: [action(9, 937, 11)] }) : summary({ preset_id: 2 }))
    })
    await open('/?provider=codex-cli')
    const list = within(needsYouList())
    expect(list.getAllByRole('listitem')).toHaveLength(1)
    expect(list.getByRole('link', { name: /Open #937/ })).toBeInTheDocument()
    expect(list.queryByRole('link', { name: /Open #47/ })).not.toBeInTheDocument()
    const filtered = summaryReads(requests).filter((request) => request.query.has('provider'))
    expect(filtered.every((request) => request.query.toString() === 'provider=codex-cli')).toBe(true)
    expect(filtered).toHaveLength(2)
  })

  it('sends both filters together and not the team filter', async () => {
    const { requests } = serveFilters((id) => jsonResponse(summary({ preset_id: id })))
    await open('/?scope_id=8&provider=claude-code')
    expect(screen.getByText('Nothing needs you right now.')).toBeInTheDocument()
    const filtered = summaryReads(requests).filter((request) => request.query.has('scope_id'))
    expect(filtered.length).toBeGreaterThan(0)
    expect(filtered.every((request) => request.query.toString() === 'scope_id=8&provider=claude-code')).toBe(true)
  })

  it('keeps the team filter out of the filtered read', async () => {
    const { requests } = serveFilters(() => jsonResponse(summary({ human_actions: [action(8, 47, 9)] })))
    await open('/?team_id=1&scope_id=8')
    const filtered = summaryReads(requests).filter((request) => request.query.has('scope_id'))
    expect(filtered.map((request) => request.path)).toEqual(['agent-teams/presets/1/summary'])
    expect(filtered.every((request) => request.query.toString() === 'scope_id=8')).toBe(true)
  })

  it('shows the rows that loaded and says that another read failed', async () => {
    serveFilters((id) => (id === 2 ? jsonResponse({ detail: 'broken' }, 500)
      : jsonResponse(summary({ human_actions: [action(8, 47, 9)] }))))
    await open('/?scope_id=8')
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not read what needs you')
    expect(within(needsYouList()).getByRole('link', { name: /Open #47/ })).toBeInTheDocument()
  })

  it('does not call a failed filtered read an empty list', async () => {
    serveFilters(() => jsonResponse({ detail: 'broken' }, 500))
    await open('/?scope_id=8')
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not read what needs you')
    expect(screen.queryByText('Nothing needs you right now.')).not.toBeInTheDocument()
  })

  it('says it is checking, not that nothing needs you, while the filtered read is pending', async () => {
    serveFilters(() => new Promise<Response>(() => undefined))
    await open('/?scope_id=8')
    expect(screen.getByText('Checking what needs you…')).toBeInTheDocument()
    expect(screen.queryByText('Nothing needs you right now.')).not.toBeInTheDocument()
  })
})
