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
  async function run(name: string, expectedMessages: string[]) {
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
        rejoin_index: -1,
        away_indexes: [],
        expires_at: new Date(Date.now() + 60_000).toISOString(),
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
  await expect(page.locator('.user-message p')).toHaveText(messages)
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
  }) => {
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
    for (const message of messages) {
      await input.fill(message)
      await page.getByRole('button', { name: m.send, exact: true }).click()
      await expect(
        page
          .locator('article.turn')
          .filter({ hasText: message })
          .locator('.assistant-message'),
      ).toHaveCount(1)
    }
    expect(
      (await (await request.get(`/api/v1/conversations/${scene.id}`)).json())
        .turn_count,
    ).toBe(LONG_TURNS)
    await page.reload()
    await expect(page.locator('.assistant-message')).toHaveCount(30)
    await page.getByRole('button', { name: m.olderTurns, exact: true }).click()
    await expect(page.locator('.assistant-message')).toHaveCount(LONG_TURNS)
    await expect(page.locator('.user-message p')).toHaveText(messages)
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
  await page.route(route, (r) =>
    fulfillFault(r, evidence, {
      status: 503,
      json: { code: 'connection_failed' },
    }),
  )
  await page
    .getByRole('button', { name: ja.conversationMenu, exact: true })
    .click()
  await page.getByText(ja.pinnedNotes, { exact: true }).click()
  await page.getByRole('button', { name: ja.save, exact: true }).click()
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

test('explains a separated companion beside the composer and queues joining without sending', async ({
  page,
  request,
  evidence,
}) => {
  const scene = await (
    await request.post('/api/v1/conversations', {
      data: {
        title: 'Apart',
        initial_state: { place: 'Beach', character_place: 'Bookshop' },
      },
    })
  ).json()
  await page.goto(`/conversations/${scene.id}`)
  await expect(page.locator('.composer-options .presence-notice')).toBeVisible()
  await page
    .getByRole('button', { name: ja.joinCompanion, exact: true })
    .click()
  await expect(page.locator('.queued-action')).toContainText('Bookshop')
  const saved = await (
    await request.get(`/api/v1/conversations/${scene.id}`)
  ).json()
  expect(saved.state.place).toBe('Beach')
  expect(saved.turn_count).toBe(0)
  await evidence.capture(
    'Joining is an unsent player action; the saved location is unchanged',
  )
})
