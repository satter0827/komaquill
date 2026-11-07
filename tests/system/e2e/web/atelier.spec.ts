import { randomUUID } from 'node:crypto'
import { test, expect, fulfillFault } from './fixtures'
import ja from '../../../../src/frontend/src/app/locales/ja.json' with { type: 'json' }
import en from '../../../../src/frontend/src/app/locales/en.json' with { type: 'json' }

test('recovers the starting choices without treating a failed list as empty', async ({
  page,
  evidence,
}) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.route(
    '**/api/v1/characters',
    (route) =>
      fulfillFault(route, evidence, {
        status: 503,
        json: { code: 'connection_failed' },
      }),
    { times: 1 },
  )
  await page.goto('/new')
  const choice = page.locator('.selection-character')
  await expect(choice.getByRole('alert')).toBeVisible()
  await expect(choice.getByText(ja.noPresets)).toHaveCount(0)
  await expect(page.getByLabel(ja.chooseStory)).toBeVisible()
  const begin = page.getByRole('button', { name: ja.create, exact: true })
  await expect(begin).toBeDisabled()
  await evidence.capture('The failed list is distinct from an empty collection')
  await choice.getByRole('button', { name: ja.reloadData }).click()
  await expect(begin).toBeEnabled()
  await evidence.capture('The starting choices can be used after retry')

  await page.setViewportSize({ width: 1440, height: 960 })
  await page.route(
    '**/api/v1/stories',
    (route) =>
      fulfillFault(route, evidence, {
        status: 503,
        json: { code: 'connection_failed' },
      }),
    { times: 1 },
  )
  await page.getByRole('link', { name: ja.play, exact: true }).click()
  const main = page.getByRole('main')
  await expect(main.getByRole('alert')).toHaveCount(1)
  await expect(main.locator('.selection-story').getByRole('alert')).toHaveCount(
    0,
  )
  await expect(begin).toBeDisabled()
})

test('recovers the album and drawing links from failed reads', async ({
  page,
  evidence,
}) => {
  for (const [path, route] of [
    ['/images', '/images'],
    ['/runtime', '/runtime/views'],
  ]) {
    await page.route(
      `**/api/v1${route}`,
      (request) =>
        fulfillFault(request, evidence, {
          status: 503,
          json: { code: 'connection_failed' },
        }),
      { times: 1 },
    )
    await page.goto(path)
    if (path === '/runtime')
      await page.getByText(ja.drawingSettings, { exact: true }).click()
    const main = page.getByRole('main')
    const alert = main.getByRole('alert')
    await expect(alert).toBeVisible()
    const reload = alert.getByRole('button', { name: ja.reloadData })
    await expect(reload).toBeVisible()
    await reload.click()
    await expect(alert).toHaveCount(0)
    await expect(
      path === '/images'
        ? main.getByText(ja.albumEmpty)
        : main.getByRole('link', { name: ja.openComfy }),
    ).toBeVisible()
    await evidence.capture(`The ${path} view after recovering its data`)
  }
})

