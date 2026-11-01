import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BookOpen, Plus, UserRound } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'
import * as q from '../api/generated/@tanstack/react-query.gen'
import * as api from '../api/generated/sdk.gen'
import type {
  CharacterPreset,
  CharacterInput,
  StoryInput,
  CharacterSaveRequest,
  PlayerInput,
  PlayerProfile,
  PresetSaveResult,
  StoryPreset,
} from '../api/generated/types.gen'
import type { MessageKey } from '../app/messages'
import {
  useFailure,
  useMessages,
  usePreferences,
  useFormatMessage,
} from '../app/preferences'
import { Button } from '../components/ui/button'
import { CreationStatus, useCreation } from './creation'
import { creationOptions, presetKey, refreshPresets } from './operations'

export type Kind = 'player' | 'character' | 'story'
export type Preset = PlayerProfile | CharacterPreset | StoryPreset
export const paths = {
  player: '/player',
  character: '/characters',
  story: '/stories',
}
const presetLabels = {
  player: 'players',
  character: 'characters',
  story: 'stories',
} as const
const fields = {
  player: [
    'name',
    'age',
    'gender',
    'address',
    'description',
    'appearance',
    'role',
  ],
  character: [
    'name',
    'age',
    'gender',
    'description',
    'appearance',
    'role',
    'personality',
    'speech',
    'background',
    'relationship',
  ],
  story: ['name', 'description', 'world', 'opening', 'place', 'goal'],
} as const
type Fields = PlayerInput & Partial<CharacterInput & StoryInput>
type PresetSaveOptions = Omit<CharacterSaveRequest, 'value'>
type SaveSubmission = {
  value: Fields
  previous?: Preset
  options: PresetSaveOptions
}

async function save(
  kind: Kind,
  value: Fields,
  previous: Preset | undefined,
  options: PresetSaveOptions,
): Promise<
  PresetSaveResult | { preset: PlayerProfile; job: null; image_error: null }
> {
  const query = { expected_revision: previous?.revision ?? 0 }
  const player: PlayerInput = {
    name: value.name,
    description: value.description ?? '',
    appearance: value.appearance ?? '',
    role: value.role ?? '',
    address: value.address ?? '',
    age: value.age ?? '',
    gender: value.gender ?? '',
  }
  if (kind === 'player')
    return {
      preset: (await api.updatePlayer({ query, body: player })).data!,
      job: null,
      image_error: null,
    }
  if (kind === 'character') {
    const body = {
      ...player,
      personality: value.personality ?? '',
      speech: value.speech ?? '',
      background: value.background ?? '',
      relationship: value.relationship ?? '',
    }
    return (await api.saveCharacter({ body: { ...options, value: body } }))
      .data!
  }
  const body = {
    name: value.name,
    description: value.description ?? '',
    world: value.world ?? '',
    opening: value.opening ?? '',
    place: value.place ?? '',
    goal: value.goal ?? '',
    character_id: value.character_id || null,
  }
  return (await api.saveStory({ body: { ...options, value: body } })).data!
}

async function duplicate(kind: Kind, identity: string) {
  const options = { path: { identity } }
  if (kind === 'character') return api.duplicateCharacter(options)
  return api.duplicateStory(options)
}

async function remove(kind: Kind, preset: Preset) {
  const options = {
    path: { identity: preset.id },
    query: { expected_revision: preset.revision ?? 0 },
  }
  if (kind === 'character') return api.deleteCharacter(options)
  return api.deleteStory(options)
}

