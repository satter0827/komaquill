import { afterAll, afterEach, beforeAll, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { App } from '../src/app/shell'
import { client } from '../src/api/generated/client.gen'
import m from '../src/app/locales/ja.json'

const base = 'http://localhost'
client.setConfig({ baseUrl: base, throwOnError: true })
const server = setupServer(
  http.get(`${base}/api/v1/conversations`, () => HttpResponse.json([])),
  http.get(`${base}/api/v1/stories`, () =>
    HttpResponse.json({ code: 'storage_failed' }, { status: 500 }),
  ),
)
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterAll(() => server.close())
afterEach(() => server.resetHandlers())

it('shows a failed library load and recovers without losing saved stories', async () => {
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <MemoryRouter>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  expect(await screen.findByRole('alert')).toHaveTextContent(
    m.errors.storage_failed,
  )
  server.use(
    http.get(`${base}/api/v1/stories`, () =>
      HttpResponse.json([
        {
          id: 'saved',
          name: 'Saved story',
          description: 'Retained setting',
          revision: 0,
        },
      ]),
    ),
  )
  await userEvent.click(screen.getByRole('button', { name: m.refresh }))
  expect(
    await screen.findByRole('heading', { name: 'Saved story' }),
  ).toBeVisible()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})
