import { useState } from 'react'
import type { ContentLabel } from '../api/generated/types.gen'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import * as q from '../api/generated/@tanstack/react-query.gen'
import { useFailure, useMessages, usePreferences } from '../app/preferences'

export function ContentSettings() {
  const [requested, setRequested] = useState<boolean | null>(null)
  const m = useMessages()
  const preferences = usePreferences()
  const cache = useQueryClient()
  const failure = useFailure()
  const labels = useQuery({
    ...q.listContentOptions(),
    enabled: preferences.value.nsfw_enabled,
  })
  const classify = useMutation({
    ...q.classifyContentMutation(),
    onSuccess: () => cache.invalidateQueries(),
  })
  return (
    <section>
      <h2>{m.contentSettings}</h2>
      <label>
        <input
          type="checkbox"
          checked={requested ?? preferences.value.nsfw_enabled}
          disabled={preferences.pending}
          onChange={(event) => {
            const enabled = event.target.checked
            setRequested(enabled)
            void preferences
              .update({ nsfw_enabled: enabled })
              .catch(() => {})
              .finally(() => setRequested(null))
          }}
        />
        {m.enableNsfw}
      </label>
      <p className="subtle">{m.nsfwDescription}</p>
      {preferences.value.nsfw_enabled && (
        <>
          <h3>{m.contentClassification}</h3>
          <div className="content-classification">
            {(labels.data ?? []).map((label) => (
              <ContentItem
                key={`${label.kind}:${label.id}`}
                label={label}
                labels={labels.data ?? []}
                pending={classify.isPending}
                onChange={(rating) =>
                  classify.mutate({
                    body: {
                      operation_id: crypto.randomUUID(),
                      kind: label.kind,
                      id: label.id,
                      expected_revision: label.revision ?? 0,
                      rating,
                    },
                  })
                }
              />
            ))}
          </div>
        </>
      )}
      {(classify.error || labels.error) && (
        <p role="alert" className="error">
          {failure(classify.error || labels.error)}
        </p>
      )}
    </section>
  )
}

function ContentItem({
  label,
  labels,
  pending,
  onChange,
}: {
  label: ContentLabel
  labels: ContentLabel[]
  pending: boolean
  onChange: (rating: 'general' | 'nsfw') => void
}) {
  const m = useMessages()
  const preferences = usePreferences()
  const image = useQuery({
    ...q.getImageOptions({ path: { identity: label.id } }),
    enabled: label.readable !== false && label.kind === 'image',
  })
  const job = useQuery({
    ...q.getJobOptions({ path: { identity: label.id } }),
    enabled: label.readable !== false && label.kind === 'job',
  })
  const unreadable = label.readable === false || image.isError || job.isError
  const target = labels.find((item) => item.id === job.data?.request.target_id)
  const created = image.data?.created_at || job.data?.created_at
  const title =
    label.title ||
    target?.title ||
    job.data?.request.prompt ||
    (created
      ? new Date(created).toLocaleString(preferences.value.locale)
      : m.contentItem)
  const kinds = {
    conversation: m.conversationLabel,
    preset: m.contentPreset,
    image: m.images,
    archive: m.contentArchive,
    job: m.creationLabel,
  }
  return (
    <label className="content-item">
      <span>
        <strong>{title}</strong>
        <small>{kinds[label.kind]}</small>
        {unreadable && (
          <span role="status" className="error">
            {m.contentUnreadable}
            <br />
            <code>{label.id}</code>
          </span>
        )}
        {!!image.data?.images?.length && (
          <img
            src={`/api/v1/images/${label.id}/files/0`}
            alt={m.contentPreview}
            loading="lazy"
          />
        )}
      </span>
      <select
        aria-label={`${m.contentClassification}: ${title}`}
        value={label.rating}
        disabled={pending || unreadable}
        onChange={(event) => {
          if (event.target.value !== 'unclassified')
            onChange(event.target.value as 'general' | 'nsfw')
        }}
      >
        <option value="unclassified">{m.unclassified}</option>
        <option value="general">{m.generalContent}</option>
        <option value="nsfw">{m.nsfwContent}</option>
      </select>
    </label>
  )
}