function JobMonitor({
  identity,
  recoveryPath,
  displayComplete = false,
}: {
  identity: string
  recoveryPath?: string
  displayComplete?: boolean
}) {
  const cache = useQueryClient()
  const m = useMessages()
  const failure = useFailure()
  const job = useQuery(creationOptions(identity))

  useEffect(() => {
    if (job.data?.state === 'complete') {
      void refreshPresets(cache)
    }
  }, [job.data?.state, cache])
  const error = job.error ?? (job.data?.error ? { code: job.data.error } : null)
  return (
    <>
      <span className={error ? 'error' : 'subtle'}>
        {error
          ? recoveryPath
            ? m.imageUnavailable
            : failure(error)
          : job.data
            ? job.data.state === 'complete'
              ? displayComplete
                ? m.imageReady
                : null
              : m[`job_${job.data.state}`]
            : m.imagePreparing}
      </span>
      {error && recoveryPath && (
        <Link className="text-button" to={recoveryPath}>
          {m.createImage} →
        </Link>
      )}
    </>
  )
}

export function PresetCard({ preset, kind }: { preset: Preset; kind: Kind }) {
  const m = useMessages()
  const formatMessage = useFormatMessage()
  return (
    <article className={`preset-card preset-${kind}`}>
      <Link
        to={`${paths[kind]}/${preset.id}`}
        className="preset-cover"
        aria-label={formatMessage(
          preset.sample ? 'editSamplePresetLabel' : 'editPresetLabel',
          { name: preset.name },
        )}
      >
        {preset.image_id ? (
          <img
            src={`/api/v1/images/${preset.image_id}/files/0`}
            alt={preset.name}
          />
        ) : kind === 'story' ? (
          <BookOpen size={38} strokeWidth={1} />
        ) : (
          <UserRound size={38} strokeWidth={1} />
        )}
        {preset.sample && <span className="sample-badge">{m.sample}</span>}
      </Link>
      <div className="preset-card-body">
        <h2>
          <Link to={`${paths[kind]}/${preset.id}`}>{preset.name}</Link>
        </h2>
        {'role' in preset && preset.role && (
          <p className="preset-role">{preset.role}</p>
        )}
        <p>
          {preset.description ||
            ('personality' in preset ? preset.personality : '')}
        </p>
        {'age' in preset && (preset.age || preset.gender) && (
          <dl className="preset-facts">
            {preset.age && (
              <div>
                <dt>{m.age}</dt>
                <dd>{preset.age}</dd>
              </div>
            )}
            {preset.gender && (
              <div>
                <dt>{m.gender}</dt>
                <dd>{preset.gender}</dd>
              </div>
            )}
          </dl>
        )}
        {preset.image_job_id && !preset.image_id && (
          <JobMonitor
            identity={preset.image_job_id}
            recoveryPath={`${paths[kind]}/${preset.id}`}
          />
        )}
        <Link className="text-button" to={`/new?${kind}=${preset.id}`}>
          {m.use} →
        </Link>
      </div>
    </article>
  )
}

export function PlayerPage() {
  const m = useMessages()
  const failure = useFailure()
  const [search] = useSearchParams()
  const query = useQuery(q.getPlayerOptions())
  if (query.error)
    return (
      <p className="error" role="alert">
        {failure(query.error)}
      </p>
    )
  if (!query.data) return <p className="page">{m.loading}</p>
  return (
    <PresetEditor
      kind="player"
      preset={query.data}
      returnTo={search.get('returnTo')}
    />
  )
}

