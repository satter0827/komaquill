import {
  retainImage,
  retainImageList,
} from '../../../../src/frontend/src/shared/operations'
import { MemoryRouter } from 'react-router'
import { afterAll, afterEach, beforeAll, expect, it } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { useState } from 'react'
import {
  ImagesPage,
  groupImageDays,
} from '../../../../src/frontend/src/features/images/index'
import { ImageResult } from '../../../../src/frontend/src/shared/images/result'
import { OriginalImage } from '../../../../src/frontend/src/shared/images/original'
import {
  ImageActivityProvider,
  imageMonitorKey,
  useTrackCreation,
  useTrackImage,
} from '../../../../src/frontend/src/shared/image-activity'
import {
  PreferencesProvider,
  defaultPreferences,
} from '../../../../src/frontend/src/shared/preferences'
import { client } from '../../../../src/frontend/src/api/generated/client.gen'
import type {
  ImageInput,
  Run,
} from '../../../../src/frontend/src/api/generated/types.gen'
import { getImageQueryKey } from '../../../../src/frontend/src/api/generated/@tanstack/react-query.gen'
import {
  CreationStatus,
  useCreation,
} from '../../../../src/frontend/src/shared/creation'
import m from '../../../../src/frontend/src/app/locales/ja.json'

const base = 'http://localhost'
client.setConfig({ baseUrl: base, throwOnError: true })
const requests: ImageInput[] = []
const records = new Map<string, Run>()
let reads = 0
let collections = 0
function saved(id: string, state: Run['state'] = 'saved'): Run {
  return {
    run_id: id,
    state,
    created_at: new Date(2026, 10, 5, 0, 30).toISOString(),
    runtime_id: 'current',
    images: state === 'saved' ? ['scene.png'] : [],
  } as Run
}
const preferences = { ...defaultPreferences, llm_profile: 'local' }
const server = setupServer(
  http.post(`${base}/api/v1/availability`, () =>
    HttpResponse.json({ ready: true, connections: [] }),
  ),
  http.get(`${base}/api/v1/preferences`, () => HttpResponse.json(preferences)),
  http.get(`${base}/api/v1/runtime`, () => HttpResponse.json({ pending: [] })),
  http.get(`${base}/api/v1/images`, () =>
    HttpResponse.json([...records.values()]),
  ),
  http.get(`${base}/api/v1/images/:id`, ({ params }) => {
    reads++
    return records.has(String(params.id))
      ? HttpResponse.json(records.get(String(params.id)))
      : HttpResponse.json({ code: 'request_not_found' }, { status: 404 })
  }),
  http.post(`${base}/api/v1/images/:id/collect`, ({ params }) => {
    collections++
    const value = saved(String(params.id))
    records.set(value.run_id, value)
    return HttpResponse.json(value)
  }),
  http.post(`${base}/api/v1/images`, async ({ request }) => {
    const body = (await request.json()) as ImageInput
    requests.push(body)
    const value = saved(body.request_id, 'queued')
    records.set(value.run_id, value)
    return HttpResponse.json(value, { status: 202 })
  }),
)
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterAll(() => server.close())
afterEach(() => {
  server.resetHandlers()
  records.clear()
  requests.length = 0
  reads = 0
  collections = 0
})
function images() {
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  function Screen() {
    const [visible, setVisible] = useState(true)
    return (
      <>
        <button onClick={() => setVisible(!visible)}>Switch page</button>
        {visible ? <ImagesPage /> : <p>Another page</p>}
      </>
    )
  }
  render(
    <QueryClientProvider client={cache}>
      <MemoryRouter>
        <PreferencesProvider>
          <ImageActivityProvider>
            <Screen />
          </ImageActivityProvider>
        </PreferencesProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return cache
}
async function compose() {
  await userEvent.click(await screen.findByRole('button', { name: m.generate }))
  return screen.findByLabelText(m.imagePrompt)
}
const dayLabel = () =>
  new Intl.DateTimeFormat('ja', { dateStyle: 'long' }).format(
    new Date(2026, 10, 5),
  )

it('keeps all days collapsed and displays saved files without individual lookups or collection', async () => {
  records.set('kept', saved('kept'))
  const cache = images()
  const day = await screen.findByText(dayLabel())
  expect(screen.queryByRole('img')).not.toBeInTheDocument()
  await userEvent.click(day)
  const image = screen.getByRole('img')
  expect(image.closest('a')).toHaveAttribute(
    'href',
    '/api/v1/images/kept/files/0',
  )
  expect(image.closest('a')).toHaveAttribute('target', '_blank')
  expect(reads).toBe(0)
  expect(collections).toBe(0)
  expect(screen.queryByText('画像ができました')).not.toBeInTheDocument()
  await userEvent.click(day)
  expect(screen.queryByRole('img')).not.toBeInTheDocument()
  cache.clear()
})
it('retries file display without collecting or generating again', async () => {
  records.set('kept', saved('kept'))
  const cache = images()
  await userEvent.click(await screen.findByText(dayLabel()))
  const original = screen.getByRole('img')
  fireEvent.error(original)
  await userEvent.click(screen.getByRole('button', { name: m.retryCollection }))
  expect(screen.getByRole('img')).not.toBe(original)
  expect(requests).toHaveLength(0)
  expect(collections).toBe(0)
  cache.clear()
})
it('displays a new image URL after the previous file failed', async () => {
  const view = render(<OriginalImage identity="old" />)
  fireEvent.error(screen.getByRole('img'))
  expect(screen.queryByRole('img')).not.toBeInTheDocument()
  view.rerender(<OriginalImage identity="new" />)
  expect(screen.getByRole('img')).toHaveAttribute(
    'src',
    '/api/v1/images/new/files/0',
  )
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})
it('does not collect an old unfinished run when its day is opened', async () => {
  records.set('old', { ...saved('old', 'queued'), runtime_id: 'past' })
  const cache = images()
  await userEvent.click(await screen.findByText(dayLabel()))
  expect(await screen.findByText(m.imagePreparing)).toBeVisible()
  expect(reads).toBe(0)
  expect(collections).toBe(0)
  cache.clear()
})
it('tracks the accepted run, opens its day, and distinguishes new explicit requests', async () => {
  const cache = images()
  await userEvent.type(await compose(), 'A rainy bookshop')
  await userEvent.click(
    screen.getByRole('button', { name: m.drawRequestedScene }),
  )
  expect(await screen.findByText(m.albumAdded)).toBeVisible()
  expect(screen.getAllByRole('img')).toHaveLength(1)
  expect(screen.getByRole('link', { name: m.viewLatestImage })).toHaveAttribute(
    'href',
    `#image-${requests[0].request_id}`,
  )
  await waitFor(() =>
    expect(
      screen.getByRole('button', { name: m.drawRequestedScene }),
    ).toBeEnabled(),
  )
  await userEvent.click(
    screen.getByRole('button', { name: m.drawRequestedScene }),
  )
  await waitFor(() => expect(requests).toHaveLength(2))
  expect(requests[0].allow_real_llm).toBe(true)
  expect(requests[1].request_id).not.toBe(requests[0].request_id)
  cache.clear()
})
it('retains text and uses a new id after a confirmed pre-submission rejection', async () => {
  server.use(
    http.post(`${base}/api/v1/images`, async ({ request }) => {
      const body = (await request.json()) as ImageInput
      requests.push(body)
      if (requests.length === 1)
        return HttpResponse.json(
          { code: 'real_llm_not_allowed' },
          { status: 403 },
        )
      const value = saved(body.request_id)
      records.set(value.run_id, value)
      return HttpResponse.json(value, { status: 202 })
    }),
  )
  const cache = images()
  const prompt = await compose()
  await userEvent.type(prompt, 'A bookshop')
  await userEvent.click(
    screen.getByRole('button', { name: m.drawRequestedScene }),
  )
  expect(await screen.findByRole('alert')).toHaveTextContent(
    m.errors.real_llm_not_allowed,
  )
  expect(prompt).toHaveValue('A bookshop')
  await userEvent.click(
    screen.getByRole('button', { name: m.drawRequestedScene }),
  )
  await waitFor(() => expect(requests).toHaveLength(2))
  expect(requests[1].request_id).not.toBe(requests[0].request_id)
  cache.clear()
})
it('recovers a lost receipt by reading the same run without submitting twice', async () => {
  server.use(
    http.post(`${base}/api/v1/images`, async ({ request }) => {
      const body = (await request.json()) as ImageInput
      requests.push(body)
      records.set(body.request_id, saved(body.request_id))
      return HttpResponse.error()
    }),
  )
  const cache = images()
  await userEvent.type(await compose(), 'A bookshop')
  await userEvent.click(
    screen.getByRole('button', { name: m.drawRequestedScene }),
  )
  expect(await screen.findByText(m.albumAdded)).toBeVisible()
  expect(requests).toHaveLength(1)
  expect(collections).toBe(0)
  cache.clear()
})
it('continues accepted collection after leaving the album', async () => {
  let release: () => void = () => {}
  const delayed = new Promise<void>((resolve) => {
    release = resolve
  })
  server.use(
    http.post(`${base}/api/v1/images/:id/collect`, async ({ params }) => {
      collections++
      await delayed
      const value = saved(String(params.id))
      records.set(value.run_id, value)
      return HttpResponse.json(value)
    }),
  )
  const cache = images()
  await userEvent.type(await compose(), 'A bookshop')
  await userEvent.click(
    screen.getByRole('button', { name: m.drawRequestedScene }),
  )
  await waitFor(() => expect(collections).toBe(1))
  await userEvent.click(screen.getByRole('button', { name: 'Switch page' }))
  release()
  await waitFor(() =>
    expect(
      cache.getQueryData<Run>(
        getImageQueryKey({ path: { identity: requests[0].request_id } }),
      )?.state,
    ).toBe('saved'),
  )
  await waitFor(() =>
    expect(
      cache
        .getQueryCache()
        .find({ queryKey: imageMonitorKey(requests[0].request_id) })
        ?.getObserversCount() ?? 0,
    ).toBe(0),
  )
  expect(requests).toHaveLength(1)
  cache.clear()
})
it('confirms a lost receipt after leaving the album and collects only that request', async () => {
  let receiptAvailable = false
  server.use(
    http.post(`${base}/api/v1/images`, async ({ request }) => {
      const body = (await request.json()) as ImageInput
      requests.push(body)
      records.set(body.request_id, saved(body.request_id, 'queued'))
      return HttpResponse.error()
    }),
    http.get(`${base}/api/v1/images/:id`, ({ params }) => {
      reads++
      return receiptAvailable
        ? HttpResponse.json(records.get(String(params.id)))
        : HttpResponse.json({ code: 'request_not_found' }, { status: 404 })
    }),
  )
  const cache = images()
  await userEvent.type(await compose(), 'A bookshop')
  await userEvent.click(
    screen.getByRole('button', { name: m.drawRequestedScene }),
  )
  await screen.findByText(m.receiptUnknown)
  await userEvent.click(screen.getByRole('button', { name: 'Switch page' }))
  receiptAvailable = true
  await waitFor(() => expect(collections).toBe(1), { timeout: 3000 })
  expect(requests).toHaveLength(1)
  expect(
    cache.getQueryData<Run>(
      getImageQueryKey({ path: { identity: requests[0].request_id } }),
    )?.state,
  ).toBe('saved')
  cache.clear()
})
it('retains the request when its failed response arrives after the album unmounts', async () => {
  let fail!: () => void
  const delayedFailure = new Promise<void>((resolve) => {
    fail = resolve
  })
  server.use(
    http.post(`${base}/api/v1/images`, async ({ request }) => {
      const body = (await request.json()) as ImageInput
      requests.push(body)
      records.set(body.request_id, saved(body.request_id, 'queued'))
      await delayedFailure
      return HttpResponse.error()
    }),
  )
  const cache = images()
  await userEvent.type(await compose(), 'A bookshop')
  await userEvent.click(
    screen.getByRole('button', { name: m.drawRequestedScene }),
  )
  await waitFor(() => expect(requests).toHaveLength(1))
  await userEvent.click(screen.getByRole('button', { name: 'Switch page' }))
  fail()
  await waitFor(() => expect(collections).toBe(1))
  expect(requests).toHaveLength(1)
  cache.clear()
})
it('groups local calendar dates and counts actual files while retaining terminal observations', () => {
  const first = saved('first')
  const second = { ...saved('second'), images: ['one.png', 'two.png'] }
  const yesterday = {
    ...saved('past'),
    created_at: new Date(2026, 10, 4, 23, 59).toISOString(),
  }
  const groups = groupImageDays([yesterday, first, second], 'ja')
  expect(groups).toHaveLength(2)
  expect(groups[0].key).toBe('2026-11-5')
  expect(groups[0].runs.reduce((sum, run) => sum + run.images!.length, 0)).toBe(
    3,
  )
  expect(retainImageList([first], [saved('first', 'queued')])).toEqual([first])
})

it('retains recovered files while recording a generation failure', () => {
  const partial = { ...saved('partial', 'queued'), images: ['first.png'] }
  const failed = { ...saved('partial', 'failed'), error: 'generation_failed' }
  expect(retainImage(partial, failed)).toMatchObject({
    state: 'failed',
    error: 'generation_failed',
    images: ['first.png'],
  })
})
it('keeps accepted creation jobs alive before an image ID exists', async () => {
  let submitted = false
  server.use(
    http.get(`${base}/api/v1/creation-jobs/accepted`, () =>
      HttpResponse.json({
        id: 'accepted',
        state: submitted ? 'submitted' : 'accepted',
        image_id: submitted ? 'created-image' : null,
      }),
    ),
  )
  records.set('created-image', saved('created-image', 'queued'))
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  function Start() {
    const track = useTrackCreation()
    return (
      <button onClick={() => track('accepted')}>Track accepted creation</button>
    )
  }
  const view = render(
    <QueryClientProvider client={cache}>
      <ImageActivityProvider>
        <Start />
      </ImageActivityProvider>
    </QueryClientProvider>,
  )
  await userEvent.click(
    screen.getByRole('button', { name: 'Track accepted creation' }),
  )
  await waitFor(() =>
    expect(
      cache
        .getQueryCache()
        .getAll()
        .some(
          (query) =>
            query.state.data &&
            (query.state.data as { id?: string }).id === 'accepted',
        ),
    ).toBe(true),
  )
  view.rerender(
    <QueryClientProvider client={cache}>
      <ImageActivityProvider>
        <div>Another page</div>
      </ImageActivityProvider>
    </QueryClientProvider>,
  )
  submitted = true
  await cache.invalidateQueries({
    predicate: (query) =>
      !!query.state.data &&
      (query.state.data as { id?: string }).id === 'accepted',
  })
  await waitFor(() => expect(collections).toBe(1))
  await waitFor(() =>
    expect(
      cache.getQueryData<Run>(
        getImageQueryKey({ path: { identity: 'created-image' } }),
      )?.state,
    ).toBe('saved'),
  )
  cache.clear()
})

it('retains an unconfirmed creation across navigation before its image ID exists', async () => {
  let available = false
  let identity = ''
  let submissions = 0
  server.use(
    http.post(`${base}/api/v1/creation-jobs`, async ({ request }) => {
      identity = ((await request.json()) as { operation_id: string })
        .operation_id
      submissions++
      return HttpResponse.error()
    }),
    http.get(`${base}/api/v1/creation-jobs/:id`, () =>
      available
        ? HttpResponse.json({
            id: identity,
            state: 'submitted',
            image_id: 'portrait',
          })
        : HttpResponse.json({ code: 'request_not_found' }, { status: 404 }),
    ),
  )
  records.set('portrait', saved('portrait', 'queued'))
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  function Editor() {
    const creation = useCreation()
    return (
      <>
        <button
          onClick={() =>
            creation.start({ kind: 'portrait', target_id: 'character' })
          }
        >
          Draw portrait
        </button>
        <CreationStatus creation={creation} />
      </>
    )
  }
  function Pages() {
    const [visible, setVisible] = useState(true)
    return (
      <>
        <button onClick={() => setVisible(!visible)}>Switch editor</button>
        {visible ? <Editor /> : <p>Another page</p>}
      </>
    )
  }
  render(
    <QueryClientProvider client={cache}>
      <ImageActivityProvider>
        <Pages />
      </ImageActivityProvider>
    </QueryClientProvider>,
  )
  await userEvent.click(screen.getByRole('button', { name: 'Draw portrait' }))
  await screen.findByText(m.receiptUnknown)
  await userEvent.click(screen.getByRole('button', { name: 'Switch editor' }))
  available = true
  await waitFor(() => expect(collections).toBe(1), { timeout: 3000 })
  expect(submissions).toBe(1)
  await waitFor(() =>
    expect(
      cache.getQueryData<Run>(
        getImageQueryKey({ path: { identity: 'portrait' } }),
      )?.state,
    ).toBe('saved'),
  )
  cache.clear()
})

it('does not collect retained pending IDs from another session', async () => {
  records.set('old-session', saved('old-session', 'queued'))
  server.use(
    http.get(`${base}/api/v1/runtime`, () =>
      HttpResponse.json({ connection: 'connected', pending: ['old-session'] }),
    ),
    http.post(`${base}/api/v1/availability`, async ({ request }) => {
      const body = (await request.json()) as { kind: string }
      return HttpResponse.json({
        ready: body.kind !== 'collection',
        connections: [],
      })
    }),
  )
  const cache = images()
  await screen.findByRole('heading', { name: m.images })
  await waitFor(() =>
    expect(
      cache
        .getQueryCache()
        .getAll()
        .some(
          (query) =>
            query.queryKey[0] === 'availability' &&
            query.state.status === 'success',
        ),
    ).toBe(true),
  )
  expect(collections).toBe(0)
  expect(reads).toBe(0)
  cache.clear()
})

it('links to recovered partial images while retaining the generation failure', async () => {
  server.use(
    http.post(`${base}/api/v1/images/:id/collect`, ({ params }) => {
      collections++
      const value = {
        ...saved(String(params.id), 'failed'),
        images: ['partial.png'],
        error: 'generation_failed',
        failure: {
          code: 'generation_failed',
          stage: 'generation',
          retryable: false,
        },
      } as Run
      records.set(value.run_id, value)
      return HttpResponse.json(value)
    }),
  )
  const cache = images()
  await userEvent.type(await compose(), 'A coast')
  await userEvent.click(
    screen.getByRole('button', { name: m.drawRequestedScene }),
  )
  expect(await screen.findByText(m.errors.generation_failed)).toBeVisible()
  expect(screen.queryByText(m.albumAdded)).not.toBeInTheDocument()
  expect(screen.getByRole('link', { name: m.viewLatestImage })).toHaveAttribute(
    'href',
    `#image-${requests[0].request_id}`,
  )
  await userEvent.click(document.querySelector('.album-days summary')!)
  expect(await screen.findByRole('img')).toBeVisible()
  expect(collections).toBe(1)
  cache.clear()
})

it('shares the Run read between a displayed image and its active monitor', async () => {
  records.set('concurrent', saved('concurrent', 'queued'))
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  function Start() {
    const track = useTrackImage()
    const [visible, setVisible] = useState(false)
    return (
      <>
        <button
          onClick={() => {
            track('concurrent')
            setVisible(true)
          }}
        >
          Show accepted image
        </button>
        {visible && <ImageResult identity="concurrent" />}
      </>
    )
  }
  render(
    <QueryClientProvider client={cache}>
      <MemoryRouter>
        <ImageActivityProvider>
          <Start />
        </ImageActivityProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  await userEvent.click(
    screen.getByRole('button', { name: 'Show accepted image' }),
  )
  await screen.findByRole('img')
  expect(collections).toBe(1)
  expect(reads).toBe(1)
  cache.clear()
})

it('refreshes connection observations after collection fails while its day is collapsed', async () => {
  let disconnected = false
  let refreshed = 0
  server.use(
    http.post(`${base}/api/v1/availability`, ({ request }) => {
      if (new URL(request.url).searchParams.get('refresh') === 'true')
        refreshed++
      return HttpResponse.json({ ready: !disconnected, connections: [] })
    }),
    http.post(`${base}/api/v1/images/:id/collect`, () => {
      collections++
      disconnected = true
      return HttpResponse.json(
        { code: 'image_backend_unavailable' },
        { status: 503 },
      )
    }),
  )
  const cache = images()
  await userEvent.type(await compose(), 'A coast')
  await userEvent.click(
    screen.getByRole('button', { name: m.drawRequestedScene }),
  )
  await waitFor(() => expect(refreshed).toBeGreaterThan(0))
  await waitFor(() =>
    expect(
      screen.getByRole('button', { name: m.retryCollection }),
    ).toBeDisabled(),
  )
  expect(collections).toBe(1)
  expect(screen.queryByRole('img')).not.toBeInTheDocument()
  expect(requests).toHaveLength(1)
  cache.clear()
})
