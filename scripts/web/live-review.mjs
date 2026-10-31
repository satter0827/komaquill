import path from 'node:path'
import { readFile } from 'node:fs/promises'
import { nodeEnvironment } from '../../bootstrap/node-environment.mjs'
import { Evidence, contained, jsonFile } from './evidence.mjs'
import { captureJourney } from './journey.mjs'

Object.assign(process.env, nodeEnvironment())
const { chromium } = await import('@playwright/test')
const root = process.cwd()
const directory = contained(
  path.join(root, '.artifacts/records/reports'),
  process.argv[2],
)
const ja = JSON.parse(
  await readFile(
    new URL('../../src/frontend/src/app/locales/ja.json', import.meta.url),
  ),
)
const en = JSON.parse(
  await readFile(
    new URL('../../src/frontend/src/app/locales/en.json', import.meta.url),
  ),
)
const browser = await chromium.launch()
const page = await browser.newPage({
  baseURL: 'http://127.0.0.1:8767',
  viewport: { width: 1440, height: 960 },
})
page.setDefaultTimeout(30000)
const evidence = new Evidence(page, path.join(directory, 'browser'), {
  mode: 'exploration',
  real_llm: true,
  real_images: true,
})
let status = 'failed'
const step = evidence.step.bind(evidence)
evidence.step = async (label, action) => {
  console.log(JSON.stringify({ phase: 'started', step: label }))
  const result = await step(label, action)
  console.log(JSON.stringify({ phase: 'captured', step: label }))
  return result
}
try {
  await captureJourney(page, evidence, { live: true })
  await page.setViewportSize({ width: 1440, height: 960 })
  const conversation = page.url()
  await evidence.step(
    'Continue in English with a changed character state',
    async () => {
      await page.goto('/settings')
      await page
        .getByRole('combobox', { name: ja.language, exact: true })
        .selectOption('en')
      await page.goto(conversation)
      await page
        .getByRole('textbox', { name: en.messageLabel })
        .fill(
          '上着を脱いで青いシャツになり、庭の椅子に座ってください。あなたの年齢と仕事も教えてください。',
        )
      await page.getByRole('button', { name: en.send, exact: true }).click()
      await page
        .locator('.assistant-message')
        .nth(1)
        .waitFor({ timeout: 300000 })
    },
  )
  await evidence.step(
    'Continue in Japanese and draw the current state explicitly',
    async () => {
      await page.goto('/settings')
      await page
        .getByRole('combobox', { name: en.language, exact: true })
        .selectOption('ja')
      await page.goto(conversation)
      await page
        .getByRole('textbox', { name: ja.messageLabel })
        .fill(
          'Stay in the garden in your blue shirt. Tell me about the garden.',
        )
      await page.getByRole('button', { name: ja.send, exact: true }).click()
      await page
        .locator('.assistant-message')
        .nth(2)
        .waitFor({ timeout: 300000 })
      await page
        .getByRole('button', { name: ja.drawScene, exact: true })
        .click()
      await page
        .locator('.turn .image-result img')
        .nth(1)
        .waitFor({ timeout: 660000 })
    },
  )
  await evidence.step(
    'Generate a standalone image using Japanese input',
    async () => {
      await page.goto('/images')
      await page
        .getByLabel(ja.imagePrompt, { exact: true })
        .fill('雨の港町の古書店。窓辺に木の机と青い封筒が一つ。人物はいない。')
      const receipt = page.waitForResponse(
        (response) =>
          new URL(response.url()).pathname === '/api/v1/images' &&
          response.request().method() === 'POST',
      )
      await page.getByRole('button', { name: ja.generate, exact: true }).click()
      const created = await (await receipt).json()
      await page
        .locator(`img[src="/api/v1/images/${created.run_id}/files/0"]`)
        .waitFor({ timeout: 660000 })
    },
  )
  status = 'passed'
} finally {
  await evidence.finish(status, 'passed')
  await jsonFile(path.join(directory, 'browser-index.json'), {
    format_version: 1,
    scenario: 'browser/scenario.json',
    status,
    quality_review: 'not_reviewed',
  })
  await browser.close()
}
if (status !== 'passed') process.exitCode = 1
