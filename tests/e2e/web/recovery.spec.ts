import { test, expect } from './fixtures'
import ja from '../../../src/frontend/src/app/locales/ja.json' with { type: 'json' }
import en from '../../../src/frontend/src/app/locales/en.json' with { type: 'json' }

test('confirms an album receipt across navigation without submitting again', async ({
  page,
  request,
  evidence,
}) => {
  let receiptAvailable = false
  const submitted: string[] = []
  let collections = 0
  await page.route('**/api/v1/runtime', (route) =>
    route.fulfill({ json: { connection: 'disconnected', pending: [] } }),
  )
  await page.route('**/api/v1/images/*', async (route) => {
    if (!receiptAvailable)
      return route.fulfill({ status: 404, json: { code: 'request_not_found' } })
    return route.continue()
  })
  await page.route('**/api/v1/images', async (route) => {
    if (route.request().method() !== 'POST') return route.continue()
    submitted.push(route.request().postDataJSON().request_id)
    const response = await route.fetch()
    expect(response.status()).toBe(202)
    await route.abort('failed')
  })
  page.on('request', (req) => {
    if (req.method() === 'POST' && req.url().endsWith('/collect')) collections++
  })
  await page.goto('/images')
  await page.getByRole('button', { name: ja.generate, exact: true }).click()
  await page.getByLabel(ja.imagePrompt).fill('A quiet bookshop')
  await page
    .getByRole('button', { name: ja.drawRequestedScene, exact: true })
    .click()
  await expect(page.getByText(ja.receiptUnknown, { exact: true })).toBeVisible()
  await evidence.capture(
    'Lost album receipt retains the original request before navigation',
  )
  await page.getByRole('link', { name: ja.play, exact: true }).click()
  receiptAvailable = true
  await expect
    .poll(
      async () =>
        (await (await request.get(`/api/v1/images/${submitted[0]}`)).json())
          .state,
    )
    .toBe('saved')
  expect(submitted).toHaveLength(1)
  expect(collections).toBe(1)
  await page.getByRole('link', { name: ja.images, exact: true }).click()
  await page.locator('.album-days summary').first().click()
  await expect(page.locator(`#image-${submitted[0]} img`)).toHaveJSProperty(
    'naturalWidth',
    8,
  )
  expect(submitted).toHaveLength(1)
  await evidence.capture(
    'The same request was saved while another page was open',
  )
})

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
