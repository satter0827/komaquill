import { OriginalImage } from '../../shared/images/original'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'
import * as q from '../../api/generated/@tanstack/react-query.gen'
import {
  useFailure,
  useFormatMessage,
  useMessages,
  usePreferences,
} from '../../shared/preferences'
import { Button } from '../../shared/ui/button'
import { QueryNotice } from '../../shared/ui/query-notice'
import { paths } from '../../shared/navigation'
import { presetsForLocale } from '../../shared/story/presets'

function selectionChanged(error: unknown) {
  return (
    !!error &&
    typeof error === 'object' &&
    'code' in error &&
    (error.code === 'operation_conflict' || error.code === 'request_not_found')
  )
}

export function NewConversation({ compact = false }: { compact?: boolean }) {
  const m = useMessages()
  const format = useFormatMessage()
  const failure = useFailure()
  const preferences = usePreferences()
  const cache = useQueryClient()
  const navigate = useNavigate()
  const [search, setSearch] = useSearchParams()
  const [expanded, setExpanded] = useState<boolean>()
  const player = useQuery(q.getPlayerOptions())
  const characters = useQuery({
    ...q.listCharactersOptions(),
  })
  const stories = useQuery({
    ...q.listStoriesOptions(),
  })
  const locale = preferences.value.locale
  const storyId = search.get('story')
  const storyChoices = presetsForLocale(stories.data, locale, storyId)
  const story = storyId
    ? storyChoices.find((item) => item.id === storyId)
    : storyChoices[0]
  const characterId = search.get('character') ?? story?.character_id
  const characterChoices = presetsForLocale(
    characters.data,
    locale,
    characterId,
  )
  const character = characterId
    ? characterChoices.find((item) => item.id === characterId)
    : characterChoices[0]
  const choiceError = !!(player.error || characters.error || stories.error)
  const loadingChoices =
    player.isPending || characters.isPending || stories.isPending
  const selectionComplete = !!(player.data && character && story)
  const mutation = useMutation({
    ...q.startConversationMutation(),
    onError: async (error) => {
      if (!selectionChanged(error)) return
      await Promise.all([
        cache.invalidateQueries({ queryKey: q.getPlayerQueryKey() }),
        cache.invalidateQueries({ queryKey: q.listCharactersQueryKey() }),
        cache.invalidateQueries({ queryKey: q.listStoriesQueryKey() }),
      ])
    },
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
  if (compact && !selectionComplete)
    return (
      <Link className="button primary" to="/new">
        {m.newConversation}
      </Link>
    )
  return (
    <section
      className={compact ? 'home-section start-page' : 'page start-page'}
    >
      {compact ? <h2>{m.newConversation}</h2> : <h1>{m.newConversation}</h1>}
      {!compact && <p className="subtle">{m.newDescription}</p>}
      <form
        onSubmit={(event) => {
          event.preventDefault()
          if (
            preferences.ready &&
            player.data &&
            character &&
            story &&
            !mutation.isPending &&
            !choiceError
          )
            mutation.mutate({
              body: {
                player_revision: player.data!.revision,
                character: { id: character!.id, revision: character!.revision },
                story: { id: story!.id, revision: story!.revision },
                locale: preferences.value.locale,
              },
            })
        }}
      >
        <details
          className="start-selection"
          open={
            expanded ?? (choiceError || (!selectionComplete && !loadingChoices))
          }
          onToggle={(event) => setExpanded(event.currentTarget.open)}
        >
          <summary>
            {m.changeStartSelection}
            {player.data && character && story && (
              <span className="selection-summary">
                <span>
                  <span>{m.chooseStory}</span> {story.name}
                </span>
                <span>
                  <span>{m.chooseCharacter}</span> {character.name}
                </span>
                <span>
                  <span>{m.choosePlayer}</span> {player.data.name}
                </span>
              </span>
            )}
          </summary>
          <div className="details-content selection-grid">
            {(['story', 'character'] as const).map((kind) => {
              const record = kind === 'story' ? story : character
              const query = kind === 'story' ? stories : characters
              const choices = kind === 'story' ? storyChoices : characterChoices
              return (
                <section
                  className={`selection-card selection-${kind}`}
                  key={kind}
                >
                  <label>
                    {kind === 'story' ? m.chooseStory : m.chooseCharacter}
                    <select
                      value={record?.id ?? ''}
                      disabled={mutation.isPending || !choices.length}
                      onChange={(event) => change(kind, event.target.value)}
                    >
                      {!record && (
                        <option value="" disabled>
                          {m.selectPreset}
                        </option>
                      )}
                      {choices.map((item) => (
                        <option key={item.id} value={item.id}>
                          {item.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  {query.isPending && <p role="status">{m.loading}</p>}
                  <QueryNotice
                    error={query.error}
                    retained={query.data !== undefined}
                    pending={query.isFetching}
                    onRetry={() => void query.refetch()}
                  />
                  {record ? (
                    <p className="subtle">
                      {record.description ||
                        ('opening' in record
                          ? record.opening || record.world || record.goal
                          : record.appearance || record.role) ||
                        record.name}
                    </p>
                  ) : query.isSuccess ? (
                    <p className="subtle">
                      {choices.length
                        ? m.startSelectionUnavailable
                        : m.noPresets}
                    </p>
                  ) : null}
                  <div className="actions">
                    {record && (
                      <Link
                        className="text-button"
                        to={`${paths[kind]}/${record.id}?returnTo=${encodeURIComponent('/new?' + search.toString())}`}
                        aria-label={format('editPresetLabel', {
                          name: record.name,
                        })}
                      >
                        {m.edit}
                      </Link>
                    )}
                    <Link
                      className="text-button"
                      to={`${paths[kind]}/new?returnTo=${encodeURIComponent('/new?' + search.toString())}`}
                    >
                      {m.new} →
                    </Link>
                  </div>
                  {record?.image_id && (
                    <OriginalImage
                      className="selection-image"
                      identity={record.image_id}
                      alt={record.name}
                    />
                  )}
                </section>
              )
            })}
            <section className="selection-card">
              <h2>{m.choosePlayer}</h2>
              {player.isPending && <p role="status">{m.loading}</p>}
              <QueryNotice
                error={player.error}
                retained={!!player.data}
                pending={player.isFetching}
                onRetry={() => void player.refetch()}
              />
              <p>{player.data?.name}</p>
              <p className="subtle">{player.data?.description}</p>
              <Link
                className="text-button"
                to={`/player?returnTo=${encodeURIComponent('/new?' + search.toString())}`}
              >
                {m.edit}
              </Link>
            </section>
          </div>
        </details>
        {loadingChoices && !expanded && <p role="status">{m.loading}</p>}
        <Button
          variant="primary"
          type="submit"
          disabled={
            !preferences.ready ||
            !player.data ||
            !character ||
            !story ||
            choiceError ||
            mutation.isPending
          }
        >
          {m.create}
        </Button>
        {mutation.error && (
          <p role="alert" className="error">
            {selectionChanged(mutation.error)
              ? m.startSettingsChanged
              : failure(mutation.error)}
          </p>
        )}
      </form>
    </section>
  )
}
