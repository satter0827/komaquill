import type {
  Preferences,
  WaiPreferences,
  WaiSettingsView,
} from '../../api/generated/types.gen'
import type { MessageKey } from '../../shared/messages'
import {
  useFailure,
  useMessages,
  usePreferences,
} from '../../shared/preferences'
import { Link } from 'react-router'
import { useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getSettingsOptions } from '../../api/generated/@tanstack/react-query.gen'
import { Button } from '../../shared/ui/button'

function WaiSettings({ options }: { options: WaiSettingsView }) {
  const m = useMessages()
  const preferences = usePreferences()
  const current = preferences.value.image_prompts.wai ?? {}
  const [draft, setDraft] = useState<WaiPreferences | null>(null)
  const values = draft ?? current
  const defaults = options.defaults
  const change = <Key extends keyof WaiPreferences>(
    field: Key,
    value: WaiPreferences[Key],
  ) => setDraft({ ...values, [field]: value })

  return (
    <form
      className="settings-grid"
      onSubmit={(event) => {
        event.preventDefault()
        if (!draft) return
        const saved = draft
        void preferences
          .update((latest) => ({
            image_prompts: { ...latest.image_prompts, wai: saved },
          }))
          .then(() => setDraft((latest) => (latest === saved ? null : latest)))
          .catch(() => {})
      }}
    >
      <h3>{m.waiSettings}</h3>
      <p className="subtle">{m.waiSettingsScope}</p>
      <label>
        <input
          type="checkbox"
          checked={values.quality_enabled ?? defaults.quality_enabled ?? true}
          onChange={(event) => change('quality_enabled', event.target.checked)}
        />
        {m.waiQualityEnabled}
      </label>
      <p className="subtle">{m.waiQualityScope}</p>
      {(['quality_positive', 'quality_negative'] as const).map((field) => (
        <label key={field}>
          {field === 'quality_positive'
            ? m.waiQualityPositive
            : m.waiQualityNegative}
          <textarea
            rows={3}
            value={values[field] ?? defaults[field] ?? ''}
            onChange={(event) => change(field, event.target.value)}
          />
        </label>
      ))}
      <Button
        type="button"
        disabled={preferences.pending}
        onClick={() =>
          setDraft({
            ...values,
            quality_enabled: null,
            quality_positive: null,
            quality_negative: null,
          })
        }
      >
        {m.waiResetQuality}
      </Button>
      <details>
        <summary>{m.waiAdvanced}</summary>
        <div className="details-content settings-grid">
          <p className="subtle">{m.waiAdvancedScope}</p>
          {(
            [
              ['width', m.waiWidth, 16],
              ['height', m.waiHeight, 16],
              ['candidates', m.waiCandidates, 1],
              ['seed', m.waiSeed, 1],
              ['steps', m.waiSteps, 1],
              ['cfg', m.waiCfg, 'any'],
              ['detail_strength', m.waiDetailStrength, 'any'],
            ] as const
          ).map(([field, label, step]) => (
            <label key={field}>
              {label}
              <input
                type="number"
                value={values[field] ?? ''}
                placeholder={String(defaults[field] ?? m.waiRandomSeed)}
                min={field === 'seed' ? 0 : undefined}
                max={
                  field === 'seed'
                    ? Number.MAX_SAFE_INTEGER
                    : field === 'candidates'
                      ? options.max_candidates
                      : undefined
                }
                step={step}
                onChange={(event) =>
                  change(
                    field,
                    event.target.value === ''
                      ? null
                      : event.target.valueAsNumber,
                  )
                }
              />
            </label>
          ))}
          {(['sampler', 'scheduler'] as const).map((field) => (
            <label key={field}>
              {field === 'sampler' ? m.waiSampler : m.waiScheduler}
              <select
                value={values[field] ?? ''}
                onChange={(event) => change(field, event.target.value || null)}
              >
                <option value="">{defaults[field]}</option>
                {(options.choices[field] ?? []).map((choice) => (
                  <option key={choice} value={choice}>
                    {choice}
                  </option>
                ))}
              </select>
            </label>
          ))}
        </div>
      </details>
      <Button
        type="submit"
        variant="primary"
        disabled={!draft || preferences.pending}
      >
        {m.save}
      </Button>
    </form>
  )
}

function DrawingPreferences() {
  const m = useMessages()
  const preferences = usePreferences()
  const settings = useQuery(getSettingsOptions())
  const [draft, setDraft] = useState<
    Partial<
      Pick<NonNullable<Preferences['image_prompts']>, 'positive' | 'negative'>
    >
  >({})
  const current = preferences.value.image_prompts

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
            value={draft[field] ?? current[field] ?? ''}
            onChange={(event) =>
              setDraft({ ...draft, [field]: event.target.value })
            }
            onBlur={() => {
              const saved = draft[field]
              if (saved === undefined || saved === (current[field] ?? ''))
                return
              void preferences
                .update((latest) => ({
                  image_prompts: { ...latest.image_prompts, [field]: saved },
                }))
                .then(() =>
                  setDraft((latest) =>
                    latest[field] === saved
                      ? { ...latest, [field]: undefined }
                      : latest,
                  ),
                )
                .catch(() => {})
            }}
          />
        </label>
      ))}
      {settings.data?.supports_negative_prompt === false && (
        <p className="subtle">{m.negativePromptUnavailable}</p>
      )}
      {settings.data?.wai && <WaiSettings options={settings.data.wai} />}
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
      <Link className="button" to="/trash">
        {m.trashTitle}
      </Link>
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
