import assert from 'node:assert/strict'
import path from 'node:path'
import { readFile } from 'node:fs/promises'
import { chromium, request } from '@playwright/test'
import {
  Evidence,
  contained,
  diagnostic,
  jsonFile,
} from '../../../tools/testing/web/evidence.mjs'
import ja from '../../../src/frontend/src/app/locales/ja.json' with { type: 'json' }
import en from '../../../src/frontend/src/app/locales/en.json' with { type: 'json' }

const file = contained(path.resolve('.artifacts'), process.argv[2])
const config = JSON.parse(await readFile(file, 'utf8'))
const locale = config.locale ?? 'ja'
const messages = locale === 'en' ? en : ja
assert.equal(config.format_version, 1)
assert.equal(typeof config.real_llm, 'boolean')
const origin = new URL(config.endpoint)
assert(['127.0.0.1', 'localhost', '[::1]'].includes(origin.hostname))
assert(!origin.username && !origin.password && !origin.search && !origin.hash)
assert.equal(config.messages.length, config.target_turns)
const directory = path.dirname(file)
const browser = await chromium.launch()
const api = await request.newContext({
  baseURL: origin.origin,
  extraHTTPHeaders: { Connection: 'close' },
})
const page = await browser.newPage({
  baseURL: origin.origin,
  viewport: { width: 1440, height: 960 },
})
const evidence = new Evidence(page, path.join(directory, 'browser'), {
  mode: 'assertion',
  real_llm: config.real_llm,
  real_images: false,
})
page.setDefaultTimeout(15000)
const apiPath = `/api/v1/conversations/${config.conversation}`
function inspectInventory(saved) {
  for (const person of ['player', 'character']) {
    for (const object of saved.state[person].held_objects ?? []) {
      assert(!config.forbidden_held_objects?.includes(object.name))
    }
    if (config.expected_inventory) {
      assert.deepEqual(
        saved.state[person].held_objects,
        config.expected_inventory[person],
      )
    }
  }
}
let status = 'failed'
let check = 'open_conversation'
let operationId = null
function inspectTurn(turn, index) {
  operationId = turn.operation_id
  check = 'inventory_constraints'
  inspectInventory(turn)
  check = 'reply_content'
  assert(turn.assistant.trim() || turn.action_narration?.trim() || turn.meeting)
  for (const fragment of config.forbidden_reply_fragments ?? [])
    assert(!turn.assistant.includes(fragment))
  const expectedReply = config.expected_replies?.[String(index)]
  if (expectedReply) assert(turn.assistant.includes(expectedReply))
  check = 'shared_place'
  assert(!Object.hasOwn(turn.state, 'character_place'))
  const expectedPlace = config.expected_places?.[String(index)]
  if (expectedPlace) assert.equal(turn.state.place, expectedPlace)
  check = 'movement_result'
  const expectedMove = config.expected_moves?.[String(index)]
  assert.equal(
    turn.action?.kind === 'move' ? turn.action.destination : undefined,
    expectedMove,
  )
  if (expectedMove) assert.equal(turn.action_narration, '')
  check = 'meeting_result'
  const expectedMeeting = config.expected_meetings?.[String(index)]
  if (expectedMeeting) {
    assert.equal(turn.meeting?.status, expectedMeeting.status)
    for (const key of ['date', 'time', 'place'])
      assert.equal(turn.meeting[key] ?? null, expectedMeeting[key] ?? null)
  }
}

