import { expect, test } from '../support/guard'

// #1230: the Repositories page does not scroll sideways at 390 px. The repository table
// scrolls inside its own box; the page and its heading stay within the viewport.

test.use({ viewport: { width: 390, height: 844 } })

test('@active the Repositories page has no horizontal page scroll at 390 px', async ({ page }) => {
  await page.goto('/repositories')
  await expect(page.getByRole('heading', { name: 'Repositories', exact: true })).toBeVisible()
  await expect(page.locator('main table tbody tr').first()).toBeVisible()

  const widths = await page.evaluate(() => ({
    document: document.documentElement.scrollWidth,
    viewport: document.documentElement.clientWidth,
  }))
  expect(widths.document, 'the page is wider than the viewport').toBeLessThanOrEqual(widths.viewport)

  const outside = await page.locator('main').evaluate(main => {
    const box = main.querySelector('table')!.parentElement!
    return [...main.querySelectorAll<HTMLElement>('*')]
      .filter(element => !box.contains(element))
      .map(element => ({ tag: element.tagName.toLowerCase(), right: element.getBoundingClientRect().right }))
      .filter(item => item.right > document.documentElement.clientWidth + 0.5)
  })
  expect(outside, 'elements outside the table box that leave the viewport').toEqual([])
})
