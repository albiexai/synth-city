import type { AgentTeamSummary } from '../../src/types/agentTeams'
import { contrastFailures, useTheme, type Theme } from '../support/contrast'
import { expect, test } from '../support/guard'
import { expectKeyboardReachable } from '../support/keyboard'
import { localClockPattern, seedAgo } from '../support/time'

// #508: the Overview answers the Director's three questions. In the idle
// state the page fits one viewport and states "idle and paused" once; with
// open Director actions, the first visible item is the action list; no empty
// section shows a filter message when no filter is set.
// Seed: Release crew has open Director actions (#101 attention, #102 review);
// Quiet team is idle and paused (autonomy off, scope intake disabled).
// Design reference: screens 01 (Needs you), 02 (Idle and paused), 21 (light).
// Active since #508 (with the Progress card).

test.use({ viewport: { width: 1440, height: 900 } })

test('@active with open Director actions, the first visible item is the action list', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible()
  const first = page.locator('main').getByRole('heading').nth(1)
  await expect(first).toHaveText(/Needs you|Your actions|Do I need to act/i)
  await expect(first).toBeInViewport()
})

test('@active the idle state fits one viewport and says "idle and paused" once', async ({ page }) => {
  await page.goto('/?team_id=2')
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible()
  await expect(page.locator('main').getByText(/idle and paused/i)).toHaveCount(1)
  const height = await page.evaluate(() => document.documentElement.scrollHeight)
  expect(height, 'the idle Overview fits 900 px').toBeLessThanOrEqual(900)
})

test('@active the idle Overview does not scroll inside the page', async ({ page }) => {
  await page.goto('/?team_id=2')
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible()
  await expect(page.locator('main').getByText('Nothing needs you right now.')).toBeVisible()
  // The page scrolls inside <main>, so the document height proves nothing. Measure <main>.
  const { scroll, client } = await page.locator('main').evaluate(element => ({ scroll: element.scrollHeight, client: element.clientHeight }))
  expect(scroll, 'the idle Overview fits the main area').toBeLessThanOrEqual(client)
})

test('@active the Progress card shows Done, Under way, Still to do and the decisions ahead', async ({ page }) => {
  await page.goto('/')
  const main = page.locator('main')
  await expect(main.getByRole('heading', { name: /^Progress/ })).toBeVisible()
  const card = main.getByRole('article', { name: /^Progress:/ }).first()
  await expect(card).toBeVisible()
  for (const name of [/^Done/, /^Under way/, /^Still to do/]) await expect(card.getByRole('heading', { name })).toBeVisible()
  await expect(card.getByText(/Your decisions ahead/)).toBeVisible()
  // Order: the action list comes before the Progress card.
  const headings = await main.getByRole('heading').allInnerTexts()
  expect(headings.findIndex(text => /^Needs you/.test(text))).toBeLessThan(headings.findIndex(text => /^Progress/.test(text)))
})

for (const theme of ['light', 'dark'] as Theme[]) {
  test(`@active the Progress card draws a bar over the known work when the backlog is unknown in the ${theme} theme`, async ({ page }) => {
    // Release crew has an unavailable milestone in the offline seed (#652).
    await useTheme(page, theme)
    await page.goto('/')
    const card = page.locator('main').getByRole('article', { name: /^Progress:/ }).filter({ hasText: 'Release crew' })
    await expect(card).toBeVisible()
    const bar = card.getByRole('img')
    await expect(bar).toBeVisible()
    // An escalated or failed item adds "N need attention" (#1129); the rest of the label is unchanged.
    await expect(bar).toHaveAttribute('aria-label', /^\d+ done, \d+ under way, (\d+ need attention, )?\d+ still to do, backlog unknown$/)
    await expect(card.getByText('backlog unknown', { exact: true })).toBeVisible()
    await expect(card.getByText(/known items? done/)).toBeVisible()
    await expect(bar.getByTestId('unknown-remainder')).toBeVisible()
  })

  test(`@active the Progress card draws the full readable backlog in the ${theme} theme`, async ({ page }) => {
    // The live duplicate-name Retired crew has one not-started issue (#652). It has no recorded campaign, and a team
    // without one shows only while work is under way, queued or needs attention (#1415): record it as active here.
    await page.route('**/api/v1/agent-teams/presets/*/summary*', async route => {
      const response = await route.fetch()
      const summary: AgentTeamSummary = await response.json()
      if (summary.campaign) summary.campaign.campaign_status = 'active'
      await route.fulfill({ response, json: summary })
    })
    await useTheme(page, theme)
    await page.goto('/?team_id=4')
    const card = page.locator('main').getByRole('article', { name: /^Progress:/ })
    await expect(card).toBeVisible()
    const bar = card.getByRole('img')
    await expect(bar).toBeVisible()
    await expect(bar).toHaveAttribute('aria-label', '0 done, 0 under way, 1 still to do, backlog known')
    await expect(card.getByText('0 of 1 done', { exact: false })).toBeVisible()
    await expect(card.getByText('1 not started', { exact: false })).toBeVisible()
    await expect(card.getByText('backlog unknown', { exact: true })).toHaveCount(0)
    await expect(bar.getByTestId('unknown-remainder')).toHaveCount(0)
    const segments = await bar.locator(':scope > span').evaluateAll(elements => elements.map(element => element.getBoundingClientRect().width))
    const width = await bar.evaluate(element => element.getBoundingClientRect().width)
    expect(segments.reduce((sum, value) => sum + value, 0)).toBeCloseTo(width, 0)
    // The still-to-do segment fills the bar; it is found by identity, not position (#1129 added a segment).
    const todo = await bar.getByTestId('campaign-bar-todo').evaluate(element => element.getBoundingClientRect().width)
    expect(todo).toBeCloseTo(width, 0)
    expect(await contrastFailures(card)).toEqual([])
  })
}

