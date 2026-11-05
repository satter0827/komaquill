import { OriginalImage } from './images'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useSearchParams } from 'react-router'
import { useState } from 'react'
import * as q from '../api/generated/@tanstack/react-query.gen'
import { useFailure, useMessages, usePreferences } from '../app/preferences'
import { Button } from '../components/ui/button'
import { paths } from './library'

export function NewConversation({ compact = false }: { compact?: boolean }) {
  const m = useMessages()
  const [style, setStyle] = useState({ tone: '', topics: '', avoid: '' })
  const failure = useFailure()
  const preferences = usePreferences()
  const cache = useQueryClient()
  const navigate = useNavigate()
  const [search, setSearch] = useSearchParams()
  const player = useQuery(q.getPlayerOptions())
  const characters = useQuery({
    ...q.listCharactersOptions(),
  })
  const stories = useQuery({
    ...q.listStoriesOptions(),
  })
  const choices = (kind: 'character' | 'story') =>
    ((kind === 'character' ? characters.data : stories.data) ?? []).filter(
      (item) => !item.sample || item.sample_locale === preferences.value.locale,
    )
  const story =
    choices('story').find((item) => item.id === search.get('story')) ??
    (search.has('story') ? undefined : choices('story')[0])
  const recommended =
    story && 'character_id' in story ? story.character_id : null
  const character =
    choices('character').find(
      (item) => item.id === (search.get('character') ?? recommended),
    ) ??
    (search.has('character') || recommended
      ? undefined
      : choices('character')[0])
  const mutation = useMutation({
    ...q.startConversationMutation(),
    onSuccess: async (conversation) => {
      await cache.invalidateQueries({ queryKey: q.listConversationsQueryKey() })
      navigate(`/conversations/${conversation.id}`)
    },
  })
  const change = (kind: 'character' | 'story', id: string) => {
    const next = new URLSearchParams(search)
    next.set(kind, id)
    if (kind === 'story') next.delete('character')
    setSearch(next)
  }
  if (compact && (!player.data || !character || !story))
    return (
      <Link className="button primary" to="/new">
        {m.newConversation}
      </Link>
    )
  return (
    <section
      className={compact ? 'home-section start-page' : 'page start-page'}
    >
      {!compact && <div className="eyebrow">{m.newEyebrow}</div>}
      {compact ? <h2>{m.newConversation}</h2> : <h1>{m.newConversation}</h1>}
      {!compact && <p className="subtle">{m.newDescription}</p>}
      <form
        onSubmit={(event) => {
          event.preventDefault()
          if (player.data && character && story)
            mutation.mutate({
              body: {
                player_revision: player.data!.revision,
                character: { id: character!.id, revision: character!.revision },
                story: { id: story!.id, revision: story!.revision },
                locale: preferences.value.locale,
                ...style,
              },
            })
        }}
      >
        <details
          className="start-selection"
          open={!player.data || !character || !story}
        >
          <summary>
            {player.data && character && story
              ? `${player.data.name} · ${character.name} · ${story.name}`
              : m.chooseStory}
          </summary>
          <div className="details-content selection-grid">
            <section className="selection-card">
              <h2>{m.choosePlayer}</h2>
              <p>{player.data?.name}</p>
              <p className="subtle">{player.data?.description}</p>
              <Link
                to={`/player?returnTo=${encodeURIComponent('/new?' + search.toString())}`}
              >
                {m.edit}
              </Link>
            </section>
            {(['story', 'character'] as const).map((kind) => {
              const record = kind === 'story' ? story : character
              return (
                <section
                  className={`selection-card selection-${kind}`}
                  key={kind}
                >
                  {record?.image_id && (
                    <OriginalImage
                      className="selection-image"
                      identity={record.image_id}
                      alt={record.name}
                    />
                  )}

                  <label>
                    {kind === 'story' ? m.chooseStory : m.chooseCharacter}
                    <select
                      value={record?.id ?? ''}
                      disabled={mutation.isPending}
                      onChange={(event) => change(kind, event.target.value)}
                    >
                      {!record && <option value="">{m.noPresets}</option>}
                      {choices(kind).map((item) => (
                        <option key={item.id} value={item.id}>
                          {item.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <p className="subtle">
                    {record
                      ? record.description ||
                        ('opening' in record
                          ? record.opening || record.world || record.goal
                          : record.appearance || record.role) ||
                        record.name
                      : m.noPresets}
                  </p>
                  <Link
                    className="text-button"
                    to={`${paths[kind]}/new?returnTo=${encodeURIComponent('/new?' + search.toString())}`}
                  >
                    {m.new} →
                  </Link>
                </section>
              )
            })}
          </div>
        </details>
        <details>
          <summary>{m.expressionSettings}</summary>
          <div className="details-content">
            {(['tone', 'topics', 'avoid'] as const).map((key) => (
              <label key={key}>
                {m[key]}
                <textarea
                  value={style[key]}
                  onChange={(event) =>
                    setStyle({ ...style, [key]: event.target.value })
                  }
                />
              </label>
            ))}
          </div>
        </details>
        <Button
          className="primary"
          type="submit"
          disabled={
            !preferences.ready ||
            !player.data ||
            !character ||
            !story ||
            mutation.isPending
          }
        >
          {m.create}
        </Button>
        {[
          mutation.error,
          player.error,
          characters.error,
          ...(compact ? [] : [stories.error]),
        ]
          .filter(Boolean)
          .map((error, index) => (
            <p key={index} role="alert" className="error">
              {failure(error)}
            </p>
          ))}
      </form>
    </section>
  )
}
