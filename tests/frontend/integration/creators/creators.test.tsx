import { afterAll, afterEach, beforeAll, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import {
  LibraryPage,
  PlayerPage,
} from '../../../../src/frontend/src/features/library/index'
import { client } from '../../../../src/frontend/src/api/generated/client.gen'
import type {
  CreationJob,
  CreationRequest,
} from '../../../../src/frontend/src/api/generated/types.gen'
import m from '../../../../src/frontend/src/app/locales/ja.json'

const base = 'http://localhost'
client.setConfig({ baseUrl: base, throwOnError: true })
const server = setupServer(
  http.post(`${base}/api/v1/availability`, () =>
    HttpResponse.json({ ready: true, connections: [] }),
  ),
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

it.each(['player', 'character'] as const)(
  'retains %s multiline item drafts and preserves explicit empty clothes',
  async (kind) => {
    let stored: Record<string, unknown> = {
      id: 'saved',
      name: 'Mira',
      revision: 0,
      initial_clothing: null,
      initial_held_objects: [
        { id: 'cup', name: 'Cup' },
        { id: 'book', name: 'Book' },
      ],
    }
    const requests: Array<Record<string, unknown>> = []
    const update = (value: Record<string, unknown>) => {
      requests.push(value)
      stored = { ...stored, ...value, revision: Number(stored.revision) + 1 }
      return stored
    }
    server.use(
      http.get(`${base}/api/v1/player`, () => HttpResponse.json(stored)),
      http.get(`${base}/api/v1/characters`, () => HttpResponse.json([stored])),
      http.put(`${base}/api/v1/player`, async ({ request }) =>
        HttpResponse.json(
          update((await request.json()) as Record<string, unknown>),
        ),
      ),
      http.post(`${base}/api/v1/characters/save`, async ({ request }) => {
        const body = (await request.json()) as {
          value: Record<string, unknown>
        }
        return HttpResponse.json({
          preset: update(body.value),
          job: null,
          image_error: null,
        })
      }),
    )
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
        <MemoryRouter
          initialEntries={[kind === 'player' ? '/player' : '/characters/saved']}
        >
          <Routes>
            <Route path="/player" element={<PlayerPage />} />
            <Route
              path="/characters/:identity"
              element={<LibraryPage kind="character" />}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    const items = await screen.findByLabelText(m.initial_held_objects)
    expect(items).toHaveValue('Cup\nBook')
    await userEvent.type(screen.getByLabelText(m.requiredName), ' Updated')
    await userEvent.click(screen.getByRole('button', { name: m.save }))
    await waitFor(() => expect(requests).toHaveLength(1))
    expect(requests[0]).toMatchObject({
      initial_clothing: null,
      initial_held_objects: [
        { id: 'cup', name: 'Cup' },
        { id: 'book', name: 'Book' },
      ],
    })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: m.save })).toBeEnabled(),
    )
    await userEvent.clear(items)
    await userEvent.type(items, 'Cup{Enter}Book{Enter}Key{Enter}')
    expect(items).toHaveValue('Cup\nBook\nKey\n')
    const clothes = screen.getByLabelText(m.initial_clothing)
    await userEvent.type(clothes, 'Shirt')
    await userEvent.clear(clothes)
    await userEvent.click(screen.getByRole('button', { name: m.save }))
    await waitFor(() => expect(requests).toHaveLength(2))
    expect(requests[1]).toMatchObject({
      initial_clothing: '',
      initial_held_objects: [
        { id: 'cup', name: 'Cup' },
        { id: 'book', name: 'Book' },
        expect.objectContaining({ name: 'Key', id: expect.any(String) }),
      ],
    })
  },
)

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
  expect(screen.getByText(m.saved)).toBeVisible()
  await userEvent.click(button)
  await userEvent.click(await screen.findByRole('button', { name: m.useDraft }))
  expect(screen.getByLabelText(m.requiredName)).toHaveValue('New story')
  expect(screen.getByLabelText(m.defaultCharacter)).toHaveValue('chosen')
  expect(screen.getByRole('status')).toHaveTextContent(m.unsavedChanges)
})

