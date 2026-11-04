import { MemoryRouter } from 'react-router'
import { afterAll, afterEach, beforeAll, expect, it } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { ImagesPage } from '../src/features/images'
import { PreferencesProvider, defaultPreferences } from '../src/app/preferences'
import { client } from '../src/api/generated/client.gen'
import type { ImageInput } from '../src/api/generated/types.gen'
import { getImageQueryKey } from '../src/api/generated/@tanstack/react-query.gen'
import m from '../src/app/locales/ja.json'

const base = 'http://localhost'
client.setConfig({ baseUrl: base, throwOnError: true })
const requests: ImageInput[] = []
const preferences = { ...defaultPreferences, llm_profile: 'local' }
const server = setupServer(
  http.post(`${base}/api/v1/availability`, () =>
    HttpResponse.json({ ready: true, connections: [] }),
  ),
  http.get(`${base}/api/v1/preferences`, () => HttpResponse.json(preferences)),
  http.get(`${base}/api/v1/images`, () => HttpResponse.json([])),
  http.post(`${base}/api/v1/images`, async ({ request }) => {
    const body = (await request.json()) as ImageInput
    requests.push(body)
    return HttpResponse.json(
      { run_id: body.request_id, state: 'queued', images: [] },
      { status: 202 },
    )
  }),
)
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterAll(() => server.close())
afterEach(() => {
  server.resetHandlers()
  requests.length = 0
})

function images() {
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={cache}>
      <MemoryRouter>
        <PreferencesProvider>
          <ImagesPage />
        </PreferencesProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return cache
}

it('retries a failed file display locally without collecting or generating again', async () => {
  const saved = { run_id: 'kept', state: 'saved', images: ['scene.png'] }
  server.use(
    http.get(`${base}/api/v1/images`, () => HttpResponse.json([saved])),
    http.get(`${base}/api/v1/images/kept`, () => HttpResponse.json(saved)),
  )
  const cache = images()
  const original = await screen.findByRole('img')
  fireEvent.error(original)
  expect(await screen.findByRole('alert')).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: m.retryCollection }))
  const retried = screen.getByRole('img')
  expect(retried).not.toBe(original)
  expect(retried).toHaveAttribute('src', '/api/v1/images/kept/files/0')
  fireEvent.load(retried)
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(requests).toHaveLength(0)
  cache.clear()
})

it('can retry a saved image lookup without a generation connection', async () => {
  let broken = false
  let collections = 0
  const saved = { run_id: 'kept', state: 'saved', images: ['scene.png'] }
  server.use(
    http.get(`${base}/api/v1/images`, () => HttpResponse.json([saved])),
    http.get(`${base}/api/v1/images/kept`, () =>
      broken
        ? HttpResponse.json({ code: 'connection_failed' }, { status: 503 })
        : HttpResponse.json(saved),
    ),
    http.post(`${base}/api/v1/images/kept/collect`, () => {
      collections += 1
      return HttpResponse.json(saved)
    }),
  )
  const cache = images()
  await screen.findByRole('img')
  broken = true
  await act(async () => {
    await cache.invalidateQueries({
      queryKey: getImageQueryKey({ path: { identity: 'kept' } }),
    })
  })
  await screen.findByRole('alert')
  await waitFor(() =>
    expect(
      screen.getByRole('button', { name: m.retryCollection }),
    ).toBeEnabled(),
  )
  broken = false
  await userEvent.click(screen.getByRole('button', { name: m.retryCollection }))
  await waitFor(() =>
    expect(screen.queryByRole('alert')).not.toBeInTheDocument(),
  )
  expect(collections).toBe(1)
  expect(requests).toHaveLength(0)
  cache.clear()
})

it('authorizes each explicit image operation without a permission checkbox', async () => {
  images()
  const prompt = await screen.findByLabelText(m.imagePrompt)
  await userEvent.type(prompt, 'A rainy bookshop')
  await userEvent.click(screen.getByRole('button', { name: m.generate }))
  await waitFor(() => expect(requests).toHaveLength(1))
  expect(screen.queryByLabelText(m.allowReal)).not.toBeInTheDocument()
  await waitFor(() =>
    expect(screen.getByRole('button', { name: m.generate })).not.toBeDisabled(),
  )
  await userEvent.click(screen.getByRole('button', { name: m.generate }))
  await waitFor(() => expect(requests).toHaveLength(2))
  expect(requests[0].allow_real_llm).toBe(true)
  expect(requests[1].allow_real_llm).toBe(true)
})

it('allows a new authorized operation after a pre-submission rejection', async () => {
  server.use(
    http.post(`${base}/api/v1/images`, async ({ request }) => {
      const body = (await request.json()) as ImageInput
      requests.push(body)
      return requests.length === 1
        ? HttpResponse.json({ code: 'real_llm_not_allowed' }, { status: 403 })
        : HttpResponse.json(
            { run_id: body.request_id, state: 'queued', images: [] },
            { status: 202 },
          )
    }),
  )
  images()
  const prompt = await screen.findByLabelText(m.imagePrompt)
  await userEvent.type(prompt, '雨の古書店')
  await userEvent.click(screen.getByRole('button', { name: m.generate }))
  expect(await screen.findByRole('alert')).toHaveTextContent(
    m.errors.real_llm_not_allowed,
  )
  expect(prompt).not.toBeDisabled()
  await userEvent.click(screen.getByRole('button', { name: m.generate }))
  await waitFor(() => expect(requests).toHaveLength(2))
  expect(requests[1].allow_real_llm).toBe(true)
  expect(requests[1].request_id).not.toBe(requests[0].request_id)
})

it('waits for saved preferences before showing generation controls', async () => {
  let release: () => void = () => {}
  const pending = new Promise<void>((resolve) => {
    release = resolve
  })
  server.use(
    http.get(`${base}/api/v1/preferences`, async () => {
      await pending
      return HttpResponse.json(preferences)
    }),
  )
  images()
  try {
    expect(screen.queryByLabelText(m.imagePrompt)).not.toBeInTheDocument()
  } finally {
    release()
  }
  const prompt = await screen.findByLabelText(m.imagePrompt)
  await userEvent.type(prompt, 'A bookshop')
  await userEvent.click(screen.getByRole('button', { name: m.generate }))
  await waitFor(() => expect(requests).toHaveLength(1))
  expect(requests[0].llm_profile).toBe('local')
})

it('recovers an initial image lookup failure without submitting again', async () => {
  let reads = 0
  const saved = { run_id: 'kept', state: 'saved', images: ['scene.png'] }
  server.use(
    http.get(`${base}/api/v1/images`, () =>
      HttpResponse.json([{ ...saved, state: 'queued', images: [] }]),
    ),
    http.get(`${base}/api/v1/images/kept`, () =>
      ++reads === 1
        ? HttpResponse.json({ code: 'storage_failed' }, { status: 500 })
        : HttpResponse.json(saved),
    ),
    http.post(`${base}/api/v1/images/kept/collect`, () =>
      HttpResponse.json(saved),
    ),
  )
  images()
  expect(await screen.findByRole('alert')).toHaveTextContent(
    m.errors.storage_failed,
  )
  await userEvent.click(screen.getByRole('button', { name: m.retryCollection }))
  expect(
    await screen.findByRole('img', {
      name: m.imageAlt.replace('{index}', '1'),
    }),
  ).toBeVisible()
  expect(requests).toHaveLength(0)
})
