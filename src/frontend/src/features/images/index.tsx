import { ImageResult } from '../../shared/images/result'
import { isRejectedRequest, POLL_INTERVAL_MS } from '../../api/client'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'
import * as q from '../../api/generated/@tanstack/react-query.gen'
import type { ImageInput, Run } from '../../api/generated/types.gen'
import {
  useFailure,
  useFormatMessage,
  useMessages,
  usePreferences,
} from '../../shared/preferences'
import { Button } from '../../shared/ui/button'
import {
  imageOptions,
  retainImage,
  retainImageList,
  watchReceipt,
} from '../../shared/operations'
import { recheckAvailability, useAvailability } from '../../shared/availability'
import {
  imageMonitorKey,
  useImageMonitorError,
  useTrackImage,
  useTrackImageReceipt,
} from '../../shared/image-activity'

export function groupImageDays(runs: Run[], locale: string) {
  const groups = new Map<
    string,
    { key: string; label: string; order: number; runs: Run[] }
  >()
  for (const run of [...runs].sort(
    (a, b) =>
      (Date.parse(b.created_at ?? '') || 0) -
        (Date.parse(a.created_at ?? '') || 0) ||
      a.run_id.localeCompare(b.run_id),
  )) {
    const date = new Date(run.created_at ?? '')
    const known = Number.isFinite(date.getTime())
    const key = known
      ? `${date.getFullYear()}-${date.getMonth() + 1}-${date.getDate()}`
      : 'undated'
    const group = groups.get(key) ?? {
      key,
      label: known
        ? new Intl.DateTimeFormat(locale, { dateStyle: 'long' }).format(date)
        : '',
      order: known ? date.getTime() : 0,
      runs: [],
    }
    group.runs.push(run)
    groups.set(key, group)
  }
  return [...groups.values()].sort((a, b) => b.order - a.order)
}

