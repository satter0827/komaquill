import { describe, it, expect, beforeAll, afterAll, afterEach } from 'vitest'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route } from 'react-router'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { ConversationPage } from '../src/features/conversations'
import { NewConversation } from '../src/features/start'
import m from '../src/app/locales/ja.json'
import { client } from '../src/api/generated/client.gen'
import {
  getConversationQueryKey,
  getOperationQueryKey,
} from '../src/api/generated/@tanstack/react-query.gen'

const base = 'http://localhost'
client.setConfig({ baseUrl: base, throwOnError: true })
const server = setupServer(
  http.post(`${base}/api/v1/availability`, () =>
    HttpResponse.json({ ready: true, connections: [] }),
  ),
  http.get(`${base}/api/v1/conversations/:id/bookmarks`, () =>
    HttpResponse.json([]),
  ),
  http.get(`${base}/api/v1/player`, () =>
    HttpResponse.json({ id: 'player', name: 'Player', revision: 0 }),
  ),
  http.get(`${base}/api/v1/conversations/:id/images`, () =>
    HttpResponse.json([]),
  ),
  ...(['players', 'characters', 'stories'] as const).map((collection) =>
    http.get(`${base}/api/v1/${collection}`, () =>
      HttpResponse.json([
        {
          id: collection,
          name: collection,
          revision: 0,
          sample: true,
          sample_locale: 'ja',
          description: '',
          image_id: null,
        },
      ]),
    ),
  ),
  http.get(`${base}/api/v1/conversations/one/operations`, () =>
    HttpResponse.json([]),
  ),
  http.get(`${base}/api/v1/settings`, () =>
    HttpResponse.json({
      profiles: [{ id: 'fake', provider: 'fake', model: 'fake-list' }],
      sample_input: 'Hello',
    }),
  ),
  http.post(`${base}/api/v1/conversations/from-presets`, () =>
    HttpResponse.json({
      id: 'one',
      definition: { title: 'A new scene', llm_profile: 'fake' },
      state: {},
      turns: [],
      revision: 0,
    }),
  ),
  http.get(`${base}/api/v1/conversations/one`, () =>
    HttpResponse.json({
      id: 'one',
      definition: { title: 'A new scene', llm_profile: 'fake' },
      state: {},
      turns: [],
      revision: 0,
    }),
  ),
  http.post(`${base}/api/v1/conversations/one/turns`, () =>
    HttpResponse.json(
      { id: 'op', conversation_id: 'one', state: 'accepted' },
      { status: 202 },
    ),
  ),
  http.get(`${base}/api/v1/operations/op`, () =>
    HttpResponse.json({ id: 'op', conversation_id: 'one', state: 'running' }),
  ),
)
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterAll(() => server.close())
afterEach(() => server.resetHandlers())

