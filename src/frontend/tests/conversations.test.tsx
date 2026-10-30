import { describe, it, expect, beforeAll, afterAll, afterEach } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route } from 'react-router'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import {
  NewConversation,
  ConversationPage,
} from '../src/features/conversations'
import { client } from '../src/api/generated/client.gen'

const base = 'http://localhost'
client.setConfig({ baseUrl: base, throwOnError: true })
const server = setupServer(
  http.get(`${base}/api/v1/conversations/one/operations`, () =>
    HttpResponse.json([]),
  ),
  http.get(`${base}/api/v1/settings`, () =>
    HttpResponse.json({
      profiles: [{ id: 'fake', provider: 'fake', model: 'fake-list' }],
      sample_input: 'Hello',
    }),
  ),
  http.post(`${base}/api/v1/conversations`, () =>
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
    const input = await screen.findByRole('textbox', { name: 'Message' })
    await userEvent.type(input, 'Hello')
    await userEvent.click(screen.getByRole('button', { name: 'Send message' }))
    await userEvent.clear(input)
    await userEvent.type(input, 'Next draft')
    release!()
    await waitFor(() => expect(cache.isMutating()).toBe(0))
    expect(input).toHaveValue('Next draft')
  })
  it('creates a conversation and opens the saved result', async () => {
    app('/new')
    await userEvent.type(screen.getByLabelText('Title'), 'A new scene')
    await userEvent.click(
      screen.getByRole('button', { name: 'Create conversation' }),
    )
    expect(
      await screen.findByRole('heading', { name: 'A new scene' }),
    ).toBeVisible()
  })
  it('disables duplicate sending while a reply is pending', async () => {
    app('/conversations/one')
    const input = await screen.findByRole('textbox', { name: 'Message' })
    await userEvent.type(input, 'Hello')
    await userEvent.click(screen.getByRole('button', { name: 'Send message' }))
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Preparing a reply',
    )
    expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled()
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
      await screen.findByRole('textbox', { name: 'Message' }),
      'Hello',
    )
    await userEvent.click(screen.getByRole('button', { name: 'Send message' }))
    await userEvent.click(
      await screen.findByRole('button', { name: 'Retry the same request' }),
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
            { message: 'State changed. Reopen the editor.' },
            { status: 409 },
          )
        },
      ),
    )
    app('/conversations/one', cache)
    await userEvent.click(
      await screen.findByRole('button', { name: 'Edit state' }),
    )
    await userEvent.clear(screen.getByLabelText('State values (JSON)'))
    await userEvent.type(
      screen.getByLabelText('State values (JSON)'),
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
    await userEvent.click(screen.getByRole('button', { name: 'Save state' }))
    expect(commands).toEqual([
      { command: 'set_state', expected_revision: 0, state: { place: 'draft' } },
    ])
    expect(await screen.findByRole('alert')).toHaveTextContent('State changed')
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
      await screen.findByRole('textbox', { name: 'Message' }),
      'Hello',
    )
    await userEvent.click(screen.getByRole('button', { name: 'Send message' }))
    expect(
      await screen.findByRole('button', { name: 'Retry the same request' }),
    ).toBeVisible()
    expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled()
  })
})
