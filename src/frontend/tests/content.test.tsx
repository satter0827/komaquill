import { afterAll, afterEach, beforeAll, expect, it } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { PreferencesProvider } from '../src/app/preferences'
import { ContentSettings } from '../src/features/content'
import { client } from '../src/api/generated/client.gen'
import m from '../src/app/locales/ja.json'

const base = 'http://localhost'
client.setConfig({ baseUrl: base, throwOnError: true })
const server = setupServer()
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

it('identifies unreadable records and keeps healthy classifications editable', async () => {
  const broken = '00000000-0000-4000-8000-000000000001'
  const healthy = '00000000-0000-4000-8000-000000000002'
  let rating = 'general'
  server.use(
    http.get(`${base}/api/v1/preferences`, () =>
      HttpResponse.json({ nsfw_enabled: true, locale: 'ja', revision: 0 }),
    ),
    http.get(`${base}/api/v1/content`, () =>
      HttpResponse.json([
        {
          id: broken,
          kind: 'archive',
          rating: 'unclassified',
          readable: false,
        },
        {
          id: healthy,
          kind: 'conversation',
          title: 'Usable scene',
          rating,
          revision: 0,
          readable: true,
        },
      ]),
    ),
    http.put(`${base}/api/v1/content`, async ({ request }) => {
      const body = (await request.json()) as { id: string; rating: string }
      expect(body.id).toBe(healthy)
      rating = body.rating
      return HttpResponse.json({
        ...body,
        kind: 'conversation',
        title: 'Usable scene',
        revision: 1,
        readable: true,
      })
    }),
  )
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={cache}>
      <PreferencesProvider>
        <ContentSettings />
      </PreferencesProvider>
    </QueryClientProvider>,
  )
  expect(await screen.findByText(m.contentUnreadable)).toBeVisible()
  expect(screen.getByText(broken)).toBeVisible()
  expect(
    screen.getByRole('combobox', {
      name: `${m.contentClassification}: ${m.contentItem}`,
    }),
  ).toBeDisabled()
  const usable = screen.getByRole('combobox', {
    name: `${m.contentClassification}: Usable scene`,
  })
  await userEvent.selectOptions(usable, 'nsfw')
  await waitFor(() => expect(usable).toHaveValue('nsfw'))
  cache.clear()
})
