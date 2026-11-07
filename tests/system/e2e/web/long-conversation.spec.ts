import { test, expect, fulfillFault } from './fixtures'
import { execFile } from 'node:child_process'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { promisify } from 'node:util'
import path from 'node:path'
import ja from '../../../../src/frontend/src/app/locales/ja.json' with { type: 'json' }
import en from '../../../../src/frontend/src/app/locales/en.json' with { type: 'json' }

const LONG_TURNS = 32
const LONG_TIMEOUT_MS = 180_000
const runFile = promisify(execFile)

test('exercises the conversation runner with a scripted provider and safe failure records', async ({
  page,
  request,
  baseURL,
}, info) => {
  const scene = await (
    await request.post('/api/v1/conversations', {
      data: {
        title: 'Scripted runner',
        locale: 'ja',
        llm_profile: 'lmstudio-4b',
      },
    })
  ).json()
  const messages = ['A scripted first message.', 'A scripted second message.']
  async function run(
    name: string,
    expectedMessages: string[],
    overrides: Record<string, unknown> = {},
  ) {
    const directory = info.outputPath(name)
    await mkdir(directory, { recursive: true })
    const file = path.join(directory, 'browser-input.json')
    await writeFile(
      file,
      JSON.stringify({
        format_version: 1,
        real_llm: false,
        endpoint: baseURL,
        conversation: scene.id,
        messages: expectedMessages,
        target_turns: expectedMessages.length,
        moves: {},
        expires_at: new Date(Date.now() + 60_000).toISOString(),
        expected_inventory: { player: null, character: null },
        expected_meetings: {},
        ...overrides,
      }),
    )
    return runFile(
      process.execPath,
      ['scripts/acceptance/conversations/long.mjs', file],
      { timeout: 60_000 },
    )
  }
  await run('success', messages)
  const passed = JSON.parse(
    await readFile(info.outputPath('success/browser-result.json'), 'utf8'),
  )
  expect(passed.status).toBe('passed')
  expect(passed.real_llm).toBe(false)
  expect(passed.unexpected_events).toEqual([])
  await run('expired-saved', messages, {
    read_only: true,
    expires_at: new Date(Date.now() - 60_000).toISOString(),
  })
  const retainedOperations = await (
    await request.get(`/api/v1/conversations/${scene.id}/operations`)
  ).json()
  expect(retainedOperations).toHaveLength(2)
  const inventoryFailure = await run('inventory-mismatch', messages, {
    expected_inventory: {
      player: [{ id: 'absent', name: 'Cup' }],
      character: null,
    },
  }).then(
    () => null,
    (error: { code: number }) => error,
  )
  expect(inventoryFailure?.code).toBe(1)
  const inventoryDiagnostic = JSON.parse(
    await readFile(
      info.outputPath('inventory-mismatch/browser-failure.json'),
      'utf8',
    ),
  )
  expect(inventoryDiagnostic.check).toBe('inventory_constraints')
  const savedCheckOperation = await (
    await request.get(`/api/v1/operations/${inventoryDiagnostic.operation_id}`)
  ).json()
  expect(savedCheckOperation.conversation_id).toBe(scene.id)
  const other = await (
    await request.post('/api/v1/conversations', {
      data: {
        title: 'Operation diagnostics',
        locale: 'ja',
        llm_profile: 'lmstudio-4b',
      },
    })
  ).json()
  const liveFailure = await run(
    'new-operation-mismatch',
    [messages[0], 'Is the imaginary compass here?'],
    {
      conversation: other.id,
      expected_inventory: null,
      forbidden_held_objects: ['Imaginary compass'],
    },
  ).then(
    () => null,
    (error: { code: number }) => error,
  )
  expect(liveFailure?.code).toBe(1)
  const operationDiagnostic = JSON.parse(
    await readFile(
      info.outputPath('new-operation-mismatch/browser-failure.json'),
      'utf8',
    ),
  )
  expect(operationDiagnostic.check).toBe('inventory_constraints')
  const failedCheckOperation = await (
    await request.get(`/api/v1/operations/${operationDiagnostic.operation_id}`)
  ).json()
  expect(failedCheckOperation.conversation_id).toBe(other.id)
  const latestChecked = await (
    await request.get(`/api/v1/conversations/${other.id}/turns?limit=1`)
  ).json()
  expect(operationDiagnostic.operation_id).toBe(
    latestChecked.items[0].operation_id,
  )
  const malformed = await (
    await request.post('/api/v1/conversations', {
      data: {
        title: 'Dialogue diagnostics',
        locale: 'ja',
        llm_profile: 'lmstudio-4b',
      },
    })
  ).json()
  const dialogueFailure = await run(
    'dialogue-fragment',
    ['Please summarize our preparation.'],
    {
      conversation: malformed.id,
      forbidden_reply_fragments: ['}}]}{'],
    },
  ).then(
    () => null,
    (error: { code: number }) => error,
  )
  expect(dialogueFailure?.code).toBe(1)
  const dialogueDiagnostic = JSON.parse(
    await readFile(
      info.outputPath('dialogue-fragment/browser-failure.json'),
      'utf8',
    ),
  )
  expect(dialogueDiagnostic.check).toBe('reply_content')
  const marker = 'private-order-marker'
  const rejected = await run('mismatch', [marker, messages[1]]).then(
    () => null,
    (error: { code: number; stderr: string }) => error,
  )
  expect(rejected?.code).toBe(1)
  expect(rejected?.stderr ?? '').not.toContain(marker)
  const raw = await readFile(
    info.outputPath('mismatch/browser-failure.json'),
    'utf8',
  )
  expect(raw).not.toContain(marker)
  expect(JSON.parse(raw).type).toBe('AssertionError')
  expect(
    JSON.parse(
      await readFile(info.outputPath('mismatch/browser-result.json'), 'utf8'),
    ).status,
  ).toBe('failed')
  await page.goto(`/conversations/${scene.id}`)
  await expect(page.locator('.user-message p')).toHaveText(
    messages.filter(Boolean),
  )
  await expect(page.locator('.assistant-message')).toHaveCount(2)
})

