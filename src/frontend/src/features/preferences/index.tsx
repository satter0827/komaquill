import type { Preferences } from '../../api/generated/types.gen'
import type { MessageKey } from '../../shared/messages'
import {
  useFailure,
  useMessages,
  usePreferences,
} from '../../shared/preferences'
import type { ReactNode } from 'react'

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
