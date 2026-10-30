import { test, expect } from '@playwright/test'

test('creates, sends and restores a saved conversation', async ({ page }) => {
  await page.goto('/new')
  const title = `Saved conversation ${Date.now()}`
  await page.getByLabel('Title', { exact: true }).fill(title)
  await page
    .getByRole('button', { name: 'Create conversation', exact: true })
    .click()
  await expect(
    page.getByRole('heading', { name: title, exact: true }),
  ).toBeVisible()
  await page
    .getByRole('textbox', { name: 'Message', exact: true })
    .fill('Hello')
  await page.getByRole('button', { name: 'Send message', exact: true }).click()
  await expect(
    page.getByText(
      'Hello. This is a saved sample response for checking conversation storage and display.',
      { exact: true },
    ),
  ).toBeVisible()
  await page.reload()
  await expect(page.getByText('greeted', { exact: true })).toBeVisible()
})

test('generates and collects an image through the HTTP API', async ({
  page,
}) => {
  await page.goto('/images')
  await page
    .getByLabel('Describe an image', { exact: true })
    .fill('a quiet green garden')
  await page
    .getByRole('button', { name: 'Generate image', exact: true })
    .click()
  await page
    .getByRole('button', { name: 'Collect available images', exact: true })
    .first()
    .click()
  const image = page
    .getByRole('img', { name: 'Generated candidate 1', exact: true })
    .first()
  await expect(image).toBeVisible()
  await expect(image).toHaveJSProperty('naturalWidth', 8)
})

test('keeps the composer usable on a narrow screen', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await expect(
    page.getByRole('heading', { name: 'Start with a conversation' }),
  ).toBeVisible()
  await expect(
    page.getByRole('button', { name: 'Show navigation' }),
  ).toBeVisible()
})
