import {
  queryOptions,
  type QueryClient,
  type QueryKey,
} from '@tanstack/react-query'
import { POLL_INTERVAL_MS } from '../api/client'
import type {
  CreationJob,
  OperationView,
  Run,
} from '../api/generated/types.gen'
import * as q from '../api/generated/@tanstack/react-query.gen'
import { collectImage, getImage, getJob } from '../api/generated/sdk.gen'

export function isTerminal(state?: string) {
  return ['complete', 'failed', 'interrupted', 'saved', 'lost'].includes(
    state ?? '',
  )
}

export function operationPending(operation: OperationView) {
  return !isTerminal(operation.state) || operation.image_state === 'pending'
}

export function keepTerminal<T extends { state?: string }>(
  current: T | undefined,
  next: T,
): T {
  return current && isTerminal(current.state) && !isTerminal(next.state)
    ? current
    : next
}

export function watchReceipt<T extends { id: string }>(
  cache: QueryClient,
  queryKey: QueryKey,
  identity: string,
  confirm: (value: T) => void,
) {
  const queries = cache.getQueryCache()
  const unsubscribe = queries.subscribe((event) => {
    if (
      event.type !== 'updated' ||
      event.action.type !== 'success' ||
      event.query !== queries.find({ queryKey, exact: true })
    )
      return
    const value = event.query.state.data as T | undefined
    if (value?.id !== identity) return
    unsubscribe()
    confirm(value)
  })
  return unsubscribe
}

export async function observeImage(identity: string) {
  const current = (await getImage({ path: { identity } })).data!
  return isTerminal(current.state)
    ? current
    : (await collectImage({ path: { identity } })).data!
}

export function operationOptions(identity: string) {
  return queryOptions({
    ...q.getOperationOptions({ path: { identity } }),
    structuralSharing: (previous, next) => {
      const current = previous as OperationView | undefined
      const value = keepTerminal(current, next as OperationView)
      return current &&
        ((current.image_state !== 'pending' &&
          value.image_state === 'pending') ||
          (current.image_id && !value.image_id))
        ? {
            ...value,
            image_id: current.image_id,
            image_state: current.image_state,
            image_error: current.image_error,
            image_error_message: current.image_error_message,
          }
        : value
    },
    refetchInterval: (query) =>
      query.state.data && !operationPending(query.state.data)
        ? false
        : POLL_INTERVAL_MS,
  })
}

export function creationOptions(identity: string) {
  return queryOptions({
    ...q.getJobOptions({ path: { identity } }),
    structuralSharing: (previous, next) =>
      keepTerminal(previous as CreationJob | undefined, next as CreationJob),
    queryFn: async () => {
      const current = (await getJob({ path: { identity } })).data!
      if (current.state === 'submitted' && current.image_id) {
        await observeImage(current.image_id)
        return (await getJob({ path: { identity } })).data!
      }
      return current
    },
    refetchInterval: (query) =>
      isTerminal(query.state.data?.state) ? false : POLL_INTERVAL_MS,
  })
}

export function imageOptions(identity: string) {
  return queryOptions({
    ...q.getImageOptions({ path: { identity } }),
    structuralSharing: (previous, next) => {
      const current = previous as Run | undefined
      const value = keepTerminal(current, next as Run)
      return current &&
        (current.images?.length ?? 0) > (value.images?.length ?? 0)
        ? current
        : value
    },
    queryFn: () => observeImage(identity),
    refetchInterval: (query) =>
      query.state.error || isTerminal(query.state.data?.state)
        ? false
        : POLL_INTERVAL_MS,
  })
}

export function presetKey(kind: 'player' | 'character' | 'story') {
  return kind === 'player'
    ? q.getPlayerQueryKey()
    : kind === 'character'
      ? q.listCharactersQueryKey()
      : q.listStoriesQueryKey()
}

export function refreshPresets(cache: QueryClient) {
  return Promise.all(
    (['character', 'story'] as const).map((kind) =>
      cache.invalidateQueries({
        queryKey: presetKey(kind),
      }),
    ),
  )
}
