import { expect, type Page } from '@playwright/test'

// The synthetic Director key of the E2E instance (OPERATOR_TOKEN in
// scripts/e2e-instance.sh). Code and the current UI call it "operator token";
// #518 renames the people-facing text to "Director key". Match both.
export const DIRECTOR_KEY = 'e2e-director-key-0000000000000000'
const DIALOG_NAME = /Operator token|Director key/i
const SUBMIT_NAME = /Use token|Use key|Unlock|Continue/i
const OPENER_NAME = /^Enter (the Director )?key$/

/** Wait for the Director key dialog. */
export async function expectDirectorKeyDialog(page: Page): Promise<void> {
  await expect(page.getByRole('dialog', { name: DIALOG_NAME })).toBeVisible()
}

/** Open the Director key dialog if it is closed, enter the key and wait until it closes. */
export async function enterDirectorKey(page: Page, key: string = DIRECTOR_KEY): Promise<void> {
  const dialog = page.getByRole('dialog', { name: DIALOG_NAME })
  const opener = page.getByRole('button', { name: OPENER_NAME }).first()
  await expect(dialog.or(opener).first()).toBeVisible()
  if (!(await dialog.isVisible())) {
    await opener.click()
  }
  await expect(dialog).toBeVisible()
  // A password input has no textbox role; the dialog has one key field.
  await dialog.locator('input:not([type=checkbox]):not([type=hidden])').first().fill(key)
  await dialog.getByRole('button', { name: SUBMIT_NAME }).click()
  await expect(dialog).toBeHidden()
}

/** Choose "Unlock events" on Audit. The page asks for the key only then (#515). */
export async function chooseUnlockEvents(page: Page): Promise<void> {
  await page.getByRole('button', { name: /unlock events/i }).first().click()
}
