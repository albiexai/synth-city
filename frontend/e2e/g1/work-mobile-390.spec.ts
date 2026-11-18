import { expect, test } from '../support/guard'

// #1166: the Work page does not scroll sideways at 390 px. The work table scrolls
// inside its own box; the page, its filters and its chips stay within the viewport.

test.use({ viewport: { width: 390, height: 844 } })

test('@active the Work page has no horizontal page scroll at 390 px', async ({ page }) => {
  await page.goto('/work')
  await expect(page.getByRole('heading', { name: 'Work', exact: true })).toBeVisible()
  await expect(page.locator('main [data-work-table] tbody tr').first()).toBeVisible()

  const widths = await page.evaluate(() => ({
    document: document.documentElement.scrollWidth,
    viewport: document.documentElement.clientWidth,
  }))
  expect(widths.document, 'the page is wider than the viewport').toBeLessThanOrEqual(widths.viewport)

  const outside = await page.locator('main').evaluate(main => {
    const table = main.querySelector('[data-work-table]')!
    return [...main.querySelectorAll<HTMLElement>('*')]
      .filter(element => !table.contains(element))
      .map(element => ({ tag: element.tagName.toLowerCase(), right: element.getBoundingClientRect().right }))
      .filter(item => item.right > document.documentElement.clientWidth + 0.5)
  })
  expect(outside, 'elements outside the table box that leave the viewport').toEqual([])
})
