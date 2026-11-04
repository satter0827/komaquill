import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from '@tanstack/react-query'
import { useEffect } from 'react'
import { Link } from 'react-router'
import { checkAvailability } from '../api/generated/sdk.gen'
import type { AvailabilityRequest } from '../api/generated/types.gen'
import { useFailure, useMessages } from '../app/preferences'
import { Button } from '../components/ui/button'

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
      const result = await checkAvailability({
        body: request,
        query: { refresh: true },
      })
      cache.setQueryData(query.queryKey, result.data!)
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
      return (
        await checkAvailability({ body: checked, query: { refresh: true } })
      ).data!
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

export function AvailabilityNotice({
  status,
}: {
  status: ReturnType<typeof useAvailability>
}) {
  const m = useMessages()
  const failure = useFailure()
  if (status.ready) return null
  const unavailable = status.data?.connections?.find((c) => c.state !== 'ready')
  return (
    <div className="availability-notice" role="status">
      <p>
        {status.isFetching
          ? m.checkingConnection
          : status.error
            ? failure(status.error)
            : unavailable?.reason
              ? failure({ code: unavailable.reason })
              : m.connectionRequired}
      </p>
      <div className="actions">
        <Button onClick={status.refresh} disabled={status.isFetching}>
          {m.refresh}
        </Button>
        <Link to="/settings">{m.connectionSettings}</Link>
      </div>
    </div>
  )
}
