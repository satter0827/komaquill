import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  API_READ_TIMEOUT_MS,
  API_REQUEST_TIMEOUT_MS,
} from '../../../../src/frontend/src/api/client'
import { client } from '../../../../src/frontend/src/api/generated/client.gen'
import {
  getConversation,
  sendTurn,
} from '../../../../src/frontend/src/api/generated/sdk.gen'

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('HTTP request lifetime', () => {
  it.each(['deadline', 'caller'] as const)(
    'aborts an unresponsive read through %s cancellation',
    async (source) => {
      const deadline = new AbortController()
      const caller = new AbortController()
      const timeout = vi
        .spyOn(AbortSignal, 'timeout')
        .mockReturnValue(deadline.signal)
      const fetch = vi.fn(
        (request: Request) =>
          new Promise<Response>((_, reject) => {
            request.signal.addEventListener(
              'abort',
              () => reject(request.signal.reason),
              { once: true },
            )
          }),
      )
      vi.stubGlobal('fetch', fetch)
      client.setConfig({ baseUrl: 'http://localhost', throwOnError: true })
      const pending = getConversation({
        path: { identity: 'scene' },
        signal: caller.signal,
      })
      const rejected = expect(pending).rejects.toThrow('Stopped request')
      await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
      expect(timeout).toHaveBeenCalledWith(API_READ_TIMEOUT_MS)
      const request = fetch.mock.calls[0][0]
      expect(request.signal.aborted).toBe(false)
      ;(source === 'deadline' ? deadline : caller).abort(
        new DOMException('Stopped request', 'AbortError'),
      )
      await rejected
      expect(request.signal.aborted).toBe(true)
      expect(fetch).toHaveBeenCalledTimes(1)
    },
  )

  it('preserves a submitted body and stops without automatically resending it', async () => {
    const deadline = new AbortController()
    const timeout = vi
      .spyOn(AbortSignal, 'timeout')
      .mockReturnValue(deadline.signal)
    const fetch = vi.fn(
      (request: Request) =>
        new Promise<Response>((_, reject) => {
          request.signal.addEventListener(
            'abort',
            () => reject(request.signal.reason),
            { once: true },
          )
        }),
    )
    vi.stubGlobal('fetch', fetch)
    client.setConfig({ baseUrl: 'http://localhost', throwOnError: true })
    const body = {
      operation_id: 'retained',
      expected_revision: 0,
      text: 'A retained message',
    }
    const pending = sendTurn({ path: { identity: 'scene' }, body })
    const rejected = expect(pending).rejects.toThrow('Request deadline reached')
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
    expect(await fetch.mock.calls[0][0].clone().json()).toEqual(body)
    expect(timeout).toHaveBeenCalledWith(API_REQUEST_TIMEOUT_MS)
    deadline.abort(new DOMException('Request deadline reached', 'TimeoutError'))
    await rejected
    expect(fetch).toHaveBeenCalledTimes(1)
  })
})
