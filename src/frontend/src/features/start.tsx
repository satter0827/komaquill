import { Link, useNavigate, useSearchParams } from 'react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { startConversation } from '../api/generated/sdk.gen'
import { useFailure, useMessages, usePreferences } from '../app/preferences'
import { Button } from '../components/ui/button'
import { loadPresets, paths, playerQuery } from './library'
import type { Preset } from './library'

export function NewConversation() {
  const m = useMessages()
  const failure = useFailure()
  const preferences = usePreferences()
  const cache = useQueryClient()
  const navigate = useNavigate()
  const [search, setSearch] = useSearchParams()
  const player = useQuery(playerQuery)
  const characters = useQuery({
    queryKey: ['characters'],
    queryFn: () => loadPresets('character'),
  })
  const stories = useQuery({
    queryKey: ['stories'],
    queryFn: () => loadPresets('story'),
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
    mutationFn: async () => {
      const ref = (value: Preset) => ({
        id: value.id,
        revision: value.revision,
      })
      return (
        await startConversation({
          body: {
            player_revision: player.data!.revision,
            character: ref(character!),
            story: ref(story!),
            locale: preferences.value.locale,
          },
        })
      ).data!
    },
    onSuccess: async (conversation) => {
      await cache.invalidateQueries({ queryKey: ['conversations'] })
      navigate(`/conversations/${conversation.id}`)
    },
  })
  const change = (kind: 'character' | 'story', id: string) => {
    const next = new URLSearchParams(search)
    next.set(kind, id)
    if (kind === 'story') next.delete('character')
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
          if (player.data && character && story) mutation.mutate()
        }}
      >
        <div className="selection-grid">
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
                  <img
                    className="selection-image"
                    src={`/api/v1/images/${record.image_id}/files/0`}
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
        {[mutation.error, player.error, characters.error, stories.error]
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
