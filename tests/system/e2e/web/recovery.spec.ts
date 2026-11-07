import { test, expect, fulfillFault, abortFault } from './fixtures'
import ja from '../../../../src/frontend/src/app/locales/ja.json' with { type: 'json' }
import en from '../../../../src/frontend/src/app/locales/en.json' with { type: 'json' }
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'

const runFile = promisify(execFile)

for (const loseRequest of [false, true]) {
  test(`recovers a lost resume ${loseRequest ? 'request' : 'receipt'} without another model call`, async ({
    page,
    request,
    evidence,
  }, info) => {
    const workspace = info.outputPath('server/workspace')
    const prepare = await runFile(process.env.KOMAQUILL_WEB_PYTHON!, [
      '-B',
      '-m',
      'tests.system.support.conversations',
      workspace,
    ])
    const initial = JSON.parse(prepare.stdout)
    expect(initial.invocation_ids).toHaveLength(1)
    const operationPath = `/api/v1/operations/${initial.operation_id}`
    const conversationPath = `/api/v1/conversations/${initial.conversation_id}`
    const old = await (await request.get(operationPath)).json()
    expect(old).toMatchObject({
      state: 'interrupted',
      can_resume: true,
      resume_count: 0,
    })
    let resumeRequested = false
    let observedOld = false
    let releaseResume!: () => void
    const oldObservation = new Promise<void>((done) => {
      releaseResume = done
    })
    let submissions = 0
    let operationReads = 0
    let holdReceiptRead = false
    let notifyReceiptRead!: () => void
    let releaseReceiptRead!: () => void
    let notifyNextAttempt!: () => void
    let releaseNextAttempt!: () => void
    const receiptReadStarted = new Promise<void>((done) => {
      notifyReceiptRead = done
    })
    const receiptReadAllowed = new Promise<void>((done) => {
      releaseReceiptRead = done
    })
    const nextAttemptStarted = new Promise<void>((done) => {
      notifyNextAttempt = done
    })
    const nextAttemptAllowed = new Promise<void>((done) => {
      releaseNextAttempt = done
    })
    await page.route(`**${conversationPath}/operations`, (route) =>
      route.fulfill({ json: [old] }),
    )
    await page.route(`**${operationPath}`, async (route) => {
      operationReads++
      if (holdReceiptRead) {
        notifyReceiptRead()
        await receiptReadAllowed
        await route.fulfill({ json: old })
      } else if (resumeRequested && !observedOld) {
        observedOld = true
        await route.fulfill({ json: old })
        releaseResume()
      } else await route.continue()
    })
    await page.route(`**${operationPath}/resume?*`, async (route) => {
      submissions++
      await oldObservation
      if (loseRequest && submissions === 1) return abortFault(route, evidence)
      if (loseRequest && submissions === 2) {
        notifyNextAttempt()
        await nextAttemptAllowed
      }
      const response = await route.fetch()
      expect(response.status()).toBe(200)
      expect(await response.json()).toMatchObject({ resume_count: 1 })
      if (loseRequest) await route.fulfill({ response })
      else await abortFault(route, evidence)
    })
    try {
      await page.goto(`/conversations/${initial.conversation_id}`)
      const input = page.getByRole('textbox', { name: ja.messageLabel })
      await input.fill('Next unsent draft')
      resumeRequested = true
      await page
        .getByRole('button', { name: ja.resumeOperation, exact: true })
        .click()
      if (loseRequest) {
        const resume = page.getByRole('button', {
          name: ja.resumeOperation,
          exact: true,
        })
        await expect(resume).toBeEnabled()
        expect(submissions).toBe(1)
        holdReceiptRead = true
        await page
          .getByRole('button', { name: ja.retryRequest, exact: true })
          .click()
        await receiptReadStarted
        holdReceiptRead = false
        expect(submissions).toBe(1)
        await resume.click()
        await nextAttemptStarted
        const readsBeforeRelease = operationReads
        releaseReceiptRead()
        await expect
          .poll(() => operationReads)
          .toBeGreaterThan(readsBeforeRelease)
        await expect(resume).toBeDisabled()
        await expect(
          page.getByRole('button', { name: ja.send, exact: true }),
        ).toBeDisabled()
        await evidence.capture(
          'An older receipt read keeps the next resume pending and preserves its draft',
        )
        releaseNextAttempt()
      }
      await expect(page.locator('.assistant-message')).toHaveCount(1)
      await expect(input).toHaveValue('Next unsent draft')
      await expect(
        page.getByRole('button', { name: ja.send, exact: true }),
      ).toBeEnabled()
      await expect(
        page.getByRole('button', { name: ja.resumeOperation, exact: true }),
      ).toHaveCount(0)
      expect(observedOld).toBe(true)
      expect(submissions).toBe(loseRequest ? 2 : 1)
      const inspect = await runFile(process.env.KOMAQUILL_WEB_PYTHON!, [
        '-B',
        '-m',
        'tests.system.support.conversations',
        workspace,
        initial.operation_id,
      ])
      const saved = JSON.parse(inspect.stdout)
      expect(saved).toMatchObject({
        resume_count: 1,
        turn_count: 1,
        action_count: 1,
        place: 'Garden',
      })
      expect(saved.invocation_ids).toEqual(initial.invocation_ids)
      await evidence.capture(
        'Resumed the same operation after a lost receipt; one move and no new inference',
      )
    } finally {
      releaseReceiptRead()
      releaseNextAttempt()
    }
  })
}

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
      return fulfillFault(route, evidence, {
        status: 404,
        json: { code: 'request_not_found' },
      })
    return route.continue()
  })
  await page.route('**/api/v1/images', async (route) => {
    if (route.request().method() !== 'POST') return route.continue()
    submitted.push(route.request().postDataJSON().request_id)
    const response = await route.fetch()
    expect(response.status()).toBe(202)
    await abortFault(route, evidence)
  })
  page.on('request', (req) => {
    if (req.method() === 'POST' && req.url().endsWith('/collect')) collections++
  })
  await page.goto('/images')
  await page.getByRole('button', { name: ja.openDrawingForm }).click()
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
        await abortFault(route, evidence)
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
    await abortFault(route, evidence)
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
      return submitted.length === 1
        ? abortFault(route, evidence)
        : route.continue()
    },
  )
  evidence.expectFailure({
    kind: 'response',
    route: '/api/v1/operations/:value',
    method: 'GET',
    status: 404,
    maximum: 4,
  })
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
