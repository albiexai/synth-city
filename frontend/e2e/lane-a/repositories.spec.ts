import { contrastFailures, useTheme, type Theme } from '../support/contrast'
import { expect, test } from '../support/guard'
import { expectKeyboardReachable } from '../support/keyboard'

// #536: Repositories is the single home for repository setup and settings.
// Settings are viewed and edited in one place only; there is one flow to add a
// repository; no repository page shows a copy of the Work table or an internal
// scope number in its title.
// Design reference: screens 07 (list), 08 (repository page), 09 (add).
// Active since #536.

test('@active repository settings are viewed and edited only on the repository page', async ({ page }) => {
  await page.goto('/repositories/1')
  await expect(page.locator('main').getByRole('heading', { name: /example-org\/sample-app/ }).first()).toBeVisible()
  await expect(page.locator('main').getByRole('button', { name: /edit|save/i }).first()).toBeVisible()
  await page.goto('/teams/1?tab=autonomy')
  await expect(page.locator('main')).toBeVisible()
  await page.waitForLoadState('networkidle').catch(() => undefined)
  await expect(page.locator('main').getByRole('button', { name: /edit (repository|scope|watched)|save (repository|scope)/i })).toHaveCount(0)
  await expect(page.locator('main a[href="/repositories/1"]').first()).toBeVisible()
})

test('@active there is one flow to add a repository', async ({ page }) => {
  await page.goto('/repositories')
  await expect(page.locator('main a[href="/repositories/new"]').first()).toBeVisible()
  await page.goto('/teams/1?tab=autonomy')
  await page.waitForLoadState('networkidle').catch(() => undefined)
  await expect(page.locator('main').getByRole('button', { name: /add (a )?(repository|scope|watched repo)/i })).toHaveCount(0)
})

test('@active a repository page shows no Work table copy and no scope number in its title', async ({ page }) => {
  await page.goto('/repositories/1')
  const title = page.locator('main').getByRole('heading').first()
  await expect(title).toBeVisible()
  await expect(title).not.toContainText(/scope\s*\d+/i)
  expect(await page.title()).not.toMatch(/scope\s*\d+/i)
  await expect(page.locator('main table').filter({ hasText: /Add the CSV export button/ })).toHaveCount(0)
  await expect(page.locator('main a[href^="/work?"]').first()).toBeVisible()
})

test('@active Teams links to scoped workspace states and wait reasons at desktop and 390 px', async ({ page }) => {
  const longPath = '/fixture/' + 'long-worktree-path-'.repeat(12)
  await page.route('**/api/v1/agent-teams/github-scopes/1/workspaces/readiness', async route => {
    expect(route.request().method()).toBe('GET')
    expect(route.request().headers()['x-synth-city-operator-token']).toBe('fixture-director-key')
    await route.fulfill({
      json: {
        scope_id: 1,
        workspaces: [
          { workspace_id: 1, path: longPath, state: 'free', reason: null, leased_issue_number: null },
          { workspace_id: 2, path: '/fixture/leased', state: 'leased', reason: null, leased_issue_number: 42 },
          { workspace_id: 3, path: '/fixture/blocked', state: 'blocked', reason: 'repository_identity_mismatch', leased_issue_number: null },
          { workspace_id: 4, path: '/fixture/missing', state: 'missing', reason: 'workspace_missing', leased_issue_number: null },
        ],
        waiting_items: [{ work_item_id: 85, issue_number: 56, reason: 'no_free_workspace' }],
      },
    })
  })

  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto('/teams/1?tab=autonomy')
  await page.getByRole('link', { name: 'Workspaces for example-org/sample-app' }).click()
  await expect(page).toHaveURL(/\/repositories\/1#workspaces$/)
  const card = page.getByRole('region', { name: 'Workspaces' })
  await expect(card).toBeFocused()
  await card.getByRole('button', { name: 'Load workspace status' }).click()
  const dialog = page.getByRole('dialog', { name: 'Director key' })
  await dialog.getByLabel(/^Director key/).fill('fixture-director-key')
  await dialog.getByRole('button', { name: 'Use key' }).click()

  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 900 })
    for (const label of ['Free', 'Leased to #42', 'Blocked', 'Missing', 'repository_identity_mismatch', 'No workspace is free.']) {
      await expect(card.getByText(label, { exact: true })).toBeVisible()
    }
    await expect(card.getByRole('link', { name: '#56' })).toHaveAttribute('href', '/work/85')
    const fits = await card.evaluate(element =>
      element.scrollWidth <= element.clientWidth &&
      document.documentElement.scrollWidth <= document.documentElement.clientWidth,
    )
    expect(fits, `Workspace status fits at ${width} px`).toBe(true)
  }
})

for (const theme of ['light', 'dark'] as Theme[]) {
  test(`@active Repositories meets AA contrast and is keyboard reachable in the ${theme} theme`, async ({ page }) => {
    await useTheme(page, theme)
    for (const route of ['/repositories', '/repositories/1']) {
      await page.goto(route)
      await expect(page.locator('main').getByRole('heading').first()).toBeVisible()
      const failures = await contrastFailures(page.locator('main'))
      expect(failures, `${route}: ${JSON.stringify(failures)}`).toEqual([])
    }
    await expectKeyboardReachable(page, page.locator('main'), 3)
  })
}
