import { test, expect } from './fixtures'
import { randomUUID } from 'node:crypto'
import ja from '../../../src/frontend/src/app/locales/ja.json' with { type: 'json' }
import en from '../../../src/frontend/src/app/locales/en.json' with { type: 'json' }

test('saves the single player and personal attributes', async ({
  page,
  evidence,
}) => {
  await page.goto('/player')
  await page.getByLabel(ja.age, { exact: true }).fill('28')
  await page.getByLabel(ja.gender, { exact: true }).fill('女性')
  await page.getByRole('button', { name: ja.save, exact: true }).click()
  await expect(page.getByRole('status').first()).toHaveText(ja.saved)
  await page.reload()
  await expect(page.getByLabel(ja.age, { exact: true })).toHaveValue('28')
  await expect(page.getByRole('button', { name: ja.duplicate })).toHaveCount(0)
  await expect(
    page.getByRole('button', { name: ja.delete, exact: true }),
  ).toHaveCount(0)
  await evidence.capture('One saved player with age and gender')
})

test('automatically collects a portrait and preserves it after duplication', async ({
  page,
  evidence,
}) => {
  const name = 'Actor-' + randomUUID().slice(0, 8)
  await page.goto('/characters/new')
  await page.getByLabel(ja.name, { exact: true }).fill(name)
  await page
    .getByLabel(ja.appearance, { exact: true })
    .fill('An adult with short red hair.')
  await page
    .getByLabel(ja.initial_clothing, { exact: true })
    .fill('A green coat')
  await page.getByRole('button', { name: ja.save, exact: true }).click()
  await page.getByRole('button', { name: ja.createImage, exact: true }).click()
  await expect(page.locator('.cover-preview img')).toHaveJSProperty(
    'naturalWidth',
    8,
  )
  await evidence.capture('Portrait collected without a manual action')
  await page.reload()
  await expect(page.locator('.cover-preview img')).toHaveJSProperty(
    'naturalWidth',
    8,
  )
  await page.getByLabel(ja.name, { exact: true }).fill(name + ' edited')
  await page.getByRole('button', { name: ja.save, exact: true }).click()
  await expect(page.getByRole('status').first()).toHaveText(ja.saved)
  await page.getByRole('button', { name: ja.duplicate }).click()
  await expect(
    page.locator('.preset-card h2').filter({ hasText: name }),
  ).toHaveCount(2)
})

