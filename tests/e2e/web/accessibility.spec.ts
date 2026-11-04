import { randomUUID } from 'node:crypto'
import { test, expect } from './fixtures'
import ja from '../../../src/frontend/src/app/locales/ja.json' with { type: 'json' }
import en from '../../../src/frontend/src/app/locales/en.json' with { type: 'json' }

for (const locale of ['ja', 'en'] as const) {
  const messages = locale === 'ja' ? ja : en
  test(`${locale}: primary pages and editor errors expose accessible controls`, async ({
    page,
    request,
    auditAccessibility,
  }) => {
    const preferences = await (await request.get('/api/v1/preferences')).json()
    await request.put('/api/v1/preferences', {
      data: { ...preferences, locale },
    })
    for (const path of [
      '/',
      '/player',
      '/characters',
      '/stories',
      '/new',
      '/images',
      '/settings',
    ]) {
      await page.goto(path)
      await expect(page.locator('main')).toBeVisible()
      await expect(page.locator('h1')).toBeVisible()
      await auditAccessibility()
    }
    await page.goto('/characters/new')
    const name = page.getByLabel(messages.name, { exact: true })
    await name.fill(`Unsaved ${locale}`)
    await page.route('**/api/v1/characters/save', (route) =>
      route.fulfill({
        status: 422,
        contentType: 'application/json',
        body: '{"code":"invalid_request"}',
      }),
    )
    await page.getByRole('button', { name: messages.save, exact: true }).click()
    await expect(page.getByRole('alert').first()).toBeVisible()
    await expect(name).toHaveValue(`Unsaved ${locale}`)
    await auditAccessibility()
  })

  test(`${locale}: conversation, confirmation and recovery states remain accessible`, async ({
    page,
    request,
    auditAccessibility,
  }) => {
    const preferences = await (await request.get('/api/v1/preferences')).json()
    await request.put('/api/v1/preferences', {
      data: { ...preferences, locale },
    })
    const created = await (
      await request.post('/api/v1/characters/save', {
        data: {
          operation_id: randomUUID(),
          value: { name: `Audit ${locale}` },
        },
      })
    ).json()
    await page.goto(`/characters/${created.preset.id}`)
    await page
      .getByRole('button', { name: messages.delete, exact: true })
      .click()
    await expect(page.locator('.confirmation')).toBeVisible()
    await auditAccessibility()
    await page.goto('/new')
    await page
      .getByRole('button', { name: messages.create, exact: true })
      .click()
    await expect(page).toHaveURL(/\/conversations\//)
    await auditAccessibility()
    await page.setViewportSize({ width: 390, height: 844 })
    const state = page.getByRole('button', {
      name: messages.showState,
      exact: true,
    })
    await state.focus()
    await page.keyboard.press('Enter')
    await expect(page.locator('dialog')).toBeVisible()
    await auditAccessibility()
    await page.keyboard.press('Escape')
    await expect(page.locator('dialog')).not.toBeVisible()
    await expect(state).toBeFocused()
    await page.route('**/api/v1/conversations', (route) =>
      route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: '{"code":"operation_failed"}',
      }),
    )
    await page.goto('/')
    await expect(page.getByRole('alert')).toBeVisible()
    await auditAccessibility()
  })

  test.describe(`${locale}: enlarged layout`, () => {
    test.use({ viewport: { width: 640, height: 450 }, deviceScaleFactor: 2 })
    test('keeps input visible and history scrollable at double render scale', async ({
      page,
      request,
      evidence,
      auditAccessibility,
    }) => {
      const preferences = await (
        await request.get('/api/v1/preferences')
      ).json()
      await request.put('/api/v1/preferences', {
        data: { ...preferences, locale },
      })
      await page.goto('/new')
      await page
        .getByRole('button', { name: messages.create, exact: true })
        .click()
      await expect(page).toHaveURL(/\/conversations\//)
      await expect(
        page.getByRole('textbox', { name: messages.messageLabel }),
      ).toBeInViewport()
      const history = page.getByRole('log', {
        name: messages.conversationHistory,
      })
      await history.focus()
      await page.keyboard.press('PageDown')
      await expect
        .poll(() => history.evaluate((element) => element.scrollTop))
        .toBeGreaterThan(0)
      await evidence.capture('Enlarged layout with keyboard scrolling')
      await auditAccessibility()
      const state = page.getByRole('button', {
        name: messages.showState,
        exact: true,
      })
      await state.focus()
      await page.keyboard.press('Enter')
      await expect(page.locator('dialog')).toBeVisible()
      await evidence.capture('Enlarged scene details')
      await auditAccessibility()
      await page.keyboard.press('Escape')
      await expect(state).toBeFocused()
    })
  })
}
