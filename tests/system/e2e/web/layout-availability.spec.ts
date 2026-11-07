import { randomUUID } from 'node:crypto'
import { test, expect, fulfillFault } from './fixtures'
import ja from '../../../../src/frontend/src/app/locales/ja.json' with { type: 'json' }
import en from '../../../../src/frontend/src/app/locales/en.json' with { type: 'json' }

const LAYOUT_TIMEOUT_MS = 120_000
const viewports = {
  desktop: { width: 1440, height: 960 },
  mobile: { width: 390, height: 844 },
}

for (const locale of ['ja', 'en'] as const) {
  for (const [viewport, size] of Object.entries(viewports)) {
    test(`${locale} ${viewport}: pages retain natural widths across display preferences`, async ({
      page,
      request,
      evidence,
    }) => {
      test.setTimeout(LAYOUT_TIMEOUT_MS)
      await page.setViewportSize(size)
      const m = locale === 'ja' ? ja : en
      const title = 'Conversation layout review'
      const retainedMessage =
        'Keep this accepted message after the reply fails.'
      const scene = await (
        await request.post('/api/v1/conversations', {
          data: { title, llm_profile: 'fake', locale },
        })
      ).json()
      const conversationRoute = `/conversations/${scene.id}`
      const accepted = await request.post(
        `/api/v1/conversations/${scene.id}/turns`,
        {
          data: {
            operation_id: randomUUID(),
            expected_revision: 0,
            text: retainedMessage,
            action: { kind: 'move', destination: 'Coast' },
          },
        },
      )
      expect(accepted.status()).toBe(202)
      for (const density of ['comfortable', 'compact']) {
        for (const font_size of ['normal', 'large']) {
          const preferences = await (
            await request.get('/api/v1/preferences')
          ).json()
          await request.put('/api/v1/preferences', {
            data: { ...preferences, locale, density, font_size },
          })
          for (const route of [
            '/',
            '/player',
            '/characters',
            '/characters/new',
            '/stories',
            '/stories/new',
            '/new',
            '/images',
            '/settings',
            conversationRoute,
          ]) {
            await page.goto(route)
            await expect(page.locator('main')).toBeVisible()
            await expect(
              route === conversationRoute
                ? page.getByRole('heading', { name: title, exact: true })
                : page.locator('main h1'),
            ).toBeVisible()
            expect(
              await page.evaluate(
                () => document.documentElement.scrollWidth <= window.innerWidth,
              ),
            ).toBe(true)
            if (route === '/characters/new') {
              const name = page.getByLabel(m.requiredName, { exact: true })
              const description = page.getByLabel(m.description, {
                exact: true,
              })
              const trigger = page.getByRole('button', {
                name: m.createDraft,
                exact: true,
              })
              const before = (await name.boundingBox())!
              const beforeDescription = (await description.boundingBox())!
              await trigger.click()
              await expect(
                page.getByRole('dialog', { name: m.createDraft, exact: true }),
              ).toBeVisible()
              const after = (await name.boundingBox())!
              const afterDescription = (await description.boundingBox())!
              expect(after.width).toBeCloseTo(before.width, 0)
              expect(after.height).toBeCloseTo(before.height, 0)
              expect(afterDescription.y - after.y - after.height).toBeCloseTo(
                beforeDescription.y - before.y - before.height,
                0,
              )
              await page.keyboard.press('Escape')
              await expect(trigger).toBeFocused()
            }
            if (route === '/player') {
              const input = await page
                .getByLabel(m.requiredName, { exact: true })
                .boundingBox()
              expect(input!.width).toBeGreaterThan(size.width < 600 ? 250 : 400)
            }
            if (route === conversationRoute) {
              await expect(
                page.getByText(retainedMessage, { exact: true }),
              ).toBeVisible()
              await expect(
                page.getByText(m.newChapter, { exact: true }),
              ).not.toBeVisible()
              await page.getByText(m.addAction, { exact: true }).click()
              const destination = page.getByRole('textbox', {
                name: m.preparationValue,
                exact: true,
              })
              await destination.fill('Review destination')
              const inputBox = await destination.boundingBox()
              const actionButton = page.getByRole('button', {
                name: m.queueAction,
                exact: true,
              })
              const buttonBox = await actionButton.boundingBox()
              expect(buttonBox!.width).toBeCloseTo(inputBox!.width, 0)
              expect(buttonBox!.height).toBeGreaterThanOrEqual(44)
              await evidence.capture(
                `${locale} ${viewport} ${density} ${font_size} action controls`,
              )
              await actionButton.click()
              await expect(
                page.getByRole('textbox', { name: m.messageLabel }),
              ).toBeFocused()
              await expect(
                page.getByRole('button', {
                  name: m.removeQueuedAction,
                  exact: true,
                }),
              ).toBeVisible()
              await expect(
                page.getByRole('button', { name: m.send, exact: true }),
              ).toBeEnabled()
              await page
                .getByRole('textbox', { name: m.messageLabel, exact: true })
                .fill('A new unsent draft')
            }
            await evidence.capture(
              `${locale} ${viewport} ${density} ${font_size} ${route}`,
            )
          }
        }
      }
    })
  }
}

