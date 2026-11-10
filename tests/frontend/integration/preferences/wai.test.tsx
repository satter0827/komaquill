import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter } from 'react-router'
import { afterAll, beforeAll, expect, it } from 'vitest'
import { client } from '../../../../src/frontend/src/api/generated/client.gen'
import type { Preferences } from '../../../../src/frontend/src/api/generated/types.gen'
import m from '../../../../src/frontend/src/app/locales/ja.json'
import { PreferencesPage } from '../../../../src/frontend/src/features/preferences'
import {
  defaultPreferences,
  PreferencesProvider,
} from '../../../../src/frontend/src/shared/preferences'

const base = 'http://localhost'
let current: Preferences = {
  ...defaultPreferences,
  image_prompts: { positive: 'window light', negative: 'text' },
}
const server = setupServer(
  http.get(`${base}/api/v1/preferences`, () => HttpResponse.json(current)),
  http.put(`${base}/api/v1/preferences`, async ({ request }) => {
    const saved = (await request.json()) as Preferences
    current = { ...saved, revision: (saved.revision ?? 0) + 1 }
    return HttpResponse.json(current)
  }),
  http.get(`${base}/api/v1/settings`, () =>
    HttpResponse.json({
      profiles: [],
      sample_input: '',
      supports_negative_prompt: true,
      wai: {
        defaults: {
          quality_enabled: true,
          quality_positive: 'anime illustration',
          quality_negative: 'sketch',
        },
        choices: {},
        max_candidates: 4,
      },
    }),
  ),
)

beforeAll(() => {
  client.setConfig({ baseUrl: base, throwOnError: true })
  server.listen({ onUnhandledRequest: 'error' })
})
afterAll(() => server.close())

it('saves a cleared quality prompt and restores inherited quality without losing other settings', async () => {
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  const user = userEvent.setup()
  render(
    <MemoryRouter>
      <QueryClientProvider client={cache}>
        <PreferencesProvider>
          <PreferencesPage connections={null} />
        </PreferencesProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  )

  const positive = await screen.findByLabelText(m.waiQualityPositive)
  expect(positive).toHaveValue('anime illustration')
  await user.clear(positive)
  await user.click(screen.getByLabelText(m.waiQualityEnabled))
  await user.click(screen.getByText(m.waiAdvanced))
  await user.type(screen.getByLabelText(m.waiSeed), '0')
  const save = screen.getByRole('button', { name: m.save })
  await user.click(save)
  await waitFor(() =>
    expect(current.image_prompts).toEqual({
      positive: 'window light',
      negative: 'text',
      wai: { quality_enabled: false, quality_positive: '', seed: 0 },
    }),
  )
  await waitFor(() => expect(save).toBeDisabled())
  expect(positive).toHaveValue('')

  await user.click(screen.getByRole('button', { name: m.waiResetQuality }))
  await user.click(save)
  await waitFor(() =>
    expect(current.image_prompts).toEqual({
      positive: 'window light',
      negative: 'text',
      wai: {
        quality_enabled: null,
        quality_positive: null,
        quality_negative: null,
        seed: 0,
      },
    }),
  )
  expect(positive).toHaveValue('anime illustration')
})
