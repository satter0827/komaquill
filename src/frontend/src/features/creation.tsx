import { isRejectedRequest } from '../api/client'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import * as q from '../api/generated/@tanstack/react-query.gen'
import type { CreationJob, CreationRequest } from '../api/generated/types.gen'
import { useFailure, useMessages } from '../app/preferences'
import { Button } from '../components/ui/button'
import { recheckAvailability, useAvailabilityRecovery } from './availability'
import { useTrackCreation } from './image-activity'
import {
  creationOptions,
  keepTerminal,
  refreshPresets,
  watchReceipt,
} from './operations'

export function useCreation() {
  const trackCreation = useTrackCreation()
  const cache = useQueryClient()
  const [prepared, setPrepared] = useState<CreationRequest | null>(null)
  const [identity, setIdentity] = useState<string | null>(null)
  const mutation = useMutation({
    ...q.createJobMutation(),
    onError: (error, submitted) => {
      void recheckAvailability(cache)
      if (isRejectedRequest(error)) {
        setPrepared(null)
      } else trackCreation(submitted.body.operation_id)
    },
    onSuccess: (data) => {
      cache.setQueryData<CreationJob>(
        q.getJobQueryKey({ path: { identity: data.id ?? '' } }),
        (current) => keepTerminal(current, data),
      )
      trackCreation(data.id)
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
  useAvailabilityRecovery(job.error ?? job.data?.error)
  useEffect(() => {
    if (!mutation.isError || !prepared) return
    return watchReceipt(
      cache,
      q.getJobQueryKey({ path: { identity: prepared.operation_id } }),
      prepared.operation_id,
      (confirmed: CreationJob) => {
        trackCreation(confirmed.id)
        setIdentity(confirmed.id)
        setPrepared(null)
        reset()
      },
    )
  }, [cache, mutation.isError, prepared, reset, trackCreation])

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
      {creation.mutation.isPending && (
        <p className="subtle">{m.requestSending}</p>
      )}
      {creation.prepared && creation.mutation.isError && (
        <p className="subtle">{m.receiptUnknown}</p>
      )}
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
              disabled={creation.job.isFetching}
              onClick={() => void creation.job.refetch()}
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