function app(path: string, cache?: QueryClient) {
  return render(
    <QueryClientProvider
      client={
        cache ??
        new QueryClient({
          defaultOptions: {
            queries: { retry: false },
            mutations: { retry: false },
          },
        })
      }
    >
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/new" element={<NewConversation />} />
          <Route
            path="/conversations/:identity"
            element={<ConversationPage />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

async function actionDestination() {
  const trigger = await screen.findByRole('button', {
    name: m.addAction,
  })
  if (trigger.getAttribute('aria-expanded') !== 'true')
    await userEvent.click(trigger)
  return screen.findByLabelText(m.destination)
}

describe('conversation workflow', () => {
  it.each(['/new', '/new?story=stories'])(
    'uses the story counterpart on first entry at %s',
    async (path) => {
      server.use(
        http.get(`${base}/api/v1/characters`, () =>
          HttpResponse.json([
            { id: 'first', name: 'First', sample: true, sample_locale: 'ja' },
            {
              id: 'recommended',
              name: 'Recommended',
              sample: true,
              sample_locale: 'ja',
            },
          ]),
        ),
        http.get(`${base}/api/v1/stories`, () =>
          HttpResponse.json([
            {
              id: 'stories',
              name: 'Story',
              character_id: 'recommended',
              opening: 'A saved opening',
              description: '',
              sample: true,
              sample_locale: 'ja',
            },
          ]),
        ),
      )
      app(path)
      await waitFor(() =>
        expect(screen.getByLabelText(m.chooseCharacter)).toHaveValue(
          'recommended',
        ),
      )
      await userEvent.click(
        document.querySelector('.start-selection > summary')!,
      )
      expect(screen.getByText('A saved opening')).toBeVisible()
      expect(screen.queryByText(m.noPresets)).not.toBeInTheDocument()
    },
  )

  it('keeps an explicit counterpart when a story recommends someone else', async () => {
    server.use(
      http.get(`${base}/api/v1/characters`, () =>
        HttpResponse.json([
          { id: 'chosen', name: 'Chosen', sample: true, sample_locale: 'ja' },
          {
            id: 'recommended',
            name: 'Recommended',
            sample: true,
            sample_locale: 'ja',
          },
        ]),
      ),
      http.get(`${base}/api/v1/stories`, () =>
        HttpResponse.json([
          {
            id: 'stories',
            name: 'Story',
            character_id: 'recommended',
            sample: true,
            sample_locale: 'ja',
          },
        ]),
      ),
    )
    app('/new?story=stories&character=chosen')
    await waitFor(() =>
      expect(screen.getByLabelText(m.chooseCharacter)).toHaveValue('chosen'),
    )
  })

  it('shows one empty selection when an explicitly selected preset is missing', async () => {
    app('/new?story=missing')
    await waitFor(() =>
      expect(screen.getByLabelText(m.chooseCharacter)).toHaveValue(
        'characters',
      ),
    )
    const select = screen.getByLabelText(m.chooseStory) as HTMLSelectElement
    expect(
      [...select.options].filter((option) => option.value === ''),
    ).toHaveLength(1)
    expect(screen.getByRole('button', { name: m.create })).toBeDisabled()
  })

  it.each(['llm_unavailable', 'identity_mismatch'] as const)(
    'retains editable input and its queued action after a connection rejection: %s',
    async (code) => {
      let posts = 0
      server.use(
        http.post(`${base}/api/v1/conversations/one/turns`, () => {
          posts++
          return HttpResponse.json(
            { code, stage: 'connection' },
            { status: 503 },
          )
        }),
      )
      app('/conversations/one')
      await userEvent.type(await actionDestination(), 'Beach')
      await userEvent.click(screen.getByRole('button', { name: m.queueAction }))
      const input = screen.getByRole('textbox', { name: m.messageLabel })
      await userEvent.type(input, 'Keep this message')
      await userEvent.click(screen.getByRole('button', { name: m.send }))
      await screen.findByText(m.errors[code])
      expect(input).toHaveValue('Keep this message')
      expect(
        screen.getByRole('button', { name: m.removeQueuedAction }),
      ).toBeEnabled()
      expect(
        screen.queryByRole('button', { name: m.retryRequest }),
      ).not.toBeInTheDocument()
      await userEvent.type(input, ' edited')
      expect(posts).toBe(1)
    },
  )
  it('keeps an accepted turn busy while its first status response is pending', async () => {
    let release: () => void = () => {}
    let polling = false
    const pending = new Promise<void>((resolve) => {
      release = resolve
    })
    server.use(
      http.get(`${base}/api/v1/operations/:identity`, async ({ params }) => {
        if (params.identity !== 'op')
          return HttpResponse.json(
            { code: 'request_not_found' },
            { status: 404 },
          )
        polling = true
        await pending
        return HttpResponse.json({
          id: 'op',
          conversation_id: 'one',
          state: 'complete',
        })
      }),
    )
    app('/conversations/one')
    const input = await screen.findByRole('textbox', { name: m.messageLabel })
    await userEvent.type(input, 'Hello')
    await userEvent.click(screen.getByRole('button', { name: m.send }))
    try {
      await waitFor(() => expect(polling).toBe(true))
      await userEvent.type(input, 'Next')
      expect(screen.getByRole('button', { name: m.send })).toBeDisabled()
    } finally {
      release()
    }
  })
  it.each(['Next draft', 'Hello'])(
    'preserves a new draft when an earlier submission is accepted (%s)',
    async (nextDraft) => {
      const cache = new QueryClient({
        defaultOptions: {
          queries: { retry: false },
          mutations: { retry: false },
        },
      })
      let release: (() => void) | undefined
      const pending = new Promise<void>((resolve) => {
        release = resolve
      })
      server.use(
        http.post(`${base}/api/v1/conversations/one/turns`, async () => {
          await pending
          return HttpResponse.json(
            { id: 'op', conversation_id: 'one', state: 'accepted' },
            { status: 202 },
          )
        }),
        http.get(`${base}/api/v1/operations/:identity`, () =>
          HttpResponse.json({ code: 'request_not_found' }, { status: 404 }),
        ),
      )
      app('/conversations/one', cache)
      const input = await screen.findByRole('textbox', { name: m.messageLabel })
      await userEvent.type(input, 'Hello')
      await userEvent.click(screen.getByRole('button', { name: m.send }))
      await userEvent.clear(input)
      await userEvent.type(input, nextDraft)
      release!()
      await waitFor(() => expect(cache.isMutating()).toBe(0))
      expect(input).toHaveValue(nextDraft)
    },
  )
  it('creates a conversation and opens the saved result', async () => {
    app('/new')
    await waitFor(() =>
      expect(screen.getByLabelText(m.chooseCharacter)).toHaveValue(
        'characters',
      ),
    )
    await userEvent.click(screen.getByRole('button', { name: m.create }))
    expect(
      await screen.findByRole('heading', { name: 'A new scene' }),
    ).toBeVisible()
  })
  it('disables duplicate sending while a reply is pending', async () => {
    app('/conversations/one')
    const input = await screen.findByRole('textbox', { name: m.messageLabel })
    await userEvent.type(input, 'Hello')
    await userEvent.click(screen.getByRole('button', { name: m.send }))
    expect(await screen.findByRole('status')).toHaveTextContent(m.pending)
    expect(screen.getByRole('button', { name: m.send })).toBeDisabled()
  })
  it('restores saved input after a failed reply and rechecks the failed connection', async () => {
    let freshChecks = 0
    const failed = {
      id: 'failed',
      conversation_id: 'one',
      state: 'failed',
      error: 'llm_unavailable',
      created_at: '2026-11-04T00:00:00Z',
      input: {
        text: 'A saved message',
        action: { kind: 'move', destination: 'Garden' },
      },
    }
    server.use(
      http.get(`${base}/api/v1/conversations/one`, () =>
        HttpResponse.json({
          id: 'one',
          definition: { title: 'Scene', llm_profile: 'local' },
          state: {},
          turns: [],
          revision: 1,
        }),
      ),
      http.get(`${base}/api/v1/conversations/one/operations`, () =>
        HttpResponse.json([failed]),
      ),
      http.post(`${base}/api/v1/availability`, ({ request }) => {
        const refreshed =
          new URL(request.url).searchParams.get('refresh') === 'true'
        if (refreshed) freshChecks += 1
        const now = new Date()
        return HttpResponse.json({
          ready: !refreshed,
          connections: refreshed
            ? [
                {
                  kind: 'llm',
                  target: 'local',
                  state: 'unavailable',
                  reason: 'llm_unavailable',
                  observed_at: now.toISOString(),
                  expires_at: new Date(now.getTime() + 15000).toISOString(),
                },
              ]
            : [],
        })
      }),
    )
    app('/conversations/one')
    await waitFor(() => expect(freshChecks).toBeGreaterThan(0))
    expect(await screen.findByText('A saved message')).toBeVisible()
    expect(screen.getByText('Gardenへ向かう')).toBeVisible()
    expect(screen.queryByText(m.newChapter)).not.toBeInTheDocument()
    await userEvent.type(
      screen.getByRole('textbox', { name: m.messageLabel }),
      'Next draft',
    )
    expect(screen.getByRole('button', { name: m.send })).toBeDisabled()
    expect(screen.getByRole('button', { name: m.drawScene })).toBeDisabled()
  })
  it('checks a lost response by its original ID without posting again', async () => {
    const requests: Array<{ operation_id: string }> = []
    const observations: string[] = []
    let received = false
    server.use(
      http.get(`${base}/api/v1/conversations/one`, () =>
        HttpResponse.json({
          id: 'one',
          definition: { title: 'Scene', llm_profile: 'local' },
          state: {},
          turns: [],
          revision: 0,
        }),
      ),
      http.post(
        `${base}/api/v1/conversations/one/turns`,
        async ({ request }) => {
          requests.push((await request.json()) as { operation_id: string })
          return HttpResponse.error()
        },
      ),
      http.get(`${base}/api/v1/operations/:identity`, ({ params }) => {
        observations.push(String(params.identity))
        return received
          ? HttpResponse.json({
              id: params.identity,
              conversation_id: 'one',
              state: 'complete',
            })
          : HttpResponse.json({ code: 'request_not_found' }, { status: 404 })
      }),
    )
    app('/conversations/one')
    await userEvent.type(
      await screen.findByRole('textbox', { name: m.messageLabel }),
      'Hello',
    )
    await userEvent.click(screen.getByRole('button', { name: m.send }))
    await screen.findByRole('button', { name: m.retryRequest })
    expect(screen.getByRole('button', { name: m.drawScene })).toBeDisabled()
    expect(await actionDestination()).toBeDisabled()
    received = true
    await userEvent.click(
      await screen.findByRole('button', { name: m.retryRequest }),
    )
    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: m.retryRequest }),
      ).not.toBeInTheDocument(),
    )
    expect(requests).toHaveLength(1)
    expect(observations.length).toBeGreaterThan(0)
    expect(new Set(observations)).toEqual(new Set([requests[0].operation_id]))
  })
  it('clears an interrupted receipt when an explicit resume completes the same operation', async () => {
    const cache = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    let resumed = false
    const interrupted = {
      id: 'retained',
      conversation_id: 'one',
      state: 'interrupted',
      error: 'operation_interrupted',
      can_resume: true,
      input: { text: 'Accepted before interruption' },
    }
    const receipt = () =>
      resumed
        ? {
            ...interrupted,
            state: 'complete',
            error: null,
            can_resume: false,
            input: null,
          }
        : interrupted
    cache.setQueryData(
      getOperationQueryKey({ path: { identity: 'retained' } }),
      interrupted,
    )
    server.use(
      http.get(`${base}/api/v1/conversations/one/operations`, () =>
        HttpResponse.json([receipt()]),
      ),
      http.get(`${base}/api/v1/operations/retained`, () =>
        HttpResponse.json(receipt()),
      ),
      http.post(`${base}/api/v1/operations/retained/resume`, () => {
        resumed = true
        return HttpResponse.json(receipt())
      }),
    )
    app('/conversations/one', cache)
    await userEvent.click(
      await screen.findByRole('button', { name: m.resumeOperation }),
    )
    await waitFor(() =>
      expect(
        cache.getQueryData(
          getOperationQueryKey({ path: { identity: 'retained' } }),
        ),
      ).toMatchObject({ state: 'complete' }),
    )
    expect(
      screen.queryByRole('button', { name: m.resumeOperation }),
    ).not.toBeInTheDocument()
  })
  it('requires confirmation of a stale action draft and sends it with the message', async () => {
    const cache = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })
    const requests: unknown[] = []
    server.use(
      http.post(
        `${base}/api/v1/conversations/one/turns`,
        async ({ request }) => {
          const body = await request.json()
          requests.push(body)
          return HttpResponse.json({
            id: 'op',
            conversation_id: 'one',
            state: 'accepted',
          })
        },
      ),
    )
    app('/conversations/one', cache)
    await userEvent.type(await actionDestination(), 'Beach')
    act(() =>
      cache.setQueryData(
        getConversationQueryKey({ path: { identity: 'one' } }),
        {
          id: 'one',
          definition: { title: 'Scene', llm_profile: 'fake' },
          state: {},
          turns: [],
          revision: 1,
        },
      ),
    )
    await userEvent.click(screen.getByRole('button', { name: m.queueAction }))
    await userEvent.type(
      screen.getByRole('textbox', { name: m.messageLabel }),
      'Go together?',
    )
    expect(requests).toHaveLength(0)
    expect(screen.getByRole('button', { name: m.send })).toBeDisabled()
    await userEvent.click(
      screen.getByRole('button', { name: m.confirmCurrentAction }),
    )
    await userEvent.click(screen.getByRole('button', { name: m.send }))
    await waitFor(() => expect(requests).toHaveLength(1))
    expect(requests[0]).toMatchObject({
      expected_revision: 1,
      text: 'Go together?',
      action: { kind: 'move', destination: 'Beach' },
    })
  })
  it.each(['intention', 'pose'])(
    'starts a replacement %s action at the current revision',
    async (next) => {
      const cache = new QueryClient({
        defaultOptions: {
          queries: { retry: false },
          mutations: { retry: false },
        },
      })
      const requests: unknown[] = []
      server.use(
        http.post(
          `${base}/api/v1/conversations/one/turns`,
          async ({ request }) => {
            requests.push(await request.json())
            return HttpResponse.json({ id: 'op', state: 'accepted' })
          },
        ),
      )
      app('/conversations/one', cache)
      await userEvent.type(await actionDestination(), 'Discarded')
      act(() =>
        cache.setQueryData(
          getConversationQueryKey({ path: { identity: 'one' } }),
          {
            id: 'one',
            definition: { title: 'Scene', llm_profile: 'fake' },
            state: {},
            turns: [],
            revision: 1,
          },
        ),
      )
      await userEvent.selectOptions(
        screen.getByLabelText(m.chooseAction),
        next === 'pose' ? 'pose' : 'intention',
      )
      await userEvent.type(
        screen.getByLabelText(
          next === 'pose' ? m.preparationValue : m.currentIntention,
        ),
        'New draft',
      )
      await userEvent.click(screen.getByRole('button', { name: m.queueAction }))
      await userEvent.type(
        screen.getByRole('textbox', { name: m.messageLabel }),
        'Hello',
      )
      await userEvent.click(screen.getByRole('button', { name: m.send }))
      await waitFor(() => expect(requests).toHaveLength(1))
      expect(requests[0]).toMatchObject({
        expected_revision: 1,
        action:
          next === 'pose'
            ? { kind: 'prepare', pose: 'New draft' }
            : { kind: 'intention', text: 'New draft' },
      })
    },
  )
  it('refreshes a required visual review after a rejected send without resending or losing input', async () => {
    let rejected = false
    let posts = 0
    server.use(
      http.get(`${base}/api/v1/conversations/one`, () =>
        HttpResponse.json({
          id: 'one',
          definition: {
            title: 'Scene',
            llm_profile: 'fake',
            character: { visual_review_required: rejected },
          },
          state: {},
          turns: [],
          revision: 0,
        }),
      ),
      http.post(`${base}/api/v1/conversations/one/turns`, () => {
        posts++
        rejected = true
        return HttpResponse.json(
          { code: 'visual_review_required' },
          { status: 409 },
        )
      }),
    )
    app('/conversations/one')
    const input = await screen.findByRole('textbox', { name: m.messageLabel })
    await userEvent.type(input, 'Keep this message')
    await userEvent.click(screen.getByRole('button', { name: m.send }))
    await screen.findByLabelText(m.appearance)
    expect(input).toHaveValue('Keep this message')
    expect(
      screen.queryByRole('button', { name: m.retryRequest }),
    ).not.toBeInTheDocument()
    expect(posts).toBe(1)
  })
  it('retains a queued action when the pane closes and waits for one send receipt', async () => {
    const requests: Array<{ operation_id: string }> = []
    let received = false
    const cache = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })
    server.use(
      http.post(
        `${base}/api/v1/conversations/one/turns`,
        async ({ request }) => {
          requests.push((await request.json()) as { operation_id: string })
          return HttpResponse.error()
        },
      ),
      http.get(`${base}/api/v1/operations/:id`, ({ params }) =>
        received
          ? HttpResponse.json({
              id: params.id,
              conversation_id: 'one',
              state: 'complete',
            })
          : HttpResponse.json({ code: 'request_not_found' }, { status: 404 }),
      ),
    )
    app('/conversations/one', cache)
    await userEvent.type(await actionDestination(), 'Beach')
    await userEvent.click(screen.getByRole('button', { name: m.queueAction }))
    await userEvent.click(screen.getByRole('button', { name: m.showState }))
    await userEvent.click(screen.getByRole('button', { name: m.closeState }))
    const input = screen.getByRole('textbox', { name: m.messageLabel })
    await userEvent.type(input, 'Go together?')
    await userEvent.click(screen.getByRole('button', { name: m.send }))
    await screen.findByRole('button', { name: m.retryRequest })
    expect(requests).toHaveLength(1)
    await userEvent.clear(input)
    await userEvent.type(input, 'Next draft')
    expect(screen.getByRole('button', { name: m.send })).toBeDisabled()
    received = true
    await act(() =>
      cache.invalidateQueries({
        queryKey: getOperationQueryKey({
          path: { identity: requests[0].operation_id },
        }),
      }),
    )
    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: m.removeQueuedAction }),
      ).not.toBeInTheDocument(),
    )
    expect(input).toHaveValue('Next draft')
    expect(requests).toHaveLength(1)
  })
  it('keeps an unknown message and action when a manual check still finds no receipt', async () => {
    const requests: unknown[] = []
    let observations = 0
    server.use(
      http.post(
        `${base}/api/v1/conversations/one/turns`,
        async ({ request }) => {
          const body = await request.json()
          requests.push(body)
          return HttpResponse.error()
        },
      ),
      http.get(`${base}/api/v1/operations/:id`, () => {
        observations++
        return HttpResponse.json({ code: 'request_not_found' }, { status: 404 })
      }),
    )
    app('/conversations/one')
    await userEvent.type(await actionDestination(), 'Beach')
    await userEvent.click(screen.getByRole('button', { name: m.queueAction }))
    await userEvent.type(
      screen.getByRole('textbox', { name: m.messageLabel }),
      'Go together?',
    )
    await userEvent.click(screen.getByRole('button', { name: m.send }))
    await waitFor(() => expect(observations).toBeGreaterThan(0))
    const before = observations
    await userEvent.click(
      await screen.findByRole('button', { name: m.retryRequest }),
    )
    await waitFor(() => expect(observations).toBeGreaterThan(before))
    expect(requests).toHaveLength(1)
    expect(screen.queryByText(m.pending)).not.toBeInTheDocument()
    expect(screen.getByText(m.receiptUnknown)).toBeVisible()
    expect(screen.getByRole('textbox', { name: m.messageLabel })).toHaveValue(
      'Go together?',
    )
    expect(
      screen.getByRole('button', { name: m.removeQueuedAction }),
    ).toBeDisabled()
    expect(requests[0]).toMatchObject({
      text: 'Go together?',
      action: { kind: 'move', destination: 'Beach' },
    })
  })
  it('keeps visual review recovery reachable after the saved review arrives', async () => {
    const cache = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })
    const original = {
      id: 'one',
      definition: {
        title: 'Review',
        llm_profile: 'fake',
        user: { visual_review_required: true },
        character: { visual_review_required: true },
      },
      state: {},
      turns: [],
      revision: 0,
    }
    let submitted: unknown
    server.use(
      http.get(`${base}/api/v1/conversations/one`, () =>
        HttpResponse.json(original),
      ),
      http.put(
        `${base}/api/v1/conversations/one/visuals`,
        async ({ request }) => {
          submitted = await request.json()
          return HttpResponse.error()
        },
      ),
    )
    app('/conversations/one', cache)
    await userEvent.click(
      await screen.findByRole('button', { name: m.confirmAppearance }),
    )
    await screen.findByRole('button', { name: m.checkAction })
    const reviewed = {
      ...original,
      definition: {
        ...original.definition,
        user: { visual_review_required: false },
        character: { visual_review_required: false },
      },
      revision: 1,
      visual_reviews: [{ request: submitted, revision: 1 }],
    }
    server.use(
      http.get(`${base}/api/v1/conversations/one`, () =>
        HttpResponse.json(reviewed),
      ),
    )
    act(() =>
      cache.setQueryData(
        getConversationQueryKey({ path: { identity: 'one' } }),
        reviewed,
      ),
    )
    await userEvent.click(
      await screen.findByRole('button', { name: m.checkAction }),
    )
    expect(await actionDestination()).toBeEnabled()
  })
  it('reviews only the unconfirmed person and keeps unspecified clothes as null', async () => {
    let submitted: unknown
    server.use(
      http.get(`${base}/api/v1/conversations/one`, () =>
        HttpResponse.json({
          id: 'one',
          definition: {
            title: 'Review',
            llm_profile: 'fake',
            user: { appearance: 'Brown hair', initial_clothing: 'Red coat' },
            character: {
              appearance: 'Silver hair and apron',
              visual_review_required: true,
              initial_clothing: null,
            },
          },
          state: {
            player: { clothing: 'Red coat' },
            character: { clothing: null },
          },
          turns: [],
          revision: 0,
        }),
      ),
      http.put(
        `${base}/api/v1/conversations/one/visuals`,
        async ({ request }) => {
          submitted = await request.json()
          return HttpResponse.json(
            { code: 'operation_conflict' },
            { status: 409 },
          )
        },
      ),
    )
    app('/conversations/one')
    const input = await screen.findByLabelText(m.appearance)
    expect(screen.getAllByLabelText(m.appearance)).toHaveLength(1)
    await userEvent.type(input, 'Silver hair')
    await userEvent.click(
      screen.getByRole('button', { name: m.confirmAppearance }),
    )
    await waitFor(() =>
      expect(submitted).toMatchObject({
        character: {
          appearance: 'Silver hair',
          initial_clothing: null,
          current_clothing: null,
        },
      }),
    )
    expect(submitted).not.toHaveProperty('player')
  })
  it.each([true, false])(
    'only recovers a lost response automatically if its receipt is known (%s)',
    async (known) => {
      let submissions = 0
      server.use(
        http.post(`${base}/api/v1/conversations/one/turns`, () => {
          submissions += 1
          return HttpResponse.error()
        }),
        http.get(`${base}/api/v1/operations/:identity`, ({ params }) =>
          known
            ? HttpResponse.json({
                id: params.identity,
                conversation_id: 'one',
                state: 'complete',
              })
            : HttpResponse.json({ code: 'request_not_found' }, { status: 404 }),
        ),
      )
      app('/conversations/one')
      await userEvent.type(
        await screen.findByRole('textbox', { name: m.messageLabel }),
        'Hello',
      )
      await userEvent.click(screen.getByRole('button', { name: m.send }))
      if (known) {
        await waitFor(() =>
          expect(
            screen.getByRole('textbox', { name: m.messageLabel }),
          ).toHaveValue(''),
        )
        expect(
          screen.queryByRole('button', { name: m.retryRequest }),
        ).not.toBeInTheDocument()
        await userEvent.type(
          screen.getByRole('textbox', { name: m.messageLabel }),
          'Next',
        )
        expect(screen.getByRole('button', { name: m.send })).toBeEnabled()
      } else {
        expect(
          await screen.findByRole('button', { name: m.retryRequest }),
        ).toBeVisible()
        expect(screen.getByRole('button', { name: m.send })).toBeDisabled()
      }
      expect(submissions).toBe(1)
    },
  )
})

