import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from '@tanstack/react-query'
import { useCallback, useEffect, useSyncExternalStore } from 'react'
import { Link } from 'react-router'
import { checkAvailability } from '../api/generated/sdk.gen'
import type {
  Availability,
  AvailabilityRequest,
} from '../api/generated/types.gen'
import { useFailure, useMessages } from './preferences'
import { Button } from './ui/button'

const MIN_REFRESH_INTERVAL_MS = 1000
const CONNECTION_FAILURE_CODES = new Set([
  'connection_failed',
  'authentication_failed',
  'identity_mismatch',
  'llm_unavailable',
  'llm_dependencies_missing',
  'image_backend_unavailable',
  'invalid_configuration',
  'http_failed',
  'stopping',
])

export function useAvailabilityRecovery(error: unknown) {
  const cache = useQueryClient()
  const code =
    typeof error === 'string'
      ? error
      : error && typeof error === 'object' && 'code' in error
        ? String(error.code)
        : error instanceof Error
          ? 'connection_failed'
          : undefined
  useEffect(() => {
    if (code && CONNECTION_FAILURE_CODES.has(code))
      void recheckAvailability(cache)
  }, [cache, code])
}

export async function recheckAvailability(cache: QueryClient) {
  const queries = cache
    .getQueryCache()
    .findAll({ queryKey: ['availability'], type: 'active' })
  await cache.cancelQueries({ queryKey: ['availability'] })
  await cache.invalidateQueries({
    queryKey: ['availability'],
    refetchType: 'none',
  })
  return Promise.allSettled(
    queries.map(async (query) => {
      const request = query.queryKey[1] as AvailabilityRequest | undefined
      if (!request) return
      await cache.fetchQuery({
        queryKey: query.queryKey,
        queryFn: async () =>
          (await checkAvailability({ body: request, query: { refresh: true } }))
            .data!,
        staleTime: 0,
      })
    }),
  )
}

export function useAvailability(request?: AvailabilityRequest) {
  const cache = useQueryClient()
  const refresh = useMutation({
    mutationFn: async (checked: AvailabilityRequest) => {
      await cache.cancelQueries({
        queryKey: ['availability', checked],
        exact: true,
      })
      return cache.fetchQuery({
        queryKey: ['availability', checked],
        queryFn: async () =>
          (await checkAvailability({ body: checked, query: { refresh: true } }))
            .data!,
        staleTime: 0,
      })
    },
    onSuccess: (data, checked) =>
      cache.setQueryData(['availability', checked], data),
    onError: (_, checked) =>
      cache.invalidateQueries({
        queryKey: ['availability', checked],
        exact: true,
        refetchType: 'none',
      }),
  })
  const refreshNow = refresh.mutate
  useEffect(() => {
    if (!request) return
    const focused = () => refreshNow(request)
    window.addEventListener('focus', focused)
    return () => window.removeEventListener('focus', focused)
  }, [request, refreshNow])
  const query = useQuery({
    queryKey: ['availability', request],
    queryFn: async () => (await checkAvailability({ body: request! })).data!,
    enabled: !!request,
    staleTime: (query) => {
      const expiries = query.state.data?.connections?.map((c) =>
        new Date(c.expires_at).getTime(),
      )
      return expiries?.length
        ? Math.max(0, Math.min(...expiries) - query.state.dataUpdatedAt)
        : Infinity
    },
    refetchOnWindowFocus: false,
    refetchInterval: (query) => {
      const expiries = query.state.data?.connections?.map((c) =>
        new Date(c.expires_at).getTime(),
      )
      return expiries?.length
        ? Math.max(MIN_REFRESH_INTERVAL_MS, Math.min(...expiries) - Date.now())
        : false
    },
    retry: false,
  })
  return {
    ...query,
    error: query.error ?? refresh.error,
    refresh: () => request && refresh.mutate(request),
    isFetching: query.isFetching || refresh.isPending,
    ready:
      !!request &&
      !!query.data?.ready &&
      !query.isStale &&
      !query.isFetching &&
      !query.isError &&
      !refresh.isPending,
  }
}

export function useConnectionSummary() {
  const cache = useQueryClient()
  const queries = cache.getQueryCache()
  const subscribe = useCallback(
    (notify: () => void) => queries.subscribe(notify),
    [queries],
  )
  const snapshot = useCallback(
    () =>
      queries
        .findAll({ queryKey: ['availability'], type: 'active' })
        .map(
          (query) =>
            `${query.queryHash}:${query.state.dataUpdatedAt}:${query.state.errorUpdatedAt}:${query.state.fetchStatus}`,
        )
        .join('|'),
    [queries],
  )
  useSyncExternalStore(subscribe, snapshot, snapshot)
  const active = queries.findAll({ queryKey: ['availability'], type: 'active' })
  return {
    checking: active.some((query) => query.state.fetchStatus === 'fetching'),
    failed: active.some(
      (query) =>
        query.state.error ||
        (query.state.data as Availability | undefined)?.ready === false,
    ),
    errors: active.flatMap((query) =>
      query.state.error ? [query.state.error] : [],
    ),
    connections: [
      ...new Map(
        active
          .flatMap((query) =>
            (
              (query.state.data as Availability | undefined)?.connections ?? []
            ).map((value) =>
              query.state.error
                ? { ...value, state: 'unknown' as const }
                : value,
            ),
          )
          .sort((a, b) => a.observed_at.localeCompare(b.observed_at))
          .map((value) => [`${value.kind}-${value.target}`, value]),
      ).values(),
    ],
    refresh: () => recheckAvailability(cache),
  }
}

export function ConnectionSummary() {
  const m = useMessages()
  const failure = useFailure()
  const status = useConnectionSummary()
  return (
    <details className="connection-summary">
      <summary>
        {m.connectionSummary}
        <span
          className="connection-indicator"
          data-warning={status.failed || undefined}
          aria-hidden="true"
        />
        <span className="subtle">
          {status.failed
            ? m.connectionCheckNeeded
            : status.connections.length
              ? m.connectionReady
              : m.notObserved}
        </span>
      </summary>
      <div className="details-content">
        <dl>
          {(['llm', 'comfy'] as const).map((kind) => {
            const values = status.connections.filter(
              (value) => value.kind === kind,
            )
            const unavailable = values.find(
              (value) =>
                value.state !== 'ready' ||
                Date.parse(value.expires_at) <= Date.now(),
            )
            return (
              <div key={kind}>
                <dt>
                  {kind === 'llm'
                    ? m.conversationConnection
                    : m.drawingConnection}
                </dt>
                <dd>
                  {!values.length
                    ? m.notObserved
                    : unavailable
                      ? m.connectionCheckNeeded
                      : m.connectionReady}
                </dd>
              </div>
            )
          })}
        </dl>
        <p className="connection-progress" role="status">
          {status.checking ? m.checkingConnection : ''}
        </p>
        {status.connections
          .filter((value) => value.reason)
          .map((value) => (
            <p key={`${value.kind}-${value.target}`} className="error">
              {failure({ code: value.reason })}
            </p>
          ))}
        {status.errors.map((error, index) => (
          <p key={index} className="error">
            {failure(error)}
          </p>
        ))}
        <Button
          onClick={() => void status.refresh()}
          disabled={status.checking}
        >
          {m.refresh}
        </Button>
        <Link to="/settings">{m.connectionSettings}</Link>
      </div>
    </details>
  )
}
