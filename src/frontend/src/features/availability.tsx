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
    mutationFn: async () => {
      await cache.cancelQueries({
        queryKey: ['availability', request],
        exact: true,
      })
      return (
        await checkAvailability({ body: request!, query: { refresh: true } })
      ).data!
    },
    onSuccess: (data) => cache.setQueryData(['availability', request], data),
    onError: () =>
      cache.invalidateQueries({
        queryKey: ['availability', request],
        exact: true,
        refetchType: 'none',
      }),
  })
  const refreshNow = refresh.mutate
  useEffect(() => {
    if (!request) return
    const focused = () => refreshNow()
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
    refresh: () => refresh.mutate(),
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
