import { randomUUID } from 'node:crypto'
import { test, expect } from './fixtures'
import ja from '../../../src/frontend/src/app/locales/ja.json' with { type: 'json' }
import en from '../../../src/frontend/src/app/locales/en.json' with { type: 'json' }

for (const [locale, m] of [
  ['ja', ja],
  ['en', en],
] as const) {
  test(`${locale}: situation open and closed intent survives width changes`, async ({
    page,
    request,
    evidence,
  }) => {
    const preferences = await (await request.get('/api/v1/preferences')).json()
    await request.put('/api/v1/preferences', {
      data: { ...preferences, locale },
    })
    const scene = await (
      await request.post('/api/v1/conversations', {
        data: { title: 'Situation reflow', llm_profile: 'fake' },
      })
    ).json()
    await page.goto(`/conversations/${scene.id}`)
    const input = page.getByRole('textbox', { name: m.messageLabel })
    await input.fill('A retained message')
    await page.getByRole('button', { name: m.showState, exact: true }).click()
    await expect(page.locator('.state-panel')).toBeVisible()
    await page.setViewportSize({ width: 390, height: 844 })
    const dialog = page.getByRole('dialog', {
      name: m.currentState,
      exact: true,
    })
    await expect(dialog).toBeVisible()
    await evidence.capture(`${locale} situation remains open after narrowing`)
    await page.setViewportSize({ width: 1440, height: 960 })
    await expect(dialog).not.toBeVisible()
    await expect(page.locator('.state-panel')).toBeVisible()
    await page.setViewportSize({ width: 390, height: 844 })
    await expect(dialog).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(dialog).not.toBeVisible()
    await page.setViewportSize({ width: 1440, height: 960 })
    await expect(page.locator('.state-panel')).not.toBeVisible()
    await expect(input).toHaveValue('A retained message')
    await page.setViewportSize({ width: 390, height: 844 })
    const trigger = page.getByRole('button', { name: m.showState, exact: true })
    await trigger.focus()
    await page.keyboard.press('Enter')
    await expect(dialog).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(trigger).toBeFocused()
  })
}

test('action popover retains its draft and returns focus without a separate send', async ({
  page,
  request,
}) => {
  const scene = await (
    await request.post('/api/v1/conversations', {
      data: { title: 'Action popup', llm_profile: 'lmstudio-4b' },
    })
  ).json()
  await page.goto(`/conversations/${scene.id}`)
  const message = page.getByRole('textbox', { name: ja.messageLabel })
  await message.fill('Come with me?')
  await page.getByRole('button', { name: ja.addAction, exact: true }).click()
  await page.getByLabel(ja.destination, { exact: true }).fill('Garden')
  await page.keyboard.press('Escape')
  await expect(message).toBeFocused()
  await page.getByRole('button', { name: ja.addAction, exact: true }).click()
  await expect(page.getByLabel(ja.destination, { exact: true })).toHaveValue(
    'Garden',
  )
  await page.getByRole('button', { name: ja.queueAction, exact: true }).click()
  await expect(
    page.getByRole('dialog', { name: ja.addAction, exact: true }),
  ).not.toBeVisible()
  await expect(message).toBeFocused()
  await expect(message).toHaveValue('Come with me?')
  const before = await (
    await request.get(`/api/v1/conversations/${scene.id}`)
  ).json()
  expect(before.revision).toBe(0)
  await page.getByRole('button', { name: ja.send, exact: true }).click()
  await expect(page.locator('.assistant-message')).toHaveCount(1)
  const after = await (
    await request.get(`/api/v1/conversations/${scene.id}`)
  ).json()
  expect(
    await (
      await request.get(`/api/v1/conversations/${scene.id}/actions`)
    ).json(),
  ).toHaveLength(1)
  expect(after.state.place).toBe('Garden')
})

test('privacy covers portals and preserves focus, draft and history position', async ({
  page,
  request,
  evidence,
  auditAccessibility,
}) => {
  const scene = await (
    await request.post('/api/v1/conversations', {
      data: { title: 'Opaque privacy', llm_profile: 'lmstudio-4b' },
    })
  ).json()
  await page.goto(`/conversations/${scene.id}`)
  const input = page.getByRole('textbox', { name: ja.messageLabel })
  await input.fill('An unsent draft')
  await page.getByRole('button', { name: ja.hideScreen, exact: true }).hover()
  await auditAccessibility()
  await page
    .getByRole('button', { name: ja.drawingSettings, exact: true })
    .click()
  await expect(
    page.getByRole('dialog', { name: ja.drawingSettings, exact: true }),
  ).toBeVisible()
  await page.getByRole('button', { name: ja.hideScreen, exact: true }).click()
  const cover = page.getByRole('dialog', { name: ja.hideScreen, exact: true })
  await expect(cover).toBeVisible()
  await expect(cover).toHaveCSS('background-color', 'rgb(255, 246, 247)')
  const bounds = await cover.boundingBox()
  expect(bounds?.width).toBeGreaterThanOrEqual(1440)
  expect(bounds?.height).toBeGreaterThanOrEqual(960)
  await page.keyboard.press('Escape')
  await expect(cover).toBeVisible()
  await evidence.capture(
    'Opaque privacy covers the application and popup portals',
  )
  await page.getByRole('button', { name: ja.showScreen, exact: true }).click()
  await expect(
    page.getByRole('button', { name: ja.hideScreen, exact: true }),
  ).toBeFocused()
  await expect(input).toHaveValue('An unsent draft')
})