test('continues dialogue with narration and draws only on request', async ({
  page,
  request,
  evidence,
}) => {
  await page.goto('/new')
  await page.getByRole('button', { name: ja.create, exact: true }).click()
  await expect(page).toHaveURL(/\/conversations\//)
  const id = page.url().split('/').at(-1)!
  const input = page.getByRole('textbox', { name: ja.messageLabel })
  for (const [index, message] of [
    'Hello from the browser fixture.',
    'Tell me about this place.',
    'Please continue.',
  ].entries()) {
    await input.fill(message)
    await page.getByRole('button', { name: ja.send, exact: true }).click()
    await expect(page.locator('.assistant-message')).toHaveCount(index + 1)
  }
  await expect(page.locator('.scene-narration').first()).toContainText('雨音')
  expect(
    await (await request.get(`/api/v1/conversations/${id}/images`)).json(),
  ).toHaveLength(0)
  await evidence.capture('Three replies and narration without automatic images')
  const draw = page.getByRole('button', { name: ja.drawScene, exact: true })
  await expect(draw.locator('svg')).toHaveCount(1)
  await expect(draw).toHaveText('')
  await input.fill('Unsent text remains here')
  await draw.click()
  await expect(input).toHaveValue('Unsent text remains here')
  await expect(
    page.getByRole('img', { name: ja.imageAlt.replace('{index}', '1') }).last(),
  ).toHaveJSProperty('naturalWidth', 8)
  await input.fill('Continue after drawing this scene.')
  await page.getByRole('button', { name: ja.send, exact: true }).click()
  await expect(page.locator('.assistant-message')).toHaveCount(4)
  await expect(
    page.locator('article.turn').nth(2).locator('.image-result img'),
  ).toHaveCount(1)
  await expect(
    page.locator('article.turn').nth(3).locator('.image-result img'),
  ).toHaveCount(0)
  await evidence.capture('The explicitly requested scene is displayed')
  await page.reload()
  await expect(page.locator('.assistant-message')).toHaveCount(4)
  await expect(page.locator('.scene-narration').first()).toContainText('雨音')
  await expect(
    page.getByRole('img', { name: ja.imageAlt.replace('{index}', '1') }).last(),
  ).toHaveJSProperty('naturalWidth', 8)
})

test('saves a banner and starts with the recommended character', async ({
  page,
}) => {
  const name = 'Story-' + randomUUID().slice(0, 8)
  await page.goto('/stories/new')
  await page.getByLabel(ja.name, { exact: true }).fill(name)
  await page
    .getByLabel(ja.opening, { exact: true })
    .fill('A visitor waits at the clocktower.')
  await page.getByLabel(ja.defaultCharacter).selectOption({ label: '栞' })
  await page.getByRole('button', { name: ja.save, exact: true }).click()
  await page.getByRole('button', { name: ja.createImage, exact: true }).click()
  await expect(page.locator('.cover-preview.wide img')).toHaveJSProperty(
    'naturalWidth',
    8,
  )
  const storyId = page.url().split('/').at(-1)
  await page.goto('/new?story=' + storyId)
  await expect(
    page.getByLabel(ja.chooseCharacter).locator('option:checked'),
  ).toHaveText('栞')
  await page.reload()
  await expect(
    page.getByText('A visitor waits at the clocktower.', { exact: true }),
  ).toBeVisible()
  await page.getByRole('button', { name: ja.create, exact: true }).click()
  await expect(page.locator('.conversation-banner img')).toHaveJSProperty(
    'naturalWidth',
    8,
  )
})

test('uses English for dialogue and narration after changing language', async ({
  page,
  evidence,
}) => {
  await page.goto('/new')
  await page.getByRole('button', { name: ja.create, exact: true }).click()
  await expect(page).toHaveURL(/\/conversations\//)
  const conversation = page.url()
  await page.goto('/settings')
  await page
    .getByRole('combobox', { name: ja.language, exact: true })
    .selectOption('en')
  await expect(page.locator('html')).toHaveAttribute('lang', 'en')
  await page.goto(conversation)
  await page.getByRole('textbox', { name: en.messageLabel }).fill('こんにちは')
  await page.getByRole('button', { name: en.send, exact: true }).click()
  await expect(page.locator('.scene-narration').first()).toContainText('Rain')
  await evidence.capture('English dialogue and narration')
})

test('closes narrow scene details with Escape and restores focus', async ({
  page,
}) => {
  await page.goto('/new')
  await page.getByRole('button', { name: ja.create, exact: true }).click()
  await expect(page).toHaveURL(/\/conversations\//)
  await page.setViewportSize({ width: 390, height: 844 })
  const open = page.getByRole('button', { name: ja.showState, exact: true })
  await open.click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).not.toBeVisible()
  await expect(open).toBeFocused()
  await expect(
    page.getByRole('textbox', { name: ja.messageLabel }),
  ).toBeInViewport()
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true)
})

test('generates and automatically collects a standalone image', async ({
  page,
}) => {
  await page.goto('/images')
  await page
    .getByLabel(ja.imagePrompt, { exact: true })
    .fill('a quiet green garden')
  const receipt = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === '/api/v1/images' &&
      response.request().method() === 'POST',
  )
  await page.getByRole('button', { name: ja.generate, exact: true }).click()
  const created = await (await receipt).json()
  await expect(
    page.locator(`img[src="/api/v1/images/${created.run_id}/files/0"]`),
  ).toHaveJSProperty('naturalWidth', 8)
})

test('shows a library error and recovers saved content', async ({
  page,
  evidence,
}) => {
  await page.route('**/api/v1/stories', (route) =>
    route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ code: 'storage_failed' }),
    }),
  )
  await page.goto('/')
  await expect(page.getByRole('alert')).toContainText(ja.errors.storage_failed)
  await evidence.capture('A failed load has an explicit recovery action')
  await page.unroute('**/api/v1/stories')
  await page.getByRole('button', { name: ja.refresh }).click()
  await expect(page.getByRole('alert')).toHaveCount(0)
})