export function ImagesPage() {
  const [open, setOpen] = useState(false)
  const [prompt, setPrompt] = useState('')
  const [prepared, setPrepared] = useState<ImageInput | null>(null)
  const [latestId, setLatestId] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())
  const announced = useRef<string | null>(null)
  const m = useMessages()
  const format = useFormatMessage()
  const failure = useFailure()
  const preferences = usePreferences()
  const cache = useQueryClient()
  const track = useTrackImage()
  const trackReceipt = useTrackImageReceipt()
  const images = useQuery({
    ...q.listImagesOptions(),
    structuralSharing: (previous, next) =>
      retainImageList(previous as Run[] | undefined, next as Run[]),
  })
  const latest = useQuery({
    ...imageOptions(latestId ?? ''),
    enabled: false,
  })
  const monitorError = useImageMonitorError(latestId ?? '')
  const collectionError =
    latest.data?.collection_failure ??
    monitorError ??
    (latest.data?.error === 'collection_failed' ? latest.data.failure : null)
  const recovery = useAvailability(
    collectionError && latestId
      ? { kind: 'collection', identity: latestId }
      : undefined,
  )
  const availability = useAvailability(
    open
      ? { kind: 'image', prompt, llm_profile: preferences.value.llm_profile }
      : undefined,
  )
  const accept = useCallback(
    (run: Run) => {
      cache.setQueryData<Run>(
        q.getImageQueryKey({ path: { identity: run.run_id } }),
        (current) => retainImage(current, run),
      )
      cache.setQueryData<Run[]>(q.listImagesQueryKey(), (values) => [
        run,
        ...(values ?? []).filter((item) => item.run_id !== run.run_id),
      ])
      setLatestId(run.run_id)
      setPrepared(null)
      track(run.run_id)
      void cache.invalidateQueries({ queryKey: q.listImagesQueryKey() })
    },
    [cache, track],
  )
  const submit = useMutation({
    ...q.submitImageMutation(),
    onError: (error, submitted) => {
      void recheckAvailability(cache)
      if (isRejectedRequest(error)) setPrepared(null)
      else trackReceipt(submitted.body.request_id)
    },
    onSuccess: accept,
  })
  const receipt = useQuery({
    ...imageOptions(prepared?.request_id ?? ''),
    enabled: !!prepared && submit.isError,
    retry: false,
    refetchInterval: submit.isError && prepared ? POLL_INTERVAL_MS : false,
  })
  const reset = submit.reset
  useEffect(() => {
    if (!prepared || !submit.isError) return
    return watchReceipt<Run>(
      cache,
      imageOptions(prepared.request_id).queryKey,
      prepared.request_id,
      (confirmed) => {
        accept(confirmed)
        reset()
      },
    )
  }, [cache, prepared, submit.isError, accept, reset])
  const days = groupImageDays(images.data ?? [], preferences.value.locale)
  useEffect(() => {
    if (
      latest.data?.state !== 'saved' ||
      announced.current === latest.data.run_id
    )
      return
    const day = groupImageDays([latest.data], preferences.value.locale)[0]
    setExpanded((values) => new Set([...values, day.key]))
    announced.current = latest.data.run_id
  }, [latest.data, preferences.value.locale])
  const waiting = submit.isPending || (!!prepared && submit.isError)
  return (
    <section className="page album-page">
      <div className="page-heading">
        <h1>{m.images}</h1>
        <Button
          variant="primary"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
        >
          {m.generate}
        </Button>
      </div>
      <p className="subtle">{m.imageDescription}</p>
      {open && (
        <form
          className="image-form"
          onSubmit={(event) => {
            event.preventDefault()
            if (!availability.ready || waiting) return
            const body: ImageInput = {
              request_id: crypto.randomUUID(),
              prompt,
              llm_profile: preferences.value.llm_profile,
              allow_real_llm: true,
            }
            setPrepared(body)
            setLatestId(null)
            submit.mutate({ body })
          }}
        >
          <label>
            {m.imagePrompt}
            <textarea
              required
              rows={4}
              value={prompt}
              disabled={waiting}
              onChange={(event) => setPrompt(event.target.value)}
            />
          </label>
          <Button
            variant="primary"
            type="submit"
            disabled={waiting || !prompt.trim() || !availability.ready}
          >
            {m.drawRequestedScene}
          </Button>
        </form>
      )}
      {(submit.isPending || prepared || latestId) && (
        <div className="creation-progress" role="status">
          <p>
            {submit.isPending
              ? m.requestSending
              : prepared
                ? m.receiptUnknown
                : latest.data?.state === 'saved'
                  ? m.albumAdded
                  : collectionError
                    ? failure(collectionError)
                    : latest.data?.state === 'lost'
                      ? m.drawingResultUnknown
                      : latest.data?.failure
                        ? failure(latest.data.failure)
                        : m.imagePreparing}
          </p>
          {!!latest.data?.images?.length && (
            <a
              href={`#image-${latestId}`}
              onClick={(event) => {
                event.preventDefault()
                const day = groupImageDays(
                  [latest.data!],
                  preferences.value.locale,
                )[0]
                setExpanded((values) => new Set([...values, day.key]))
                requestAnimationFrame(() => {
                  const image = document.getElementById(`image-${latestId}`)
                  image?.scrollIntoView({ block: 'start' })
                  image?.focus({ preventScroll: true })
                })
              }}
            >
              {m.viewLatestImage}
            </a>
          )}
          {prepared && submit.isError && (
            <Button onClick={() => void receipt.refetch()}>
              {m.retryRequest}
            </Button>
          )}
          {collectionError && latestId && (
            <Button
              disabled={!recovery.ready}
              onClick={() => {
                track(latestId)
                void cache.invalidateQueries({
                  queryKey: imageMonitorKey(latestId),
                })
              }}
            >
              {m.retryCollection}
            </Button>
          )}
        </div>
      )}
      {submit.error && !prepared && (
        <p role="alert" className="error">
          {failure(submit.error)}
        </p>
      )}
      {images.error && (
        <p role="alert" className="error">
          {failure(images.error)}
        </p>
      )}
      {days.length === 0 && !images.isPending && <p>{m.albumEmpty}</p>}
      <div className="album-days">
        {days.map((day) => (
          <details
            key={day.key}
            open={expanded.has(day.key)}
            onToggle={(event) => {
              const isOpen = event.currentTarget.open
              setExpanded((values) => {
                if (values.has(day.key) === isOpen) return values
                const next = new Set(values)
                if (isOpen) next.add(day.key)
                else next.delete(day.key)
                return next
              })
            }}
          >
            <summary>
              {day.label || m.undatedImages}
              <span className="subtle">
                {format('albumDayCount', {
                  count: day.runs.reduce(
                    (count, run) => count + (run.images?.length ?? 0),
                    0,
                  ),
                })}
              </span>
            </summary>
            {expanded.has(day.key) && (
              <div className="details-content image-grid">
                {day.runs.map((run) => (
                  <ImageResult
                    key={run.run_id}
                    identity={run.run_id}
                    run={run}
                  />
                ))}
              </div>
            )}
          </details>
        ))}
      </div>
    </section>
  )
}