// #1256: the campaign health block. The offline seed records no campaign, so the list and the health reads are
// answered here. The block shows item time, the unknown cost with its reason and the labelled four-hour window, and its
// link goes to a page that exists (the Retrospectives list).
const HEALTH = {
  schema_version: 1, campaign_id: 41, source: 'live', as_of: new Date().toISOString(),
  item_time: { items: 41, timed: 41, coverage: 'full', unknown_reasons: [], bands: [
    { band: '0-100', items: 41, timed: 41, median_seconds: 2280, p85_seconds: 4260, small_sample: false, coverage: 'full', unknown_reasons: [] },
    { band: 'over-400', items: 3, timed: 3, median_seconds: 9000, p85_seconds: 12000, small_sample: true, coverage: 'partial', unknown_reasons: [] }] },
  item_time_reason: null,
  cost: { value_usd: null, coverage: 'unavailable', members_total: null, members_known: null, unknown_members: [], prices_as_of: null, reasons: ['cost_not_recorded'] },
  bottleneck: { window: 'last_four_hours', label: 'Last 4 hours', started_at: new Date().toISOString(), as_of: new Date().toISOString(),
    coverage: 'full', unknown_reasons: [], items: 9, credited_seconds: 8700, state: 'ranked', stages: [{
      key: 'k', group: 'waiting', stage: 'decision', actor: 'the Leader with a long responsible name that must wrap on a phone',
      credited_seconds: 5400, share: 0.62, items: 9, median_seconds: 600, small_sample: false, coverage: 'full' }], largest_observed: null },
  bottleneck_reason: null,
}

for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
  for (const theme of ['light', 'dark'] as Theme[]) {
    test(`@active the campaign health block fits, reads and links at ${viewport.width}px in the ${theme} theme`, async ({ page }) => {
      await page.setViewportSize(viewport)
      await useTheme(page, theme)
      const presets: { presets: { id: number; name: string }[] } = await (await page.request.get('/api/v1/agent-teams/presets')).json()
      const team = presets.presets.find(preset => preset.name === 'Release crew')!
      await page.route('**/api/v1/agent-teams/presets/*/summary*', async route => {
        const response = await route.fetch()
        const summary: AgentTeamSummary = await response.json()
        if (summary.campaign) summary.campaign.campaign_status = 'active'
        await route.fulfill({ response, json: summary })
      })
      const row = {
        id: 41, team: { id: team.id, name: team.name }, selector: 'label:health-check', status: 'active',
        started_at: new Date().toISOString(), finished_at: null,
        merged: { value: 3, reason: null }, in_scope: { value: 5, reason: null }, repositories: [],
        repositories_unknown_reason: 'no_snapshot', source: null,
      }
      await page.route('**/api/v1/factory/campaigns?*', route => route.fulfill({ json: {
        schema_version: 1, generated_at: new Date().toISOString(), campaigns: [row], total: 1,
        counts: { live: 1, finished: 0 }, hidden_unknown_repositories: 0, finished_before_recording: 0,
        has_more: false, next_cursor: null } }))
      await page.route('**/api/v1/factory/campaigns/41/health', route => route.fulfill({ json: HEALTH }))
      await page.goto('/')
      const card = page.locator('main').getByRole('article', { name: /^Progress:/ }).filter({ hasText: 'Release crew' })
      const block = card.getByRole('group', { name: 'Campaign health' })
      await expect(block).toBeVisible()
      await expect(block.getByText('38 min')).toBeVisible()
      await expect(block.getByText(/over-400 lines .* small sample/)).toBeVisible()
      await expect(block.getByText(/the cost is not recorded yet/)).toBeVisible()
      await expect(block.getByText(/^Waiting for the Leader with a long responsible name/)).toBeVisible()
      await expect(block.getByText(/^Last 4 hours/)).toBeVisible()
      // The card keeps its counts and the block fits the card, also on a phone.
      await expect(card.getByRole('heading', { name: /^Done/ })).toBeVisible()
      const fits = await block.evaluate(element => element.scrollWidth <= element.clientWidth + 1)
      expect(fits, 'the health block does not scroll sideways').toBe(true)
      const page_fits = await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1)
      expect(page_fits, 'the page does not scroll sideways').toBe(true)
      expect(await contrastFailures(block)).toEqual([])
      if (process.env.E2E_SHOT_DIR) await card.screenshot({ path: `${process.env.E2E_SHOT_DIR}/health-${viewport.width}-${theme}.png` })
      // The link works: it opens the Retrospectives list for this team's live campaigns.
      await block.getByRole('link', { name: /Live retrospective/ }).click()
      await expect(page).toHaveURL(new RegExp(`/insights/retrospectives\\?team_id=${team.id}&status=live`))
      await expect(page.getByRole('main').getByText('health-check').first()).toBeVisible()
    })
  }
}

