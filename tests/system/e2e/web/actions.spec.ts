import { test, expect } from './fixtures'
import ja from '../../../../src/frontend/src/app/locales/ja.json' with { type: 'json' }
import en from '../../../../src/frontend/src/app/locales/en.json' with { type: 'json' }

const REACTION_COMPLETION_TIMEOUT_MS = 15_000

for (const locale of ['ja', 'en'] as const) {
  test(`${locale}: reviews one person's appearance without changing the other`, async ({
    page,
    request,
    evidence,
    auditAccessibility,
  }) => {
    const m = locale === 'ja' ? ja : en
    const preferences = await (await request.get('/api/v1/preferences')).json()
    await request.put('/api/v1/preferences', {
      data: { ...preferences, locale },
    })
    const created = await request.post('/api/v1/conversations', {
      data: {
        title: 'Appearance review',
        locale,
        llm_profile: 'fake',
        user: {
          name: 'Player',
          appearance: 'Brown hair',
          initial_clothing: 'Red coat',
        },
        character: {
          name: 'Companion',
          appearance: 'Silver hair and apron',
          visual_review_required: true,
        },
      },
    })
    expect(created.ok()).toBe(true)
    const original = await created.json()
    await page.goto(`/conversations/${original.id}`)
    const appearance = page.getByLabel(m.appearance, { exact: true })
    await expect(appearance).toHaveCount(1)
    await appearance.fill('Silver hair')
    await page
      .getByRole('textbox', { name: m.messageLabel })
      .fill('Keep my message')
    await expect(
      page.getByRole('button', { name: m.send, exact: true }),
    ).toBeDisabled()
    await auditAccessibility()
    await evidence.capture(
      'Only the unconfirmed companion needs appearance review',
    )
    const saved = page.waitForResponse(
      (response) =>
        response.request().method() === 'PUT' &&
        response.url().endsWith(`/api/v1/conversations/${original.id}/visuals`),
    )
    await page.getByRole('button', { name: m.confirmAppearance }).focus()
    await page.keyboard.press('Enter')
    expect((await saved).ok()).toBe(true)
    await page.getByText(m.addAction, { exact: true }).click()
    await expect(page.getByLabel(m.destination, { exact: true })).toBeEnabled()
    await expect(
      page.getByRole('textbox', { name: m.messageLabel }),
    ).toHaveValue('Keep my message')
    const confirmed = await (
      await request.get(`/api/v1/conversations/${original.id}`)
    ).json()
    expect(confirmed.definition.user).toEqual(original.definition.user)
    expect(confirmed.state.player).toEqual(original.state.player)
    expect(confirmed.definition.character.appearance).toBe('Silver hair')
    expect(confirmed.definition.character.initial_clothing).toBeNull()
    expect(confirmed.state.character.clothing).toBeNull()
    await page.reload()
    await page.getByText(m.addAction, { exact: true }).click()
    await expect(page.getByLabel(m.destination, { exact: true })).toBeEnabled()
    await expect(appearance).toHaveCount(0)
    await evidence.capture(
      'Review survived reload and retained the other person and unspecified clothes',
    )
  })
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
    await page.getByText(m.addAction, { exact: true }).click()
    await page.getByLabel(m.destination, { exact: true }).fill('Beach')
    const receipt = page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        response.url().endsWith(`/api/v1/conversations/${identity}/turns`),
    )
    await page.getByRole('button', { name: m.queueAction, exact: true }).click()
    await page
      .getByRole('textbox', { name: m.messageLabel })
      .fill(locale === 'ja' ? '海へ行きます。' : 'I am leaving for the beach.')
    await page.getByRole('button', { name: m.send, exact: true }).click()
    const accepted = await receipt
    expect(accepted.status()).toBe(202)
    const action = await accepted.json()
    await expect
      .poll(
        async () =>
          (await (await request.get(`/api/v1/operations/${action.id}`)).json())
            .state,
        { timeout: REACTION_COMPLETION_TIMEOUT_MS },
      )
      .toBe('complete')
    await expect(page.locator('.scene-narration')).toHaveCount(1)
    const saved = await (
      await request.get(`/api/v1/conversations/${identity}`)
    ).json()
    expect(saved.state.place).toBe('Beach')
    expect(saved.state.character_place).not.toBe('Beach')
    expect(
      await (
        await request.get(`/api/v1/conversations/${identity}/actions`)
      ).json(),
    ).toHaveLength(1)
    await evidence.capture(
      'Confirmed player movement with an independent companion location',
    )
    await page.reload()
    await page.getByRole('button', { name: m.showState, exact: true }).click()
    await expect(page.getByText('Beach', { exact: true }).first()).toBeVisible()
    const resumed = await (
      await request.get(`/api/v1/conversations/${identity}`)
    ).json()
    expect(
      await (
        await request.get(`/api/v1/conversations/${identity}/actions`)
      ).json(),
    ).toHaveLength(1)
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
  await page.getByText(ja.addAction, { exact: true }).click()
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
  const receipt = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      response.url().endsWith(`/api/v1/conversations/${identity}/turns`),
  )
  await proposal
    .getByRole('button', { name: ja.queueAction, exact: true })
    .click()
  await page
    .getByRole('textbox', { name: ja.messageLabel })
    .fill('海へ行きます。')
  await page.getByRole('button', { name: ja.send, exact: true }).click()
  const accepted = await receipt
  expect(accepted.status()).toBe(202)
  const action = await accepted.json()
  await expect
    .poll(
      async () =>
        (await (await request.get(`/api/v1/operations/${action.id}`)).json())
          .state,
      { timeout: REACTION_COMPLETION_TIMEOUT_MS },
    )
    .toBe('complete')
  await expect(proposal).toHaveCount(0)
  await expect(page.locator('.scene-narration')).toHaveCount(1)
  const confirmed = await (
    await request.get(`/api/v1/conversations/${identity}`)
  ).json()
  expect(confirmed.state.place).toBe('Beach')
  expect(
    await (
      await request.get(`/api/v1/conversations/${identity}/actions`)
    ).json(),
  ).toHaveLength(1)
  expect(confirmed.proposals).toHaveLength(0)
  const recorded = await (
    await request.get(`/api/v1/conversations/${identity}/actions`)
  ).json()
  expect(recorded[0].request.proposal_id).toBe(pending.proposals[0].id)
  await evidence.capture(
    'Confirmation applies the action once before the next reaction',
  )
})

