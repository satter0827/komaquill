import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import * as q from '../api/generated/@tanstack/react-query.gen'
import type { CreationJob, CreationRequest } from '../api/generated/types.gen'
import { useFailure, useMessages } from '../app/preferences'
import { Button } from '../components/ui/button'
import {
  creationOptions,
  keepTerminal,
  refreshPresets,
  watchReceipt,
} from './operations'

export function useCreation() {
  const cache = useQueryClient()
  const [prepared, setPrepared] = useState<CreationRequest | null>(null)
  const [identity, setIdentity] = useState<string | null>(null)
  const mutation = useMutation({
    ...q.createJobMutation(),
    onError: (error) => {
      const code =
        error && typeof error === 'object' && 'code' in error
          ? error.code
          : null
      if (
        code === 'invalid_request' ||
        code === 'request_not_found' ||
        code === 'operation_conflict' ||
        code === 'operation_busy'
      ) {
        setPrepared(null)
      }
    },
    onSuccess: (data) => {
      cache.setQueryData<CreationJob>(
        q.getJobQueryKey({ path: { identity: data.id ?? '' } }),
        (current) => keepTerminal(current, data),
      )
      setIdentity(data.id)
      setPrepared(null)
      void refreshPresets(cache)
    },
  })
  const job = useQuery({
    ...creationOptions(identity ?? prepared?.operation_id ?? ''),
    enabled: !!identity || (!!prepared && mutation.isError),
  })
  const reset = mutation.reset
  useEffect(() => {
    if (!mutation.isError || !prepared) return
    return watchReceipt(
      cache,
      q.getJobQueryKey({ path: { identity: prepared.operation_id } }),
      prepared.operation_id,
      (confirmed: CreationJob) => {
        setIdentity(confirmed.id)
        setPrepared(null)
        reset()
      },
    )
  }, [cache, mutation.isError, prepared, reset])

  useEffect(() => {
    if (job.data?.state === 'complete') void refreshPresets(cache)
  }, [job.data?.state, cache])
  const start = (request: Omit<CreationRequest, 'operation_id'>) => {
    const body = prepared ?? { ...request, operation_id: crypto.randomUUID() }
    setPrepared(body)
    setIdentity(null)
    mutation.mutate({ body })
  }
  return {
    start,
    job,
    mutation,
    prepared,
    busy:
      mutation.isPending ||
      (!!prepared && mutation.isError) ||
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
  if (
    data?.state === 'complete' &&
    data.image_id &&
    !creation.mutation.error &&
    !creation.job.error
  )
    return null
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
          {creation.prepared && (
            <Button
              disabled={creation.mutation.isPending}
              onClick={() =>
                creation.prepared && creation.start(creation.prepared)
              }
            >
              {m.retryRequest}
            </Button>
          )}
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
