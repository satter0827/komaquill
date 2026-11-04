import { afterAll, afterEach, beforeAll, expect, it } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { useState } from 'react'
import {
  AvailabilityNotice,
  recheckAvailability,
  useAvailability,
} from '../src/features/availability'
import { defaultPreferences } from '../src/app/preferences'
import { Button } from '../src/components/ui/button'
import { client } from '../src/api/generated/client.gen'
import m from '../src/app/locales/ja.json'

const base = 'http://localhost'
client.setConfig({ baseUrl: base, throwOnError: true })
const server = setupServer()
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

it('uses fixed content policy without a mutable viewing preference', () => {
  expect(defaultPreferences).not.toHaveProperty('nsfw_enabled')
})

it('keeps a delayed refresh attached to the input it checked', async () => {
  let release: () => void = () => {}
  let checkingOldInput = false
  const pending = new Promise<void>((resolve) => {
    release = resolve
  })
  server.use(
    http.post(`${base}/api/v1/availability`, async ({ request }) => {
      const body = (await request.json()) as { prompt: string }
      if (
        body.prompt === 'A coast' &&
        new URL(request.url).searchParams.get('refresh') === 'true'
      ) {
        checkingOldInput = true
        await pending
      }
      const ready = body.prompt === 'A coast'
      const now = new Date()
      return HttpResponse.json({
        ready,
        connections: ready
          ? []
          : [
              {
                kind: 'llm',
                target: 'local',
                state: 'unavailable',
                reason: 'llm_unavailable',
                observed_at: now.toISOString(),
                expires_at: new Date(now.getTime() + 15000).toISOString(),
              },
            ],
      })
    }),
  )
  function Control() {
    const [prompt, setPrompt] = useState('A coast')
    const status = useAvailability({ kind: 'image', prompt })
    return (
      <>
        <input
          aria-label="Prompt"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
        />
        <Button disabled={!status.ready}>{m.generate}</Button>
        <AvailabilityNotice status={status} />
      </>
    )
  }
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  render(
    <QueryClientProvider client={cache}>
      <MemoryRouter>
        <Control />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  try {
    await waitFor(() =>
      expect(screen.getByRole('button', { name: m.generate })).toBeEnabled(),
    )
    act(() => window.dispatchEvent(new Event('focus')))
    await waitFor(() => expect(checkingOldInput).toBe(true))
    await userEvent.clear(screen.getByLabelText('Prompt'))
    await userEvent.type(screen.getByLabelText('Prompt'), '海辺')
    await act(async () => {
      release()
      await pending
    })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: m.refresh })).toBeEnabled(),
    )
    expect(screen.getByRole('button', { name: m.generate })).toBeDisabled()
    expect(screen.getByText(m.errors.llm_unavailable)).toBeVisible()
  } finally {
    release()
    cache.clear()
  }
})

it('keeps execution disabled until a required connection is confirmed and refreshes explicitly', async () => {
  let available = false
  server.use(
    http.post(`${base}/api/v1/availability`, ({ request }) => {
      if (new URL(request.url).searchParams.get('refresh') === 'true')
        available = true
      const now = new Date()
      return HttpResponse.json({
        ready: available,
        connections: [
          {
            kind: 'llm',
            target: 'local',
            state: available ? 'ready' : 'unavailable',
            reason: available ? null : 'llm_unavailable',
            observed_at: now.toISOString(),
            expires_at: new Date(now.getTime() + 15000).toISOString(),
          },
        ],
      })
    }),
  )
  function Control() {
    const status = useAvailability({ kind: 'image', prompt: 'A coast' })
    return (
      <>
        <Button disabled={!status.ready}>{m.generate}</Button>
        <AvailabilityNotice status={status} />
      </>
    )
  }
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  render(
    <QueryClientProvider client={cache}>
      <MemoryRouter>
        <Control />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  expect(screen.getByRole('button', { name: m.generate })).toBeDisabled()
  await waitFor(() =>
    expect(screen.getByRole('button', { name: m.refresh })).toBeEnabled(),
  )
  await userEvent.click(screen.getByRole('button', { name: m.refresh }))
  await waitFor(() =>
    expect(screen.getByRole('button', { name: m.generate })).toBeEnabled(),
  )
  cache.clear()
})

it('bypasses retained connection observations after an execution failure and on focus', async () => {
  let connected = true
  let forced = 0
  server.use(
    http.post(`${base}/api/v1/availability`, ({ request }) => {
      const refresh =
        new URL(request.url).searchParams.get('refresh') === 'true'
      if (refresh) forced += 1
      const ready = refresh ? connected : true
      const now = new Date()
      return HttpResponse.json({
        ready,
        connections: [
          {
            kind: 'comfy',
            target: 'current',
            state: ready ? 'ready' : 'unavailable',
            reason: ready ? null : 'image_backend_unavailable',
            observed_at: now.toISOString(),
            expires_at: new Date(now.getTime() + 15000).toISOString(),
          },
        ],
      })
    }),
  )
  function Control() {
    const status = useAvailability({ kind: 'image', prompt: 'A coast' })
    return <Button disabled={!status.ready}>{m.generate}</Button>
  }
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  render(
    <QueryClientProvider client={cache}>
      <Control />
    </QueryClientProvider>,
  )
  const button = screen.getByRole('button', { name: m.generate })
  await waitFor(() => expect(button).toBeEnabled())
  connected = false
  await act(async () => {
    await recheckAvailability(cache)
  })
  await waitFor(() => expect(button).toBeDisabled())
  expect(forced).toBe(1)
  connected = true
  act(() => window.dispatchEvent(new Event('focus')))
  await waitFor(() => expect(button).toBeEnabled())
  expect(forced).toBe(2)
  cache.clear()
})

it('disables execution when a fresh check fails even if the retained observation was ready', async () => {
  let rejected = true
  server.use(
    http.post(`${base}/api/v1/availability`, ({ request }) => {
      if (
        new URL(request.url).searchParams.get('refresh') === 'true' &&
        rejected
      )
        return HttpResponse.json({ code: 'connection_failed' }, { status: 503 })
      return HttpResponse.json({ ready: true, connections: [] })
    }),
  )
  function Control() {
    const status = useAvailability({ kind: 'image', prompt: 'A coast' })
    return (
      <>
        <Button disabled={!status.ready}>{m.generate}</Button>
        <AvailabilityNotice status={status} />
      </>
    )
  }
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  render(
    <QueryClientProvider client={cache}>
      <MemoryRouter>
        <Control />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  const button = screen.getByRole('button', { name: m.generate })
  await waitFor(() => expect(button).toBeEnabled())
  act(() => window.dispatchEvent(new Event('focus')))
  await waitFor(() =>
    expect(screen.getByRole('button', { name: m.refresh })).toBeEnabled(),
  )
  expect(button).toBeDisabled()
  expect(screen.getByText(m.errors.connection_failed)).toBeVisible()
  rejected = false
  await userEvent.click(screen.getByRole('button', { name: m.refresh }))
  await waitFor(() => expect(button).toBeEnabled())
  cache.clear()
})