for (const [locale, m] of [
  ['ja', ja],
  ['en', en],
] as const) {
  test(`${locale}: sends a long conversation through the UI and restores every page`, async ({
    page,
    request,
    evidence,
    baseURL,
  }, info) => {
    test.setTimeout(LONG_TIMEOUT_MS)
    const response = await request.post('/api/v1/conversations', {
      data: {
        title: 'Long conversation',
        locale,
        llm_profile: 'lmstudio-4b',
        scenario: { place: 'Bookshop' },
      },
    })
    expect(response.status()).toBe(201)
    const scene = await response.json()
    if (locale === 'en') {
      await page.goto('/settings')
      await page
        .getByRole('combobox', { name: ja.language, exact: true })
        .selectOption('en')
    }
    await page.goto(`/conversations/${scene.id}`)
    const input = page.getByRole('textbox', { name: m.messageLabel })
    const messages = Array.from(
      { length: LONG_TURNS },
      (_, i) => `Browser turn ${i + 1}: please continue.`,
    )
    messages[0] = 'I head to the beach.'
    messages[20] = ''
    for (const [index, message] of messages.entries()) {
      if (index === 10 || index === 20) {
        await page
          .getByRole('button', { name: m.changePlace, exact: true })
          .click()
        await page
          .getByLabel(m.destination, { exact: true })
          .fill(index === 10 ? 'Park' : 'Bookshop')
      }
      await input.fill(message)
      const [receipt] = await Promise.all([
        page.waitForResponse(
          (response) =>
            response.request().method() === 'POST' &&
            new URL(response.url()).pathname ===
              `/api/v1/conversations/${scene.id}/turns`,
        ),
        page.getByRole('button', { name: m.send, exact: true }).click(),
      ])
      expect(receipt.status()).toBe(202)
      const turn = message
        ? page.locator('article.turn').filter({ hasText: message })
        : page.locator('article.turn').filter({
            has: page.locator('.user-action', { hasText: 'Bookshop' }),
          })
      await expect(turn.locator('.assistant-message')).toHaveCount(1)
    }
    expect(
      (await (await request.get(`/api/v1/conversations/${scene.id}`)).json())
        .turn_count,
    ).toBe(LONG_TURNS)
    await page.reload()
    await expect(page.locator('.assistant-message')).toHaveCount(30)
    await page.getByRole('button', { name: m.olderTurns, exact: true }).click()
    await expect(page.locator('.assistant-message')).toHaveCount(LONG_TURNS)
    await expect(page.locator('.user-message p')).toHaveText(
      messages.filter(Boolean),
    )
    expect(await page.locator('article.turn').count()).toBe(LONG_TURNS)
    expect(
      await (
        await request.get(`/api/v1/conversations/${scene.id}/images`)
      ).json(),
    ).toHaveLength(0)
    const history = page.getByRole('log', { name: m.conversationHistory })
    await history.hover()
    await page.mouse.wheel(0, -100000)
    await expect(
      page.getByRole('button', { name: m.latestReply }),
    ).toBeVisible()
    await evidence.capture(
      'All 32 turns restored in order after reload and pagination',
    )
    await page.getByRole('button', { name: m.latestReply }).click()
    await expect
      .poll(() =>
        history.evaluate((x) => x.scrollHeight - x.scrollTop - x.clientHeight),
      )
      .toBeLessThan(5)
    await expect(
      page.getByText(messages.at(-1)!, { exact: true }),
    ).toBeVisible()
    const file = info.outputPath('saved-browser-input.json')
    await writeFile(
      file,
      JSON.stringify({
        format_version: 1,
        real_llm: false,
        endpoint: baseURL,
        conversations: [scene.id],
        conversation_turn_counts: { [scene.id]: LONG_TURNS },
        has_images: false,
        standard_views: [],
      }),
    )
    await runFile(process.execPath, ['scripts/acceptance/web.mjs', file], {
      timeout: 60_000,
    })
  })
}

