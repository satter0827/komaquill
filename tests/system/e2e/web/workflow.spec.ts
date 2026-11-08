import { test, expect, fulfillFault } from './fixtures'
import { randomUUID } from 'node:crypto'
import ja from '../../../../src/frontend/src/app/locales/ja.json' with { type: 'json' }
import en from '../../../../src/frontend/src/app/locales/en.json' with { type: 'json' }

test('saves the single player and personal attributes', async ({
  page,
  evidence,
}) => {
  await page.goto('/player')
  await page.getByText(ja.basicDetails, { exact: true }).click()
  await page.getByLabel(ja.age, { exact: true }).fill('28')
  await page.getByLabel(ja.gender, { exact: true }).fill('女性')
  await page.getByRole('button', { name: ja.save, exact: true }).click()
  await expect(
    page.locator('.editor-page form').getByRole('status'),
  ).toHaveText(ja.saved)
  await page.reload()
  await page.getByText(ja.basicDetails, { exact: true }).click()
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
  await page.getByText(ja.basicDetails, { exact: true }).click()
  await page.getByLabel(ja.requiredName, { exact: true }).fill(name)
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
  await page.getByLabel(ja.requiredName, { exact: true }).fill(name + ' edited')
  await page.getByRole('button', { name: ja.save, exact: true }).click()
  await expect(
    page.locator('.editor-page form').getByRole('status'),
  ).toHaveText(ja.saved)
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
  await expect(draw).toHaveText(ja.drawScene)
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
  request,
}) => {
  const name = 'Story-' + randomUUID().slice(0, 8)
  await page.goto('/stories/new')
  await page.getByText(ja.basicDetails, { exact: true }).click()
  await page.getByLabel(ja.requiredName, { exact: true }).fill(name)
  await page
    .getByLabel(ja.opening, { exact: true })
    .fill('A visitor waits at the clocktower.')
  await page.getByLabel(ja.tone, { exact: true }).fill('Gentle and concise')
  await page.getByLabel(ja.topics, { exact: true }).fill('Clock repair')
  await page.getByLabel(ja.avoid, { exact: true }).fill('Violence')
  await page.getByLabel(ja.defaultCharacter).selectOption({ label: '栞' })
  await page.getByRole('button', { name: ja.save, exact: true }).click()
  await page.getByRole('button', { name: ja.createImage, exact: true }).click()
  await expect(page.locator('.cover-preview.wide img')).toHaveJSProperty(
    'naturalWidth',
    8,
  )
  const storyId = page.url().split('/').at(-1)
  await page.goto('/new?story=' + storyId)
  await page.locator('.start-selection > summary').click()
  await expect(
    page.getByLabel(ja.chooseCharacter).locator('option:checked'),
  ).toHaveText('栞')
  await page.reload()
  await page.locator('.start-selection > summary').click()
  await expect(
    page.getByText('A visitor waits at the clocktower.', { exact: true }),
  ).toBeVisible()
  await expect(page.getByLabel(ja.tone, { exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: ja.create, exact: true }).click()
  await expect(page.locator('.conversation-banner img')).toHaveJSProperty(
    'naturalWidth',
    8,
  )
  const identity = page.url().split('/').at(-1)
  const started = await (
    await request.get('/api/v1/conversations/' + identity)
  ).json()
  expect(started.definition).toMatchObject({
    tone: 'Gentle and concise',
    topics: 'Clock repair',
    avoid: 'Violence',
  })
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

test('separates the current situation from the starting settings', async ({
  page,
  request,
}) => {
  const created = await request.post('/api/v1/conversations', {
    data: {
      title: 'The lighthouse',
      scenario: {
        opening: 'The door swings open.',
        world: 'A coastal town without electricity.',
        place: 'The lighthouse',
      },
    },
  })
  expect(created.ok()).toBe(true)
  await page.goto('/conversations/' + (await created.json()).id)
  await page.getByRole('button', { name: ja.showState, exact: true }).click()
  const situation = page.locator('.state-panel')
  await expect(situation).toBeVisible()
  await expect(situation).not.toContainText(/版 \d|未指定|これまでの行動/)
  await page.keyboard.press('Escape')
  await page
    .getByRole('button', { name: ja.conversationMenu, exact: true })
    .click()
  await page
    .getByRole('button', { name: ja.startedSettings, exact: true })
    .click()
  const settings = page.getByRole('dialog', { name: ja.startedSettings })
  await expect(settings).toContainText('The door swings open.')
  await expect(settings).toContainText('A coastal town without electricity.')
})

test('renames and restores a conversation from the trash', async ({ page }) => {
  await page.goto('/new')
  await page.getByRole('button', { name: ja.create, exact: true }).click()
  await expect(page).toHaveURL(/\/conversations\//)
  const conversationUrl = page.url()
  const menu = page.getByRole('button', {
    name: ja.conversationMenu,
    exact: true,
  })
  const openManagement = async () => {
    if ((await menu.getAttribute('aria-expanded')) !== 'true')
      await menu.click()
    await page.getByRole('button', { name: ja.manageWork, exact: true }).click()
    return page.getByRole('dialog', { name: ja.manageWork })
  }
  const rename = await openManagement()
  await rename.getByRole('textbox', { name: ja.name }).fill('雨のアトリエ')
  await rename.getByRole('button', { name: ja.renameWork }).click()
  await expect(page.locator('.conversation-header')).toContainText(
    '雨のアトリエ',
  )
  const trash = await openManagement()
  await trash.getByRole('button', { name: ja.moveToTrash }).click()
  await page.waitForURL((url) => url.pathname === '/')
  await page.goto('/trash')
  const work = page.getByRole('listitem').filter({ hasText: '雨のアトリエ' })
  await work.getByRole('button', { name: ja.restoreWork }).click()
  await expect(work).toHaveCount(0)
  await page.goto(conversationUrl)
  await expect(page.locator('.conversation-header')).toContainText(
    '雨のアトリエ',
  )
})

test('generates and automatically collects a standalone image', async ({
  page,
}) => {
  await page.goto('/images')
  await page.getByRole('button', { name: ja.openDrawingForm }).click()
  await page
    .getByLabel(ja.imagePrompt, { exact: true })
    .fill('a quiet green garden')
  const receipt = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === '/api/v1/images' &&
      response.request().method() === 'POST',
  )
  await page
    .getByRole('button', { name: ja.drawRequestedScene, exact: true })
    .click()
  const created = await (await receipt).json()
  await expect(
    page.locator(`img[src="/api/v1/images/${created.run_id}/files/0"]`),
  ).toHaveJSProperty('naturalWidth', 8)
})

test('saves and applies both common prompts in the next album drawing', async ({
  page,
  request,
}) => {
  await page.goto('/settings')
  const positive = page.getByRole('textbox', { name: ja.commonPositivePrompt })
  const negative = page.getByRole('textbox', { name: ja.commonNegativePrompt })
  await positive.fill('soft afternoon light')
  await negative.fill('lettering')
  await negative.press('Tab')
  await expect
    .poll(async () => {
      const saved = await (await request.get('/api/v1/preferences')).json()
      return saved.image_prompts
    })
    .toEqual({ positive: 'soft afternoon light', negative: 'lettering' })
  await page.goto('/images')
  await page.getByRole('button', { name: ja.openDrawingForm }).click()
  await page
    .getByRole('textbox', { name: ja.imagePrompt })
    .fill('A quiet bookshop')
  const accepted = page.waitForResponse(
    (response) =>
      response.url().endsWith('/api/v1/images') &&
      response.request().method() === 'POST',
  )
  await page.getByRole('button', { name: ja.drawRequestedScene }).click()
  const response = await accepted
  expect(response.ok()).toBe(true)
  const image = await response.json()
  expect(image.compiled_input.body).toContain('soft afternoon light')
  expect(image.compiled_input.negative).toContain('lettering')
})

test('shows a library error and recovers saved content', async ({
  page,
  evidence,
}) => {
  await page.route('**/api/v1/stories', (route) =>
    fulfillFault(route, evidence, {
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
  await page.locator('.start-selection > summary').click()
  await page.getByRole('link', { name: ja.edit, exact: true }).click()
  await expect(page.getByLabel(ja.requiredName, { exact: true })).toHaveValue(
    player.name,
  )
  await page.getByText(ja.basicDetails, { exact: true }).click()
  await page.getByLabel(ja.age, { exact: true }).fill('37')
  await page.getByRole('button', { name: ja.save, exact: true }).click()
  await expect
    .poll(() => new URL(page.url()).pathname + new URL(page.url()).search)
    .toBe(selection)
  await page.getByRole('link', { name: ja.players, exact: true }).click()
  await page.getByText(ja.basicDetails, { exact: true }).click()
  await expect(page.getByLabel(ja.age, { exact: true })).toHaveValue('37')
  await page
    .getByRole('link', { name: ja.newConversation, exact: true })
    .click()
  await expect(page.locator('.selection-summary')).toContainText(player.name)
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

test('reviews a draft separately and edits a saved character', async ({
  page,
  request,
  evidence,
  auditAccessibility,
}) => {
  const preferences = await (await request.get('/api/v1/preferences')).json()
  await request.put('/api/v1/preferences', {
    data: { ...preferences, locale: 'en' },
  })
  const name = 'Saved watchmaker'
  const description = 'An adult who repairs clocks.'
  const created = await (
    await request.post('/api/v1/characters/save', {
      data: {
        operation_id: randomUUID(),
        value: { name, description, initial_clothing: 'Blue coat' },
      },
    })
  ).json()
  await page.goto('/characters')
  const edit = page.getByRole('link', {
    name: en.editPresetLabel.replace('{name}', name),
    exact: true,
  })
  await expect(edit).toHaveText(en.edit)
  await edit.click()
  const trigger = page.getByRole('button', {
    name: en.createDraft,
    exact: true,
  })
  const editorName = page.getByLabel(en.requiredName, { exact: true })
  await expect(editorName).toHaveValue(name)
  await trigger.click()
  const dialog = page.getByRole('dialog', { name: en.createDraft, exact: true })
  const idea = dialog.getByRole('textbox', {
    name: en.draftPrompt,
    exact: true,
  })
  await idea.fill('An adult watchmaker.')
  await page.keyboard.press('Escape')
  await expect(trigger).toBeFocused()
  await trigger.click()
  await expect(idea).toHaveValue('An adult watchmaker.')
  await dialog
    .getByRole('button', { name: en.generateDraft, exact: true })
    .click()
  await expect(
    dialog.getByText('Fixture character', { exact: true }),
  ).toBeVisible()
  await expect(editorName).toHaveValue(name)
  await page.setViewportSize({ width: 390, height: 844 })
  await auditAccessibility()
  await evidence.capture('Draft review in the narrow editor')
  await dialog.getByRole('button', { name: en.useDraft, exact: true }).click()
  await expect(dialog).toBeHidden()
  await expect(editorName).toHaveValue('Fixture character')
  const personality = page.getByRole('textbox', {
    name: en.personality,
    exact: true,
  })
  await expect(
    page.getByRole('textbox', { name: en.description, exact: true }),
  ).toHaveValue(description)
  await expect(personality).toBeVisible()
  await expect(personality).toHaveValue('Calm')
  await expect(
    page.getByRole('textbox', { name: en.initial_clothing, exact: true }),
  ).toHaveValue('Blue coat')
  const stored = await (await request.get('/api/v1/characters')).json()
  expect(
    stored.find((item: { id: string }) => item.id === created.preset.id),
  ).toMatchObject({ name, revision: 0 })
  await page.getByRole('button', { name: en.save, exact: true }).click()
  await expect(
    page.locator('.editor-page form').getByRole('status'),
  ).toHaveText(en.saved)
  await page.reload()
  await expect(editorName).toHaveValue('Fixture character')
  await page.getByText(en.basicDetails, { exact: true }).click()
  await expect(personality).toHaveValue('Calm')
  await evidence.capture('Saved settings after applying and editing a draft')
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
  await page.getByText(ja.basicDetails, { exact: true }).click()
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
  await expect(page.locator('.editor-preview .image-result')).toHaveCount(0)
  await expect(
    page.locator('.editor-preview').getByText(ja.job_complete, { exact: true }),
  ).toHaveCount(0)
  await page.reload()
  await page.getByText(ja.basicDetails, { exact: true }).click()
  await expect(page.getByLabel(ja.age, { exact: true })).toHaveValue('60')
  await expect(page.locator('.editor-preview img')).toHaveCount(1)
  await expect(page.locator('.editor-preview .image-result')).toHaveCount(0)
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