async function inspectDisplayedMeeting(article, index) {
  const expected = config.expected_meetings?.[String(index)]
  if (!expected) return
  check = 'meeting_display'
  const details = article.getByRole('region', { name: messages.meetingDetails })
  await details.waitFor()
  if (expected.status === 'confirmed') {
    assert.deepEqual(
      await details.locator('dd').allTextContents(),
      ['date', 'time', 'place'].map(
        (key) => expected[key] ?? messages.meetingUndecided,
      ),
    )
  } else {
    const expectedText =
      expected.status === 'cancelled'
        ? messages.meetingCancelled
        : messages.meetingUnknown
    assert.equal(await details.locator('p').last().innerText(), expectedText)
  }
}
async function inspectSavedTurns(count) {
  operationId = null
  const turns = []
  let before = null
  while (turns.length < count) {
    check = 'read_saved_turns'
    const response = await api.get(
      apiPath +
        '/turns?limit=30' +
        (before === null ? '' : `&before=${before}`),
    )
    assert.equal(response.status(), 200)
    const page = await response.json()
    turns.unshift(...page.items)
    if (page.next_before === null) break
    assert(
      Number.isInteger(page.next_before) &&
        page.next_before >= 0 &&
        page.next_before < (before ?? count),
    )
    before = page.next_before
  }
  assert.equal(turns.length, count)
  for (const [index, turn] of turns.entries()) {
    operationId = turn.operation_id
    check = 'saved_input_order'
    assert.equal(turn.user, config.messages[index])
    inspectTurn(turn, index)
  }
}
try {
  if (locale === 'en') {
    const preferences = await (await api.get('/api/v1/preferences')).json()
    if (preferences.locale !== locale) {
      assert(!config.read_only)
      const response = await api.put('/api/v1/preferences', {
        data: { ...preferences, locale },
      })
      assert.equal(response.status(), 200)
    }
  }
  await page.goto(`/conversations/${config.conversation}`)
  await page.getByRole('textbox', { name: messages.messageLabel }).waitFor()
  let saved = await (await api.get(apiPath)).json()
  assert(!config.read_only || saved.turn_count === config.target_turns)
  await inspectSavedTurns(saved.turn_count)
  for (let index = saved.turn_count; index < config.target_turns; index++) {
    operationId = null
    check = 'prepare_input'
    assert(!config.read_only)
    assert(Date.now() < Date.parse(config.expires_at))
    await evidence.step(
      `Send and display conversation turn ${index + 1}`,
      async () => {
        if (config.moves[String(index)]) {
          await page
            .getByRole('button', { name: messages.changePlace, exact: true })
            .click()
          await page
            .getByLabel(messages.destination, { exact: true })
            .fill(config.moves[String(index)])
        }
        await page
          .getByRole('textbox', { name: messages.messageLabel })
          .fill(config.messages[index])
        check = 'accept_input'
        const [response] = await Promise.all([
          page.waitForResponse(
            (response) =>
              response.request().method() === 'POST' &&
              new URL(response.url()).pathname === apiPath + '/turns',
          ),
          page
            .getByRole('button', { name: messages.send, exact: true })
            .click(),
        ])
        assert.equal(response.status(), 202)
        const operation = await response.json()
        operationId = operation.id
        check = 'operation_complete'
        let complete = false
        while (Date.now() < Date.parse(config.expires_at)) {
          const observed = await api.get(`/api/v1/operations/${operation.id}`)
          assert.equal(observed.status(), 200)
          const value = await observed.json()
          await jsonFile(path.join(directory, 'progress.json'), {
            turn: index + 1,
            operation_id: operation.id,
            state: value.state,
            error: value.error,
          })
          if (value.state === 'complete') {
            complete = true
            break
          }
          assert(
            ['accepted', 'running'].includes(value.state),
            `Operation ended: ${value.error || value.state}`,
          )
          await new Promise((resolve) => setTimeout(resolve, 1000))
        }
        assert(complete, 'The permitted conversation deadline was reached.')
        const turn = page
          .locator('article.turn')
          .filter({ hasText: config.messages[index] })
        check = 'reply_visible'
        await turn.locator('.assistant-message').waitFor()
        saved = await (await api.get(apiPath)).json()
        check = 'inventory_constraints'
        inspectInventory(saved)
        check = 'turn_saved'
        assert.equal(saved.turn_count, index + 1)
        const latest = await (await api.get(apiPath + '/turns?limit=1')).json()
        inspectTurn(latest.items[0], index)
        await inspectDisplayedMeeting(turn, index)
      },
    )
    if (index === 9) {
      await page.reload()
      await page.locator('.assistant-message').nth(9).waitFor()
    }
  }
  await inspectSavedTurns(saved.turn_count)
  await page.reload()
  check = 'inventory_constraints'
  inspectInventory(saved)
  check = 'restored_turn_order'
  await page
    .locator('.assistant-message')
    .nth(config.target_turns - 1)
    .waitFor()
  assert.equal(
    await page.locator('.assistant-message').count(),
    config.target_turns,
  )
  const restored = await page.locator('.user-message p').allTextContents()
  assert(
    restored.length === config.messages.length &&
      restored.every((text, index) => text === config.messages[index]),
    'The saved message order differs from the scenario.',
  )
  for (let index = 0; index < config.target_turns; index++)
    await inspectDisplayedMeeting(
      page.locator('article.turn').nth(index),
      index,
    )
  check = 'unexpected_images'
  assert.equal((await (await api.get(apiPath + '/images')).json()).length, 0)
  status = 'passed'
} catch (error) {
  await jsonFile(path.join(directory, 'browser-failure.json'), {
    format_version: 1,
    ...diagnostic(error),
    check,
    operation_id: operationId,
  })
} finally {
  await evidence.capture('Final long conversation after reload')
  const result = await evidence.finish(status, 'passed')
  await jsonFile(path.join(directory, 'browser-result.json'), result)
  await browser.close()
  await api.dispose()
  if (status !== 'passed' || result.unexpected_events.length)
    process.exitCode = 1
}
