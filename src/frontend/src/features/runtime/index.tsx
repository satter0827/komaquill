import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import * as q from '../../api/generated/@tanstack/react-query.gen'
import { recheckAvailability } from '../../shared/availability'
import {
  useFailure,
  useFormatMessage,
  useMessages,
  usePreferences,
} from '../../shared/preferences'
import { Button } from '../../shared/ui/button'
import { QueryNotice } from '../../shared/ui/query-notice'

const DEFAULT_EXTERNAL_ENDPOINT = 'http://127.0.0.1:8188'

export function RuntimePage({ embedded = false }: { embedded?: boolean }) {
  const m = useMessages()
  const format = useFormatMessage()
  const failure = useFailure()
  const preferences = usePreferences()
  const cache = useQueryClient()
  const [endpoint, setEndpoint] = useState(DEFAULT_EXTERNAL_ENDPOINT)
  const runtime = useQuery(q.getRuntimeOptions())
  const views = useQuery(q.getRuntimeViewsOptions())
  const updated = async () => {
    await Promise.all([
      recheckAvailability(cache),
      cache.invalidateQueries({ queryKey: q.getRuntimeQueryKey() }),
      cache.invalidateQueries({ queryKey: q.getRuntimeViewsQueryKey() }),
    ])
  }
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
  const busy = refresh.isPending || start.isPending || stop.isPending
  const status = runtime.isSuccess ? runtime.data : undefined
  const entries = views.data ?? []
  const target = status?.shutdown_target
  const owned = target === 'owned'
  const external = target === 'external'
  const externalUrls = new Set(
    entries
      .filter((view) => view.backend === 'external')
      .map((view) => view.url),
  )
  const profiles = new Map<string, typeof entries>()
  for (const view of entries) {
    const group = profiles.get(view.profile) ?? []
    group.push(view)
    profiles.set(view.profile, group)
  }
  const connection =
    status?.connection === 'connected'
      ? m.runtimeConnected
      : status?.connection === 'disconnected' ||
          status?.connection === 'stopped' ||
          status?.connection === 'not_started'
        ? m.runtimeDisconnected
        : m.notObserved

  return (
    <section className={embedded ? 'runtime-panel' : 'page runtime-panel'}>
      {!embedded && <h1>{m.connectionSettings}</h1>}
      <section className="runtime-card">
        <h2>{m.currentObservation}</h2>
        <p role="status" className="runtime-connection">
          {connection}
        </p>
        <dl>
          <dt>{m.gpu}</dt>
          <dd>
            {status?.gpu === 'active'
              ? m.running
              : status?.gpu === 'absent'
                ? external
                  ? m.unmanagedEnvironment
                  : m.stopped
                : status?.gpu === 'stopped'
                  ? m.stopped
                  : m.unknown}
          </dd>
          <dt>{m.collector}</dt>
          <dd>
            {status?.observed_at
              ? status.collector_running
                ? m.running
                : m.stopped
              : m.unknown}
          </dd>
          <dt>{m.observedAt}</dt>
          <dd>
            {status?.observed_at
              ? new Date(status.observed_at).toLocaleString(
                  preferences.value.locale,
                )
              : m.notObserved}
          </dd>
        </dl>
        <div className="actions">
          <Button onClick={() => refresh.mutate({})} disabled={busy}>
            {m.refresh}
          </Button>
        </div>
        <QueryNotice
          error={runtime.error}
          pending={runtime.isFetching}
          onRetry={() => void runtime.refetch()}
        />
        {!!refresh.error && (
          <p role="alert" className="error">
            {failure(refresh.error)}
          </p>
        )}
      </section>
      <form
        className="runtime-card"
        onSubmit={(event) => {
          event.preventDefault()
          if (!busy) start.mutate({ body: { backend: 'external', endpoint } })
        }}
      >
        <h2>{m.runtimeEndpointTitle}</h2>
        <p className="subtle">{m.runtimeDescription}</p>
        <label>
          {m.externalEndpoint}
          <input
            type="url"
            required
            value={endpoint}
            onChange={(event) => setEndpoint(event.target.value)}
          />
        </label>
        <div className="actions">
          <Button type="submit" variant="primary" disabled={busy}>
            {m.start}
          </Button>
        </div>
        {!!start.error && (
          <p role="alert" className="error">
            {failure(start.error)}
          </p>
        )}
      </form>
      <details className="runtime-card">
        <summary>{m.drawingSettings}</summary>
        <div className="details-content">
          {views.isPending && <p role="status">{m.loading}</p>}
          <QueryNotice
            error={views.error}
            retained={views.data !== undefined}
            pending={views.isFetching}
            onRetry={() => void views.refetch()}
          />
          {views.isSuccess && entries.length === 0 && (
            <p className="subtle">{m.runtimeNoViews}</p>
          )}
          {[...externalUrls].map((url) => (
            <a
              key={url}
              className="text-button"
              href={url}
              target="_blank"
              rel="noopener noreferrer"
            >
              {m.openComfy}
            </a>
          ))}
          {[...profiles].map(([profile, group]) => (
            <section className="runtime-profile" key={profile}>
              <h3>{profile}</h3>
              <div className="actions">
                {group.map((view) => {
                  const mode = view.view === 'app' ? m.appMode : m.nodeGraph
                  return view.backend === 'external' ? (
                    <a
                      className="text-button"
                      key={view.view}
                      href={`/api/v1/runtime/workflows/${encodeURIComponent(profile)}/${view.view}`}
                      download="workflow.json"
                    >
                      {format('downloadWorkflow', { mode })}
                    </a>
                  ) : (
                    <a
                      className="text-button"
                      key={view.view}
                      href={view.url}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {mode}
                    </a>
                  )
                })}
              </div>
            </section>
          ))}
        </div>
      </details>
      <section className="runtime-card">
        <h2>{m.runtimeEndTitle}</h2>
        {target ? (
          <div className="actions">
            <Button
              variant={owned ? 'danger' : 'secondary'}
              disabled={busy}
              onClick={() => stop.mutate({})}
            >
              {owned ? m.stop : m.disconnect}
            </Button>
          </div>
        ) : (
          <p className="subtle">{m.runtimeCheckBeforeEnd}</p>
        )}
        {!!stop.error && (
          <p role="alert" className="error">
            {failure(stop.error)}
          </p>
        )}
      </section>
    </section>
  )
}
