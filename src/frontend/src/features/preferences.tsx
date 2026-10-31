import type { Preferences } from '../api/generated/types.gen'
import { useFailure, useMessages, usePreferences } from '../app/preferences'
import type { MessageKey } from '../app/messages'
import { RuntimePage } from './runtime'

export function PreferencesPage() {
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
  return (
    <section className="page preferences-page">
      <div className="eyebrow">{m.preferencesEyebrow}</div>
      <h1>{m.preferences}</h1>
      <p className="subtle">{m.settingsDescription}</p>
      <div className="settings-grid">
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
        {(Object.keys(options) as (keyof typeof options)[]).map((key) => (
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
        ))}
      </div>
      {!!preferences.error && (
        <p className="error" role="alert">
          {failure(preferences.error)}
        </p>
      )}
      <details className="connection-details">
        <summary>{m.connectionSettings}</summary>
        <RuntimePage />
      </details>
    </section>
  )
}
