import assert from 'node:assert/strict'
import path from 'node:path'
import { readFile } from 'node:fs/promises'
import { chromium, expect } from '@playwright/test'
import ja from '../../src/frontend/src/app/locales/ja.json' with { type: 'json' }
import en from '../../src/frontend/src/app/locales/en.json' with { type: 'json' }
import {
  Evidence,
  contained,
  diagnostic,
  jsonFile,
} from '../../tools/testing/web/evidence.mjs'

const configPath = contained(path.resolve('.artifacts'), process.argv[2])
const config = JSON.parse(await readFile(configPath, 'utf8'))
assert.equal(config.format_version, 1)
assert.equal(config.real_llm, true)
const origin = new URL(config.endpoint)
assert(['127.0.0.1', 'localhost', '[::1]'].includes(origin.hostname))
assert(!origin.username && !origin.password && !origin.search && !origin.hash)
const output = path.dirname(configPath)
const browser = await chromium.launch()
const page = await browser.newPage({
  baseURL: origin.origin,
  viewport: { width: 1440, height: 960 },
})
const evidence = new Evidence(page, path.join(output, 'browser'), {
  mode: 'assertion',
  real_llm: true,
  real_images: false,
})
const inferenceTimeout = 180_000
const character = '青井'
const story = '港の時計店'
const button = (name) => page.getByRole('button', { name, exact: true })
const field = (name) => page.getByLabel(name, { exact: true })
let sends = 0
page.on('request', (request) => {
  if (
    request.method() === 'POST' &&
    /^\/api\/v1\/conversations\/[^/]+\/turns$/.test(
      new URL(request.url()).pathname,
    )
  )
    sends += 1
})

async function draft(kind, name, idea) {
  await page.goto(`/${kind}/new`)
  await button(ja.createDraft).click()
  const dialog = page.getByRole('dialog', { name: ja.createDraft, exact: true })
  await dialog.getByLabel(ja.draftPrompt).fill(idea)
  await dialog
    .getByRole('button', { name: ja.generateDraft, exact: true })
    .click()
  const apply = dialog.getByRole('button', { name: ja.useDraft, exact: true })
  await expect(apply).toBeVisible({ timeout: inferenceTimeout })
  await evidence.capture('Review the actual model draft before applying it')
  await apply.click()
  await expect(field(ja.requiredName)).not.toHaveValue('')
  await field(ja.requiredName).fill(name)
  if (kind === 'stories') {
    await page
      .getByRole('combobox', { name: ja.defaultCharacter, exact: true })
      .selectOption({ label: character })
  }
  await button(ja.save).click()
  await expect(page.getByRole('heading', { name, level: 1 })).toBeVisible()
  await page.reload()
  await expect(field(ja.requiredName)).toHaveValue(name)
  return new URL(page.url()).pathname.split('/').at(-1)
}

async function conversation(storyId, m, lines) {
  await page.goto(`/new?story=${storyId}`)
  await expect(page.locator('.selection-summary')).toContainText(story)
  await expect(page.locator('.selection-summary')).toContainText(character)
  await button(m.create).click()
  await expect(page).toHaveURL(/\/conversations\/[^/]+$/)
  const replies = page.locator('.assistant-message')
  const messages = page.locator('.user-message p')
  const before = sends
  for (const [index, line] of lines.entries()) {
    await field(m.messageLabel).fill(line)
    await button(m.send).click()
    await expect(replies).toHaveCount(index + 1, { timeout: inferenceTimeout })
    await expect(replies.nth(index).locator('.dialogue p')).toContainText(/\S/)
    await page.reload()
    await expect(replies).toHaveCount(index + 1)
    await expect(messages).toHaveText(lines.slice(0, index + 1))
    assert.equal(sends, before + index + 1)
    await evidence.capture('Reopen the saved dialogue and check its order')
  }
}

let status = 'failed'
try {
  const preferences = await page.request.get('/api/v1/preferences')
  assert(preferences.ok())
  assert.equal((await preferences.json()).llm_profile, 'lmstudio-4b')
  await evidence.step('Generate, apply, edit and save a character draft', () =>
    draft(
      'characters',
      character,
      '青井という成人の時計職人。港の時計店で働く、静かで親切な人。',
    ),
  )
  let storyId
  await evidence.step(
    'Generate, apply, edit and save a story draft',
    async () => {
      storyId = await draft(
        'stories',
        story,
        '題名は「港の時計店」。雨の午後、旅人が時計店を訪れる静かな日常の物語。',
      )
    },
  )
  await evidence.step('Start, save and reopen a Japanese conversation', () =>
    conversation(storyId, ja, [
      'こんにちは。ここで少し雨宿りしてもいいですか？',
      'このお店で一番古い時計はどれですか？',
    ]),
  )
  await evidence.step(
    'Change the display and conversation language',
    async () => {
      await page.goto('/settings')
      await page
        .getByRole('combobox', { name: ja.language, exact: true })
        .selectOption('en')
      await expect(
        page.getByRole('heading', { name: en.preferences, level: 1 }),
      ).toBeVisible()
    },
  )
  await evidence.step('Start, save and reopen an English conversation', () =>
    conversation(storyId, en, [
      'May I wait here until the rain stops?',
      'Which clock in the shop is the oldest?',
    ]),
  )
  assert.equal(sends, 4)
  status = 'passed'
} catch (error) {
  await jsonFile(path.join(output, 'browser-failure.json'), diagnostic(error))
} finally {
  await evidence.capture('Final visible state')
  const result = await evidence.finish(status, 'passed')
  await jsonFile(path.join(output, 'browser-result.json'), result)
  await browser.close()
  if (status !== 'passed' || result.unexpected_events.length)
    process.exitCode = 1
}