it('resolves a lost creation response with the original ID and input', async () => {
  const requests: CreationRequest[] = []
  let job: CreationJob | null = null
  let received = false
  const observations: string[] = []
  server.use(
    http.post(`${base}/api/v1/creation-jobs`, async ({ request }) => {
      const body = (await request.json()) as CreationRequest
      requests.push(body)
      job = {
        id: body.operation_id,
        request: body,
        state: 'complete',
        result: { name: 'Mira' },
      }
      return HttpResponse.error()
    }),
    http.get(`${base}/api/v1/creation-jobs/:identity`, ({ params }) => {
      observations.push(String(params.identity))
      return received
        ? HttpResponse.json(job)
        : HttpResponse.json({ code: 'request_not_found' }, { status: 404 })
    }),
  )
  const button = await draftEditor()
  await userEvent.click(button)
  const retry = await screen.findByRole('button', { name: m.retryRequest })
  expect(screen.getByText(m.receiptUnknown)).toBeVisible()
  await userEvent.clear(screen.getByLabelText(m.draftPrompt))
  await userEvent.type(screen.getByLabelText(m.draftPrompt), 'A different idea')
  expect(button).toBeDisabled()
  received = true
  await userEvent.click(retry)
  expect(await screen.findByRole('button', { name: m.useDraft })).toBeVisible()
  expect(requests).toHaveLength(1)
  expect(observations.length).toBeGreaterThan(0)
  expect(new Set(observations)).toEqual(new Set([requests[0].operation_id]))
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

it('saves text without requesting generation or authorizing an LLM', async () => {
  const identity = crypto.randomUUID()
  let saved: Record<string, unknown> | null = null
  const requests: Record<string, unknown>[] = []
  server.use(
    http.get(`${base}/api/v1/characters`, () =>
      HttpResponse.json(saved ? [saved] : []),
    ),
    http.post(`${base}/api/v1/characters/save`, async ({ request }) => {
      const body = (await request.json()) as {
        value: Record<string, unknown>
        operation_id: string
      }
      requests.push(body)
      saved = {
        ...body.value,
        id: identity,
        revision: 0,
        image_id: null,
        image_job_id: null,
      }
      return HttpResponse.json({
        preset: saved,
        job: null,
        image_error: 'image_backend_unavailable',
      })
    }),
  )
  await draftEditor()
  await userEvent.type(screen.getByLabelText(m.requiredName), 'Casey')
  await userEvent.type(screen.getByLabelText(m.age), '60')
  await userEvent.type(screen.getByLabelText(m.gender), 'man')
  await userEvent.click(screen.getByRole('button', { name: m.save }))
  await waitFor(() => expect(screen.getByText(m.saved)).toBeVisible())
  expect(requests).toHaveLength(1)
  expect(requests[0]).toMatchObject({
    value: { name: 'Casey', age: '60', gender: 'man' },
  })
  expect(requests[0].operation_id).toBeTruthy()
  expect(screen.getByLabelText(m.requiredName)).toHaveValue('Casey')
  expect(requests[0]).not.toHaveProperty('image_policy')
  expect(requests[0]).not.toHaveProperty('allow_real_llm')
})

it('reads an older unfinished portrait once without starting a polling or collection loop', async () => {
  let reads = 0
  server.use(
    http.get(`${base}/api/v1/characters`, () =>
      HttpResponse.json([
        {
          id: 'past',
          name: 'Past portrait',
          revision: 0,
          image_job_id: 'old-job',
        },
      ]),
    ),
    http.get(`${base}/api/v1/creation-jobs/old-job`, () => {
      reads++
      return HttpResponse.json({
        id: 'old-job',
        state: 'accepted',
        image_id: null,
      })
    }),
  )
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  const view = render(
    <QueryClientProvider client={cache}>
      <MemoryRouter initialEntries={['/characters']}>
        <LibraryPage kind="character" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  try {
    await screen.findByText(m.job_accepted)
    await act(async () => {
      vi.advanceTimersByTime(6000)
    })
    await waitFor(() => expect(cache.isFetching()).toBe(0))
    expect(reads).toBe(1)
  } finally {
    view.unmount()
    cache.clear()
    vi.useRealTimers()
  }
})

it('uses the save receipt to reach stable drawing controls before a slow list refresh', async () => {
  let stored = false
  let release: () => void = () => {}
  const refreshed = new Promise<void>((resolve) => {
    release = resolve
  })
  const record = {
    id: 'saved-receipt',
    name: 'Stable companion',
    revision: 0,
    description: '',
  }
  const requests: CreationRequest[] = []
  server.use(
    http.get(`${base}/api/v1/characters`, async () => {
      if (stored) {
        await refreshed
        return HttpResponse.json([record])
      }
      return HttpResponse.json([])
    }),
    http.post(`${base}/api/v1/characters/save`, () => {
      stored = true
      return HttpResponse.json({ preset: record })
    }),
    http.post(`${base}/api/v1/creation-jobs`, async ({ request }) => {
      const body = (await request.json()) as CreationRequest
      requests.push(body)
      return HttpResponse.json(
        { id: body.operation_id, request: body, state: 'accepted' },
        { status: 202 },
      )
    }),
    http.get(`${base}/api/v1/creation-jobs/:id`, ({ params }) =>
      HttpResponse.json({ id: params.id, state: 'accepted' }),
    ),
  )
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const view = render(
    <QueryClientProvider client={cache}>
      <MemoryRouter initialEntries={['/characters/new']}>
        <Routes>
          <Route
            path="/characters/:identity"
            element={<LibraryPage kind="character" />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  try {
    await userEvent.type(
      await screen.findByLabelText(m.requiredName),
      record.name,
    )
    await userEvent.click(screen.getByRole('button', { name: m.save }))
    const draw = await screen.findByRole('button', { name: m.createImage })
    await waitFor(() => expect(draw).toBeEnabled())
    await userEvent.click(draw)
    await waitFor(() => expect(requests).toHaveLength(1))
    expect(requests[0]).toMatchObject({
      target_id: record.id,
      expected_revision: 0,
      kind: 'portrait',
    })
    expect(screen.getByRole('button', { name: m.createImage })).toBeDisabled()
  } finally {
    release()
    view.unmount()
    cache.clear()
  }
})
