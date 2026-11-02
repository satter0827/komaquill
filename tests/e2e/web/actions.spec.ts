import { test, expect } from './fixtures'
import ja from '../../../src/frontend/src/app/locales/ja.json' with { type: 'json' }
import en from '../../../src/frontend/src/app/locales/en.json' with { type: 'json' }

for (const locale of ['ja', 'en'] as const) {
  test(`${locale}: confirms a player action and preserves it after reload`, async ({
    page,
    request,
    evidence,
  }) => {
    const m = locale === 'ja' ? ja : en
    const preferences = await (await request.get('/api/v1/preferences')).json()
    await request.put('/api/v1/preferences', {
      data: { ...preferences, locale },
    })
    await page.goto('/new')
    await page.getByRole('button', { name: m.create, exact: true }).click()
    await expect(page).toHaveURL(/\/conversations\//)
    const identity = page.url().split('/').at(-1)!
    await page.getByLabel(m.destination, { exact: true }).fill('Beach')
    await page
      .getByRole('button', { name: m.confirmAction, exact: true })
      .click()
    await expect(page.locator('.scene-narration')).toHaveCount(1)
    const saved = await (
      await request.get(`/api/v1/conversations/${identity}`)
    ).json()
    expect(saved.state.place).toBe('Beach')
    expect(saved.state.character_place).not.toBe('Beach')
    expect(saved.actions).toHaveLength(1)
    await evidence.capture(
      'Confirmed player movement with an independent companion location',
    )
    await page.reload()
    await expect(page.getByText('Beach', { exact: true }).first()).toBeVisible()
    const resumed = await (
      await request.get(`/api/v1/conversations/${identity}`)
    ).json()
    expect(resumed.actions).toHaveLength(1)
    expect(resumed.state.place).toBe('Beach')
  })
}

test('a chat action remains a proposal until the player confirms it', async ({
  page,
  request,
  evidence,
}) => {
  const preferences = await (await request.get('/api/v1/preferences')).json()
  await request.put('/api/v1/preferences', {
    data: { ...preferences, locale: 'ja' },
  })
  await page.goto('/new')
  await page.getByRole('button', { name: ja.create, exact: true }).click()
  await expect(page).toHaveURL(/\/conversations\//)
  const identity = page.url().split('/').at(-1)!
  await page
    .getByRole('textbox', { name: ja.messageLabel })
    .fill('I head to the beach.')
  await page.getByRole('button', { name: ja.send, exact: true }).click()
  const proposal = page.locator('.action-proposal')
  await expect(proposal).toHaveCount(1)
  const pending = await (
    await request.get(`/api/v1/conversations/${identity}`)
  ).json()
  expect(pending.state.place).not.toBe('Beach')
  expect(pending.actions).toHaveLength(0)
  await evidence.capture(
    'An unconfirmed action leaves the current scene unchanged',
  )
  await proposal
    .getByRole('button', { name: ja.confirmAction, exact: true })
    .click()
  await expect(proposal).toHaveCount(0)
  await expect(page.locator('.scene-narration')).toHaveCount(1)
  const confirmed = await (
    await request.get(`/api/v1/conversations/${identity}`)
  ).json()
  expect(confirmed.state.place).toBe('Beach')
  expect(confirmed.actions).toHaveLength(1)
  expect(confirmed.proposals[0].state).toBe('applied')
  await evidence.capture(
    'Confirmation applies the action once before the next reaction',
  )
})
