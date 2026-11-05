import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import * as q from '../api/generated/@tanstack/react-query.gen'

import { useFailure, useMessages, usePreferences } from '../app/preferences'
import { Button } from '../components/ui/button'
import { recheckAvailability } from './availability'

export function RuntimePage({ embedded = false }: { embedded?: boolean }) {
  const messages = useMessages()
  const failure = useFailure()
  const preferences = usePreferences()
  const cache = useQueryClient()
  const updated = async () => {
    await Promise.all([
      recheckAvailability(cache),
      cache.invalidateQueries({ queryKey: q.getRuntimeQueryKey() }),
      cache.invalidateQueries({ queryKey: q.getRuntimeViewsQueryKey() }),
    ])
  }
  const [endpoint, setEndpoint] = useState('http://127.0.0.1:8188')
  const runtime = useQuery({
    ...q.getRuntimeOptions(),
  })
  const views = useQuery({
    ...q.getRuntimeViewsOptions(),
  })
  const refresh = useMutation({
    ...q.refreshRuntimeMutation(),
    onSuccess: updated,
  })
  const start = useMutation({
    ...q.startRuntimeMutation(),
    onSuccess: updated,
  })
  const stop = useMutation({
    ...q.stopRuntimeMutation(),
    onSuccess: updated,
  })
  return (
    <section className="page runtime-page">
      <div className="eyebrow">{messages.runtimeEyebrow}</div>
      {embedded ? <h2>{messages.runtime}</h2> : <h1>{messages.runtime}</h1>}
      <p className="subtle">{messages.runtimeDescription}</p>
      <div className="runtime-card">
        <div className="section-label">{messages.currentObservation}</div>
        <dl>
          <dt>{messages.gpu}</dt>
          <dd>
            {runtime.data?.gpu === 'active'
              ? messages.running
              : runtime.data?.gpu === 'absent'
                ? views.data?.some((view) => view.backend === 'external')
                  ? messages.unmanagedEnvironment
                  : messages.stopped
                : messages.unknown}
          </dd>
          <dt>{messages.connection}</dt>
          <dd>
            {runtime.data?.connection === 'connected'
              ? messages.running
              : runtime.data?.connection === 'disconnected'
                ? messages.stopped
                : messages.unknown}
          </dd>
          <dt>{messages.collector}</dt>
          <dd>
            {runtime.data?.observed_at
              ? runtime.data.collector_running
                ? messages.running
                : messages.stopped
              : messages.unknown}
          </dd>
          <dt>{messages.observedAt}</dt>
          <dd>
            {runtime.data?.observed_at
              ? new Date(runtime.data.observed_at).toLocaleString(
                  preferences.value.locale,
                )
              : messages.notObserved}
          </dd>
        </dl>
        <Button onClick={() => refresh.mutate({})} disabled={refresh.isPending}>
          {messages.refresh}
        </Button>
      </div>
      <details>
        <summary>{messages.drawingSettings}</summary>
        <div className="details-content runtime-views">
          {views.data?.map((view) => (
            <div key={`${view.profile}-${view.view}`}>
              <a href={view.url} target="_blank" rel="noopener noreferrer">
                {view.profile} ·{' '}
                {view.view === 'app' ? messages.appMode : messages.nodeGraph}
              </a>
              {view.backend === 'external' && (
                <a
                  href={`/api/v1/runtime/workflows/${encodeURIComponent(view.profile)}/${view.view}`}
                  download="workflow.json"
                >
                  {messages.downloadWorkflow}
                </a>
              )}
            </div>
          ))}
        </div>
      </details>
      <form
        onSubmit={(event) => {
          event.preventDefault()
          start.mutate({ body: { backend: 'external', endpoint } })
        }}
      >
        <label>
          {messages.externalEndpoint}
          <input
            type="url"
            required
            value={endpoint}
            onChange={(event) => setEndpoint(event.target.value)}
          />
        </label>
        <Button type="submit" variant="primary" disabled={start.isPending}>
          {messages.start}
        </Button>
      </form>
      <Button
        variant="danger"
        onClick={() => stop.mutate({})}
        disabled={stop.isPending}
      >
        {views.data?.some((view) => view.backend === 'external')
          ? messages.disconnect
          : messages.stop}
      </Button>
      {[runtime.error, views.error, start.error, stop.error, refresh.error]
        .filter(Boolean)
        .map((error, index) => (
          <p key={index} role="alert" className="error">
            {failure(error)}
          </p>
        ))}
    </section>
  )
}
