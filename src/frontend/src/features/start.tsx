import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { getSettings, startConversation } from '../api/generated/sdk.gen'
import { useFailure, useMessages, usePreferences } from '../app/preferences'
import { Button } from '../components/ui/button'
import { keys, loadPresets, paths } from './library'
import type { Kind, Preset } from './library'

export function NewConversation() {
  const m = useMessages()
  const failure = useFailure()
  const preferences = usePreferences()
  const cache = useQueryClient()
  const navigate = useNavigate()
  const [search, setSearch] = useSearchParams()
  const [mode, setMode] = useState<string | null>(null)
  const playerQuery = useQuery({
    queryKey: ['players'],
    queryFn: () => loadPresets('player'),
  })
  const characterQuery = useQuery({
    queryKey: ['characters'],
    queryFn: () => loadPresets('character'),
  })
  const storyQuery = useQuery({
    queryKey: ['stories'],
    queryFn: () => loadPresets('story'),
  })
  const profiles = useQuery({
    queryKey: ['settings'],
    queryFn: async () => (await getSettings()).data!,
  })
  const queries = {
    player: playerQuery,
    character: characterQuery,
    story: storyQuery,
  }
  const choices = (kind: Kind) =>
    (queries[kind].data ?? []).filter(
      (preset) =>
        !preset.sample || preset.sample_locale === preferences.value.locale,
    )
  const selected = (kind: Kind): Preset | undefined => {
    const list = choices(kind)
    if (search.has(kind))
      return list.find((preset) => preset.id === search.get(kind))
    return (
      list.find((preset) => preset.id === search.get(kind)) ??
      (kind === 'player'
        ? list.find((preset) => preset.id === preferences.value.player_id)
        : undefined) ??
      list[0]
    )
  }
  const player = selected('player'),
    character = selected('character'),
    story = selected('story')
  const profile = mode ?? preferences.value.llm_profile
  const sample = [player, character, story].every((value) => value?.sample)
  const ready =
    preferences.ready &&
    player &&
    character &&
    story &&
    profiles.data &&
    (profile !== 'fake' || sample)
  const mutation = useMutation({
    mutationFn: async () => {
      if (!player || !character || !story) throw new Error()
      const reference = (preset: Preset) => ({
        id: preset.id,
        revision: preset.revision ?? 0,
      })
      return (
        await startConversation({
          body: {
            player: reference(player),
            character: reference(character),
            story: reference(story),
            locale: preferences.value.locale,
            llm_profile: profile,
            images: preferences.value.image_frequency === 'automatic',
          },
        })
      ).data!
    },
    onSuccess: async (conversation) => {
      await cache.invalidateQueries({ queryKey: ['conversations'] })
      navigate(`/conversations/${conversation.id}`)
    },
  })
  const change = (kind: Kind, identity: string) => {
    const next = new URLSearchParams(search)
    next.set(kind, identity)
    if (kind === 'story') {
      const chosen = choices(kind).find((preset) => preset.id === identity)
      if (chosen && 'character_id' in chosen && chosen.character_id)
        next.set('character', chosen.character_id)
    }
    setSearch(next)
  }
  return (
    <section className="page start-page">
      <div className="eyebrow">{m.newEyebrow}</div>
      <h1>{m.newConversation}</h1>
      <p className="subtle">{m.newDescription}</p>
      <form
        onSubmit={(event) => {
          event.preventDefault()
          if (ready) mutation.mutate()
        }}
      >
        <div className="selection-grid">
          {(['story', 'character', 'player'] as const).map((kind) => {
            const record = selected(kind)
            const label =
              kind === 'story'
                ? m.chooseStory
                : kind === 'character'
                  ? m.chooseCharacter
                  : m.choosePlayer
            return (
              <section
                className={`selection-card selection-${kind}`}
                key={kind}
              >
                {record?.image_id && (
                  <img
                    className="selection-image"
                    src={`/api/v1/images/${record.image_id}/files/0`}
                    alt={record.name}
                  />
                )}
                <label>
                  {label}
                  <select
                    required
                    value={record?.id ?? ''}
                    disabled={mutation.isPending}
                    onChange={(event) => change(kind, event.target.value)}
                  >
                    {!record && <option value="">{m.noPresets}</option>}
                    {!record && <option value="">{m.noPresets}</option>}
                    {choices(kind).map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                        {item.sample ? ` · ${m.sample}` : ''}
                      </option>
                    ))}
                  </select>
                </label>
                <p className="subtle">
                  {record?.description ||
                    (record && 'appearance' in record
                      ? record.appearance
                      : '') ||
                    m.noPresets}
                </p>
                <Link
                  className="text-button"
                  to={`${paths[kind]}/new?returnTo=${encodeURIComponent('/new?' + search.toString())}`}
                >
                  {m.new} →
                </Link>
                <Link className="text-button" to={paths[kind]}>
                  {m[keys[kind]]} →
                </Link>
              </section>
            )
          })}
        </div>
        <label>
          {m.profile}
          <select
            value={profile}
            disabled={mutation.isPending}
            onChange={(event) => setMode(event.target.value)}
          >
            {profiles.data?.profiles.map((item) => (
              <option key={item.id} value={item.id}>
                {item.provider === 'fake' ? m.sampleMode : m.localModel}
              </option>
            ))}
          </select>
        </label>
        {profile === 'fake' && (
          <p className="notice">
            {sample ? m.sampleNotice : m.sampleRestriction}
          </p>
        )}
        <Button
          className="primary"
          type="submit"
          disabled={!ready || mutation.isPending}
        >
          {m.create}
        </Button>
        {[
          mutation.error,
          playerQuery.error,
          characterQuery.error,
          storyQuery.error,
          profiles.error,
        ]
          .filter(Boolean)
          .map((error, index) => (
            <p key={index} className="error" role="alert">
              {failure(error)}
            </p>
          ))}
      </form>
    </section>
  )
}
