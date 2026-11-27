import { contrastFailures, useTheme, type Theme } from '../support/contrast'
import { expect, test } from '../support/guard'
import { expectKeyboardReachable } from '../support/keyboard'

// #542: a team opens as a readable page. At 1440 × 900 the header and all
// members of a 4-member team are visible without scrolling; no editable field
// shows until the person selects Edit team; no charter text shows until a
// member row is expanded.
// Seed (scripts/e2e/scenarios.py, seed_lane_b): "Docs crew" (team 5) has 4
// members and no scope. Each seeded slot has "Synthetic charter for <name>."
// Design reference: screen 06 (Teams).
// Active since #542.

const TEAM = '/teams/5'
const MEMBERS = ['D2 Docs lead', 'D3 Writer', 'D4 Editor', 'D5 Reviewer']

test.use({ viewport: { width: 1440, height: 900 } })

for (const width of [1440, 390]) {
  for (const shared of [true, false]) {
    test(`@active the shared-user warning follows the install at ${width}px (shared: ${shared})`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 })
      const users = [{ name: 'controller', is_controller: true, is_default: shared }]
      if (!shared) users.push({ name: 'agent', is_controller: false, is_default: true })
      await page.route('**/api/v1/harness-scope', route => route.fulfill({
        json: { users, default_user: shared ? 'controller' : 'agent' },
      }))
      await page.route('**/api/v1/agent-mail/authentication-status', route => route.fulfill({
        json: { director_key_configured: true },
      }))
      const scopeLoaded = page.waitForResponse('**/api/v1/harness-scope')
      await page.goto(TEAM)
      await scopeLoaded
      const warning = /agents running as the controller can read the operator token/i
      if (shared) await expect(page.locator('main').getByText(warning)).toBeVisible()
      else await expect(page.locator('main').getByText(warning)).toHaveCount(0)
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)

      const promptScope = page.waitForResponse('**/api/v1/harness-scope')
      await page.getByRole('button', { name: 'From Mail' }).click()
      const prompt = page.getByRole('dialog', { name: 'Director key' })
      await expect(prompt).toBeVisible()
      await promptScope
      if (shared) await expect(prompt.getByText(warning)).toBeVisible()
      else await expect(prompt.getByText(warning)).toHaveCount(0)
      expect((await prompt.boundingBox())?.width).toBeLessThanOrEqual(width)
    })
  }

  test(`@active shows a queued handoff on the team and its work item at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    const now = Date.now()
    let queued = true
    await page.route(/\/agent-teams\/presets\/1\/github-work-items\?/, async (route) => {
      const response = await route.fetch()
      const body = await response.json()
      const item = body.items.find((entry: { id: number }) => entry.id === 3)
      if (queued) Object.assign(item, {
        handoff_state: 'queued',
        handoff_target_slot_id: 4,
        handoff_queued_at: new Date(now - 10 * 60_000).toISOString(),
        handoff_expires_at: new Date(now + 20 * 60_000).toISOString(),
      })
      await route.fulfill({ response, json: body })
    })
    await page.goto('/teams/1?tab=autonomy')
    const queue = page.getByRole('region', { name: 'Queued handoffs' })
    await expect(queue.getByText('F1 Frontend', { exact: true })).toBeVisible()
    await expect(queue.getByText(/\d+ min ago/)).toBeVisible()
    await expect(queue.getByText('Expires', { exact: true })).toBeVisible()
    await queue.getByRole('link', { name: '#103' }).click()

    const handoff = page.getByRole('region', { name: 'Queued handoff', exact: true })
    await expect(handoff.getByText('F1 Frontend', { exact: true })).toBeVisible()
    await expect(handoff.getByText(/\d+ min ago/)).toBeVisible()
    await expect(handoff.getByText('Expires', { exact: true })).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)

    queued = false
    await page.goto('/teams/1?tab=autonomy')
    await expect(page.getByRole('link', { name: 'View in Work ›' })).toBeVisible()
    await expect(queue).toHaveCount(0)
  })
}

test('@active the header and all 4 members are visible without scrolling', async ({ page }) => {
  await page.goto(TEAM)
  await expect(page.locator('main').getByRole('heading', { name: 'Docs crew' }).first()).toBeInViewport()
  for (const member of MEMBERS) {
    await expect(page.locator('main').getByText(member, { exact: true }).first(), member).toBeInViewport()
  }
  expect(await page.evaluate(() => window.scrollY)).toBe(0)
})

test('@active no editable field shows until Edit team', async ({ page }) => {
  await page.goto(TEAM)
  const main = page.locator('main')
  await expect(main.getByText('D2 Docs lead').first()).toBeVisible()
  await expect(main.locator('input:not([type=checkbox]), textarea, select')).toHaveCount(0)
  await main.getByRole('button', { name: 'Edit team' }).click()
  await expect(main.locator('input:not([type=checkbox]), textarea').first()).toBeVisible()
})

test('@active no charter shows until a member row is expanded', async ({ page }) => {
  await page.goto(TEAM)
  const main = page.locator('main')
  await expect(main.getByText('D3 Writer').first()).toBeVisible()
  await expect(main.getByText(/Synthetic charter for/)).toHaveCount(0)
  const row = main.getByText('D3 Writer', { exact: true }).first()
    .locator('xpath=ancestor::*[self::li or self::article or self::details or contains(@class,"border")][1]')
  await row.getByRole('button', { name: /expand|details|show|more/i }).first().click()
  await expect(row.getByText('Synthetic charter for D3 Writer.')).toBeVisible()
})

for (const theme of ['light', 'dark'] as Theme[]) {
  test(`@active the team page meets AA contrast and is keyboard reachable in the ${theme} theme`, async ({ page }) => {
    await useTheme(page, theme)
    await page.goto(TEAM)
    await expect(page.locator('main').getByText('D2 Docs lead').first()).toBeVisible()
    const failures = await contrastFailures(page.locator('main'))
    expect(failures, JSON.stringify(failures)).toEqual([])
    await expectKeyboardReachable(page, page.locator('main'), 3)
  })
}
