import { test, expect } from './fixtures'
import ja from '../../../src/frontend/src/app/locales/ja.json' with { type: 'json' }

test('REVIEW-DEFAULT-DIALOGUE: new conversations use a model by default', async ({
  page,
  request,
}) => {
  await page.goto('/new')
  await page.getByRole('button', { name: ja.create, exact: true }).click()
  await expect(page).toHaveURL(/\/conversations\//)
  const identity = new URL(page.url()).pathname.split('/').at(-1)
  const conversation = await (
    await request.get(`/api/v1/conversations/${identity}`)
  ).json()
  expect(conversation.definition.llm_profile).not.toBe('fake')
})

test('REVIEW-CONVERSATION-CONTROLS: conversation settings need no per-message switches', async ({
  page,
}) => {
  await page.goto('/new')
  await page.getByRole('button', { name: ja.create, exact: true }).click()
  await expect(page.locator('.composer-area')).toBeVisible()
  await expect(page.locator('.composer-area input[type=checkbox]')).toHaveCount(
    0,
  )
})

test('REVIEW-SINGLE-PLAYER: Your profile opens directly for editing', async ({
  page,
  request,
}) => {
  const player = await (await request.get('/api/v1/player')).json()
  await page.goto('/player')
  await expect(
    page.getByRole('heading', { name: player.name, exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole('textbox', { name: ja.name, exact: true }),
  ).toBeVisible()
})

for (const route of ['/player', '/characters/new']) {
  test(`REVIEW-PERSON-ATTRIBUTES: age and gender can be entered at ${route}`, async ({
    page,
  }) => {
    await page.goto(route)
    await expect(page.locator('main h1')).toBeVisible()
    await expect(page.getByLabel(/年齢/)).toBeVisible()
    await expect(page.getByLabel(/性別/)).toBeVisible()
  })
}