test('unconfirmed services disable generation and sending while text remains editable and saveable', async ({
  page,
  request,
  evidence,
}) => {
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
      data: { title: 'Disconnected', llm_profile: 'fake' },
    })
  ).json()
  await page.goto(`/conversations/${scene.id}`)
  await page
    .getByRole('textbox', { name: ja.messageLabel })
    .fill('Keep this draft')
  await expect(
    page.getByRole('button', { name: ja.send, exact: true }),
  ).toBeDisabled()
  await expect(
    page.getByRole('button', { name: ja.drawScene, exact: true }),
  ).toBeDisabled()
  await evidence.capture('Disconnected conversation retains editable input')
  await page.goto('/images')
  await page.getByRole('button', { name: ja.openDrawingForm }).click()
  await page.getByLabel(ja.imagePrompt).fill('A quiet coast')
  await expect(
    page.getByRole('button', { name: ja.drawRequestedScene, exact: true }),
  ).toBeDisabled()
  await page.goto('/characters/new')
  await page
    .getByLabel(ja.requiredName, { exact: true })
    .fill(`Saved offline ${randomUUID().slice(0, 8)}`)
  await expect(
    page.getByRole('button', { name: ja.save, exact: true }),
  ).toBeEnabled()
  await page.getByRole('button', { name: ja.save, exact: true }).click()
  await expect(page).toHaveURL(/\/characters\/[a-f0-9-]+$/)
  await expect(
    page.getByRole('button', { name: ja.createImage, exact: true }),
  ).toBeDisabled()
  await evidence.capture(
    'Text saved while image generation remains unavailable',
  )
})

test('mobile privacy restores the navigation focus and preserves the message draft', async ({
  page,
  request,
  evidence,
}) => {
  await page.setViewportSize(viewports.mobile)
  const scene = await (
    await request.post('/api/v1/conversations', {
      data: { title: 'Privacy check', llm_profile: 'fake' },
    })
  ).json()
  await page.goto(`/conversations/${scene.id}`)
  const input = page.getByRole('textbox', { name: ja.messageLabel })
  await input.fill('Unsent mobile draft')
  await page.getByRole('button', { name: ja.showNavigation }).click()
  await page.getByRole('button', { name: ja.hideScreen, exact: true }).click()
  await expect(input).not.toBeVisible()
  await page.getByRole('button', { name: ja.showScreen, exact: true }).click()
  await expect(
    page.getByRole('button', { name: ja.hideScreen, exact: true }),
  ).toBeFocused()
  await page.getByRole('link', { name: 'Privacy check', exact: true }).click()
  await expect(input).toHaveValue('Unsent mobile draft')
  await evidence.capture('Mobile privacy restored focus and the unsent message')
})

test('opens original image bytes and leaves saving to the native context menu', async ({
  page,
  request,
  evidence,
}) => {
  const identity = randomUUID()
  const submitted = await request.post('/api/v1/images', {
    data: { request_id: identity, prompt: 'An empty coast.' },
  })
  expect(submitted.status()).toBe(202)
  await request.post(`/api/v1/images/${identity}/collect`)
  await page.goto('/images')
  const image = page.locator('.image-result').filter({
    has: page.locator(`img[src="/api/v1/images/${identity}/files/0"]`),
  })
  await expect(image.locator('img')).toHaveJSProperty('naturalWidth', 8)
  const opened = page.waitForEvent('popup')
  await image.getByRole('link').click()
  const original = await opened
  await expect(original).toHaveURL(
    new RegExp(`/api/v1/images/${identity}/files/0$`),
  )
  await expect(original.locator('img')).toHaveJSProperty('naturalWidth', 8)
  await original.close()
  await image.locator('img').click({ button: 'right' })
  await page.keyboard.press('Escape')
  await expect(image.locator('a[download]')).toHaveCount(0)
  await evidence.capture('Original image link with native context menu support')
})

