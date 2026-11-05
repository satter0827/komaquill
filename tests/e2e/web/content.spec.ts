import { test, expect } from './fixtures'
import { randomUUID } from 'node:crypto'
import ja from '../../../src/frontend/src/app/locales/ja.json' with { type: 'json' }

test('uses a fixed policy without classification settings or endpoints', async ({
  page,
  request,
  evidence,
}) => {
  await page.goto('/settings')
  await expect(page.getByRole('checkbox', { name: /NSFW/i })).toHaveCount(0)
  const preferences = await (await request.get('/api/v1/preferences')).json()
  expect(preferences).not.toHaveProperty('nsfw_enabled')
  expect((await request.get('/api/v1/content')).status()).toBe(404)
  const scene = await (
    await request.post('/api/v1/conversations', {
      data: {
        title: `Saved scene ${randomUUID().slice(0, 8)}`,
        locale: 'en',
        llm_profile: 'fake',
      },
    })
  ).json()
  await page.goto(`/conversations/${scene.id}`)
  await expect(
    page.getByRole('heading', { name: scene.definition.title }),
  ).toBeVisible()
  await evidence.capture(
    'Saved content remains visible without classification controls',
  )
})

test('retains drafts when hidden and branches from saved scene notes without generation', async ({
  page,
  request,
  evidence,
}) => {
  const scene = await (
    await request.post('/api/v1/conversations', {
      data: { title: 'Scene tools', locale: 'en', llm_profile: 'fake' },
    })
  ).json()
  await page.goto(`/conversations/${scene.id}`)
  await page
    .getByRole('button', { name: ja.conversationMenu, exact: true })
    .click()
  await page.getByText(ja.pinnedNotes, { exact: true }).click()
  await page.getByLabel(ja.notesDescription).fill('Keep the blue envelope.')
  await page.getByRole('button', { name: ja.save, exact: true }).click()
  await expect(
    page.getByRole('button', { name: ja.save, exact: true }),
  ).toBeEnabled()
  const response = await request.post(
    `/api/v1/conversations/${scene.id}/turns`,
    {
      data: {
        operation_id: randomUUID(),
        expected_revision: (
          await (await request.get(`/api/v1/conversations/${scene.id}`)).json()
        ).revision,
        text: 'Hello',
        locale: 'en',
      },
    },
  )
  expect(response.ok()).toBe(true)
  await page.reload()
  await expect(page.locator('article.turn')).toHaveCount(1)
  const input = page.getByRole('textbox', { name: ja.messageLabel })
  await input.fill('Unsent draft')
  await page.getByRole('button', { name: ja.hideScreen }).click()
  await expect(input).not.toBeVisible()
  await page.getByRole('button', { name: ja.showScreen }).click()
  await expect(input).toHaveValue('Unsent draft')
  await page
    .locator('article.turn')
    .getByRole('button', { name: ja.sceneActions })
    .click()
  await page.getByLabel(ja.sceneName).fill('Saved branch')
  await page.getByRole('button', { name: ja.saveScene, exact: true }).click()
  await page
    .getByRole('button', { name: ja.conversationMenu, exact: true })
    .click()
  await expect(page.getByText('Saved branch', { exact: true })).toBeVisible()
  await page
    .getByRole('button', { name: ja.conversationMenu, exact: true })
    .click()
  await page
    .locator('article.turn')
    .getByRole('button', { name: ja.sceneActions })
    .click()
  await page.getByRole('button', { name: ja.forkScene, exact: true }).click()
  await expect(page).not.toHaveURL(new RegExp(scene.id))
  const branchId = page.url().split('/').at(-1)!
  const branch = await (
    await request.get(`/api/v1/conversations/${branchId}`)
  ).json()
  expect(branch.source_conversation_id).toBe(scene.id)
  expect(branch.notes).toEqual(['Keep the blue envelope.'])
  expect(branch.turn_count).toBe(1)
  const exported = await request.get(
    `/api/v1/conversations/${branchId}/export?include_images=false&include_conditions=false`,
  )
  expect(exported.ok()).toBe(true)
  expect(exported.headers()['cache-control']).toBe('no-store')
  await evidence.capture(
    'Branched scene retains the confirmed notes and history',
  )
})
