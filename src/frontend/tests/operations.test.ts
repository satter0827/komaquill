import { QueryClient, QueryObserver } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vitest'
import type { CreationJob, OperationView } from '../src/api/generated/types.gen'
import {
  creationOptions,
  operationOptions,
  watchReceipt,
} from '../src/features/operations'

describe('late operation observations', () => {
  it('confirms only the matching receipt once, including when confirmation updates the cache', () => {
    const cache = new QueryClient()
    const key = operationOptions('turn').queryKey
    const confirm = vi.fn((value: OperationView) =>
      cache.setQueryData(key, value),
    )
    const unsubscribe = watchReceipt(cache, key, 'turn', confirm)
    const saved: OperationView = {
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
})
