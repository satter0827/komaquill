import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { ArrowUp, ImagePlus, PanelRight, Sparkles } from 'lucide-react'
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react'
import { Link, useParams } from 'react-router'
import { isRejectedRequest, POLL_INTERVAL_MS } from '../api/client'
import * as q from '../api/generated/@tanstack/react-query.gen'
import type {
  CreationJob,
  OperationView,
  PersonDefinition,
  SendInput,
  StoryDefinition,
} from '../api/generated/types.gen'
import {
  useFailure,
  useFormatMessage,
  useMessages,
  usePreferences,
} from '../app/preferences'
import { Button } from '../components/ui/button'
import { CreationStatus, useCreation } from './creation'
import {
  AvailabilityNotice,
  recheckAvailability,
  useAvailability,
} from './availability'
import { ConversationNotes, SceneActions } from './conversation-tools'
import { ImageResult, ImageLinks } from './images'
import {
  ActionDescription,
  PlayerActions,
  usePlayerActions,
  useVisualReview,
  VisualReviewForm,
} from './player-actions'
import {
  isTerminal,
  keepTerminal,
  operationOptions,
  operationPending,
  watchReceipt,
} from './operations'

const narrowQuery = '(max-width: 850px)'
function subscribeViewport(listener: () => void) {
  const media = window.matchMedia(narrowQuery)
  media.addEventListener('change', listener)
  return () => media.removeEventListener('change', listener)
}
const narrowViewport = () => window.matchMedia(narrowQuery).matches

function Drawing({ job }: { job: CreationJob }) {
  const messages = useMessages()
  const failure = useFailure()
  return job.image_id ? (
    <ImageResult identity={job.image_id} />
  ) : (
    <p className="subtle">
      {job.error ? failure({ code: job.error }) : messages.imagePreparing}
    </p>
  )
}

export function ConversationPage() {
  const { identity = '' } = useParams()
  return <ConversationView key={identity} identity={identity} />
}