test('album keeps collapsed files unloaded and reopens the accepted result without collecting twice', async ({
  page,
  evidence,
}) => {
  const readIds: string[] = []
  const collections: string[] = []
  const files: string[] = []
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname
    if (/^\/api\/v1\/images\/[^/]+$/.test(path) && request.method() === 'GET')
      readIds.push(path)
    if (path.endsWith('/collect')) collections.push(path)
    if (path.includes('/files/')) files.push(path)
  })
  await page.goto('/images')
  await expect(page.locator('.album-days img')).toHaveCount(0)
  expect(files).toHaveLength(0)
  await expect(page.getByLabel(ja.imagePrompt)).toHaveCount(0)
  await page.getByRole('button', { name: ja.openDrawingForm }).click()
  await page.getByLabel(ja.imagePrompt).fill('A quiet coast.')
  const close = page.getByRole('button', {
    name: ja.closeDrawingForm,
    exact: true,
  })
  await expect(close).toHaveAttribute('aria-expanded', 'true')
  await close.click()
  const open = page.getByRole('button', {
    name: ja.openDrawingForm,
    exact: true,
  })
  await expect(open).toHaveAttribute('aria-expanded', 'false')
  await expect(page.getByLabel(ja.imagePrompt)).toHaveCount(0)
  await open.click()
  await expect(page.getByLabel(ja.imagePrompt)).toHaveValue('A quiet coast.')
  const accepted = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === '/api/v1/images' &&
      response.request().method() === 'POST',
  )
  await page
    .getByRole('button', { name: ja.drawRequestedScene, exact: true })
    .click()
  const run = await (await accepted).json()
  const result = page.locator(`#image-${run.run_id}`)
  await expect(result.locator('img')).toHaveJSProperty('naturalWidth', 8)
  await expect(page.getByText(ja.albumAdded, { exact: true })).toBeVisible()
  const input = page.getByLabel(ja.imagePrompt)
  await expect(input).not.toBeFocused()
  const day = page.locator('.album-days > details').filter({ has: result })
  await day.locator('summary').click()
  await expect(result).toHaveCount(0)
  const count = collections.length
  await page.getByRole('link', { name: ja.viewLatestImage }).click()
  await expect(result).toBeFocused()
  expect(collections).toHaveLength(count)
  expect(readIds.filter((path) => path.endsWith(run.run_id))).toHaveLength(1)
  await evidence.capture('Album result remains reachable after closing its day')
})

test('separates connection controls and distinguishes external from owned environments', async ({
  page,
  evidence,
}) => {
  await page.setViewportSize(viewports.mobile)
  let environment: 'external' | 'active' | 'unconfirmed' | 'none' = 'external'
  let stops = 0
  const status = () => ({
    gpu:
      environment === 'unconfirmed'
        ? 'unknown'
        : environment === 'active'
          ? 'active'
          : 'absent',
    connection:
      environment === 'unconfirmed'
        ? 'preparing'
        : environment === 'none'
          ? 'stopped'
          : 'connected',
    shutdown_target:
      environment === 'external'
        ? 'external'
        : environment === 'none'
          ? null
          : 'owned',
    collector_running: true,
    observed_at: '2026-11-07T03:00:00Z',
    pending: [],
  })
  await page.route('**/api/v1/runtime', (route) =>
    route.fulfill({ json: status() }),
  )
  await page.route('**/api/v1/runtime/views', (route) =>
    route.fulfill({
      json: (environment === 'external' || environment === 'active'
        ? ['app', 'graph']
        : []
      ).map((view) => ({
        profile: 'wai-illustrious',
        view,
        backend: environment === 'active' ? 'colab' : 'external',
        url:
          environment === 'active'
            ? `http://127.0.0.1:8188/?template=${view}`
            : 'http://127.0.0.1:8188/',
      })),
    }),
  )
  await page.route('**/api/v1/runtime/refresh', (route) =>
    route.fulfill({ json: status() }),
  )
  await page.route('**/api/v1/runtime/stop', (route) => {
    stops++
    environment = 'none'
    return route.fulfill({ json: status() })
  })
  await page.route('**/api/v1/runtime/start', (route) =>
    fulfillFault(route, evidence, {
      status: 503,
      json: { code: 'connection_failed' },
    }),
  )
  await page.goto('/settings')
  await page.getByText(ja.connectionSettings, { exact: true }).click()
  const panel = page.locator('.runtime-panel')
  await expect(panel.getByRole('status')).toHaveText(ja.runtimeConnected)
  const connect = panel.getByRole('button', { name: ja.start, exact: true })
  const disconnect = panel.getByRole('button', {
    name: ja.disconnect,
    exact: true,
  })
  await expect(disconnect).toBeVisible()
  const connectBox = (await connect.boundingBox())!
  const disconnectBox = (await disconnect.boundingBox())!
  expect(
    disconnectBox.y - connectBox.y - connectBox.height,
  ).toBeGreaterThanOrEqual(8)
  await panel.getByText(ja.drawingSettings, { exact: true }).click()
  await expect(panel.getByRole('link', { name: ja.openComfy })).toHaveCount(1)
  await expect(panel.locator('a[download]')).toHaveCount(2)
  const address = panel.getByLabel(ja.externalEndpoint)
  await address.fill('http://127.0.0.1:8189')
  await connect.click()
  await expect(panel.getByRole('alert')).toBeVisible()
  await expect(address).toHaveValue('http://127.0.0.1:8189')
  environment = 'active'
  await panel.getByRole('button', { name: ja.refresh, exact: true }).click()
  const stop = panel.getByRole('button', { name: ja.stop, exact: true })
  await expect(stop).toBeEnabled()
  await expect(disconnect).toHaveCount(0)
  await expect(
    panel.getByRole('link', { name: ja.appMode, exact: true }),
  ).toBeVisible()
  await evidence.capture('Connection controls on a narrow screen')
  environment = 'unconfirmed'
  await panel.getByRole('button', { name: ja.refresh, exact: true }).click()
  await expect(
    panel.getByText(ja.runtimeNoViews, { exact: true }),
  ).toBeVisible()
  await expect(stop).toBeEnabled()
  await expect(disconnect).toHaveCount(0)
  await stop.click()
  await expect(stop).toHaveCount(0)
  expect(stops).toBe(1)
})