export function LibraryPage({ kind }: { kind: Exclude<Kind, 'player'> }) {
  const { identity } = useParams()
  const m = useMessages()
  const preferences = usePreferences()
  const failure = useFailure()
  const characters = useQuery({
    ...q.listCharactersOptions(),
    enabled: kind === 'character',
  })
  const stories = useQuery({
    ...q.listStoriesOptions(),
    enabled: kind === 'story',
  })
  const query = kind === 'character' ? characters : stories
  const [search] = useSearchParams()
  if (query.error)
    return (
      <p className="error" role="alert">
        {failure(query.error)}
      </p>
    )
  if (!query.data) return <p className="page">{m.loading}</p>
  if (identity) {
    const preset =
      identity === 'new'
        ? undefined
        : query.data.find((item) => item.id === identity)
    if (identity !== 'new' && !preset)
      return <p className="page">{failure({ code: 'request_not_found' })}</p>
    return (
      <PresetEditor
        key={`${kind}:${identity}`}
        kind={kind}
        preset={preset}
        returnTo={search.get('returnTo')}
      />
    )
  }
  const eyebrow =
    kind === 'story'
      ? m.storyEyebrow
      : kind === 'character'
        ? m.charactersEyebrow
        : m.playersEyebrow
  const visible = query.data.filter(
    (preset) =>
      !preset.sample || preset.sample_locale === preferences.value.locale,
  )
  return (
    <section className="page library-page">
      <div className="page-heading">
        <div>
          <div className="eyebrow">{eyebrow}</div>
          <h1>{m[presetLabels[kind]]}</h1>
          <p className="subtle">{m.libraryDescription}</p>
        </div>
        <Link className="button primary" to={`${paths[kind]}/new`}>
          <Plus size={17} />
          {m.new}
        </Link>
      </div>
      {!visible.length && <p className="empty-card">{m.noPresets}</p>}
      <div className="preset-grid">
        {visible.map((preset) => (
          <PresetCard key={preset.id} preset={preset} kind={kind} />
        ))}
      </div>
    </section>
  )
}

