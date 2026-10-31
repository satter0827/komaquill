import { afterAll, afterEach, beforeAll, expect, it } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { LibraryPage } from '../src/features/library'
import { client } from '../src/api/generated/client.gen'
import type {
  CreationJob,
  CreationRequest,
} from '../src/api/generated/types.gen'
import m from '../src/app/locales/ja.json'

const base = 'http://localhost'
client.setConfig({ baseUrl: base, throwOnError: true })
const server = setupServer(
  http.get(`${base}/api/v1/characters`, () => HttpResponse.json([])),
  http.get(`${base}/api/v1/settings`, () =>
    HttpResponse.json({
      profiles: [
        { id: 'fake', provider: 'fake', model: 'fake-list' },
        { id: 'local', provider: 'openai-compatible', model: 'mock' },
      ],
    }),
  ),
  http.post(`${base}/api/v1/runtime/refresh`, () =>
    HttpResponse.json({ gpu: 'absent', connection: 'stopped' }),
  ),
)
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterAll(() => server.close())
afterEach(() => server.resetHandlers())

it('shows an empty library after filtering samples for another language', async () => {
  server.use(
    http.get(`${base}/api/v1/characters`, () =>
      HttpResponse.json([
        {
          id: 'english',
          name: 'English sample',
          sample: true,
          sample_locale: 'en',
          revision: 0,
        },
      ]),
    ),
  )
  render(
    <QueryClientProvider
      client={
        new QueryClient({
          defaultOptions: { queries: { retry: false } },
        })
      }
    >
      <MemoryRouter>
        <LibraryPage kind="character" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  expect(await screen.findByText(m.noPresets)).toBeVisible()
  expect(screen.getByRole('link', { name: m.new })).toHaveAttribute(
    'href',
    '/characters/new',
  )
})

async function draftEditor(
  path = '/characters/new',
  kind: 'character' | 'story' = 'character',
) {
  render(
    <QueryClientProvider
      client={
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
          <Route
            path={
              kind === 'character'
                ? '/characters/:identity'
                : '/stories/:identity'
            }
            element={<LibraryPage kind={kind} />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  await userEvent.click(
    await screen.findByText(m.createDraft, { selector: 'summary' }),
  )
  await userEvent.type(screen.getByLabelText(m.draftPrompt), 'A watchmaker')
  return screen.getByText(m.createDraft, { selector: 'button' })
}

it('applies draft text without changing the selected counterpart or claiming it is saved', async () => {
  let job: CreationJob
  server.use(
    http.get(`${base}/api/v1/stories`, () =>
      HttpResponse.json([
        {
          id: 'original',
          name: 'Original',
          revision: 0,
          character_id: 'chosen',
        },
      ]),
    ),
    http.get(`${base}/api/v1/characters`, () =>
      HttpResponse.json([{ id: 'chosen', name: 'Chosen' }]),
    ),
    http.post(`${base}/api/v1/creation-jobs`, async ({ request }) => {
      const body = (await request.json()) as CreationRequest
      job = {
        id: body.operation_id,
        request: body,
        state: 'complete',
        result: { name: 'New story', character_id: 'invented' },
      }
      return HttpResponse.json(job, { status: 202 })
    }),
    http.get(`${base}/api/v1/creation-jobs/:identity`, () =>
      HttpResponse.json(job),
    ),
  )
  const button = await draftEditor('/stories/original', 'story')
  expect(screen.getByRole('status')).toHaveTextContent(m.saved)
  await userEvent.click(button)
  await userEvent.click(await screen.findByRole('button', { name: m.useDraft }))
  expect(screen.getByLabelText(m.name)).toHaveValue('New story')
  expect(screen.getByLabelText(m.defaultCharacter)).toHaveValue('chosen')
  expect(screen.getByRole('status')).toBeEmptyDOMElement()
})

it('resolves a lost creation response with the original ID and input', async () => {
  const requests: CreationRequest[] = []
  let job: CreationJob | null = null
  server.use(
    http.post(`${base}/api/v1/creation-jobs`, async ({ request }) => {
      const body = (await request.json()) as CreationRequest
      requests.push(body)
      if (requests.length === 1) return HttpResponse.error()
      job = {
        id: body.operation_id,
        request: body,
        state: 'complete',
        result: { name: 'Mira' },
      }
      return HttpResponse.json(job, { status: 202 })
    }),
    http.get(`${base}/api/v1/creation-jobs/:identity`, () =>
      job
        ? HttpResponse.json(job)
        : HttpResponse.json({ code: 'request_not_found' }, { status: 404 }),
    ),
  )
  const button = await draftEditor()
  await userEvent.click(button)
  const retry = await screen.findByRole('button', { name: m.retryRequest })
  await userEvent.clear(screen.getByLabelText(m.draftPrompt))
  await userEvent.type(screen.getByLabelText(m.draftPrompt), 'A different idea')
  expect(button).toBeDisabled()
  await userEvent.click(retry)
  expect(await screen.findByRole('button', { name: m.useDraft })).toBeVisible()
  expect(requests).toHaveLength(2)
  expect(requests[0]).toEqual(requests[1])
})

it('allows correcting a creation rejected before acceptance', async () => {
  const requests: CreationRequest[] = []
  server.use(
    http.post(`${base}/api/v1/creation-jobs`, async ({ request }) => {
      const body = (await request.json()) as CreationRequest
      requests.push(body)
      return requests.length === 1
        ? HttpResponse.json({ code: 'invalid_request' }, { status: 422 })
        : HttpResponse.json(
            {
              id: body.operation_id,
              request: body,
              state: 'complete',
              result: { name: 'Mira' },
            },
            { status: 202 },
          )
    }),
    http.get(`${base}/api/v1/creation-jobs/:identity`, () =>
      HttpResponse.json({ code: 'request_not_found' }, { status: 404 }),
    ),
  )
  const button = await draftEditor()
  await userEvent.click(button)
  expect(await screen.findByText(m.errors.invalid_request)).toBeVisible()
  await userEvent.clear(screen.getByLabelText(m.draftPrompt))
  await userEvent.type(screen.getByLabelText(m.draftPrompt), 'A corrected idea')
  expect(button).not.toBeDisabled()
  expect(
    screen.queryByRole('button', { name: m.retryRequest }),
  ).not.toBeInTheDocument()
  await userEvent.click(button)
  await waitFor(() => expect(requests).toHaveLength(2))
  expect(requests[1].operation_id).not.toBe(requests[0].operation_id)
  expect(requests[1].prompt).toBe('A corrected idea')
})

it('keeps an accepted creation busy before the first status response', async () => {
  let job: CreationJob
  let polling = false
  let release: () => void = () => {}
  const pending = new Promise<void>((resolve) => {
    release = resolve
  })
  server.use(
    http.post(`${base}/api/v1/creation-jobs`, async ({ request }) => {
      const body = (await request.json()) as CreationRequest
      job = { id: body.operation_id, request: body, state: 'accepted' }
      return HttpResponse.json(job, { status: 202 })
    }),
    http.get(`${base}/api/v1/creation-jobs/:identity`, async () => {
      polling = true
      await pending
      return HttpResponse.json({
        ...job,
        state: 'complete',
        result: { name: 'Mira' },
      })
    }),
  )
  const button = await draftEditor()
  await userEvent.click(button)
  await waitFor(() => expect(polling).toBe(true))
  try {
    expect(button).toBeDisabled()
  } finally {
    release()
  }
  expect(await screen.findByRole('button', { name: m.useDraft })).toBeVisible()
})

it('waits for image connection before scheduling an image after saving', async () => {
  let release: () => void = () => {}
  const pending = new Promise<void>((resolve) => {
    release = resolve
  })
  const identity = crypto.randomUUID()
  let saved: Record<string, unknown> | null = null
  let job: CreationJob | null = null
  const requests: CreationRequest[] = []
  server.use(
    http.get(`${base}/api/v1/characters`, () =>
      HttpResponse.json(saved ? [saved] : []),
    ),
    http.post(`${base}/api/v1/characters`, async ({ request }) => {
      saved = {
        ...((await request.json()) as Record<string, unknown>),
        id: identity,
        revision: 0,
      }
      return HttpResponse.json(saved, { status: 201 })
    }),
    http.post(`${base}/api/v1/runtime/refresh`, async () => {
      await pending
      return HttpResponse.json({ gpu: 'active', connection: 'connected' })
    }),
    http.post(`${base}/api/v1/creation-jobs`, async ({ request }) => {
      const body = (await request.json()) as CreationRequest
      requests.push(body)
      job = {
        id: body.operation_id,
        request: body,
        state: 'failed',
        error: 'operation_failed',
      }
      return HttpResponse.json(job, { status: 202 })
    }),
    http.get(`${base}/api/v1/creation-jobs/:identity`, () =>
      HttpResponse.json(job),
    ),
  )
  await draftEditor()
  await userEvent.type(screen.getByLabelText(m.name), 'Casey')
  await userEvent.click(screen.getByRole('button', { name: m.save }))
  try {
    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent(m.saved),
    )
    expect(requests).toHaveLength(0)
  } finally {
    release()
  }
  await waitFor(() => expect(requests).toHaveLength(1))
  expect(requests[0].target_id).toBe(identity)
  expect(requests[0].kind).toBe('portrait')
  expect(screen.getByLabelText(m.name)).toHaveValue('Casey')
  expect(screen.getByRole('status')).toHaveTextContent(m.saved)
})
