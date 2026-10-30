import { test, expect } from '@playwright/test'
import ja from '../../../src/frontend/src/app/locales/ja.json' with { type: 'json' }
import en from '../../../src/frontend/src/app/locales/en.json' with { type: 'json' }

test('selects saved settings, sends and restores a conversation', async ({
  page,
}) => {
  await page.goto('/new')
  await expect(page.getByLabel(ja.chooseCharacter)).toContainText('栞')
  await page.getByRole('button', { name: ja.create, exact: true }).click()
  await expect(
    page.getByRole('heading', { name: '雨の日の古書店', exact: true }),
  ).toBeVisible()
  await page.getByRole('button', { name: ja.sampleAction }).click()
  await page.getByRole('button', { name: ja.send, exact: true }).click()
  await expect(page.locator('.assistant-message p').first()).toContainText(
    'つむぎ',
  )
  await page.reload()
  await expect(page.locator('.assistant-message p').first()).toContainText(
    'つむぎ',
  )
  await expect(page.locator('.state-panel')).not.toContainText('sample')
})

test('saves, duplicates and edits a character with an automatically generated image', async ({
  page,
}) => {
  await page.goto('/characters/new')
  await page.getByLabel(ja.name, { exact: true }).fill('Mira')
  await page
    .getByLabel(ja.appearance, { exact: true })
    .fill('An adult woman with short red hair and a green coat')
  await page.getByRole('button', { name: ja.save, exact: true }).click()
  await expect(page.getByRole('status').first()).toHaveText(ja.saved)
  await page.getByRole('button', { name: ja.retryCollection }).click()
  const portrait = page
    .locator('.cover-preview')
    .getByRole('img', { name: 'Mira', exact: true })
  await expect(portrait).toBeVisible()
  await expect(portrait).toHaveJSProperty('naturalWidth', 8)
  await page.reload()
  await expect(portrait).toHaveJSProperty('naturalWidth', 8)
  await page.getByRole('button', { name: ja.duplicate }).click()
  await expect(
    page.locator('.preset-card h2').filter({ hasText: 'Mira' }),
  ).toHaveCount(2)
  await page.getByRole('link', { name: 'Mira', exact: true }).first().click()
  await page.getByLabel(ja.name, { exact: true }).fill('Mira edited')
  await page.getByLabel(ja.autoImage).uncheck()
  await page.getByRole('button', { name: ja.save, exact: true }).click()
  await page.reload()
  await expect(page.getByLabel(ja.name, { exact: true })).toHaveValue(
    'Mira edited',
  )
})

test('generates and collects a standalone image', async ({ page }) => {
  await page.goto('/images')
  await page
    .getByLabel(ja.imagePrompt, { exact: true })
    .fill('a quiet green garden')
  await page.getByRole('button', { name: ja.generate, exact: true }).click()
  await page
    .getByRole('button', { name: ja.retryCollection, exact: true })
    .first()
    .click()
  const image = page
    .getByRole('img', {
      name: ja.imageAlt.replace('{index}', '1'),
      exact: true,
    })
    .first()
  await expect(image).toBeVisible()
  await expect(image).toHaveJSProperty('naturalWidth', 8)
})

test('starts a saved story with its recommended counterpart after reopening', async ({
  page,
}) => {
  await page.goto('/stories/new')
  await page.getByLabel(ja.name, { exact: true }).fill('Clocktower')
  await page
    .getByLabel(ja.opening, { exact: true })
    .fill('A visitor waits at the clocktower.')
  await expect(
    page.getByRole('option', { name: 'Shiori', exact: true }),
  ).toHaveCount(0)
  await page
    .getByLabel(ja.defaultCharacter)
    .selectOption({ label: 'Mira edited' })
  await page.getByLabel(ja.autoImage).uncheck()
  await page.getByRole('button', { name: ja.save, exact: true }).click()
  await expect(page.getByRole('status').first()).toHaveText(ja.saved)
  await page.getByRole('link', { name: ja.play, exact: true }).click()
  await page
    .locator('.preset-card')
    .filter({
      has: page.getByRole('heading', { name: 'Clocktower', exact: true }),
    })
    .getByRole('link', { name: ja.use + ' →', exact: true })
    .click()
  await expect(
    page.getByLabel(ja.chooseCharacter).locator('option:checked'),
  ).toHaveText('Mira edited')
  await expect(
    page.getByText('A visitor waits at the clocktower.', { exact: true }),
  ).toBeVisible()
  await page.reload()
  await expect(
    page.getByLabel(ja.chooseCharacter).locator('option:checked'),
  ).toHaveText('Mira edited')
  await page.getByLabel(ja.profile).selectOption({ label: ja.localModel })
  await page.getByRole('button', { name: ja.create, exact: true }).click()
  await expect(
    page.getByRole('heading', { name: 'Clocktower', exact: true }),
  ).toBeVisible()
  await page.reload()
  await expect(
    page
      .getByText('A visitor waits at the clocktower.', { exact: true })
      .first(),
  ).toBeVisible()
})

test('persists language and selects the paired English sample', async ({
  page,
}) => {
  await page.goto('/settings')
  await page
    .getByRole('combobox', { name: ja.language, exact: true })
    .selectOption('en')
  await expect(
    page.getByRole('heading', { name: en.preferences, exact: true }),
  ).toBeVisible()
  await page.reload()
  await expect(page.locator('html')).toHaveAttribute('lang', 'en')
  await page.goto('/new')
  await expect(page.getByLabel(en.chooseStory)).toContainText(
    'The Rainy Bookshop',
  )
  await page.getByRole('button', { name: en.create, exact: true }).click()
  await page.getByRole('button', { name: en.sampleAction }).click()
  await page.getByRole('button', { name: en.send, exact: true }).click()
  await expect(page.locator('.assistant-message p').first()).toContainText(
    'Hello',
  )
  await page.goto('/settings')
  await page
    .getByRole('combobox', { name: en.language, exact: true })
    .selectOption('ja')
  await expect(page.locator('html')).toHaveAttribute('lang', 'ja')
})

test('keeps selection and keyboard navigation usable on a narrow screen', async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: ja.emptyTitle })).toBeVisible()
  await page.getByRole('button', { name: ja.showNavigation }).click()
  await page
    .getByRole('link', { name: ja.newConversation, exact: true })
    .first()
    .click()
  await expect(page.getByLabel(ja.choosePlayer)).toBeVisible()
  await page.getByLabel(ja.choosePlayer).focus()
  await page.keyboard.press('Tab')
  await expect(page.locator(':focus')).toHaveClass(/text-button/)
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true)
  await page.getByRole('button', { name: ja.create, exact: true }).click()
  const composer = page.getByRole('textbox', {
    name: ja.messageLabel,
    exact: true,
  })
  await expect(composer).toBeInViewport()
  await page.getByRole('button', { name: ja.sampleAction }).click()
  await page.getByRole('button', { name: ja.send, exact: true }).click()
  await expect(page.locator('.assistant-message p').first()).toContainText(
    'つむぎ',
  )
  await page.screenshot({
    path: '.artifacts/records/reports/playwright/narrow.png',
  })
})