it('shows the current character condition without internal state values', async () => {
  server.use(
    http.get(`${base}/api/v1/conversations/one`, () =>
      HttpResponse.json({
        id: 'one',
        definition: { title: 'Garden', llm_profile: 'fake' },
        turns: [
          {
            operation_id: 'latest',
            user: 'Continue',
            assistant: 'Hello',
            narration: 'Wearing a blue shirt in the garden.',
          },
        ],
        revision: 1,
        state: {
          place: 'Garden',
          player: {},
          character: { clothing: 'Blue shirt', expression: 'Smiling' },
        },
      }),
    ),
  )
  app('/conversations/one')
  await waitFor(() =>
    expect(
      screen.getAllByText('Wearing a blue shirt in the garden.'),
    ).toHaveLength(1),
  )
  await userEvent.click(screen.getByRole('button', { name: m.showState }))
  expect(screen.getByText('Blue shirt')).toBeInTheDocument()
  expect(screen.getAllByText(m.currentState)).toHaveLength(1)
  expect(screen.getAllByText('Garden').length).toBeGreaterThan(0)
  expect(screen.queryByText('{"age":"28"}')).not.toBeInTheDocument()
})

it('edits multiple held objects without dropping other queued preparation fields', async () => {
  const requests: Array<{
    action: {
      kind: string
      clothing: string
      held_objects: Array<{ id: string; name: string }>
    }
    proposal_id?: string
  }> = []
  server.use(
    http.get(`${base}/api/v1/conversations/one`, () =>
      HttpResponse.json({
        id: 'one',
        definition: { title: 'Preparation', llm_profile: 'fake' },
        state: { player: { held_objects: [] } },
        revision: 0,
        turns: [],
        proposals: [
          {
            id: 'proposal',
            turn_id: 'source',
            state: 'pending',
            revision: 0,
            action: {
              kind: 'prepare',
              clothing: 'Blue coat',
              held_objects: [
                { id: 'book', name: 'Book' },
                { id: 'key', name: 'Key' },
              ],
            },
          },
        ],
      }),
    ),
    http.post(`${base}/api/v1/conversations/one/turns`, async ({ request }) => {
      requests.push((await request.json()) as (typeof requests)[number])
      return HttpResponse.json(
        { id: 'op', conversation_id: 'one', state: 'accepted' },
        { status: 202 },
      )
    }),
  )
  app('/conversations/one')
  await userEvent.click(await screen.findByText(m.addAction, { exact: true }))
  const proposal = document.querySelector('.action-proposal')! as HTMLElement
  await userEvent.click(
    within(proposal).getByRole('button', { name: m.queueAction }),
  )
  await userEvent.click(
    screen.getByRole('button', { name: m.editQueuedAction }),
  )
  await userEvent.selectOptions(
    screen.getByLabelText(m.chooseAction),
    'held_objects',
  )
  const objects = screen.getByLabelText(m.heldObjectsInput)
  expect(objects.tagName).toBe('TEXTAREA')
  expect(objects).toHaveValue('Book\nKey')
  await userEvent.type(objects, '{End}{Enter}Map')
  await userEvent.click(
    within(
      document.querySelector('.player-actions form')! as HTMLElement,
    ).getByRole('button', { name: m.queueAction }),
  )
  await userEvent.click(
    screen.getByRole('button', { name: m.editQueuedAction }),
  )
  const clothing = screen.getByLabelText(m.preparationValue)
  await userEvent.clear(clothing)
  await userEvent.type(clothing, 'Red coat')
  await userEvent.click(
    within(
      document.querySelector('.player-actions form')! as HTMLElement,
    ).getByRole('button', { name: m.queueAction }),
  )
  const message = screen.getByRole('textbox', { name: m.messageLabel })
  await waitFor(() => expect(message).toHaveFocus())
  await userEvent.type(message, 'Let us go.')
  await userEvent.click(screen.getByRole('button', { name: m.send }))
  await waitFor(() => expect(requests).toHaveLength(1))
  expect(requests[0].action).toMatchObject({
    kind: 'prepare',
    clothing: 'Red coat',
    held_objects: [
      { id: 'book', name: 'Book' },
      { id: 'key', name: 'Key' },
      { name: 'Map' },
    ],
  })
  expect(requests[0].proposal_id).toBeUndefined()
})