test('reply arrival does not pull a reader away from earlier conversation', async ({
  page,
  request,
}) => {
  const scene = await (
    await request.post('/api/v1/conversations', {
      data: { title: 'Reading position', llm_profile: 'lmstudio-4b' },
    })
  ).json()
  await page.goto(`/conversations/${scene.id}`)
  const input = page.getByRole('textbox', { name: ja.messageLabel })
  const history = page.getByRole('log', { name: ja.conversationHistory })
  await expect(
    page.getByRole('link', { name: ja.play, exact: true }),
  ).toHaveAttribute('aria-current', 'location')
  await input.fill(
    Array.from({ length: 80 }, (_, i) => `Retained line ${i}`).join('\n'),
  )
  await page.getByRole('button', { name: ja.send, exact: true }).click()
  await expect(page.locator('.assistant-message')).toHaveCount(1)
  const readingHeight = (await history.boundingBox())!.height
  let release = () => {}
  const held = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route(`**/conversations/${scene.id}/turns`, async (route) => {
    if (route.request().method() !== 'POST') return route.continue()
    const response = await route.fetch()
    await held
    await route.fulfill({ response })
  })
  await input.fill('Another reply, please.')
  await page.getByRole('button', { name: ja.send, exact: true }).click()
  await history.hover()
  await page.mouse.wheel(0, -6000)
  await expect(page.getByRole('button', { name: ja.latestReply })).toBeVisible()
  expect((await history.boundingBox())!.height).toBeCloseTo(readingHeight, 0)
  release()
  await expect(page.locator('.assistant-message')).toHaveCount(2)
  expect(await history.evaluate((element) => element.scrollTop)).toBeLessThan(
    10,
  )
  await page.getByRole('button', { name: ja.latestReply }).click()
  await expect
    .poll(() =>
      history.evaluate(
        (element) =>
          element.scrollHeight - element.clientHeight - element.scrollTop,
      ),
    )
    .toBeLessThan(5)
})

test('album preserves portrait and landscape proportions', async ({
  page,
  request,
}) => {
  const examples = [
    [40, 80],
    [80, 40],
  ]
  for (const [width, height] of examples) {
    const id = randomUUID()
    await request.post('/api/v1/images', {
      data: { request_id: id, prompt: 'An empty coast.' },
    })
    await request.post(`/api/v1/images/${id}/collect`)
    await page.route(`**/images/${id}/files/0`, (route) =>
      route.fulfill({
        contentType: 'image/svg+xml',
        body: `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}"><rect width="100%" height="100%" fill="#642238"/></svg>`,
      }),
    )
    await page.goto('/images')
    await page.locator('.album-days summary').first().click()
    const image = page.locator(`img[src="/api/v1/images/${id}/files/0"]`)
    await expect(image).toHaveJSProperty('naturalWidth', width)
    const bounds = await image.boundingBox()
    expect(bounds!.width / bounds!.height).toBeCloseTo(width / height, 1)
  }
})

test('unsaved notes survive popup closure and privacy survives responsive navigation changes', async ({
  page,
  request,
  auditAccessibility,
}) => {
  const scene = await (
    await request.post('/api/v1/conversations', {
      data: { title: 'Persistent visibility', llm_profile: 'fake' },
    })
  ).json()
  await page.goto(`/conversations/${scene.id}`)
  await page
    .getByRole('textbox', { name: ja.messageLabel })
    .fill('Retain the message draft.')
  await page
    .getByRole('button', { name: ja.conversationMenu, exact: true })
    .click()
  await page.getByText(ja.pinnedNotes, { exact: true }).click()
  const notes = page.getByLabel(ja.notesDescription)
  await notes.fill('Retain the unsaved notes.')
  await page.keyboard.press('Escape')
  await page
    .getByRole('button', { name: ja.conversationMenu, exact: true })
    .click()
  await expect(notes).toHaveValue('Retain the unsaved notes.')
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: ja.hideScreen, exact: true }).click()
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.locator('.workspace > .navigation')).toHaveCount(0)
  const cover = page.getByRole('dialog', { name: ja.hideScreen, exact: true })
  await expect(cover).toBeVisible()
  await page.setViewportSize({ width: 1440, height: 960 })
  await expect(page.locator('.workspace > .navigation')).toHaveCount(1)
  await page.keyboard.press('Escape')
  await expect(cover).toBeVisible()
  await page.getByRole('button', { name: ja.showScreen, exact: true }).click()
  await expect(
    page.getByRole('button', { name: ja.hideScreen, exact: true }),
  ).toBeFocused()
  await expect(
    page.getByRole('textbox', { name: ja.messageLabel }),
  ).toHaveValue('Retain the message draft.')
  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('button', { name: ja.showNavigation }).click()
  await page.getByRole('button', { name: ja.closePanel, exact: true }).hover()
  await auditAccessibility()
})
