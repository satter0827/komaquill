import { paths } from '../../shared/navigation'
import { isRejectedRequest } from '../../api/client'
import { OriginalImage } from '../../shared/images/original'
import { AvailabilityNotice, useAvailability } from '../../shared/availability'
import { objectLines, parseObjects } from '../../shared/forms/physical-objects'
import { presetsForLocale } from '../../shared/story/presets'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, UserRound } from 'lucide-react'
import { AlbumIcon } from '../../shared/ui/atelier-icons'
import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'
import * as q from '../../api/generated/@tanstack/react-query.gen'
import * as api from '../../api/generated/sdk.gen'
import type {
  CharacterPreset,
  CharacterInput,
  StoryInput,
  CharacterSaveRequest,
  PlayerInput,
  PlayerProfile,
  PresetSaveResult,
  StoryPreset,
} from '../../api/generated/types.gen'
import type { MessageKey } from '../../shared/messages'
import {
  useFailure,
  useMessages,
  usePreferences,
  useFormatMessage,
} from '../../shared/preferences'
import { Button } from '../../shared/ui/button'
import { Dialog, Modal } from '../../shared/ui/popup'
import { QueryNotice } from '../../shared/ui/query-notice'
import { CreationStatus, useCreation } from '../../shared/creation'
import {
  creationOptions,
  imageOptions,
  isTerminal,
  presetKey,
} from '../../shared/operations'

export type Kind = 'player' | 'character' | 'story'
export type Preset = PlayerProfile | CharacterPreset | StoryPreset

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
  story: [
    'name',
    'description',
    'world',
    'opening',
    'place',
    'goal',
    'tone',
    'topics',
    'avoid',
  ],
} as const
type FieldName = (typeof fields)[Kind][number]
const basicFields: readonly FieldName[] = ['name', 'description']
const storyOpeningFields: readonly FieldName[] = [...basicFields, 'opening']
const storyExpressionFields: readonly FieldName[] = ['tone', 'topics', 'avoid']
type Fields = PlayerInput & Partial<CharacterInput & StoryInput>
type DraftValues = Partial<Record<FieldName, string>>
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
    tone: value.tone ?? '',
    topics: value.topics ?? '',
    avoid: value.avoid ?? '',
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
      <div className="preset-card-body">
        <div className="preset-card-heading">
          {!preset.image_id &&
            (kind === 'story' ? (
              <AlbumIcon />
            ) : (
              <UserRound size={18} aria-hidden="true" />
            ))}
          <h2>{preset.name}</h2>
          {preset.sample && <span className="sample-badge">{m.sample}</span>}
        </div>

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
        <div className="actions">
          <Link
            className="text-button"
            to={`${paths[kind]}/${preset.id}`}
            aria-label={formatMessage(
              preset.sample ? 'editSamplePresetLabel' : 'editPresetLabel',
              { name: preset.name },
            )}
          >
            {m.edit}
          </Link>
          <Link className="text-button" to={`/new?${kind}=${preset.id}`}>
            {m.use} →
          </Link>
        </div>
      </div>
      {preset.image_id && (
        <div className="preset-cover">
          <OriginalImage identity={preset.image_id} alt={preset.name} />
        </div>
      )}
    </article>
  )
}