function PresetEditor({
  kind,
  preset,
  returnTo,
}: {
  kind: Kind
  preset?: Preset
  returnTo: string | null
}) {
  const m = useMessages()
  const failure = useFailure()
  const preferences = usePreferences()
  const cache = useQueryClient()
  const navigate = useNavigate()
  const [current, setCurrent] = useState(preset)
  const [value, setValue] = useState<Fields>(() =>
    preset
      ? (Object.fromEntries(
          Object.entries(preset).filter(
            ([, value]) => typeof value === 'string',
          ),
        ) as Fields)
      : { name: '', description: '' },
  )
  const generationProfile = preferences.value.llm_profile
  const imageRecord =
    preset && preset.id === current?.id && preset.revision >= current.revision
      ? preset
      : current
  const [idea, setIdea] = useState('')
  const [notice, setNotice] = useState(preset ? m.saved : '')
  const [confirmDelete, setConfirmDelete] = useState(false)
  const draft = useCreation()
  const [pendingSave, setPendingSave] = useState<SaveSubmission | null>(null)
  const [imageError, setImageError] = useState<string | null>(null)
  const characters = useQuery({
    ...q.listCharactersOptions(),
    enabled: kind === 'story',
  })
  const connectionQuery = {
    queryKey: q.getRuntimeQueryKey(),
    queryFn: async () => (await api.refreshRuntime()).data!,
  }
  const runtime = useQuery({ ...connectionQuery, enabled: kind !== 'player' })
  const connected = !runtime.isError && runtime.data?.connection === 'connected'
  const mutation = useMutation({
    mutationFn: (submission: SaveSubmission) =>
      save(kind, submission.value, submission.previous, submission.options),
    onError: (error) => {
      const code =
        error && typeof error === 'object' && 'code' in error
          ? error.code
          : null
      if (
        [
          'invalid_request',
          'request_not_found',
          'operation_conflict',
          'operation_busy',
        ].includes(String(code))
      )
        setPendingSave(null)
    },
    onSuccess: async (result) => {
      const record = result.preset
      setCurrent(record)
      setPendingSave(null)
      setImageError(result.image_error ?? null)
      setNotice(m.saved)
      if (result.job)
        cache.setQueryData(
          q.getJobQueryKey({ path: { identity: result.job.id ?? '' } }),
          result.job,
        )
      await cache.invalidateQueries({
        queryKey: presetKey(kind),
      })
      if (returnTo?.startsWith('/new')) {
        const url = new URL(returnTo, 'http://localhost')
        if (url.pathname === '/new') {
          if (kind !== 'player') url.searchParams.set(kind, record.id)
          navigate(url.pathname + url.search)
          return
        }
      }
      if (!preset && kind !== 'player')
        navigate(`${paths[kind]}/${record.id}`, { replace: true })
    },
  })
  const saveCurrent = (policy: 'auto' | 'always') => {
    const submission = pendingSave ?? {
      value: { ...value },
      previous: current,
      options: {
        operation_id: crypto.randomUUID(),
        target_id: current?.id ?? null,
        expected_revision: current?.revision ?? null,
        image_policy: policy,
        locale: preferences.value.locale,
        llm_profile: generationProfile,
        allow_real_llm: true,
      },
    }
    setPendingSave(submission)
    mutation.mutate(submission)
  }
  const duplicateAction = useMutation({
    mutationFn: () => duplicate(kind, current!.id),
    onSuccess: async () => {
      await cache.invalidateQueries({ queryKey: presetKey(kind) })
      navigate(paths[kind])
    },
  })
  const deletion = useMutation({
    mutationFn: () => remove(kind, current!),
    onSuccess: async () => {
      await Promise.all(
        [
          presetKey(kind),
          q.getPreferencesQueryKey(),
          q.listStoriesQueryKey(),
        ].map((queryKey) => cache.invalidateQueries({ queryKey })),
      )
      navigate(paths[kind])
    },
  })
  const update = (key: keyof Fields, text: string) => {
    setValue((old) => ({ ...old, [key]: text }))
    setNotice('')
  }
  return (
    <section className="page editor-page">
      <Link className="text-button" to={paths[kind]}>
        ← {m[presetLabels[kind]]}
      </Link>
      <div className="eyebrow">
        {kind === 'story'
          ? m.storyEyebrow
          : kind === 'character'
            ? m.charactersEyebrow
            : m.playersEyebrow}
      </div>
      <h1>{current ? current.name : m.new}</h1>
      <div className="editor-layout">
        <form
          onSubmit={(event) => {
            event.preventDefault()
            saveCurrent('auto')
          }}
        >
          <fieldset
            disabled={mutation.isPending || (!!pendingSave && mutation.isError)}
          >
            {fields[kind].map((field, index) => (
              <label key={field}>
                {m[field as MessageKey]}
                {['name', 'age', 'gender', 'address'].includes(field) ? (
                  <input
                    required={field === 'name'}
                    maxLength={120}
                    value={value[field] ?? ''}
                    onChange={(event) => update(field, event.target.value)}
                  />
                ) : (
                  <textarea
                    rows={index < 3 ? 3 : 2}
                    value={value[field] ?? ''}
                    onChange={(event) => update(field, event.target.value)}
                  />
                )}
              </label>
            ))}
            {kind === 'story' && (
              <label>
                {m.defaultCharacter}
                <select
                  value={value.character_id ?? ''}
                  onChange={(event) =>
                    update('character_id', event.target.value)
                  }
                >
                  <option value="">{m.noDefault}</option>
                  {characters.data
                    ?.filter(
                      (item) =>
                        !item.sample ||
                        item.sample_locale === preferences.value.locale,
                    )
                    .map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                </select>
              </label>
            )}
            <div className="actions">
              <Button
                className="primary"
                type="submit"
                disabled={!value.name?.trim()}
              >
                {mutation.isPending ? m.saving : m.save}
              </Button>
              {current && kind !== 'player' && (
                <>
                  <Button
                    onClick={() => duplicateAction.mutate()}
                    disabled={duplicateAction.isPending}
                  >
                    {m.duplicate}
                  </Button>
                  <Button
                    className="danger"
                    onClick={() => setConfirmDelete(true)}
                  >
                    {m.delete}
                  </Button>
                </>
              )}
            </div>
          </fieldset>
          <p role="status" className="success">
            {notice}
          </p>
          {mutation.isError && pendingSave && (
            <Button
              onClick={() => mutation.mutate(pendingSave)}
              disabled={mutation.isPending}
            >
              {m.retryRequest}
            </Button>
          )}
          {[
            mutation.error,
            duplicateAction.error,
            deletion.error,
            preferences.error,
          ]
            .filter(Boolean)
            .map((error, index) => (
              <p key={index} role="alert" className="error">
                {failure(error)}
              </p>
            ))}
          {confirmDelete && (
            <div className="confirmation" role="alert">
              <p>{m.removeNotice}</p>
              <Button
                onClick={() => deletion.mutate()}
                disabled={deletion.isPending}
              >
                {m.delete}
              </Button>
              <Button onClick={() => setConfirmDelete(false)}>
                {m.cancel}
              </Button>
            </div>
          )}
        </form>
        <aside className="editor-preview">
          {kind !== 'player' && (
            <>
              <div
                className={`cover-preview ${kind === 'story' ? 'wide' : ''}`}
              >
                {imageRecord?.image_id ? (
                  <img
                    src={`/api/v1/images/${imageRecord.image_id}/files/0`}
                    alt={imageRecord.name}
                  />
                ) : (
                  <UserRound size={52} strokeWidth={1} />
                )}
              </div>
              {!imageRecord?.image_job_id && (
                <p className="subtle">
                  {imageRecord?.image_id
                    ? m.imageReady
                    : connected
                      ? m.noImage
                      : m.imageNotConnected}
                </p>
              )}
              {current && (
                <Button
                  onClick={() => {
                    saveCurrent('always')
                  }}
                  disabled={
                    mutation.isPending || !!pendingSave || !value.name?.trim()
                  }
                >
                  {m.createImage}
                </Button>
              )}
              {runtime.error && (
                <p className="error" role="alert">
                  {failure(runtime.error)}
                </p>
              )}
              {imageRecord?.image_job_id && (
                <JobMonitor
                  identity={imageRecord.image_job_id}
                  displayComplete
                />
              )}
              {imageError && (
                <p role="alert" className="error">
                  {failure({ code: imageError })}
                </p>
              )}
              <details className="draft-panel">
                <summary>{m.createDraft}</summary>
                <p className="subtle">{m.draftDescription}</p>
                <label>
                  {m.draftPrompt}
                  <textarea
                    rows={4}
                    value={idea}
                    onChange={(event) => setIdea(event.target.value)}
                  />
                </label>
                <Button
                  onClick={() => {
                    draft.start({
                      kind:
                        kind === 'character'
                          ? 'character_draft'
                          : 'story_draft',
                      prompt: idea,
                      locale: preferences.value.locale,
                      llm_profile: generationProfile,
                      allow_real_llm: true,
                    })
                  }}
                  disabled={draft.busy || !idea.trim()}
                >
                  {m.createDraft}
                </Button>
                <CreationStatus creation={draft} />
                {draft.job.data?.result && (
                  <div className="draft-result">
                    <p>{m.draftReady}</p>
                    <dl>
                      {Object.entries(draft.job.data.result)
                        .filter(
                          ([key, value]) =>
                            fields[kind].some((field) => field === key) &&
                            value,
                        )
                        .map(([key, value]) => (
                          <div key={key}>
                            <dt>{m[key as MessageKey]}</dt>
                            <dd>{String(value)}</dd>
                          </div>
                        ))}
                    </dl>
                    <Button
                      onClick={() => {
                        setValue((previous) => ({
                          ...previous,
                          ...(Object.fromEntries(
                            Object.entries(draft.job.data!.result!).filter(
                              ([key, v]) =>
                                fields[kind].some((field) => field === key) &&
                                typeof v === 'string',
                            ),
                          ) as Fields),
                        }))
                        setNotice('')
                      }}
                    >
                      {m.useDraft}
                    </Button>
                  </div>
                )}
              </details>
            </>
          )}
        </aside>
      </div>
    </section>
  )
}
