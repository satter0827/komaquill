import { QueryClient, QueryObserver } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vitest'
import type {
  CreationJob,
  OperationView,
  Run,
} from '../../../../src/frontend/src/api/generated/types.gen'
import {
  creationOptions,
  operationOptions,
  watchReceipt,
  imageOptions,
  conversationOperationsOptions,
} from '../../../../src/frontend/src/shared/operations'

describe('late operation observations', () => {
  it('waits for the required receipt generation before confirming it once', () => {
    const cache = new QueryClient()
    const key = operationOptions('turn').queryKey
    const old: OperationView = {
      id: 'turn',
      conversation_id: 'scene',
      state: 'interrupted',
      resume_count: 0,
    }
    cache.setQueryData(key, old)
    const confirm = vi.fn()
    const unsubscribe = watchReceipt<OperationView>(
      cache,
      key,
      'turn',
      confirm,
      (value) => (value.resume_count ?? 0) >= 1,
    )
    expect(confirm).not.toHaveBeenCalled()
    cache.setQueryData(key, old)
    const accepted = { ...old, state: 'accepted' as const, resume_count: 1 }
    cache.setQueryData(key, accepted)
    cache.setQueryData(key, { ...accepted, state: 'complete' })
    expect(confirm).toHaveBeenCalledExactlyOnceWith(accepted)
    unsubscribe()
    cache.clear()
  })
  it('orders resumptions before terminal states in individual and list observations', async () => {
    const cache = new QueryClient()
    const single = operationOptions('turn')
    const list = conversationOperationsOptions('scene')
    const interrupted: OperationView = {
      id: 'turn',
      conversation_id: 'scene',
      state: 'interrupted',
      can_resume: true,
      resume_count: 0,
    }
    const running: OperationView = {
      ...interrupted,
      state: 'running',
      can_resume: false,
      resume_count: 1,
    }
    const complete: OperationView = { ...running, state: 'complete' }
    cache.setQueryData(single.queryKey, interrupted)
    cache.setQueryData(list.queryKey, [interrupted])
    await cache.fetchQuery({ ...single, queryFn: async () => running })
    await cache.fetchQuery({ ...list, queryFn: async () => [running] })
    expect(cache.getQueryData(single.queryKey)).toEqual(running)
    expect(cache.getQueryData(list.queryKey)).toEqual([running])
    await cache.fetchQuery({ ...single, queryFn: async () => interrupted })
    await cache.fetchQuery({ ...list, queryFn: async () => [interrupted] })
    expect(cache.getQueryData(single.queryKey)).toEqual(running)
    expect(cache.getQueryData(list.queryKey)).toEqual([running])
    await cache.fetchQuery({ ...single, queryFn: async () => complete })
    await cache.fetchQuery({ ...list, queryFn: async () => [complete] })
    await cache.fetchQuery({ ...single, queryFn: async () => running })
    await cache.fetchQuery({ ...list, queryFn: async () => [running] })
    expect(cache.getQueryData(single.queryKey)).toEqual(complete)
    expect(cache.getQueryData(list.queryKey)).toEqual([complete])
    cache.clear()
  })
  it('keeps observing the old interruption until the requested resumption is visible', async () => {
    const cache = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    let calls = 0
    const observer = new QueryObserver(cache, {
      ...operationOptions('turn', 1),
      queryFn: async (): Promise<OperationView> => ({
        id: 'turn',
        conversation_id: 'scene',
        state: ++calls === 1 ? 'interrupted' : 'complete',
        resume_count: calls === 1 ? 0 : 1,
      }),
    })
    const unsubscribe = observer.subscribe(() => {})
    try {
      await vi.waitFor(
        () => expect(observer.getCurrentResult().data?.state).toBe('complete'),
        { timeout: 3000 },
      )
      expect(calls).toBe(2)
    } finally {
      unsubscribe()
      cache.clear()
    }
  })
  it('retains completed operations when an earlier list response arrives later', async () => {
    const cache = new QueryClient()
    const options = conversationOperationsOptions('scene')
    let resolve!: (values: OperationView[]) => void
    const response = new Promise<OperationView[]>((done) => {
      resolve = done
    })
    const pending = cache.fetchQuery({ ...options, queryFn: () => response })
    const complete: OperationView = {
      id: 'turn',
      conversation_id: 'scene',
      state: 'complete',
      input: { text: 'Saved' },
    }
    cache.setQueryData(options.queryKey, [complete])
    resolve([{ ...complete, state: 'running' }])
    await pending
    expect(cache.getQueryData(options.queryKey)).toEqual([complete])
    cache.clear()
  })
  it('consumes an already cached image receipt once without waiting for another response', () => {
    const cache = new QueryClient()
    const key = imageOptions('image').queryKey
    const saved = {
      run_id: 'image',
      state: 'saved',
      images: ['scene.png'],
    } as Run
    cache.setQueryData(key, saved)
    const confirm = vi.fn((value: Run) => cache.setQueryData(key, value))
    const unsubscribe = watchReceipt(cache, key, 'image', confirm)
    expect(confirm).toHaveBeenCalledExactlyOnceWith(saved)
    cache.setQueryData(key, { ...saved, state: 'queued' })
    expect(confirm).toHaveBeenCalledTimes(1)
    unsubscribe()
    cache.clear()
  })
  it('confirms only the matching receipt once, including when confirmation updates the cache', () => {
    const cache = new QueryClient()
    const key = operationOptions('turn').queryKey
    const confirm = vi.fn((value: OperationView) =>
      cache.setQueryData(key, value),
    )
    const unsubscribe = watchReceipt(cache, key, 'turn', confirm)
    const saved: OperationView = {
      input: { text: 'Saved input' },
      id: 'turn',
      conversation_id: 'conversation',
      state: 'complete',
    }
    try {
      cache.setQueryData(creationOptions('turn').queryKey, {
        id: 'turn',
        state: 'complete',
        request: { operation_id: 'turn', kind: 'portrait' },
      })
      cache.setQueryData(key, { ...saved, id: 'different' })
      expect(confirm).not.toHaveBeenCalled()
      cache.setQueryData(key, saved)
      expect(confirm).toHaveBeenCalledExactlyOnceWith(saved)
      cache.setQueryData(key, { ...saved, state: 'accepted' })
      expect(confirm).toHaveBeenCalledTimes(1)
    } finally {
      unsubscribe()
      cache.clear()
    }
  })

  it('keeps polling a pending image and stops after its terminal observation', async () => {
    const cache = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    let calls = 0
    const observer = new QueryObserver(cache, {
      ...operationOptions('turn'),
      queryFn: async (): Promise<OperationView> => ({
        input: { text: 'Saved input' },
        id: 'turn',
        conversation_id: 'conversation',
        state: 'complete',
        image_state: ++calls === 1 ? 'pending' : 'submitted',
      }),
    })
    const unsubscribe = observer.subscribe(() => {})
    try {
      await vi.waitFor(() => expect(calls).toBe(2), { timeout: 3000 })
      await new Promise((resolve) => setTimeout(resolve, 1100))
      expect(calls).toBe(2)
    } finally {
      unsubscribe()
      cache.clear()
    }
  })

  it('retains a completed creation when an earlier poll arrives later', async () => {
    const cache = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    const options = creationOptions('creation')
    let resolve!: (job: CreationJob) => void
    const request = new Promise<CreationJob>((done) => {
      resolve = done
    })
    const pending = cache.fetchQuery({ ...options, queryFn: () => request })
    const completed: CreationJob = {
      id: 'creation',
      state: 'complete',
      image_id: 'saved-image',
      request: { operation_id: 'creation', kind: 'portrait' },
    }
    cache.setQueryData(options.queryKey, completed)
    resolve({ ...completed, state: 'accepted', image_id: null })
    await pending
    expect(cache.getQueryData(options.queryKey)).toEqual(completed)
    cache.clear()
  })

  it('retains the submitted image reference after text has already completed', async () => {
    const cache = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    const options = operationOptions('turn')
    let resolve!: (operation: OperationView) => void
    const request = new Promise<OperationView>((done) => {
      resolve = done
    })
    const pending = cache.fetchQuery({ ...options, queryFn: () => request })
    const saved: OperationView = {
      input: { text: 'Saved input' },
      id: 'turn',
      conversation_id: 'conversation',
      state: 'complete',
      image_state: 'submitted',
      image_id: 'image',
    }
    cache.setQueryData(options.queryKey, saved)
    resolve({ ...saved, image_state: 'pending', image_id: null })
    await pending
    expect(cache.getQueryData<OperationView>(options.queryKey)).toMatchObject({
      image_state: 'submitted',
      image_id: 'image',
    })
    cache.clear()
  })

  it('does not replace committed success with a delayed failed observation', async () => {
    const cache = new QueryClient()
    const options = operationOptions('turn')
    let resolve!: (operation: OperationView) => void
    const pending = cache.fetchQuery({
      ...options,
      queryFn: () =>
        new Promise<OperationView>((done) => {
          resolve = done
        }),
    })
    const completed: OperationView = {
      id: 'turn',
      conversation_id: 'conversation',
      state: 'complete',
      input: null,
    }
    cache.setQueryData(options.queryKey, completed)
    resolve({ ...completed, state: 'failed', error: 'connection_failed' })
    await pending
    expect(cache.getQueryData(options.queryKey)).toEqual(completed)
    cache.clear()
  })
})
