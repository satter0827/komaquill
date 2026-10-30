import { afterAll, afterEach, beforeAll, expect, it } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { ImagesPage } from '../src/features/images'
import { PreferencesProvider, defaultPreferences } from '../src/app/preferences'
import { client } from '../src/api/generated/client.gen'
import type { ImageInput } from '../src/api/generated/types.gen'
import m from '../src/app/locales/ja.json'

const base = 'http://localhost'
client.setConfig({ baseUrl: base, throwOnError: true })
const requests: ImageInput[] = []
const preferences = { ...defaultPreferences, llm_profile: 'local' }
const server = setupServer(
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
      <PreferencesProvider>
        <ImagesPage />
      </PreferencesProvider>
    </QueryClientProvider>,
  )
}

it('consumes permission for one image operation', async () => {
  images()
  const prompt = await screen.findByLabelText(m.imagePrompt)
  await userEvent.type(prompt, 'A rainy bookshop')
  await userEvent.click(screen.getByLabelText(m.allowReal))
  await userEvent.click(screen.getByRole('button', { name: m.generate }))
  await waitFor(() => expect(requests).toHaveLength(1))
  expect(screen.getByLabelText(m.allowReal)).not.toBeChecked()
  await waitFor(() =>
    expect(screen.getByRole('button', { name: m.generate })).not.toBeDisabled(),
  )
  await userEvent.click(screen.getByRole('button', { name: m.generate }))
  await waitFor(() => expect(requests).toHaveLength(2))
  expect(requests[0].allow_real_llm).toBe(true)
  expect(requests[1].allow_real_llm).toBe(false)
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
  await userEvent.click(screen.getByLabelText(m.allowReal))
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