test('keeps a saved counterpart visible when the display language changes', async ({
  page,
  request,
  evidence,
}) => {
  const characters = await (await request.get('/api/v1/characters')).json()
  const counterpart = characters.find(
    (item: { sample_locale: string }) => item.sample_locale === 'ja',
  )
  const saved = await request.post('/api/v1/stories/save', {
    data: {
      operation_id: randomUUID(),
      value: { name: 'A saved pairing', character_id: counterpart.id },
    },
  })
  expect(saved.ok()).toBe(true)
  const { preset } = await saved.json()
  const preferences = await (await request.get('/api/v1/preferences')).json()
  expect(
    (
      await request.put('/api/v1/preferences', {
        data: { ...preferences, locale: 'en' },
      })
    ).ok(),
  ).toBe(true)

  await page.goto(`/stories/${preset.id}`)
  const recommended = page.getByLabel(en.defaultCharacter)
  await expect(recommended).toHaveValue(counterpart.id)
  await expect(recommended.locator('option:checked')).toHaveText(
    counterpart.name,
  )
  await page.goto(`/new?story=${preset.id}`)
  const begin = page.getByRole('button', { name: en.create, exact: true })
  await expect(begin).toBeEnabled()
  await page.locator('.start-selection > summary').click()
  await expect(page.getByLabel(en.chooseCharacter)).toHaveValue(counterpart.id)
  await evidence.capture(
    'The saved pairing is displayed in a different UI language',
  )
  await begin.click()
  await expect(page).toHaveURL(/\/conversations\//)
  const identity = new URL(page.url()).pathname.split('/').at(-1)
  const conversation = await (
    await request.get(`/api/v1/conversations/${identity}`)
  ).json()
  expect(conversation.definition.character.name).toBe(counterpart.name)
})

for (const [locale, m] of [
  ['ja', ja],
  ['en', en],
] as const) {
  test(`${locale}: reads the library and the start choices on a narrow screen`, async ({
    page,
    request,
    evidence,
  }) => {
    const preferences = await (await request.get('/api/v1/preferences')).json()
    const updated = await request.put('/api/v1/preferences', {
      data: { ...preferences, locale },
    })
    expect(updated.ok()).toBe(true)
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/characters')
    const card = page.locator('.preset-card').first()
    await expect(card.getByRole('heading')).toBeInViewport()
    await expect(card.locator('.preset-cover')).toHaveCount(0)
    await expect(
      card.getByRole('link', { name: m.use + ' →' }),
    ).toBeInViewport()
    await evidence.capture('The name and actions come before any portrait')
    await card.getByRole('link', { name: m.use + ' →' }).click()
    const summary = page.locator('.start-selection > summary')
    await expect(
      summary.locator('.selection-summary > span > span'),
    ).toHaveText([m.chooseStory, m.chooseCharacter, m.choosePlayer])
    await evidence.capture('The three selected roles in the collapsed summary')
    await summary.click()
    const choices = page.locator('.start-selection .selection-card')
    await expect(choices.nth(0).getByLabel(m.chooseStory)).toBeVisible()
    await expect(choices.nth(1).getByLabel(m.chooseCharacter)).toBeVisible()
    await expect(choices.nth(2).getByRole('heading')).toHaveText(m.choosePlayer)
    await evidence.capture('The open choices follow the same order')
    await summary.click()
    await expect(
      page.getByRole('button', { name: m.create, exact: true }),
    ).toBeEnabled()
  })
}

test('opens connection settings from the short mobile navigation', async ({
  page,
  evidence,
  auditAccessibility,
}) => {
  await page.setViewportSize({ width: 390, height: 420 })
  await page.goto('/')
  await page.addStyleTag({ content: 'html { font-size: 200% !important; }' })
  await page.getByRole('button', { name: ja.showNavigation }).click()
  const navigation = page.getByRole('dialog', { name: ja.workspace })
  await navigation.locator('.connection-summary > summary').click()
  await evidence.capture(
    'Expanded mobile connection summary at double text size',
  )
  await navigation.getByRole('link', { name: ja.connectionSettings }).click()
  await expect(navigation).not.toBeVisible()
  await expect(page.getByRole('heading', { level: 1 })).toHaveText(
    ja.connectionSettings,
  )
  await page.locator('summary').filter({ hasText: ja.drawingSettings }).click()
  await expect(page.getByRole('link', { name: ja.openComfy })).toBeVisible()
  await auditAccessibility()
  await evidence.capture('Connection details reached directly from navigation')
})

test('reads and operates the expanded conversation controls at double text size', async ({
  page,
  request,
  evidence,
  auditAccessibility,
}) => {
  const response = await request.post('/api/v1/conversations', {
    data: { title: 'Expanded conversation tools', llm_profile: 'lmstudio-4b' },
  })
  expect(response.ok()).toBe(true)
  const { id } = await response.json()
  await page.goto(`/conversations/${id}`)
  await page
    .getByRole('textbox', { name: ja.messageLabel })
    .fill('Read with me.')
  await page.getByRole('button', { name: ja.send, exact: true }).click()
  await expect(page.locator('.assistant-message')).toHaveCount(1)
  await page.setViewportSize({ width: 390, height: 420 })
  await page.addStyleTag({ content: 'html { font-size: 200% !important; }' })
  const stateTrigger = page.getByRole('button', {
    name: ja.showState,
    exact: true,
  })
  await stateTrigger.click()
  const state = page.getByRole('dialog', { name: ja.currentState, exact: true })
  await state.getByText(ja.peopleDetails, { exact: true }).click()
  await state.getByText(ja.actionHistory, { exact: true }).click()
  await expect(state.getByText(ja.noActionHistory)).toBeVisible()
  await state.getByText(ja.fixedSettings, { exact: true }).click()
  await evidence.capture(
    'Situation with the people, action history and settings expanded',
  )
  await page.keyboard.press('Escape')
  await expect(stateTrigger).toBeFocused()
  const menu = page.getByRole('button', {
    name: ja.conversationMenu,
    exact: true,
  })
  await menu.click()
  const menuPanel = page.getByRole('dialog', {
    name: ja.conversationMenu,
    exact: true,
  })
  await menuPanel.getByText(ja.pinnedNotes, { exact: true }).click()
  await menuPanel
    .getByLabel(ja.notesDescription)
    .fill('Keep my unfinished note.')
  await menuPanel.getByText(ja.exportConversation, { exact: true }).click()
  const download = menuPanel.getByRole('link', { name: ja.downloadCopy })
  await download.focus()
  await expect(download).toBeInViewport()
  await auditAccessibility()
  await evidence.capture(
    'The export action remains reachable below an unfinished note',
  )
  await page.keyboard.press('Escape')
  await expect(menu).toBeFocused()
  await menu.click()
  await expect(menuPanel.getByLabel(ja.notesDescription)).toHaveValue(
    'Keep my unfinished note.',
  )
  await menuPanel.getByRole('button', { name: ja.closePanel }).click()
  await expect(menu).toBeFocused()
  const scene = page.getByRole('button', { name: ja.sceneActions, exact: true })
  await scene.click()
  const scenePanel = page.getByRole('dialog', {
    name: ja.sceneActions,
    exact: true,
  })
  await scenePanel.getByLabel(ja.sceneName).fill('Bookshop bookmark')
  await page.route(
    '**/api/v1/conversations/*/bookmarks',
    (route) =>
      fulfillFault(route, evidence, {
        status: 409,
        json: { code: 'operation_conflict' },
      }),
    { times: 1 },
  )
  await scenePanel.getByRole('button', { name: ja.saveScene }).click()
  const sceneError = scenePanel.getByRole('alert')
  await expect(sceneError).toBeFocused()
  await expect(sceneError).toBeInViewport({ ratio: 0.9 })
  await expect(scenePanel.getByLabel(ja.sceneName)).toHaveValue(
    'Bookshop bookmark',
  )
  await evidence.capture('The bookmark failure is shown inside its popup')
  await scenePanel.getByRole('button', { name: ja.saveScene }).click()
  await expect(scenePanel).not.toBeVisible()
  await menu.click()
  await expect(menuPanel.getByText('Bookshop bookmark')).toBeVisible()
  await evidence.capture('A saved bookmark appears in the conversation menu')
})

test.describe('preserved stories', () => {
  test.use({ archive: true })

  test('opens the original dialogue and offers a new story after it', async ({
    page,
    evidence,
    auditAccessibility,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/')
    await page.getByRole('link', { name: 'The preserved bookshop' }).click()
    const main = page.getByRole('main')
    await expect(main.getByRole('heading', { level: 1 })).toHaveText(
      'The preserved bookshop',
    )
    await expect(main.getByText(ja.archivedNotice)).toBeVisible()
    await expect(main.getByText('I open the book.')).toBeVisible()
    await expect(main.getByText('This page is still here.')).toBeVisible()
    await expect(main.getByText('The next page turns.')).toBeVisible()
    await expect(main.locator('.user-message')).toHaveCount(1)
    await auditAccessibility()
    await evidence.capture('Read-only dialogue and the next story link')
    await main.getByRole('link', { name: ja.newConversation }).click()
    await expect(page).toHaveURL(/\/new$/)
  })
})
