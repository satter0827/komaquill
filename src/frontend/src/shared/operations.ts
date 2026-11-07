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
import { getImage } from '../api/generated/sdk.gen'

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
  return current &&
    ((isTerminal(current.state) && !isTerminal(next.state)) ||
      (['complete', 'saved'].includes(current.state ?? '') &&
        current.state !== next.state))
    ? current
    : next
}

export function watchReceipt<T extends { id: string } | { run_id: string }>(
  cache: QueryClient,
  queryKey: QueryKey,
  identity: string,
  confirm: (value: T) => void,
  isConfirmed: (value: T) => boolean = () => true,
) {
  const queries = cache.getQueryCache()
  let waiting = true
  const confirmMatching = (value: T | undefined) => {
    if (!waiting || !value) return
    if (('id' in value ? value.id : value.run_id) !== identity) return
    if (!isConfirmed(value)) return
    waiting = false
    unsubscribe()
    confirm(value)
  }
  const unsubscribe = queries.subscribe((event) => {
    if (
      event.type !== 'updated' ||
      event.action.type !== 'success' ||
      event.query !== queries.find({ queryKey, exact: true })
    )
      return
    confirmMatching(event.query.state.data as T | undefined)
  })
  confirmMatching(cache.getQueryData<T>(queryKey))
  return () => {
    waiting = false
    unsubscribe()
  }
}

export function retainOperation(
  current: OperationView | undefined,
  next: OperationView,
): OperationView {
  const previousCount = current?.resume_count ?? 0
  const nextCount = next.resume_count ?? 0
  if (current && previousCount !== nextCount)
    return previousCount > nextCount ? current : next
  return keepTerminal(current, next)
}

export function operationOptions(identity: string, minimumResumeCount = 0) {
  return queryOptions({
    ...q.getOperationOptions({ path: { identity } }),
    structuralSharing: (previous, next) => {
      const current = previous as OperationView | undefined
      const value = retainOperation(current, next as OperationView)
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
      query.state.data &&
      (query.state.data.resume_count ?? 0) >= minimumResumeCount &&
      !operationPending(query.state.data)
        ? false
        : POLL_INTERVAL_MS,
  })
}

export function conversationOperationsOptions(identity: string) {
  return queryOptions({
    ...q.getConversationOperationsOptions({ path: { identity } }),
    structuralSharing: (previous, next) => {
      const current = previous as OperationView[] | undefined
      const incoming = next as OperationView[]
      const known = new Map((current ?? []).map((value) => [value.id, value]))
      const ids = new Set(incoming.map((value) => value.id))
      return [
        ...incoming.map((value) => retainOperation(known.get(value.id), value)),
        ...(current ?? []).filter((value) => !ids.has(value.id)),
      ]
    },
    refetchInterval: (query) =>
      query.state.data?.some(operationPending) ? POLL_INTERVAL_MS : false,
  })
}

export function creationOptions(identity: string) {
  return queryOptions({
    ...q.getJobOptions({ path: { identity } }),
    structuralSharing: (previous, next) =>
      keepTerminal(previous as CreationJob | undefined, next as CreationJob),
    refetchInterval: (query) =>
      isTerminal(query.state.data?.state) ? false : POLL_INTERVAL_MS,
  })
}

export function retainImage(current: Run | undefined, next: Run): Run {
  const retained = keepTerminal(current, next)
  return current &&
    (current.images?.length ?? 0) > (retained.images?.length ?? 0)
    ? { ...retained, images: current.images }
    : retained
}

export function retainImageList(
  current: Run[] | undefined,
  next: Run[],
): Run[] {
  const values = new Map((current ?? []).map((run) => [run.run_id, run]))
  for (const run of next)
    values.set(run.run_id, retainImage(values.get(run.run_id), run))
  return [...values.values()]
}

export function imageOptions(identity: string) {
  return queryOptions({
    ...q.getImageOptions({ path: { identity } }),
    structuralSharing: (previous, next) =>
      retainImage(previous as Run | undefined, next as Run),
    queryFn: async () => (await getImage({ path: { identity } })).data!,
    staleTime: (query) => (isTerminal(query.state.data?.state) ? Infinity : 0),
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
