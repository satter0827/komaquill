import { randomUUID } from 'node:crypto'
import { test, expect, fulfillFault } from './fixtures'
import ja from '../../../../src/frontend/src/app/locales/ja.json' with { type: 'json' }
import en from '../../../../src/frontend/src/app/locales/en.json' with { type: 'json' }

const SCROLL_EDGE_TOLERANCE_PX = 1

for (const locale of ['ja', 'en'] as const) {
  const messages = locale === 'ja' ? ja : en
  test(`${locale}: primary pages and editor errors expose accessible controls`, async ({
    page,
    request,
    auditAccessibility,
    evidence,
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
    const name = page.getByLabel(messages.requiredName, { exact: true })
    await name.fill(`Unsaved ${locale}`)
    await page.route('**/api/v1/characters/save', (route) =>
      fulfillFault(route, evidence, {
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
    evidence,
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
    await expect(
      page.getByRole('dialog', { name: messages.currentState, exact: true }),
    ).toBeVisible()
    await auditAccessibility()
    await page.keyboard.press('Escape')
    await expect(
      page.getByRole('dialog', { name: messages.currentState, exact: true }),
    ).not.toBeVisible()
    await expect(state).toBeFocused()
    await page.route('**/api/v1/conversations', (route) =>
      fulfillFault(route, evidence, {
        status: 503,
        contentType: 'application/json',
        body: '{"code":"operation_failed"}',
      }),
    )
    await page.goto('/')
    await expect(page.getByRole('alert')).toBeVisible()
    await auditAccessibility()
  })

  test(`${locale}: disconnected navigation keeps errors readable`, async ({
    page,
    request,
    evidence,
    auditAccessibility,
  }) => {
    const preferences = await (await request.get('/api/v1/preferences')).json()
    await request.put('/api/v1/preferences', {
      data: { ...preferences, locale },
    })
    await page.route('**/api/v1/availability*', (route) =>
      route.fulfill({
        json: {
          ready: false,
          connections: [
            {
              kind: 'llm',
              target: 'fixture',
              state: 'unavailable',
              reason: 'llm_unavailable',
              observed_at: new Date().toISOString(),
              expires_at: new Date(Date.now() + 60_000).toISOString(),
            },
          ],
        },
      }),
    )
    const scene = await (
      await request.post('/api/v1/conversations', {
        data: { title: 'Disconnected navigation', llm_profile: 'fake' },
      })
    ).json()
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 844 })
      await page.goto(`/conversations/${scene.id}`)
      if (width === 390)
        await page
          .getByRole('button', { name: messages.showNavigation })
          .click()
      const navigation = page.locator('.navigation')
      await navigation.locator('.connection-summary summary').click()
      const errorText = navigation.getByText(messages.errors.llm_unavailable, {
        exact: true,
      })
      await errorText.scrollIntoViewIfNeeded()
      const errorBox = (await errorText.boundingBox())!
      await expect(errorText).toBeInViewport({
        ratio: 1 - SCROLL_EDGE_TOLERANCE_PX / errorBox.height,
      })
      const contrast = await navigation.evaluate((element) => {
        const luminance = (color: string) => {
          const values = color
            .match(/[\d.]+/g)!
            .slice(0, 3)
            .map(Number)
          const [r, g, b] = values.map((value) => {
            const channel = value / 255
            return channel <= 0.04045
              ? channel / 12.92
              : ((channel + 0.055) / 1.055) ** 2.4
          })
          return 0.2126 * r + 0.7152 * g + 0.0722 * b
        }
        const backgrounds =
          getComputedStyle(element).backgroundImage.match(/rgb\([^)]+\)/g)!
        const minimum = (color: string) =>
          Math.min(
            ...backgrounds.map((background) => {
              const values = [luminance(color), luminance(background)]
              return (Math.max(...values) + 0.05) / (Math.min(...values) + 0.05)
            }),
          )
        return {
          backgrounds: backgrounds.length,
          error: minimum(
            getComputedStyle(element.querySelector('.error')!).color,
          ),
          warning: minimum(
            getComputedStyle(element.querySelector('[data-warning]')!)
              .backgroundColor,
          ),
        }
      })
      expect(contrast.backgrounds).toBeGreaterThanOrEqual(2)
      expect(contrast.error).toBeGreaterThanOrEqual(4.5)
      expect(contrast.warning).toBeGreaterThanOrEqual(3)
      const refresh = navigation.getByRole('button', {
        name: messages.refresh,
        exact: true,
      })
      await refresh.scrollIntoViewIfNeeded()
      const refreshBox = (await refresh.boundingBox())!
      await expect(refresh).toBeInViewport({
        ratio: 1 - SCROLL_EDGE_TOLERANCE_PX / refreshBox.height,
      })
      await evidence.capture(
        `${locale} ${width} disconnected navigation: text contrast ${contrast.error.toFixed(2)}, warning contrast ${contrast.warning.toFixed(2)}`,
      )
      await auditAccessibility()
    }
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
      await expect(
        page.getByRole('dialog', { name: messages.currentState, exact: true }),
      ).toBeVisible()
      await evidence.capture('Enlarged scene details')
      await auditAccessibility()
      await page.keyboard.press('Escape')
      await expect(state).toBeFocused()
    })
  })
}