test('retains the conversation and draft when refreshing fails, then recovers explicitly', async ({
  page,
  request,
  evidence,
}) => {
  const scene = await (
    await request.post('/api/v1/conversations', {
      data: { title: 'Retained conversation', llm_profile: 'lmstudio-4b' },
    })
  ).json()
  await page.goto(`/conversations/${scene.id}`)
  const input = page.getByRole('textbox', { name: ja.messageLabel })
  await input.fill('A saved message')
  await page.getByRole('button', { name: ja.send, exact: true }).click()
  await expect(page.locator('.assistant-message')).toHaveCount(1)
  await input.fill('Keep this unsent draft')
  const route = `**/api/v1/conversations/${scene.id}`
  let notesSaved = false
  await page.route(
    `${route}/notes`,
    async (request) => {
      const response = await request.fetch()
      notesSaved = response.ok()
      await request.fulfill({ response })
    },
    { times: 1 },
  )
  await page.route(route, (request) => {
    if (!notesSaved) return request.continue()
    return fulfillFault(request, evidence, {
      status: 503,
      json: { code: 'connection_failed' },
    })
  })
  await page
    .getByRole('button', { name: ja.conversationMenu, exact: true })
    .click()
  await page.getByText(ja.pinnedNotes, { exact: true }).click()
  const [saved] = await Promise.all([
    page.waitForResponse(
      (response) =>
        response.request().method() === 'PUT' &&
        new URL(response.url()).pathname ===
          `/api/v1/conversations/${scene.id}/notes`,
    ),
    page.getByRole('button', { name: ja.save, exact: true }).click(),
  ])
  expect(saved.ok()).toBe(true)
  await page.keyboard.press('Escape')
  await expect(page.getByText(ja.refreshFailed, { exact: true })).toBeVisible()
  await expect(page.locator('.assistant-message')).toHaveCount(1)
  await expect(input).toHaveValue('Keep this unsent draft')
  await expect(
    page.getByRole('button', { name: ja.send, exact: true }),
  ).toBeDisabled()
  await expect(
    page.getByRole('button', { name: ja.sceneActions, exact: true }),
  ).toBeDisabled()
  await evidence.capture(
    'Saved history and unsent draft remain during a refresh failure',
  )
  await page.unroute(route)
  await page.getByRole('button', { name: ja.reloadData, exact: true }).click()
  await expect(page.getByText(ja.refreshFailed, { exact: true })).toHaveCount(0)
  await expect(input).toHaveValue('Keep this unsent draft')
  await expect(
    page.getByRole('button', { name: ja.send, exact: true }),
  ).toBeEnabled()
})

test('sends a movement without an empty speech bubble', async ({
  page,
  request,
  evidence,
}) => {
  const scene = await (
    await request.post('/api/v1/conversations', {
      data: {
        title: 'Shared location',
        llm_profile: 'lmstudio-4b',
        initial_state: { place: 'Bookshop' },
      },
    })
  ).json()
  await page.setViewportSize({ width: 390, height: 420 })
  await page.goto(`/conversations/${scene.id}`)
  await page.getByRole('button', { name: ja.changePlace, exact: true }).click()
  const destination = page.getByRole('textbox', {
    name: ja.destination,
    exact: true,
  })
  await destination.pressSequentially('Garden')
  await expect(destination).toBeInViewport({ ratio: 1 })
  await page.getByRole('textbox', { name: ja.messageLabel }).fill('  ')
  await page.getByRole('button', { name: ja.send, exact: true }).click()
  await expect(page.locator('.assistant-message')).toHaveCount(1)
  await expect(page.locator('.user-message')).toHaveCount(0)
  await expect(page.locator('.user-action')).toContainText('Garden')
  await page.reload()
  await expect(page.locator('.assistant-message')).toHaveCount(1)
  const history = page.getByRole('log', { name: ja.conversationHistory })
  const visibleHistory = await history.evaluate((element) => {
    const style = getComputedStyle(element)
    return {
      height:
        element.clientHeight -
        parseFloat(style.paddingTop) -
        parseFloat(style.paddingBottom),
      lineHeight: parseFloat(
        getComputedStyle(element.querySelector('.assistant-message p')!)
          .lineHeight,
      ),
    }
  })
  expect(visibleHistory.height).toBeGreaterThanOrEqual(
    visibleHistory.lineHeight,
  )
  await expect(page.locator('.assistant-message p').last()).toBeInViewport()
  const saved = await (
    await request.get(`/api/v1/conversations/${scene.id}`)
  ).json()
  expect(saved.state.place).toBe('Garden')
  expect(saved.turn_count).toBe(1)
  await evidence.capture(
    'Action-only movement survives reload without empty speech',
  )
})
