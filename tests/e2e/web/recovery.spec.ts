import { test, expect } from './fixtures'
import ja from '../../../src/frontend/src/app/locales/ja.json' with { type: 'json' }
import en from '../../../src/frontend/src/app/locales/en.json' with { type: 'json' }

for (const [locale, messages] of [
  ['ja', ja],
  ['en', en],
] as const) {
  test(`${locale}: recovers an accepted turn after losing the response without resending`, async ({
    page,
    request,
    evidence,
  }) => {
    if (locale === 'en') {
      const saved = await (await request.get('/api/v1/preferences')).json()
      await request.put('/api/v1/preferences', {
        data: { ...saved, locale },
      })
    }
    await page.goto('/new')
    await page
      .getByRole('button', { name: messages.create, exact: true })
      .click()
    await expect(page).toHaveURL(/\/conversations\//)
    const identity = new URL(page.url()).pathname.split('/').at(-1)!
    const input = page.getByRole('textbox', { name: messages.messageLabel })
    const send = page.getByRole('button', { name: messages.send, exact: true })
    const nextMessage =
      locale === 'ja'
        ? 'Hello from the browser fixture.'
        : 'The next message is still being written.'
    let sent = 0
    await page.route(
      `**/api/v1/conversations/${identity}/turns`,
      async (route) => {
        if (route.request().method() !== 'POST') return route.continue()
        sent += 1
        if (sent !== 1) return route.continue()
        const response = await route.fetch()
        expect(response.ok()).toBe(true)
        await input.clear()
        await input.fill(nextMessage)
        await route.abort('failed')
      },
    )
    await input.fill('Hello from the browser fixture.')
    await send.click()
    await expect(page.locator('.assistant-message')).toHaveCount(1)
    await expect(input).toHaveValue(nextMessage)
    await expect(send).toBeEnabled()
    await expect(
      page.getByRole('button', { name: messages.retryRequest }),
    ).toHaveCount(0)
    expect(sent).toBe(1)
    await evidence.capture(
      'Accepted turn recovered by reading its operation; next input preserved',
    )
    await send.click()
    await expect(page.locator('.assistant-message')).toHaveCount(2)
    expect(sent).toBe(2)
    expect(
      (
        await (
          await request.get(`/api/v1/conversations/${identity}/turns`)
        ).json()
      ).items,
    ).toHaveLength(2)
  })
}

test('recovers accepted drawings after losing their responses and allows a new drawing', async ({
  page,
  request,
  evidence,
}) => {
  await page.goto('/new')
  await page.getByRole('button', { name: ja.create, exact: true }).click()
  await expect(page).toHaveURL(/\/conversations\//)
  const identity = new URL(page.url()).pathname.split('/').at(-1)!
  const input = page.getByRole('textbox', { name: ja.messageLabel })
  const draw = page.getByRole('button', { name: ja.drawScene, exact: true })
  const operations: string[] = []
  await page.route('**/api/v1/creation-jobs', async (route) => {
    operations.push(route.request().postDataJSON().operation_id)
    const response = await route.fetch()
    expect(response.ok()).toBe(true)
    await route.abort('failed')
  })
  await input.fill('Unsent text survives drawing recovery.')
  for (let count = 1; count <= 2; count += 1) {
    await draw.click()
    await expect(page.locator('.turns .image-result img')).toHaveCount(count)
    await expect(
      page.locator('.turns .image-result img').last(),
    ).toHaveJSProperty('naturalWidth', 8)
    await expect(draw).toBeEnabled()
    await expect(
      page.getByRole('button', { name: ja.retryRequest }),
    ).toHaveCount(0)
    await expect(input).toHaveValue('Unsent text survives drawing recovery.')
    expect(operations).toHaveLength(count)
    expect(new Set(operations).size).toBe(count)
  }
  expect(
    await (
      await request.get(`/api/v1/conversations/${identity}/images`)
    ).json(),
  ).toHaveLength(2)
  await page.reload()
  await expect(page.locator('.turns .image-result img')).toHaveCount(2)
  await expect(draw).toBeEnabled()
  expect(operations).toHaveLength(2)
  await evidence.capture(
    'Two distinct drawings recovered without duplicate submissions',
  )
})

test('checks an unconfirmed turn without resending its operation', async ({
  page,
  evidence,
}) => {
  await page.goto('/new')
  await page.getByRole('button', { name: ja.create, exact: true }).click()
  await expect(page).toHaveURL(/\/conversations\//)
  const identity = new URL(page.url()).pathname.split('/').at(-1)!
  const submitted: string[] = []
  let observations = 0
  page.on('request', (request) => {
    if (request.url().includes('/api/v1/operations/')) observations += 1
  })
  await page.route(
    `**/api/v1/conversations/${identity}/turns`,
    async (route) => {
      if (route.request().method() !== 'POST') return route.continue()
      submitted.push(route.request().postDataJSON().operation_id)
      return submitted.length === 1 ? route.abort('failed') : route.continue()
    },
  )
  const input = page.getByRole('textbox', { name: ja.messageLabel })
  await input.fill('Hello from the browser fixture.')
  await page.getByRole('button', { name: ja.send, exact: true }).click()
  const retry = page.getByRole('button', { name: ja.retryRequest })
  await expect(retry).toBeVisible()
  await expect(page.getByText(ja.receiptUnknown, { exact: true })).toBeVisible()
  await expect(page.getByText(ja.pending, { exact: true })).toHaveCount(0)
  await expect.poll(() => observations).toBeGreaterThan(0)
  expect(submitted).toHaveLength(1)
  await expect(input).toHaveValue('Hello from the browser fixture.')
  await evidence.capture(
    'Unconfirmed request is observed without automatic resubmission',
  )
  const before = observations
  await retry.click()
  await expect.poll(() => observations).toBeGreaterThan(before)
  expect(submitted).toHaveLength(1)
  await expect(input).toHaveValue('Hello from the browser fixture.')
  await expect(
    page.getByRole('button', { name: ja.send, exact: true }),
  ).toBeDisabled()
  await expect(retry).toBeVisible()
})