test('retains one profile across start, edit, return and start navigation', async ({
  page,
  request,
  evidence,
}) => {
  const player = await (await request.get('/api/v1/player')).json()
  const stories = await (await request.get('/api/v1/stories')).json()
  const characters = await (await request.get('/api/v1/characters')).json()
  const selection = `/new?story=${stories[0].id}&character=${characters[0].id}`
  await page.goto(selection)
  await page.getByRole('link', { name: ja.edit, exact: true }).click()
  await expect(page.getByLabel(ja.name, { exact: true })).toHaveValue(
    player.name,
  )
  await page.getByLabel(ja.age, { exact: true }).fill('37')
  await page.getByRole('button', { name: ja.save, exact: true }).click()
  await expect
    .poll(() => new URL(page.url()).pathname + new URL(page.url()).search)
    .toBe(selection)
  await page.getByRole('link', { name: ja.players, exact: true }).click()
  await expect(page.getByLabel(ja.age, { exact: true })).toHaveValue('37')
  await page
    .getByRole('link', { name: ja.newConversation, exact: true })
    .click()
  await expect(page.locator('.selection-card').first()).toContainText(
    player.name,
  )
  await page.getByRole('button', { name: ja.create, exact: true }).click()
  await expect(page).toHaveURL(/\/conversations\//)
  const identity = new URL(page.url()).pathname.split('/').at(-1)
  const conversation = await (
    await request.get(`/api/v1/conversations/${identity}`)
  ).json()
  expect(conversation.definition.user.age).toBe('37')
  await evidence.capture(
    'One edited profile survives both directions of client navigation',
  )
})

test('regenerates the current age and gender and displays one preview', async ({
  page,
  request,
  evidence,
}) => {
  const created = await (
    await request.post('/api/v1/characters/save', {
      data: {
        operation_id: randomUUID(),
        value: {
          name: 'Older companion',
          age: '32',
          gender: 'woman',
          appearance: 'Short silver hair and a black apron.',
        },
      },
    })
  ).json()
  await page.goto(`/characters/${created.preset.id}`)
  await page.getByLabel(ja.age, { exact: true }).fill('60')
  await page.getByLabel(ja.gender, { exact: true }).fill('man')
  await expect(
    page.getByRole('button', { name: ja.createImage, exact: true }),
  ).toBeDisabled()
  const received = page.waitForRequest(
    (req) =>
      new URL(req.url()).pathname === '/api/v1/characters/save' &&
      req.method() === 'POST',
  )
  await page.getByRole('button', { name: ja.save, exact: true }).click()
  expect((await received).postDataJSON()).toMatchObject({
    value: { age: '60', gender: 'man' },
  })
  const generated = page.waitForRequest(
    (req) =>
      new URL(req.url()).pathname === '/api/v1/creation-jobs' &&
      req.method() === 'POST',
  )
  await page.getByRole('button', { name: ja.createImage, exact: true }).click()
  expect((await generated).postDataJSON()).toMatchObject({
    kind: 'portrait',
    target_id: created.preset.id,
    expected_revision: 1,
  })
  await expect(page.locator('.cover-preview img')).toHaveJSProperty(
    'naturalWidth',
    8,
  )
  await expect(page.locator('.editor-preview img')).toHaveCount(1)
  await expect(
    page.locator('.editor-preview').getByText(ja.imageReady, { exact: true }),
  ).toHaveCount(1)
  await expect(
    page.locator('.editor-preview').getByText(ja.job_complete, { exact: true }),
  ).toHaveCount(0)
  await page.reload()
  await expect(page.getByLabel(ja.age, { exact: true })).toHaveValue('60')
  await expect(page.locator('.editor-preview img')).toHaveCount(1)
  await expect(
    page.locator('.editor-preview').getByText(ja.imageReady, { exact: true }),
  ).toHaveCount(1)
  await expect(
    page.locator('.editor-preview').getByText(ja.job_complete, { exact: true }),
  ).toHaveCount(0)
  await evidence.capture(
    'Saved edited attributes and a single completed preview',
  )
})

test('offers a new conversation for a retained fixed sample instead of sending a drawing request', async ({
  page,
  request,
  evidence,
}) => {
  const saved = await (
    await request.post('/api/v1/conversations', {
      data: { title: 'Fixed sample', locale: 'ja', llm_profile: 'fake' },
    })
  ).json()
  await page.goto(`/conversations/${saved.id}`)
  await expect(page.getByRole('button', { name: ja.drawScene })).toBeDisabled()
  await expect(page.getByText(ja.sampleDrawingUnavailable)).toBeVisible()
  await evidence.capture(
    'Fixed sample drawing is unavailable with a new conversation link',
  )
})
