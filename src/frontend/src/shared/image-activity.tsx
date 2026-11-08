import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import * as q from '../api/generated/@tanstack/react-query.gen'
import { collectImage } from '../api/generated/sdk.gen'
import type { Run } from '../api/generated/types.gen'
import { POLL_INTERVAL_MS } from '../api/client'
import {
  creationOptions,
  imageOptions,
  isTerminal,
  retainImage,
  refreshPresets,
} from './operations'
import { useAvailability, useAvailabilityRecovery } from './availability'

const Tracking = createContext<(identity: string) => void>(() => {})
const CreationTracking = createContext<(identity: string) => void>(() => {})
const ReceiptTracking = createContext<(identity: string) => void>(() => {})
export const useTrackImage = () => useContext(Tracking)
export const useTrackCreation = () => useContext(CreationTracking)
export const useTrackImageReceipt = () => useContext(ReceiptTracking)
export const imageMonitorKey = (identity: string) =>
  ['image-monitor', identity] as const

export function useImageMonitorError(identity: string) {
  const queries = useQueryClient().getQueryCache()
  const subscribe = useCallback(
    (notify: () => void) => queries.subscribe(notify),
    [queries],
  )
  const snapshot = useCallback(
    () =>
      queries.find({ queryKey: imageMonitorKey(identity), exact: true })?.state
        .error ?? null,
    [identity, queries],
  )
  return useSyncExternalStore(subscribe, snapshot, snapshot)
}

function ImageReceiptMonitor({
  identity,
  onFinish,
}: {
  identity: string
  onFinish: (identity: string) => void
}) {
  const track = useTrackImage()
  const receipt = useQuery({
    ...imageOptions(identity),
    refetchInterval: (query) =>
      query.state.data?.run_id === identity ? false : POLL_INTERVAL_MS,
    retry: false,
    refetchOnWindowFocus: false,
  })
  useEffect(() => {
    if (receipt.data?.run_id !== identity) return
    track(identity)
    onFinish(identity)
  }, [identity, onFinish, receipt.data, track])
  return null
}

function ImageMonitor({
  identity,
  onFinish,
}: {
  identity: string
  onFinish: (identity: string) => void
}) {
  const cache = useQueryClient()
  const observation = useQuery({
    queryKey: imageMonitorKey(identity),
    queryFn: async () => {
      const current = await cache.fetchQuery(imageOptions(identity))
      return ['saved', 'lost'].includes(current.state ?? '') &&
        !current.collection_failure
        ? current
        : (await collectImage({ path: { identity } })).data!
    },
    structuralSharing: (previous, next) =>
      retainImage(previous as Run | undefined, next as Run),
    refetchInterval: (query) =>
      query.state.error ||
      query.state.data?.collection_failure ||
      isTerminal(query.state.data?.state)
        ? false
        : POLL_INTERVAL_MS,
    retry: false,
    retryOnMount: false,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
  })
  useAvailabilityRecovery(
    observation.error ??
      observation.data?.collection_failure ??
      observation.data?.failure,
  )
  useEffect(() => {
    if (!observation.data) return
    const value = observation.data
    if (isTerminal(value.state)) {
      void refreshPresets(cache)
      void cache.invalidateQueries({ queryKey: q.listImageOutputsQueryKey() })
    }
    cache.setQueryData<Run>(
      q.getImageQueryKey({ path: { identity } }),
      (previous) => retainImage(previous, value),
    )
    cache.setQueryData<Run[]>(q.listImagesQueryKey(), (previous) => {
      const values = previous ?? []
      return values.some((run) => run.run_id === identity)
        ? values.map((run) =>
            run.run_id === identity ? retainImage(run, value) : run,
          )
        : [value, ...values]
    })
    if (isTerminal(value.state) && !value.collection_failure) onFinish(identity)
  }, [cache, identity, observation.data, onFinish])
  return null
}

function CreationMonitor({
  identity,
  onFinish,
}: {
  identity: string
  onFinish: (identity: string) => void
}) {
  const track = useTrackImage()
  const cache = useQueryClient()
  const job = useQuery(creationOptions(identity))
  useEffect(() => {
    if (job.data?.image_id && job.data.state !== 'complete')
      track(job.data.image_id)
  }, [job.data?.image_id, job.data?.state, track])
  useEffect(() => {
    if (job.data?.state === 'complete') void refreshPresets(cache)
    if (isTerminal(job.data?.state)) onFinish(identity)
  }, [cache, identity, job.data?.state, onFinish])
  return null
}

function PendingImageMonitor({
  identity,
  onFinish,
}: {
  identity: string
  onFinish: (identity: string) => void
}) {
  const availability = useAvailability({ kind: 'collection', identity })
  return availability.ready ? (
    <ImageMonitor identity={identity} onFinish={onFinish} />
  ) : null
}

export function ImageActivityProvider({ children }: { children: ReactNode }) {
  const [tracked, setTracked] = useState<string[]>([])
  const [creations, setCreations] = useState<string[]>([])
  const [receipts, setReceipts] = useState<string[]>([])
  const [finishedImages, setFinishedImages] = useState<Set<string>>(
    () => new Set(),
  )
  const [finishedCreations, setFinishedCreations] = useState<Set<string>>(
    () => new Set(),
  )
  const finishImage = useCallback((identity: string) => {
    setFinishedImages((values) =>
      values.has(identity) ? values : new Set([...values, identity]),
    )
  }, [])
  const finishCreation = useCallback((identity: string) => {
    setFinishedCreations((values) =>
      values.has(identity) ? values : new Set([...values, identity]),
    )
  }, [])
  const runtime = useQuery({
    ...q.getRuntimeOptions(),
    refetchInterval: (query) =>
      query.state.data?.connection === 'connected' ? POLL_INTERVAL_MS : false,
  })
  const track = useCallback((identity: string) => {
    setTracked((values) =>
      values.includes(identity) ? values : [...values, identity],
    )
  }, [])
  const trackCreation = useCallback((identity: string) => {
    setCreations((values) =>
      values.includes(identity) ? values : [...values, identity],
    )
  }, [])
  const trackReceipt = useCallback((identity: string) => {
    setReceipts((values) =>
      values.includes(identity) ? values : [...values, identity],
    )
  }, [])
  const finishReceipt = useCallback((identity: string) => {
    setReceipts((values) => values.filter((value) => value !== identity))
  }, [])
  const pending =
    runtime.data?.connection === 'connected'
      ? (runtime.data.pending ?? []).filter(
          (identity) =>
            !tracked.includes(identity) && !finishedImages.has(identity),
        )
      : []
  return (
    <Tracking.Provider value={track}>
      <CreationTracking.Provider value={trackCreation}>
        <ReceiptTracking.Provider value={trackReceipt}>
          {receipts.map((identity) => (
            <ImageReceiptMonitor
              key={identity}
              identity={identity}
              onFinish={finishReceipt}
            />
          ))}
          {creations
            .filter((identity) => !finishedCreations.has(identity))
            .map((identity) => (
              <CreationMonitor
                key={identity}
                identity={identity}
                onFinish={finishCreation}
              />
            ))}
          {tracked
            .filter((identity) => !finishedImages.has(identity))
            .map((identity) => (
              <ImageMonitor
                key={identity}
                identity={identity}
                onFinish={finishImage}
              />
            ))}
          {pending.map((identity) => (
            <PendingImageMonitor
              key={identity}
              identity={identity}
              onFinish={finishImage}
            />
          ))}
          {children}
        </ReceiptTracking.Provider>
      </CreationTracking.Provider>
    </Tracking.Provider>
  )
}
