import type { Preferences } from '../../api/generated/types.gen'
import type { MessageKey } from '../../shared/messages'
import {
  useFailure,
  useMessages,
  usePreferences,
} from '../../shared/preferences'
import { useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getSettingsOptions } from '../../api/generated/@tanstack/react-query.gen'

function DrawingPreferences() {
  const m = useMessages()
  const preferences = usePreferences()
  const settings = useQuery(getSettingsOptions())
  const [draft, setDraft] = useState<NonNullable<
    Preferences['image_prompts']
  > | null>(null)
  const current = preferences.value.image_prompts
  const values = draft ?? current

  return (
    <section className="settings-grid">
      <h2>{m.imageGenerationPreferences}</h2>
      <p className="subtle">{m.commonImagePromptsScope}</p>
      {(['positive', 'negative'] as const).map((field) => (
        <label key={field}>
          {field === 'positive'
            ? m.commonPositivePrompt
            : m.commonNegativePrompt}
          <textarea
            rows={3}
            value={values[field] ?? ''}
            onChange={(event) =>
              setDraft({ ...values, [field]: event.target.value })
            }
            onBlur={() => {
              if (!draft || (draft[field] ?? '') === (current[field] ?? ''))
                return
              const saved = draft
              void preferences
                .update({ image_prompts: saved })
                .then(() =>
                  setDraft((latest) => (latest === saved ? null : latest)),
                )
                .catch(() => {})
            }}
          />
        </label>
      ))}
      {settings.data?.supports_negative_prompt === false && (
        <p className="subtle">{m.negativePromptUnavailable}</p>
      )}
    </section>
  )
}

export function PreferencesPage({ connections }: { connections: ReactNode }) {
  const m = useMessages()
  const preferences = usePreferences()
  const failure = useFailure()
  const options = {
    font_size: ['normal', 'large'],
    density: ['comfortable', 'compact'],
    response_length: ['short', 'standard', 'detailed'],
  } as const
  const labels = {
    font_size: m.fontSize,
    density: m.density,
    response_length: m.responseLength,
  }
  const renderOption = (key: keyof typeof options) => (
    <label key={key}>
      {labels[key]}
      <select
        value={preferences.value[key]}
        disabled={preferences.pending}
        onChange={(event) => {
          void preferences
            .update({ [key]: event.target.value } as Partial<Preferences>)
            .catch(() => {})
        }}
      >
        {options[key].map((value) => (
          <option key={value} value={value}>
            {m[value as MessageKey]}
          </option>
        ))}
      </select>
    </label>
  )
  return (
    <section className="page preferences-page">
      <h1>{m.preferences}</h1>
      <div className="settings-grid">
        <h2>{m.displayPreferences}</h2>
        <label>
          {m.language}
          <select
            value={preferences.value.locale}
            disabled={preferences.pending}
            onChange={(event) => {
              void preferences
                .update({ locale: event.target.value as 'ja' | 'en' })
                .catch(() => {})
            }}
          >
            <option value="ja">{m.japanese}</option>
            <option value="en">{m.english}</option>
          </select>
        </label>
        {renderOption('font_size')}
        {renderOption('density')}
      </div>
      <div className="settings-grid">
        <h2>{m.conversationPreferences}</h2>
        {renderOption('response_length')}
        <p className="subtle">{m.responseLengthScope}</p>
      </div>
      <DrawingPreferences />
      {!!preferences.error && (
        <p className="error" role="alert">
          {failure(preferences.error)}
        </p>
      )}
      <details className="connection-details">
        <summary>{m.connectionSettings}</summary>
        <div className="details-content">{connections}</div>
      </details>
    </section>
  )
}
