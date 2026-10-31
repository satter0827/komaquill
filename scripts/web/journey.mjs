import { randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'

const ja = JSON.parse(
  readFileSync(
    new URL('../../src/frontend/src/app/locales/ja.json', import.meta.url),
    'utf8',
  ),
)

export async function captureJourney(page, evidence, { live = false } = {}) {
  const actor = (live ? 'ミラ-' : 'Review actor-') + randomUUID().slice(0, 8)
  const story =
    (live ? '雨の古書店-' : 'Review story-') + randomUUID().slice(0, 8)
  await evidence.step('Open the library', async () => {
    await page.goto('/')
    await page.getByRole('heading', { name: ja.emptyTitle }).waitFor()
  })
  await evidence.step('Inspect Your profile', async () => {
    await page.getByRole('link', { name: ja.players, exact: true }).click()
    await page.getByLabel(ja.name, { exact: true }).waitFor()
    if (live) {
      await page.getByLabel(ja.name, { exact: true }).fill('灯')
      await page.getByLabel(ja.address, { exact: true }).fill('灯さん')
      await page.getByLabel(ja.age, { exact: true }).fill('28')
      await page.getByLabel(ja.gender, { exact: true }).fill('女性')
      await page
        .getByRole('textbox', { name: ja.role, exact: true })
        .fill('旅人')
      await page.getByRole('button', { name: ja.save, exact: true }).click()
      await page.getByRole('status').filter({ hasText: ja.saved }).waitFor()
    }
  })
  await evidence.step('Create a character and its portrait', async () => {
    await page.goto('/characters/new')
    await page.getByLabel(ja.name, { exact: true }).fill(actor)
    await page
      .getByLabel(ja.appearance, { exact: true })
      .fill(
        live
          ? '32歳の成人女性。短い赤茶色の髪。緑のコート。'
          : 'An adult with short red hair and a green coat.',
      )
    if (live) {
      await page.getByLabel(ja.age, { exact: true }).fill('32')
      await page.getByLabel(ja.gender, { exact: true }).fill('女性')
      await page.getByLabel(ja.role, { exact: true }).fill('港町の古書店の店主')
    }
    await page.getByRole('button', { name: ja.save, exact: true }).click()
    await page
      .locator('.cover-preview img')
      .waitFor({ timeout: live ? 660000 : 30000 })
  })
  await evidence.step(
    'Save a story and its recommended counterpart',
    async () => {
      await page.goto('/stories/new')
      await page.getByLabel(ja.name, { exact: true }).fill(story)
      await page
        .getByLabel(ja.opening, { exact: true })
        .fill(
          live
            ? '雨の午後、港町の古書店に旅人が入ってきた。店主がカウンターの前で迎える。'
            : 'A visitor arrives at a quiet bookshop.',
        )
      await page.getByLabel(ja.defaultCharacter).selectOption({ label: actor })
      await page.getByRole('button', { name: ja.save, exact: true }).click()
      await page.getByRole('status').filter({ hasText: ja.saved }).waitFor()
      await page
        .locator('.cover-preview.wide img')
        .waitFor({ timeout: live ? 660000 : 30000 })
    },
  )
  await evidence.step('Choose the saved story', async () => {
    await page.getByRole('link', { name: ja.play, exact: true }).click()
    await page
      .locator('.preset-card')
      .filter({ has: page.getByRole('heading', { name: story, exact: true }) })
      .getByRole('link', { name: ja.use + ' →', exact: true })
      .click()
  })
  await evidence.step('Start the conversation', async () => {
    if (await page.getByLabel(ja.profile).count())
      await page.getByLabel(ja.profile).selectOption('lmstudio-4b')
    await page.getByRole('button', { name: ja.create, exact: true }).click()
    await page.getByRole('textbox', { name: ja.messageLabel }).waitFor()
  })
  await evidence.step(
    'Send a message and display the collected image',
    async () => {
      await page
        .getByRole('textbox', { name: ja.messageLabel })
        .fill(
          live
            ? 'Hello. Please tell me about yourself and this bookshop.'
            : 'Hello from the isolated review fixture.',
        )
      await page.getByRole('button', { name: ja.send, exact: true }).click()
      await page
        .locator('.scene-narration')
        .waitFor({ timeout: live ? 300000 : 30000 })
      await page
        .getByRole('button', { name: ja.drawScene, exact: true })
        .click()
      await page
        .locator('.turn .image-result img')
        .waitFor({ timeout: live ? 660000 : 30000 })
    },
  )
  await evidence.step('Reopen the saved conversation', async () => {
    await page.reload()
    await page.locator('.turn .image-result img').waitFor()
  })
  await evidence.step(
    'Inspect the same conversation on a narrow screen',
    async () => {
      await page.setViewportSize({ width: 390, height: 844 })
      await page.getByRole('textbox', { name: ja.messageLabel }).waitFor()
    },
  )
}