function ConversationView({ identity }: { identity: string }) {
  const cache = useQueryClient()
  const messages = useMessages()
  const failure = useFailure()
  const formatMessage = useFormatMessage()
  const preferences = usePreferences()
  const [text, setText] = useState('')
  const [subjects, setSubjects] = useState<
    'character' | 'pair' | 'environment'
  >('character')
  const [viewpoint, setViewpoint] = useState<'player' | 'observer'>('player')
  const draftVersion = useRef(0)
  const submittedDraft = useRef<{
    operationId: string
    version: number
  } | null>(null)
  const [operationId, setOperationId] = useState<string | null>(null)
  const [prepared, setPrepared] = useState<SendInput | null>(null)
  const narrow = useSyncExternalStore(
    subscribeViewport,
    narrowViewport,
    () => false,
  )
  const [wideOpen, setWideOpen] = useState(true)
  const [mobileOpen, setMobileOpen] = useState(false)
  const showState = narrow ? mobileOpen : wideOpen
  const setShowState = narrow ? setMobileOpen : setWideOpen
  const detail = useQuery({
    ...q.getConversationOptions({ path: { identity } }),
  })
  const actionControls = usePlayerActions({
    identity,
    conversation: detail.data,
  })
  const sendAvailability = useAvailability(
    detail.data ? { kind: 'conversation', identity } : undefined,
  )
  const [availabilityId] = useState(() => crypto.randomUUID())
  const drawAvailability = useAvailability(
    detail.data
      ? {
          kind: 'creation',
          creation: {
            operation_id: availabilityId,
            kind: 'conversation_image',
            target_id: identity,
            expected_revision: detail.data.revision,
            subjects,
            viewpoint,
            locale: preferences.value.locale,
            llm_profile: detail.data.definition.llm_profile,
            allow_real_llm: true,
          },
        }
      : undefined,
  )
  const history = useInfiniteQuery({
    ...q.getTurnsInfiniteOptions({ path: { identity } }),
    initialPageParam: { path: { identity } },
    getNextPageParam: (last) =>
      last.next_before == null
        ? undefined
        : { path: { identity }, query: { before: last.next_before } },
    enabled: !!detail.data?.turn_count,
  })
  useEffect(() => {
    if (detail.data?.turn_count)
      void cache.invalidateQueries({
        queryKey: q.getTurnsInfiniteQueryKey({ path: { identity } }),
      })
  }, [cache, identity, detail.data?.revision, detail.data?.turn_count])
  const savedOperations = useQuery({
    ...q.getConversationOperationsOptions({ path: { identity } }),
    refetchInterval: (query) =>
      query.state.data?.some(operationPending) ? POLL_INTERVAL_MS : false,
  })
  const operation = useQuery({
    ...operationOptions(operationId ?? ''),
    enabled: !!operationId,
  })
  useEffect(() => {
    if (operation.data || savedOperations.data)
      void cache.invalidateQueries({
        queryKey: q.getConversationQueryKey({ path: { identity } }),
      })
  }, [cache, identity, operation.data, savedOperations.data])

  const { clear: clearQueuedAction } = actionControls
  const confirmSend = useCallback(
    (result: OperationView, submitted: SendInput) => {
      cache.setQueryData<OperationView>(
        q.getOperationQueryKey({ path: { identity: result.id } }),
        (previous) => keepTerminal(previous, result),
      )
      setOperationId(result.id)
      if (
        submittedDraft.current?.operationId === submitted.operation_id &&
        submittedDraft.current.version === draftVersion.current
      )
        setText('')
      setPrepared(null)
      clearQueuedAction()
      void cache.invalidateQueries({
        queryKey: q.getConversationQueryKey({ path: { identity } }),
      })
      void cache.invalidateQueries({
        queryKey: q.getConversationOperationsQueryKey({ path: { identity } }),
      })
    },
    [cache, identity, clearQueuedAction],
  )
  const send = useMutation({
    ...q.sendTurnMutation(),
    onError: (error, submitted) => {
      void recheckAvailability(cache)
      if (isRejectedRequest(error)) {
        setPrepared(null)
        void cache.invalidateQueries({
          queryKey: q.getConversationQueryKey({ path: { identity } }),
        })
      } else setOperationId(submitted.body.operation_id)
    },
    onSuccess: (result, submitted) => {
      confirmSend(result, submitted.body)
    },
  })
  const resetSend = send.reset
  useEffect(() => {
    if (!send.isError || !prepared) return
    return watchReceipt(
      cache,
      q.getOperationQueryKey({ path: { identity: prepared.operation_id } }),
      prepared.operation_id,
      (confirmed: OperationView) => {
        confirmSend(confirmed, prepared)
        resetSend()
      },
    )
  }, [cache, send.isError, prepared, confirmSend, resetSend])
  const resume = useMutation({
    ...q.resumeOperationMutation(),
    onSuccess: (value) => {
      setOperationId(value.id)
      void cache.invalidateQueries({
        queryKey: q.getConversationOperationsQueryKey({ path: { identity } }),
      })
      void cache.invalidateQueries({
        queryKey: q.getConversationQueryKey({ path: { identity } }),
      })
    },
  })
  const drawing = useCreation()
  const drawings = useQuery({
    ...q.listConversationImagesOptions({ path: { identity } }),
    refetchInterval: (query) =>
      query.state.data?.some((job) => !isTerminal(job.state))
        ? POLL_INTERVAL_MS
        : false,
  })
  useEffect(() => {
    if (drawing.mutation.data || drawing.job.data)
      void cache.invalidateQueries({
        queryKey: q.listConversationImagesQueryKey({ path: { identity } }),
      })
  }, [cache, identity, drawing.mutation.data, drawing.job.data])
  const dialog = useRef<HTMLDialogElement>(null)
  const reviewControls = useVisualReview({
    identity,
    conversation: detail.data,
  })
  useEffect(() => {
    if (narrow && showState) dialog.current?.showModal()
    else dialog.current?.close()
  }, [narrow, showState])
  if (detail.error)
    return (
      <p role="alert" className="error">
        {failure(detail.error)}
      </p>
    )
  if (!detail.data) return <p className="loading">{messages.loading}</p>
  const conversation = {
    ...detail.data,
    turns: history.data
      ? [...history.data.pages].reverse().flatMap((page) => page.items)
      : (detail.data.turns ?? []),
  }
  const busy =
    actionControls.unresolved ||
    !!reviewControls.prepared ||
    send.isPending ||
    ['accepted', 'running'].includes(operation.data?.state ?? '') ||
    savedOperations.data?.some((value) =>
      ['accepted', 'running'].includes(value.state),
    )
  const needsReview = !!(
    conversation.definition.user?.visual_review_required ||
    conversation.definition.character?.visual_review_required
  )
  const canSend =
    !needsReview &&
    sendAvailability.ready &&
    (!actionControls.queued ||
      actionControls.queued.revision === conversation.revision) &&
    !busy &&
    !(send.isError && prepared !== null) &&
    !!text.trim()
  const latestOperation = operation.data ?? savedOperations.data?.[0]
  const snapshots = conversation.definition.presets
  const stateLabels = {
    place: messages.currentPlace,
    time: messages.currentTime,
    intention: messages.currentIntention,
    scenery: messages.narrationLabel,
    relationship: messages.relationship,
  }
  const visibleState = Object.entries(stateLabels)
    .map(
      ([key, label]) =>
        [
          key,
          label,
          conversation.state?.[key as keyof typeof stateLabels],
        ] as const,
    )
    .filter(([, , value]) => !!value)
  const content = (value: PersonDefinition | StoryDefinition | undefined) =>
    value
      ? ('opening' in value && value.opening) ||
        value.description ||
        ('world' in value && value.world) ||
        ('appearance' in value && value.appearance) ||
        ('role' in value && value.role) ||
        value.name ||
        ''
      : ''
  const sampleDrawing = conversation.definition.llm_profile === 'fake'
  const statePanel = (
    <aside className="state-panel">
      {narrow && (
        <Button onClick={() => setShowState(false)}>
          {messages.closeState}
        </Button>
      )}
      {snapshots?.character?.image_id && (
        <img
          className="character-portrait"
          src={`/api/v1/images/${snapshots.character.image_id}/files/0`}
          alt={snapshots.character.name}
        />
      )}
      {snapshots?.character?.image_id && (
        <ImageLinks identity={snapshots.character.image_id} />
      )}
      <div className="eyebrow">{messages.context}</div>
      <h2>{messages.currentState}</h2>
      <div className="state-items">
        {visibleState.map(([key, label, value]) => (
          <div key={key}>
            <span>{label}</span>
            <p>{value}</p>
          </div>
        ))}
        <div>
          <span>{messages.companionPlace}</span>
          <p>{conversation.state?.character_place || messages.notSpecified}</p>
        </div>
        {(['player', 'character'] as const).map((role) => (
          <div key={role}>
            <span>{role === 'player' ? messages.you : messages.character}</span>
            <dl>
              {(
                ['clothing', 'pose', 'expression', 'held_objects'] as const
              ).map((field) => {
                const value = conversation.state?.[role]?.[field]
                return (
                  <div key={field}>
                    <dt>
                      {field === 'held_objects'
                        ? messages.heldObjects
                        : messages[field]}
                    </dt>
                    <dd>
                      {value == null
                        ? messages.notSpecified
                        : (Array.isArray(value)
                            ? value.map((item) => item.name).join(', ')
                            : value) || messages.emptyValue}
                    </dd>
                  </div>
                )
              })}
            </dl>
          </div>
        ))}
        {!!conversation.state?.elapsed_minutes && (
          <p>
            {formatMessage('elapsedTime', {
              minutes: conversation.state.elapsed_minutes,
            })}
          </p>
        )}
      </div>
      {needsReview || reviewControls.prepared ? (
        <VisualReviewForm
          conversation={conversation}
          busy={!!busy}
          controls={reviewControls}
        />
      ) : (
        <PlayerActions
          conversation={conversation}
          busy={!!busy}
          controls={actionControls}
        />
      )}
      <ConversationNotes conversation={conversation} />
      <h3>{messages.scenario}</h3>
      <p className="state-description">
        {content(conversation.definition.scenario) || messages.openConversation}
      </p>
      {content(conversation.definition.character) && (
        <>
          <h3>{messages.character}</h3>
          <p className="state-description">
            {content(conversation.definition.character)}
          </p>
        </>
      )}
      <details className="story-details">
        <summary>{messages.fixedSettings}</summary>
        <p>{conversation.state?.goal}</p>
        {(['user', 'character'] as const).map((role) => (
          <div key={role}>
            <h3>{role === 'user' ? messages.you : messages.character}</h3>
            <dl>
              {(
                [
                  'name',
                  'age',
                  'gender',
                  'appearance',
                  'initial_clothing',
                ] as const
              ).map((field) => (
                <div key={field}>
                  <dt>{messages[field]}</dt>
                  <dd>
                    {conversation.definition[role]?.[field] ||
                      messages.notSpecified}
                  </dd>
                </div>
              ))}
            </dl>
          </div>
        ))}
        <span className="revision">
          {formatMessage('revision', {
            revision: conversation.revision ?? 0,
          })}
        </span>
      </details>
    </aside>
  )
  return (
    <div className={`conversation-layout ${showState ? '' : 'state-hidden'}`}>
      <section className="conversation-main">
        <header className="conversation-header">
          <div>
            <span className="eyebrow">{messages.conversationLabel}</span>
            <h2>{conversation.definition.title}</h2>
          </div>
          <Button
            aria-label={showState ? messages.closeState : messages.showState}
            onClick={() => setShowState(!showState)}
          >
            <PanelRight size={18} />
          </Button>
        </header>
        {snapshots?.story?.image_id && (
          <div className="conversation-banner">
            <a
              href={`/api/v1/images/${snapshots.story.image_id}/files/0`}
              target="_blank"
              rel="noopener noreferrer"
            >
              <img
                src={`/api/v1/images/${snapshots.story.image_id}/files/0`}
                alt={snapshots.story.name}
              />
            </a>
          </div>
        )}
        {snapshots?.story?.image_id && (
          <div className="banner-actions">
            <ImageLinks identity={snapshots.story.image_id} />
          </div>
        )}
        <div
          className="turns"
          role="log"
          aria-label={messages.conversationHistory}
          tabIndex={0}
        >
          {(drawings.data ?? [])
            .filter((job) => !job.turn_id)
            .map((job) => (
              <div className="turn" key={job.id}>
                <Drawing job={job} />
              </div>
            ))}
          {(conversation.turns ?? []).length === 0 && (
            <div className="conversation-empty">
              <Sparkles size={30} />
              <h2>{messages.newChapter}</h2>
              <p>
                {content(conversation.definition.scenario) ||
                  messages.beginMessage}
              </p>
            </div>
          )}
          {history.hasNextPage && (
            <Button
              onClick={() => void history.fetchNextPage()}
              disabled={history.isFetchingNextPage}
            >
              {messages.olderTurns}
            </Button>
          )}
          {history.error && <p role="alert">{failure(history.error)}</p>}
          {(conversation.turns ?? []).map((turn) => (
            <article className="turn" key={turn.operation_id}>
              {turn.user && (
                <div className="user-message">
                  <span className="message-label">{messages.you}</span>
                  <p>{turn.user}</p>
                </div>
              )}
              {(conversation.proposals ?? [])
                .filter((proposal) => proposal.turn_id === turn.operation_id)
                .map((proposal) => (
                  <div className="action-preview" key={proposal.id}>
                    <ActionDescription action={proposal.action} />
                    {proposal.state === 'pending' ? (
                      <Button onClick={() => setShowState(true)}>
                        {messages.reviewAction}
                      </Button>
                    ) : (
                      <p className="subtle">
                        {proposal.state === 'applied'
                          ? messages.actionApplied
                          : proposal.state === 'dismissed'
                            ? messages.actionDismissed
                            : messages.actionExpired}
                      </p>
                    )}
                  </div>
                ))}
              {turn.action && (
                <div className="user-message">
                  <span className="message-label">{messages.myActions}</span>
                  <ActionDescription action={turn.action} />
                </div>
              )}
              {!turn.action &&
                turn.action_id &&
                conversation.actions?.find(
                  (record) => record.request.operation_id === turn.action_id,
                ) && (
                  <div className="user-message">
                    <span className="message-label">{messages.myActions}</span>
                    <ActionDescription
                      action={
                        conversation.actions.find(
                          (record) =>
                            record.request.operation_id === turn.action_id,
                        )!.request.action
                      }
                    />
                  </div>
                )}
              <div className="assistant-message">
                {turn.assistant && (
                  <span className="message-label">
                    <Sparkles size={14} />
                    {snapshots?.character?.name ?? messages.character}
                  </span>
                )}
                {turn.narration && (
                  <p className="scene-narration">{turn.narration}</p>
                )}
                {turn.action_narration && (
                  <p className="action-narration">{turn.action_narration}</p>
                )}
                <p>{turn.assistant}</p>
                <SceneActions
                  conversation={conversation}
                  turnId={turn.operation_id}
                />
                {turn.image_id && (
                  <ImageResult
                    identity={turn.image_id}
                    operationId={turn.operation_id}
                  />
                )}
                {(drawings.data ?? [])
                  .filter((job) => job.turn_id === turn.operation_id)
                  .map((job) => (
                    <Drawing key={job.id} job={job} />
                  ))}
              </div>
            </article>
          ))}
          {busy && (
            <p className="pending" role="status">
              {messages.pending}
            </p>
          )}
        </div>
        <div className="composer-area">
          {send.error && (
            <p role="alert" className="error">
              {failure(send.error)}
              {prepared && (
                <Button
                  onClick={() => {
                    if (prepared)
                      send.mutate({ path: { identity }, body: prepared })
                  }}
                  disabled={send.isPending}
                >
                  {messages.retryRequest}
                </Button>
              )}
            </p>
          )}
          {latestOperation?.error && (
            <p role="alert" className="error">
              <span>{messages.replyFailedAfterAcceptance}</span>{' '}
              <span>{failure({ code: latestOperation.error })}</span>
            </p>
          )}
          {latestOperation?.can_resume && (
            <Button
              disabled={resume.isPending || !!busy}
              onClick={() =>
                resume.mutate({
                  path: { identity: latestOperation.id },
                  query: { allow_real_llm: true },
                })
              }
            >
              {messages.resumeOperation}
            </Button>
          )}
          {resume.error && (
            <p role="alert" className="error">
              {failure(resume.error)}
            </p>
          )}
          {sampleDrawing && (
            <p className="subtle">
              {messages.sampleDrawingUnavailable}{' '}
              <Link to="/new">{messages.newConversation}</Link>
            </p>
          )}
          <CreationStatus creation={drawing} />
          <details>
            <summary>{messages.drawingSettings}</summary>
            <label>
              {messages.drawingSubjects}
              <select
                value={subjects}
                onChange={(event) => {
                  setSubjects(event.target.value as typeof subjects)
                  if (event.target.value === 'pair') setViewpoint('observer')
                }}
              >
                <option value="character">{messages.partnerOnly}</option>
                <option value="pair">{messages.bothPeople}</option>
                <option value="environment">{messages.environmentOnly}</option>
              </select>
            </label>
            <label>
              {messages.viewpoint}
              <select
                value={viewpoint}
                onChange={(event) =>
                  setViewpoint(event.target.value as typeof viewpoint)
                }
              >
                <option value="player">{messages.playerView}</option>
                <option value="observer">{messages.observerView}</option>
              </select>
            </label>
          </details>
          {actionControls.queued && (
            <div className="queued-action">
              <strong>{messages.myActions}</strong>
              <ActionDescription action={actionControls.queued.action} />
              <Button disabled={!!busy} onClick={actionControls.clear}>
                {messages.removeQueuedAction}
              </Button>
            </div>
          )}
          <AvailabilityNotice status={sendAvailability} />
          {!drawAvailability.ready && (
            <AvailabilityNotice status={drawAvailability} />
          )}
          <form
            className="composer"
            onSubmit={(event) => {
              event.preventDefault()
              if (canSend) {
                const body = {
                  operation_id: crypto.randomUUID(),
                  text,
                  expected_revision:
                    actionControls.queued?.revision ??
                    conversation.revision ??
                    0,
                  action: actionControls.queued?.action,
                  proposal_id: actionControls.queued?.proposal_id,
                  allow_real_llm: true,
                  locale: preferences.value.locale,
                }
                submittedDraft.current = {
                  operationId: body.operation_id,
                  version: draftVersion.current,
                }
                setPrepared(body)
                send.mutate({ path: { identity }, body })
              }
            }}
          >
            <textarea
              aria-label={messages.messageLabel}
              placeholder={messages.placeholder}
              rows={2}
              value={text}
              onChange={(event) => {
                draftVersion.current += 1
                setText(event.target.value)
              }}
            />
            <Button
              className="draw-button"
              aria-label={messages.drawScene}
              title={messages.drawScene}
              disabled={
                !!busy ||
                needsReview ||
                drawing.busy ||
                sampleDrawing ||
                !drawAvailability.ready
              }
              onClick={() =>
                drawing.start({
                  kind: 'conversation_image',
                  subjects,
                  viewpoint,
                  target_id: identity,
                  expected_revision: conversation.revision,
                  locale: preferences.value.locale,
                  llm_profile: conversation.definition.llm_profile,
                  allow_real_llm: true,
                })
              }
            >
              <ImagePlus size={20} aria-hidden="true" />
            </Button>
            <Button
              type="submit"
              className="send-button"
              aria-label={messages.send}
              disabled={!canSend}
            >
              <ArrowUp size={20} />
            </Button>
          </form>
        </div>
      </section>
      {narrow ? (
        <dialog
          className="state-dialog"
          ref={dialog}
          aria-label={messages.currentState}
          onCancel={() => setMobileOpen(false)}
          onClose={() => setMobileOpen(false)}
        >
          {statePanel}
        </dialog>
      ) : (
        showState && statePanel
      )}
    </div>
  )
}