test('@active no empty section shows a filter message when no filter is set', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible()
  await page.waitForLoadState('networkidle').catch(() => undefined)
  await expect(page.locator('main')).not.toContainText(/(no|nothing) (items? )?(match|matches|matching) (the|this|these|your) filters?/i)
  await expect(page.locator('main')).not.toContainText(/reset filters to see/i)
})

for (const theme of ['light', 'dark'] as Theme[]) {
  test(`@active the Overview meets AA contrast and is keyboard reachable in the ${theme} theme`, async ({ page }) => {
    await useTheme(page, theme)
    await page.goto('/')
    await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible()
    const failures = await contrastFailures(page.locator('main'))
    expect(failures, JSON.stringify(failures)).toEqual([])
    await expectKeyboardReachable(page, page.locator('main'), 3)
  })
}

for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
  test(`@active the document stays fixed at the end of main at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport)
    const finished = Array.from({ length: 3 }, (_, index) => seedAgo({ days: index + 1, minutes: 22 }))
    // Fill the three recent-finish positions for each seeded campaign.
    await page.route('**/api/v1/agent-teams/presets/*/summary', async route => {
      const response = await route.fetch()
      const summary: AgentTeamSummary = await response.json()
      if (summary.campaign) {
        summary.campaign.campaign_status = 'active'  // the seeded teams have no campaign record (#1415)
        summary.campaign.done_count = finished.length
        summary.campaign.latest_done = finished.map((moment, index) => ({
          title: `Completed release task ${index + 1}`,
          merged_at: moment.toISOString(),
        }))
      }
      await route.fulfill({ response, json: summary })
    })

    const main = page.getByRole('main')
    const pages = [
      { path: '/', title: 'Overview', loaded: 'Completed release task 1' },
      { path: '/work', title: 'Work', loaded: 'Add the CSV export button' },
      { path: '/teams', title: 'Teams', loaded: 'Quiet team' },
    ]
    for (const { path, title, loaded } of pages) {
      await page.goto(path)
      await expect(main.getByRole('heading', { name: title, exact: true })).toBeVisible()
      await expect(main.getByText(loaded, { exact: false }).first()).toBeVisible()
      if (title === 'Overview') {
        const timestamps = main.getByRole('article', { name: /^Progress:/ }).locator('time[datetime]')
        await expect.poll(() => timestamps.count()).toBeGreaterThanOrEqual(9)
        const firstFinish = main.locator(`time[datetime="${finished[0].toISOString()}"]`).first()
        expect(await firstFinish.ariaSnapshot()).toMatch(localClockPattern(finished[0]))
      }
      const layout = await main.evaluate(element => {
        element.scrollTop = element.scrollHeight
        window.scrollTo(0, document.documentElement.scrollHeight)
        return {
          documentHeight: document.documentElement.scrollHeight,
          documentScroll: window.scrollY,
          mainScroll: element.scrollTop,
          mainEnd: element.scrollHeight - element.clientHeight,
        }
      })
      expect(layout.documentHeight, title).toBe(viewport.height)
      expect(layout.documentScroll, title).toBe(0)
      expect(layout.mainScroll, title).toBeCloseTo(layout.mainEnd, 0)
      if (title === 'Overview') expect(layout.mainScroll).toBeGreaterThan(0)
    }
  })
}