for (const locale of ['ja', 'en'] as const) {
  test(`${locale}: keeps failed cover recovery visible inside cards and editor previews`, async ({
    page,
    request,
    evidence,
  }) => {
    const m = locale === 'ja' ? ja : en
    await page.setViewportSize(
      locale === 'ja' ? viewports.desktop : viewports.mobile,
    )
    const preferences = await (await request.get('/api/v1/preferences')).json()
    await request.put('/api/v1/preferences', {
      data: {
        ...preferences,
        locale,
        font_size: locale === 'ja' ? 'normal' : 'large',
      },
    })
    const identity = randomUUID()
    await request.post('/api/v1/images', {
      data: { request_id: identity, prompt: 'An empty coast.' },
    })
    await request.post(`/api/v1/images/${identity}/collect`)
    const character = (
      await (
        await request.post('/api/v1/characters/save', {
          data: {
            operation_id: randomUUID(),
            value: { name: 'Cover recovery' },
          },
        })
      ).json()
    ).preset
    character.image_id = identity
    const story = (
      await (
        await request.post('/api/v1/stories/save', {
          data: {
            operation_id: randomUUID(),
            value: { name: 'Cover recovery' },
          },
        })
      ).json()
    ).preset
    story.image_id = identity
    await page.route('**/api/v1/characters', (route) =>
      route.fulfill({ json: [character] }),
    )
    await page.route('**/api/v1/stories', (route) =>
      route.fulfill({ json: [story] }),
    )
    const conversation = await (
      await request.post('/api/v1/conversations', {
        data: { title: 'Cover recovery', llm_profile: 'fake', locale },
      })
    ).json()
    await page.route(
      `**/api/v1/conversations/${conversation.id}`,
      async (route) => {
        const response = await route.fetch()
        const value = await response.json()
        await route.fulfill({
          json: {
            ...value,
            definition: { ...value.definition, presets: { story } },
          },
        })
      },
    )
    let broken = true
    let files = 0
    await page.route(`**/api/v1/images/${identity}/files/0`, (route) => {
      files++
      return broken
        ? fulfillFault(route, evidence, { status: 503, body: '' })
        : route.continue()
    })
    for (const [path, selector] of [
      ['/characters', '.preset-cover'],
      [`/characters/${character.id}`, '.cover-preview'],
      ['/stories', '.preset-cover'],
      [`/stories/${story.id}`, '.cover-preview'],
      [`/conversations/${conversation.id}`, '.conversation-banner'],
    ]) {
      broken = true
      await page.goto(path)
      const cover = page.locator(selector)
      const retry = cover.getByRole('button', { name: m.retryCollection })
      await expect(retry).toBeAttached()
      await evidence.capture(`Image display failure remains within ${selector}`)
      const bounds = await cover.boundingBox()
      const control = await retry.boundingBox()
      expect(control!.y).toBeGreaterThanOrEqual(bounds!.y)
      expect(control!.y + control!.height).toBeLessThanOrEqual(
        bounds!.y + bounds!.height,
      )
      broken = false
      const before = files
      await retry.click()
      await expect(cover.locator('img')).toHaveJSProperty('naturalWidth', 8)
      expect(files).toBeGreaterThan(before)
      await expect(retry).toHaveCount(0)
    }
  })
}
