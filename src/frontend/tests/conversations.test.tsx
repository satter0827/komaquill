import { describe, it, expect, beforeAll, afterAll, afterEach } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route } from 'react-router'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { ConversationPage } from '../src/features/conversations'
import { NewConversation } from '../src/features/start'
import m from '../src/app/locales/ja.json'
import { client } from '../src/api/generated/client.gen'

const base = 'http://localhost'
client.setConfig({ baseUrl: base, throwOnError: true })
const server = setupServer(
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
  it('preserves a new draft when an earlier submission is accepted', async () => {
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
    await userEvent.type(input, 'Next draft')
    release!()
    await waitFor(() => expect(cache.isMutating()).toBe(0))
    expect(input).toHaveValue('Next draft')
  })
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
  it('reuses the request ID and body after a lost response', async () => {
    const requests: unknown[] = []
    server.use(
      http.post(
        `${base}/api/v1/conversations/one/turns`,
        async ({ request }) => {
          requests.push(await request.json())
          return requests.length === 1
            ? HttpResponse.error()
            : HttpResponse.json(
                {
                  id: (requests[0] as { operation_id: string }).operation_id,
                  conversation_id: 'one',
                  state: 'complete',
                },
                { status: 202 },
              )
        },
      ),
      http.get(`${base}/api/v1/operations/:identity`, () =>
        HttpResponse.json({ code: 'request_not_found' }, { status: 404 }),
      ),
    )
    app('/conversations/one')
    await userEvent.type(
      await screen.findByRole('textbox', { name: m.messageLabel }),
      'Hello',
    )
    await userEvent.click(screen.getByRole('button', { name: m.send }))
    await userEvent.click(
      await screen.findByRole('button', { name: m.retryRequest }),
    )
    expect(requests).toHaveLength(2)
    expect(requests[0]).toEqual(requests[1])
  })
  it('keeps the editing revision when a remote update arrives', async () => {
    const cache = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })
    const commands: unknown[] = []
    server.use(
      http.post(
        `${base}/api/v1/conversations/one/commands`,
        async ({ request }) => {
          commands.push(await request.json())
          return HttpResponse.json(
            { code: 'operation_conflict' },
            { status: 409 },
          )
        },
      ),
    )
    app('/conversations/one', cache)
    await userEvent.click(await screen.findByText(m.details))
    await userEvent.click(
      await screen.findByRole('button', { name: m.editState }),
    )
    await userEvent.clear(screen.getByLabelText(m.stateValues))
    await userEvent.type(
      screen.getByLabelText(m.stateValues),
      '{{"place":"draft"}',
    )
    act(() =>
      cache.setQueryData(['conversation', 'one'], {
        id: 'one',
        definition: { title: 'A new scene', llm_profile: 'fake' },
        turns: [],
        state: { place: 'remote' },
        revision: 1,
      }),
    )
    await userEvent.click(screen.getByRole('button', { name: m.saveState }))
    expect(commands).toEqual([
      { command: 'set_state', expected_revision: 0, state: { place: 'draft' } },
    ])
    expect(await screen.findByRole('alert')).toHaveTextContent(
      m.errors.operation_conflict,
    )
  })
  it('requires resolving a lost response even when its operation is known', async () => {
    server.use(
      http.post(`${base}/api/v1/conversations/one/turns`, () =>
        HttpResponse.error(),
      ),
      http.get(`${base}/api/v1/operations/:identity`, ({ params }) =>
        HttpResponse.json({
          id: params.identity,
          conversation_id: 'one',
          state: 'complete',
        }),
      ),
    )
    app('/conversations/one')
    await userEvent.type(
      await screen.findByRole('textbox', { name: m.messageLabel }),
      'Hello',
    )
    await userEvent.click(screen.getByRole('button', { name: m.send }))
    expect(
      await screen.findByRole('button', { name: m.retryRequest }),
    ).toBeVisible()
    expect(screen.getByRole('button', { name: m.send })).toBeDisabled()
  })
})
