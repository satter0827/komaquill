import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  getRuntime,
  getRuntimeViews,
  refreshRuntime,
  startRuntime,
  stopRuntime,
} from '../api/generated/sdk.gen'
import { failure } from '../api/client'
import { Button } from '../components/ui/button'
import messages from '../app/messages.json'

export function RuntimePage() {
  const cache = useQueryClient()
  const updated = async () => {
    await Promise.all([
      cache.invalidateQueries({ queryKey: ['runtime'] }),
      cache.invalidateQueries({ queryKey: ['runtime-views'] }),
    ])
  }
  const [endpoint, setEndpoint] = useState('http://127.0.0.1:8188')
  const runtime = useQuery({
    queryKey: ['runtime'],
    queryFn: async () => (await getRuntime()).data!,
  })
  const views = useQuery({
    queryKey: ['runtime-views'],
    queryFn: async () => (await getRuntimeViews()).data!,
  })
  const refresh = useMutation({
    mutationFn: () => refreshRuntime(),
    onSuccess: updated,
  })
  const start = useMutation({
    mutationFn: () => startRuntime({ body: { backend: 'external', endpoint } }),
    onSuccess: updated,
  })
  const stop = useMutation({
    mutationFn: () => stopRuntime(),
    onSuccess: updated,
  })
  return (
    <section className="page runtime-page">
      <div className="eyebrow">{messages.runtimeEyebrow}</div>
      <h1>{messages.runtime}</h1>
      <p className="subtle">{messages.runtimeDescription}</p>
      <div className="runtime-card">
        <div className="section-label">{messages.currentObservation}</div>
        <dl>
          <dt>{messages.gpu}</dt>
          <dd>{runtime.data?.gpu ?? messages.unknown}</dd>
          <dt>{messages.connection}</dt>
          <dd>{runtime.data?.connection ?? messages.unknown}</dd>
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
              ? new Date(runtime.data.observed_at).toLocaleString()
              : messages.notObserved}
          </dd>
        </dl>
        <Button onClick={() => refresh.mutate()} disabled={refresh.isPending}>
          {messages.refresh}
        </Button>
      </div>
      <div className="runtime-views">
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
      <form
        onSubmit={(event) => {
          event.preventDefault()
          start.mutate()
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
        <Button type="submit" className="primary" disabled={start.isPending}>
          {messages.start}
        </Button>
      </form>
      <Button
        className="danger"
        onClick={() => stop.mutate()}
        disabled={stop.isPending}
      >
        {messages.stop}
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
