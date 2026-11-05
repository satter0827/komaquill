import { isRejectedRequest } from '../api/client'
import { OriginalImage } from './images'
import { useAvailability } from './availability'
import { objectLines, parseObjects } from './physical-objects'
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
import {
  creationOptions,
  imageOptions,
  isTerminal,
  presetKey,
} from './operations'

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
    'initial_clothing',
    'initial_held_objects',
    'role',
  ],
  character: [
    'name',
    'age',
    'gender',
    'description',
    'appearance',
    'initial_clothing',
    'initial_held_objects',
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
): Promise<PresetSaveResult | { preset: PlayerProfile }> {
  const query = { expected_revision: previous?.revision ?? 0 }
  const player: PlayerInput = {
    name: value.name,
    description: value.description ?? '',
    appearance: value.appearance ?? '',
    initial_clothing: value.initial_clothing ?? null,
    initial_held_objects: value.initial_held_objects ?? [],
    visual_review_required: false,
    role: value.role ?? '',
    address: value.address ?? '',
    age: value.age ?? '',
    gender: value.gender ?? '',
  }
  if (kind === 'player')
    return {
      preset: (await api.updatePlayer({ query, body: player })).data!,
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

function PresetImageStatus({
  identity,
  recoveryPath,
}: {
  identity: string
  recoveryPath?: string
}) {
  const m = useMessages()
  const failure = useFailure()
  const job = useQuery({ ...creationOptions(identity), refetchInterval: false })
  const imageId = job.data?.image_id ?? ''
  const image = useQuery({ ...imageOptions(imageId), enabled: false })
  const jobState = job.data?.state
  const refreshJob = job.refetch
  useEffect(() => {
    if (isTerminal(image.data?.state) && jobState && !isTerminal(jobState))
      void refreshJob()
  }, [image.data?.state, imageId, jobState, refreshJob])
  const error = job.error ?? (job.data?.error ? { code: job.data.error } : null)
  if (job.data?.state === 'complete' && !error) return null
  return (
    <>
      <span className={error ? 'error' : 'subtle'}>
        {error
          ? recoveryPath
            ? m.imageUnavailable
            : failure(error)
          : job.data
            ? m[`job_${job.data.state}`]
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
      <div className="preset-cover">
        {preset.image_id ? (
          <OriginalImage identity={preset.image_id} alt={preset.name} />
        ) : (
          <Link
            to={`${paths[kind]}/${preset.id}`}
            aria-label={formatMessage(
              preset.sample ? 'editSamplePresetLabel' : 'editPresetLabel',
              { name: preset.name },
            )}
          >
            {kind === 'story' ? (
              <BookOpen size={38} />
            ) : (
              <UserRound size={38} />
            )}
          </Link>
        )}
        {preset.sample && <span className="sample-badge">{m.sample}</span>}
      </div>
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
          <PresetImageStatus
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
  const [heldObjects, setHeldObjects] = useState(() =>
    preset && 'initial_held_objects' in preset
      ? objectLines(preset.initial_held_objects)
      : '',
  )
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
  const imageCreation = useCreation()
  const [availabilityId] = useState(() => crypto.randomUUID())
  const imageRequest =
    current && preset && kind !== 'player'
      ? {
          operation_id: availabilityId,
          kind:
            kind === 'character' ? ('portrait' as const) : ('banner' as const),
          target_id: current.id,
          expected_revision: current.revision,
          locale: preferences.value.locale,
          llm_profile: generationProfile,
          allow_real_llm: true,
        }
      : undefined
  const imageAvailability = useAvailability(
    imageRequest ? { kind: 'creation', creation: imageRequest } : undefined,
  )
  const draftAvailability = useAvailability(
    kind !== 'player' && idea.trim()
      ? {
          kind: 'creation',
          creation: {
            operation_id: availabilityId,
            kind: kind === 'character' ? 'character_draft' : 'story_draft',
            prompt: idea,
            locale: preferences.value.locale,
            llm_profile: generationProfile,
            allow_real_llm: true,
          },
        }
      : undefined,
  )
  const dirty =
    !current ||
    fields[kind].some((field) =>
      field === 'initial_held_objects'
        ? heldObjects !==
          objectLines(
            'initial_held_objects' in current
              ? (current.initial_held_objects ?? [])
              : [],
          )
        : (value[field] ?? '') !==
          (current[field as keyof typeof current] ?? ''),
    )
  const characters = useQuery({
    ...q.listCharactersOptions(),
    enabled: kind === 'story',
  })
  const mutation = useMutation({
    mutationFn: (submission: SaveSubmission) =>
      save(kind, submission.value, submission.previous, submission.options),
    onError: (error) => {
      if (isRejectedRequest(error)) setPendingSave(null)
    },
    onSuccess: (result) => {
      const record = result.preset
      if (kind === 'player')
        cache.setQueryData(q.getPlayerQueryKey(), record as PlayerProfile)
      else
        cache.setQueryData<Preset[]>(presetKey(kind), (records) => [
          record,
          ...(records ?? []).filter((item) => item.id !== record.id),
        ])
      setCurrent(record)
      setPendingSave(null)
      setNotice(m.saved)
      void cache.invalidateQueries({
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
  const saveCurrent = () => {
    const submission = pendingSave ?? {
      value: {
        ...value,
        initial_held_objects: parseObjects(
          heldObjects,
          current && 'initial_held_objects' in current
            ? current.initial_held_objects
            : [],
        ),
      },
      previous: current,
      options: {
        operation_id: crypto.randomUUID(),
        target_id: current?.id ?? null,
        expected_revision: current?.revision ?? null,
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
    if (key === 'initial_held_objects') setHeldObjects(text)
    else setValue((old) => ({ ...old, [key]: text }))
    setNotice('')
  }
  const renderField = (field: (typeof fields)[Kind][number], index: number) => (
    <label key={field}>
      {field === 'name' ? m.requiredName : m[field as MessageKey]}
      {['name', 'age', 'gender', 'address'].includes(field) ? (
        <input
          required={field === 'name'}
          maxLength={120}
          value={
            Array.isArray(value[field])
              ? value[field].join('\n')
              : (value[field] ?? '')
          }
          onChange={(event) => update(field, event.target.value)}
        />
      ) : (
        <textarea
          rows={index < 3 ? 3 : 2}
          value={
            field === 'initial_held_objects'
              ? heldObjects
              : Array.isArray(value[field])
                ? value[field].join('\n')
                : (value[field] ?? '')
          }
          onChange={(event) => update(field, event.target.value)}
        />
      )}
    </label>
  )
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
      <div
        className={`editor-layout ${kind === 'player' ? 'profile-layout' : ''}`}
      >
        <form
          onSubmit={(event) => {
            event.preventDefault()
            saveCurrent()
          }}
        >
          <fieldset
            disabled={mutation.isPending || (!!pendingSave && mutation.isError)}
          >
            {fields[kind]
              .filter((field) => field === 'name' || field === 'description')
              .map(renderField)}
            <details>
              <summary>{m.basicDetails}</summary>
              <div className="details-content">
                {fields[kind]
                  .filter(
                    (field) => field !== 'name' && field !== 'description',
                  )
                  .map(renderField)}
              </div>
            </details>
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
            <div className="actions editor-actions">
              <Button
                variant="primary"
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
                    variant="danger"
                    onClick={() => setConfirmDelete(true)}
                  >
                    {m.delete}
                  </Button>
                </>
              )}
            </div>
          </fieldset>
          <p role="status" className="success">
            {dirty ? m.unsavedChanges : notice || (current ? m.saved : '')}
          </p>
          {mutation.isError && pendingSave && (
            <Button
              onClick={() => mutation.mutate(pendingSave)}
              disabled={mutation.isPending}
            >
              {m.retrySave}
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
                  <OriginalImage
                    identity={imageRecord.image_id}
                    alt={imageRecord.name}
                  />
                ) : (
                  <UserRound size={52} strokeWidth={1} />
                )}
              </div>

              {!imageRecord?.image_job_id && !imageRecord?.image_id && (
                <p className="subtle">{m.noImage}</p>
              )}
              {current && preset && (
                <Button
                  onClick={() => {
                    if (imageRequest && imageAvailability.ready && !dirty)
                      imageCreation.start(imageRequest)
                  }}
                  disabled={
                    mutation.isPending ||
                    imageCreation.busy ||
                    !!pendingSave ||
                    dirty ||
                    !imageAvailability.ready
                  }
                >
                  {m.createImage}
                </Button>
              )}
              {dirty && <p className="subtle">{m.saveBeforeImage}</p>}
              {current && !dirty && kind === 'character' && (
                <Link className="button" to={`/new?character=${current.id}`}>
                  {m.useCharacter}
                </Link>
              )}
              {imageRecord?.image_stale && (
                <p className="subtle">{m.imageNeedsUpdate}</p>
              )}

              <CreationStatus creation={imageCreation} />
              {imageRecord?.image_job_id &&
                imageRecord.image_job_id !== imageCreation.job.data?.id && (
                  <PresetImageStatus identity={imageRecord.image_job_id} />
                )}
              <details className="draft-panel">
                <summary>{m.createDraft}</summary>
                <div className="details-content">
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
                    disabled={
                      draft.busy || !idea.trim() || !draftAvailability.ready
                    }
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
                          const result = draft.job.data!.result!
                          if (
                            'initial_held_objects' in result &&
                            Array.isArray(result.initial_held_objects)
                          )
                            setHeldObjects(
                              objectLines(result.initial_held_objects),
                            )
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
                </div>
              </details>
            </>
          )}
        </aside>
      </div>
    </section>
  )
}
