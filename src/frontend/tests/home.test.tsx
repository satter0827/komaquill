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

it.each(['failed', 'unavailable'])(
  'keeps a %s image separate from the story and links to recovery',
  async (state) => {
    server.use(
      http.get(`${base}/api/v1/stories`, () =>
        HttpResponse.json([
          {
            id: 'saved',
            name: 'Saved story',
            description: 'Retained setting',
            revision: 0,
            image_job_id: 'failed',
          },
        ]),
      ),
      http.get(`${base}/api/v1/creation-jobs/failed`, () =>
        state === 'unavailable'
          ? HttpResponse.json({ code: 'request_not_found' }, { status: 404 })
          : HttpResponse.json({
              id: 'failed',
              state: 'failed',
              error: 'fake_input_mismatch',
            }),
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
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    expect(await screen.findByText(m.imageUnavailable)).toBeVisible()
    expect(
      screen.getByRole('link', { name: `${m.createImage} →` }),
    ).toHaveAttribute('href', '/stories/saved')
    expect(screen.getByText('Retained setting')).toBeVisible()
    expect(
      screen.queryByText(m.errors.fake_input_mismatch),
    ).not.toBeInTheDocument()
  },
)

it('offers creation when only samples for a different language remain', async () => {
  server.use(
    http.get(`${base}/api/v1/stories`, () =>
      HttpResponse.json([
        {
          id: 'en-only',
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
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  expect(await screen.findByText(m.noPresets)).toBeVisible()
  expect(screen.getByRole('link', { name: `${m.new} →` })).toHaveAttribute(
    'href',
    '/stories/new',
  )
})