export function PlayerPage() {
  const m = useMessages()
  const [search] = useSearchParams()
  const query = useQuery(q.getPlayerOptions())
  const notice = (
    <QueryNotice
      error={query.error}
      retained={!!query.data}
      pending={query.isFetching}
      onRetry={() => void query.refetch()}
    />
  )
  if (query.error && !query.data) return notice
  if (!query.data) return <p className="page">{m.loading}</p>
  return (
    <>
      {notice}
      <PresetEditor
        kind="player"
        canMutate={!query.error}
        preset={query.data}
        returnTo={search.get('returnTo')}
      />
    </>
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
  const notice = (
    <QueryNotice
      error={query.error}
      retained={!!query.data}
      pending={query.isFetching}
      onRetry={() => void query.refetch()}
    />
  )
  if (query.error && !query.data) return notice
  if (!query.data) return <p className="page">{m.loading}</p>
  if (identity) {
    const preset =
      identity === 'new'
        ? undefined
        : query.data.find((item) => item.id === identity)
    if (identity !== 'new' && !preset)
      return <p className="page">{failure({ code: 'request_not_found' })}</p>
    return (
      <>
        {notice}
        <PresetEditor
          key={`${kind}:${identity}`}
          kind={kind}
          preset={preset}
          canMutate={!query.error}
          returnTo={search.get('returnTo')}
        />
      </>
    )
  }
  const visible = presetsForLocale<CharacterPreset | StoryPreset>(
    query.data,
    preferences.value.locale,
  )
  return (
    <section className="page library-page">
      {notice}
      <div className="page-heading">
        <div>
          <h1>{m[presetLabels[kind]]}</h1>
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

function PresetDraftDialog({
  kind,
  disabled,
  onApply,
}: {
  kind: 'character' | 'story'
  disabled: boolean
  onApply: (values: DraftValues) => void
}) {
  const m = useMessages()
  const preferences = usePreferences()
  const [open, setOpen] = useState(false)
  const [idea, setIdea] = useState('')
  const [handledDraftId, setHandledDraftId] = useState<string>()
  const [availabilityId] = useState(() => crypto.randomUUID())
  const reviewNotice = useRef<HTMLParagraphElement>(null)
  const draft = useCreation()
  const job = draft.job.data
  const result: Partial<Fields> | null | undefined =
    job?.state === 'complete' && job.id !== handledDraftId ? job.result : null
  const reviewing = !!result
  useEffect(() => {
    if (open && reviewing) reviewNotice.current?.focus()
  }, [open, reviewing])
  const suggestions = result
    ? fields[kind].flatMap((field) => {
        const value = result[field]
        const text =
          typeof value === 'string'
            ? value
            : Array.isArray(value)
              ? objectLines(value)
              : ''
        return text.trim() ? [{ field, text }] : []
      })
    : []
  const request = {
    kind:
      kind === 'character'
        ? ('character_draft' as const)
        : ('story_draft' as const),
    prompt: idea,
    locale: preferences.value.locale,
    llm_profile: preferences.value.llm_profile,
    allow_real_llm: true,
  }
  const availability = useAvailability(
    open && !result && idea.trim()
      ? {
          kind: 'creation',
          creation: { ...request, operation_id: availabilityId },
        }
      : undefined,
  )
  const canGenerate =
    !disabled && !draft.busy && !!idea.trim() && availability.ready
  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger render={<Button disabled={disabled} />}>
        {m.createDraft}
      </Dialog.Trigger>
      <Modal
        label={m.createDraft}
        closeLabel={m.closePanel}
        className="draft-dialog"
      >
        {result ? (
          <div className="draft-review">
            <p
              className="subtle"
              role="status"
              ref={reviewNotice}
              tabIndex={-1}
            >
              {suggestions.length ? m.draftReady : m.draftEmpty}
            </p>
            {suggestions.length > 0 && (
              <dl>
                {suggestions.map(({ field, text }) => (
                  <div key={field}>
                    <dt>{m[field]}</dt>
                    <dd>{text}</dd>
                  </div>
                ))}
              </dl>
            )}
            <div className="actions">
              {suggestions.length > 0 && (
                <Button
                  variant="primary"
                  disabled={disabled}
                  onClick={() => {
                    onApply(
                      Object.fromEntries(
                        suggestions.map(({ field, text }) => [field, text]),
                      ),
                    )
                    setHandledDraftId(job?.id)
                    setOpen(false)
                  }}
                >
                  {m.useDraft}
                </Button>
              )}
              <Button onClick={() => setHandledDraftId(job?.id)}>
                {m.reviseDraft}
              </Button>
            </div>
          </div>
        ) : (
          <form
            onSubmit={(event) => {
              event.preventDefault()
              if (canGenerate) draft.start(request)
            }}
          >
            <p className="subtle">{m.draftDescription}</p>
            <label>
              {m.draftPrompt}
              <textarea
                rows={4}
                value={idea}
                onChange={(event) => setIdea(event.target.value)}
              />
            </label>
            <div className="actions">
              <Button type="submit" variant="primary" disabled={!canGenerate}>
                {m.generateDraft}
              </Button>
            </div>
            {idea.trim() && !draft.busy && (
              <AvailabilityNotice availability={availability} />
            )}
            {(draft.busy ||
              draft.mutation.error ||
              draft.job.error ||
              (job && job.id !== handledDraftId)) && (
              <CreationStatus creation={draft} />
            )}
          </form>
        )}
      </Modal>
    </Dialog.Root>
  )
}

function PresetEditor({
  kind,
  preset,
  returnTo,
  canMutate,
}: {
  kind: Kind
  preset?: Preset
  returnTo: string | null
  canMutate: boolean
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
  const details = useRef<HTMLDetailsElement>(null)
  const [notice, setNotice] = useState(preset ? m.saved : '')
  const deleteCancel = useRef<HTMLButtonElement>(null)
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
  const dirty =
    fields[kind].some((field) => {
      if (field === 'initial_held_objects')
        return (
          heldObjects !==
          objectLines(
            current && 'initial_held_objects' in current
              ? (current.initial_held_objects ?? [])
              : [],
          )
        )
      const saved = current?.[field as keyof Preset]
      return field === 'initial_clothing'
        ? (value[field] ?? null) !== (saved ?? null)
        : (value[field] ?? '') !== (saved ?? '')
    }) ||
    (kind === 'story' &&
      (value.character_id || null) !==
        (current && 'character_id' in current
          ? (current.character_id ?? null)
          : null))
  const characters = useQuery({
    ...q.listCharactersOptions(),
    enabled: kind === 'story',
  })
  const recommendedId = value.character_id ?? ''
  const recommendedChoices = presetsForLocale(
    characters.data,
    preferences.value.locale,
    recommendedId,
  )
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
          if (kind === 'story' && !preset) url.searchParams.delete('character')
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
  const primaryFields = kind === 'story' ? storyOpeningFields : basicFields
  const detailFields = fields[kind].filter(
    (field) =>
      !primaryFields.includes(field) && !storyExpressionFields.includes(field),
  )
  const renderField = (field: FieldName) => (
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
          rows={primaryFields.includes(field) ? 3 : 2}
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
      {kind !== 'player' && (
        <Link className="text-button" to={paths[kind]}>
          ← {m[presetLabels[kind]]}
        </Link>
      )}
      <div className="page-heading">
        <h1>
          {kind === 'player'
            ? m.players
            : (current?.name ??
              (kind === 'story' ? m.newStory : m.newCharacter))}
        </h1>
        {kind !== 'player' && (
          <PresetDraftDialog
            kind={kind}
            disabled={!canMutate || mutation.isPending || !!pendingSave}
            onApply={({ initial_held_objects, ...changes }) => {
              if (initial_held_objects !== undefined)
                setHeldObjects(initial_held_objects)
              setValue((previous) => ({ ...previous, ...changes }))
              setNotice('')
              if (
                details.current &&
                (initial_held_objects !== undefined ||
                  Object.keys(changes).some(
                    (field) => !primaryFields.includes(field as FieldName),
                  ))
              )
                details.current.open = true
            }}
          />
        )}
      </div>
      <div
        className={`editor-layout ${kind === 'player' || !preset ? 'editor-layout-single' : ''}`}
      >
        <form
          onSubmit={(event) => {
            event.preventDefault()
            if (canMutate) saveCurrent()
          }}
        >
          <fieldset
            disabled={mutation.isPending || (!!pendingSave && mutation.isError)}
          >
            {primaryFields.map(renderField)}
            {kind === 'story' && (
              <div className="details-content">
                <label>
                  {m.defaultCharacter}
                  <select
                    value={recommendedId}
                    disabled={!characters.data || !!characters.error}
                    onChange={(event) =>
                      update('character_id', event.target.value)
                    }
                  >
                    <option value="">{m.noDefault}</option>
                    {recommendedId &&
                      !recommendedChoices.some(
                        (item) => item.id === recommendedId,
                      ) && (
                        <option value={recommendedId} disabled>
                          {characters.isPending
                            ? m.loading
                            : m.presetUnavailable}
                        </option>
                      )}
                    {recommendedChoices.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                  </select>
                </label>
                <QueryNotice
                  error={characters.error}
                  retained={characters.data !== undefined}
                  pending={characters.isFetching}
                  onRetry={() => void characters.refetch()}
                />
              </div>
            )}
            <details ref={details} className="editor-details">
              <summary>{m.basicDetails}</summary>
              <div className="details-content">
                {detailFields.map(renderField)}
                {kind === 'story' && (
                  <>
                    <h2>{m.storyExpression}</h2>
                    {storyExpressionFields.map(renderField)}
                  </>
                )}
              </div>
            </details>
            <div className="actions editor-actions">
              <Button
                variant="primary"
                type="submit"
                disabled={!canMutate || !value.name?.trim()}
              >
                {mutation.isPending ? m.saving : m.save}
              </Button>
            </div>
          </fieldset>
          <p role="status" className="success">
            {dirty ? m.unsavedChanges : notice || (current ? m.saved : '')}
          </p>
          {mutation.isError && pendingSave && (
            <Button
              onClick={() => mutation.mutate(pendingSave)}
              disabled={!canMutate || mutation.isPending}
            >
              {m.retrySave}
            </Button>
          )}
          {[mutation.error, duplicateAction.error, preferences.error]
            .filter(Boolean)
            .map((error, index) => (
              <p key={index} role="alert" className="error">
                {failure(error)}
              </p>
            ))}
        </form>
        {kind !== 'player' && preset && (
          <aside className="editor-preview">
            {imageRecord?.image_id && (
              <div
                className={`cover-preview ${kind === 'story' ? 'wide' : ''}`}
              >
                <OriginalImage
                  identity={imageRecord.image_id}
                  alt={imageRecord.name}
                />
              </div>
            )}
            {!imageRecord?.image_job_id && !imageRecord?.image_id && (
              <p className="subtle">{m.noImage}</p>
            )}
            <div className="actions">
              {current && (
                <Button
                  onClick={() => {
                    if (imageRequest && imageAvailability.ready && !dirty)
                      imageCreation.start(imageRequest)
                  }}
                  disabled={
                    !canMutate ||
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
              {current && !dirty && kind === 'character' && (
                <Link className="button" to={`/new?character=${current.id}`}>
                  {m.useCharacter}
                </Link>
              )}
            </div>
            {dirty && <p className="subtle">{m.saveBeforeImage}</p>}
            {imageRequest &&
              canMutate &&
              !dirty &&
              !pendingSave &&
              !imageCreation.busy && (
                <AvailabilityNotice availability={imageAvailability} />
              )}
            {imageRecord?.image_stale && (
              <p className="subtle">{m.imageNeedsUpdate}</p>
            )}
            <CreationStatus creation={imageCreation} />
            {imageRecord?.image_job_id &&
              imageRecord.image_job_id !== imageCreation.job.data?.id && (
                <PresetImageStatus identity={imageRecord.image_job_id} />
              )}
          </aside>
        )}
      </div>
      {current && kind !== 'player' && (
        <div className="actions editor-management">
          <Button
            onClick={() => duplicateAction.mutate()}
            disabled={
              !canMutate ||
              duplicateAction.isPending ||
              mutation.isPending ||
              !!pendingSave
            }
          >
            {m.duplicate}
          </Button>
          <Dialog.Root disablePointerDismissal>
            <Dialog.Trigger
              render={
                <Button
                  disabled={!canMutate || mutation.isPending || !!pendingSave}
                />
              }
            >
              {m.delete}
            </Dialog.Trigger>
            <Modal
              label={m.delete}
              closeLabel={m.closePanel}
              initialFocus={deleteCancel}
            >
              <p>{m.removeNotice}</p>
              {!!deletion.error && (
                <p role="alert" className="error">
                  {failure(deletion.error)}
                </p>
              )}
              <div className="actions">
                <Dialog.Close render={<Button ref={deleteCancel} />}>
                  {m.cancel}
                </Dialog.Close>
                <Button
                  variant="danger"
                  onClick={() => deletion.mutate()}
                  disabled={!canMutate || deletion.isPending}
                >
                  {m.delete}
                </Button>
              </div>
            </Modal>
          </Dialog.Root>
        </div>
      )}
    </section>
  )
}