test('edits combined preparation on a narrow screen without losing fields or object identities', async ({
  page,
  request,
  evidence,
}) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const scene = await (
    await request.post('/api/v1/conversations', {
      data: { title: 'Combined preparation', llm_profile: 'fake' },
    })
  ).json()
  const action = {
    kind: 'prepare',
    clothing: 'Blue coat',
    held_objects: [
      { id: 'book', name: 'Book' },
      { id: 'key', name: 'Key' },
    ],
  }
  await page.route(`**/api/v1/conversations/${scene.id}`, (route) =>
    route.fulfill({
      json: {
        ...scene,
        proposals: [
          {
            id: 'proposal',
            turn_id: 'source',
            state: 'pending',
            revision: scene.revision,
            action,
          },
        ],
      },
    }),
  )
  await page.goto(`/conversations/${scene.id}`)
  await page.getByText(ja.addAction, { exact: true }).click()
  await page
    .locator('.action-proposal')
    .getByRole('button', { name: ja.queueAction, exact: true })
    .click()
  await page.getByRole('button', { name: ja.editQueuedAction }).click()
  await expect(page.getByLabel(ja.chooseAction)).toBeFocused()
  await page.getByLabel(ja.chooseAction).selectOption('held_objects')
  const objects = page.getByRole('textbox', { name: ja.heldObjectsInput })
  await expect(objects).toHaveValue('Book\nKey')
  await objects.fill('Book\nKey\nMap')
  await expect(
    page.getByText(ja.additionalActionChanges, { exact: true }),
  ).toBeVisible()
  await evidence.capture(
    'Multiline objects and retained clothing in the action editor',
  )
  await page
    .locator('.player-actions form')
    .getByRole('button', { name: ja.queueAction, exact: true })
    .click()
  await expect(
    page.getByRole('textbox', { name: ja.messageLabel }),
  ).toBeFocused()
  await page.getByRole('button', { name: ja.editQueuedAction }).click()
  await page.getByLabel(ja.preparationValue).fill('Red coat')
  await page
    .locator('.player-actions form')
    .getByRole('button', { name: ja.queueAction, exact: true })
    .click()
  await expect(page.locator('.queued-action')).toContainText('Red coat')
  await expect(page.locator('.queued-action')).toContainText('Book, Key, Map')
  await expect(
    page.getByRole('textbox', { name: ja.messageLabel }),
  ).toBeFocused()
})
