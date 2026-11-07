import { test as base } from '@playwright/test'
import { collectEvidence } from '../../../../tools/testing/web/fixtures.ts'
import { isolatedServer } from '../../../../tools/testing/web/server-fixture.ts'
import { captureJourney } from '../../../../tools/testing/web/journey.mjs'
import ja from '../../../../src/frontend/src/app/locales/ja.json' with { type: 'json' }

const test = base.extend({
  baseURL: async ({}, use, info) => isolatedServer(use, info),
  evidence: [collectEvidence, { auto: true }],
})

test('Capture the creation and conversation journey for human or AI review', async ({
  page,
  evidence,
}) => {
  await captureJourney(page, evidence)
  await evidence.step('Expand the conversation situation', async () => {
    await page.getByRole('button', { name: ja.showState, exact: true }).click()
    const panel = page.getByRole('dialog', { name: ja.currentState })
    await panel.getByText(ja.peopleDetails, { exact: true }).click()
    await panel.getByText(ja.actionHistory, { exact: true }).click()
    await panel.getByText(ja.fixedSettings, { exact: true }).click()
    await panel
      .getByRole('button', { name: ja.closeState, exact: true })
      .focus()
  })
  await page.keyboard.press('Escape')
  await evidence.step(
    'Expand the conversation menu and export settings',
    async () => {
      await page
        .getByRole('button', { name: ja.conversationMenu, exact: true })
        .click()
      const panel = page.getByRole('dialog', {
        name: ja.conversationMenu,
        exact: true,
      })
      await panel.getByText(ja.pinnedNotes, { exact: true }).click()
      await panel.getByText(ja.exportConversation, { exact: true }).click()
    },
  )
  await page.keyboard.press('Escape')
  await evidence.step('Open the drawing options', async () => {
    await page
      .getByRole('button', { name: ja.drawingSettings, exact: true })
      .click()
    await page
      .getByRole('combobox', { name: ja.viewpoint, exact: true })
      .waitFor()
  })
  await page.keyboard.press('Escape')
  await evidence.step('Open the turn actions', async () => {
    await page
      .getByRole('button', { name: ja.sceneActions, exact: true })
      .first()
      .click()
    await page.getByLabel(ja.sceneName, { exact: true }).waitFor()
  })
  await evidence.step('Read the latest illustration in the album', async () => {
    await page.goto('/images')
    await page.getByRole('heading', { name: ja.images, level: 1 }).waitFor()
    await page.locator('.album-days > details[open] img').first().waitFor()
  })
  await evidence.step('Expand the image connection settings', async () => {
    await page.goto('/settings')
    await page
      .locator('summary')
      .filter({ hasText: ja.connectionSettings })
      .click()
    await page
      .locator('summary')
      .filter({ hasText: ja.drawingSettings })
      .click()
    await page.getByRole('link', { name: ja.openComfy }).waitFor()
  })
})
