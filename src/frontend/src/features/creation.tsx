import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { createJob, getJob } from '../api/generated/sdk.gen'
import type { CreationRequest } from '../api/generated/types.gen'
import { useFailure, useMessages } from '../app/preferences'
import { POLL_INTERVAL_MS } from '../api/client'
import { Button } from '../components/ui/button'

export function useCreation() {
  const cache = useQueryClient()
  const [prepared, setPrepared] = useState<CreationRequest | null>(null)
  const [identity, setIdentity] = useState<string | null>(null)
  const mutation = useMutation({
    mutationFn: async (request: CreationRequest) =>
      (await createJob({ body: request })).data!,
    onSuccess: (data) => {
      setIdentity(data.id)
      setPrepared(null)
      for (const key of ['characters', 'stories'])
        void cache.invalidateQueries({ queryKey: [key] })
    },
  })
  const job = useQuery({
    queryKey: ['creation', identity ?? prepared?.operation_id],
    enabled: !!identity || (!!prepared && mutation.isError),
    queryFn: async () => {
      const result = (
        await getJob({ path: { identity: identity ?? prepared!.operation_id } })
      ).data!
      return result
    },
    refetchInterval: (query) =>
      ['complete', 'failed', 'interrupted'].includes(
        query.state.data?.state ?? '',
      )
        ? false
        : POLL_INTERVAL_MS,
  })
  useEffect(() => {
    if (job.data?.state === 'complete')
      for (const key of ['players', 'characters', 'stories'])
        void cache.invalidateQueries({ queryKey: [key] })
  }, [job.data?.state, cache])
  const start = (request: Omit<CreationRequest, 'operation_id'>) => {
    const body = prepared ?? { ...request, operation_id: crypto.randomUUID() }
    setPrepared(body)
    setIdentity(null)
    mutation.mutate(body)
  }
  return {
    start,
    job,
    mutation,
    prepared,
    busy:
      mutation.isPending ||
      (!!job.data &&
        ['accepted', 'running', 'submitted'].includes(job.data.state)),
  }
}

export function CreationStatus({
  creation,
}: {
  creation: ReturnType<typeof useCreation>
}) {
  const m = useMessages()
  const failure = useFailure()
  const data = creation.job.data
  return (
    <div aria-live="polite">
      {data && (
        <p className={data.error ? 'error' : 'subtle'}>
          {data.error ? failure({ code: data.error }) : m[`job_${data.state}`]}
        </p>
      )}
      {creation.mutation.error && (
        <>
          <p role="alert" className="error">
            {failure(creation.mutation.error)}
          </p>
          <Button
            onClick={() =>
              creation.prepared && creation.start(creation.prepared)
            }
          >
            {m.retryRequest}
          </Button>
        </>
      )}
      {creation.job.error && (
        <p role="alert" className="error">
          {failure(creation.job.error)}
        </p>
      )}
    </div>
  )
}
