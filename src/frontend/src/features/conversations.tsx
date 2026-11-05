import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { Send, Feather, PanelRight, Sparkles } from 'lucide-react'
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from 'react'
import { useStickToBottom } from 'use-stick-to-bottom'
import { Link, useParams } from 'react-router'
import { isRejectedRequest, POLL_INTERVAL_MS } from '../api/client'
import * as q from '../api/generated/@tanstack/react-query.gen'
import type {
  CreationJob,
  OperationView,
  PersonDefinition,
  SendInput,
  StoryDefinition,
  Turn,
} from '../api/generated/types.gen'
import {
  useFailure,
  useFormatMessage,
  useMessages,
  usePreferences,
} from '../app/preferences'
import { Button } from '../components/ui/button'
import { Dialog, Modal, Popover, Popup } from '../components/ui/popup'
import { useMediaQuery } from '../app/viewport'
import { StoryReply } from './conversation-message'
import { CreationStatus, useCreation } from './creation'
import {
  recheckAvailability,
  useAvailability,
  useAvailabilityRecovery,
} from './availability'
import { ConversationNotes, SceneActions } from './conversation-tools'
import { ImageResult, OriginalImage } from './images'
import {
  ActionDescription,
  PlayerActions,
  usePlayerActions,
  useVisualReview,
  VisualReviewForm,
} from './player-actions'
import {
  creationOptions,
  keepTerminal,
  operationOptions,
  operationPending,
  watchReceipt,
} from './operations'

function Drawing({ job }: { job: CreationJob }) {
  const messages = useMessages()
  const failure = useFailure()
  const current = useQuery({
    ...creationOptions(job.id),
    initialData: job,
    enabled: false,
    refetchInterval: false,
  })
  const value = current.data ?? job
  return value.image_id ? (
    <ImageResult identity={value.image_id} />
  ) : (
    <p className="subtle">
      {value.error ? failure({ code: value.error }) : messages.imagePreparing}
    </p>
  )
}

function AcceptedTurn({ operation }: { operation: OperationView }) {
  const messages = useMessages()
  const failure = useFailure()
  const accepted = operation.input
  if (!accepted) return null
  return (
    <article className="turn" data-operation-id={operation.id}>
      {accepted.text && (
        <div className="user-message">
          <span className="message-label">{messages.you}</span>
          <p>{accepted.text}</p>
        </div>
      )}
      {accepted.action && (
        <div className="user-action">
          <span className="message-label">{messages.myActions}</span>
          <ActionDescription action={accepted.action} />
        </div>
      )}
      {operation.error && (
        <p role="alert" className="error">
          {messages.replyFailedAfterAcceptance}{' '}
          {failure({ code: operation.error })}
        </p>
      )}
    </article>
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
  const [actionOpen, setActionOpen] = useState(false)
  const actionEditor = useRef<HTMLDivElement>(null)
  const messageInput = useRef<HTMLTextAreaElement>(null)
  const composerOptions = useRef<HTMLDivElement>(null)
  const { scrollRef, contentRef, scrollToBottom, isAtBottom } =
    useStickToBottom({ initial: 'instant', resize: 'instant' })
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
  const narrow = useMediaQuery('(max-width: 1279px)')
  const [showState, setShowState] = useState(false)
  const detail = useQuery({
    ...q.getConversationOptions({ path: { identity } }),
  })
  const actionControls = usePlayerActions({
    identity,
    conversation: detail.data,
  })
  useLayoutEffect(() => {
    if (actionControls.queued && composerOptions.current)
      composerOptions.current.scrollTop = 0
  }, [actionControls.queued])
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
  useAvailabilityRecovery(
    operation.error ?? (operation.data ?? savedOperations.data?.[0])?.error,
  )
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
    onSuccess: async (value) => {
      await cache.resetQueries({
        queryKey: q.getOperationQueryKey({ path: { identity: value.id } }),
        exact: true,
      })
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
  })
  useEffect(() => {
    if (drawing.mutation.data || drawing.job.data)
      void cache.invalidateQueries({
        queryKey: q.listConversationImagesQueryKey({ path: { identity } }),
      })
  }, [cache, identity, drawing.mutation.data, drawing.job.data])
  const reviewControls = useVisualReview({
    identity,
    conversation: detail.data,
  })
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
  const replyPending =
    send.isPending ||
    (send.isError && prepared !== null) ||
    ['accepted', 'running'].includes(operation.data?.state ?? '') ||
    savedOperations.data?.some((value) =>
      ['accepted', 'running'].includes(value.state),
    )
  const busy =
    actionControls.unresolved || !!reviewControls.prepared || replyPending
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
    !!text.trim()
  const latestOperation = operation.data ?? savedOperations.data?.[0]
  const retainedOperations = new Map(
    (savedOperations.data ?? []).map((value) => [value.id, value]),
  )
  if (operation.data)
    retainedOperations.set(
      operation.data.id,
      keepTerminal(retainedOperations.get(operation.data.id), operation.data),
    )
  const completedIds = new Set(
    (conversation.turns ?? []).map((turn) => turn.operation_id),
  )
  const acceptedOperations = [...retainedOperations.values()].filter(
    (value) =>
      value.input && value.state !== 'complete' && !completedIds.has(value.id),
  )
  const timeline: (
    { kind: 'turn'; value: Turn } | { kind: 'input'; value: OperationView }
  )[] = [
    ...(conversation.turns ?? []).map((value) => ({
      kind: 'turn' as const,
      value,
    })),
    ...acceptedOperations.map((value) => ({ kind: 'input' as const, value })),
  ]
  timeline.sort(
    (a, b) =>
      Date.parse(a.value.created_at ?? '') -
      Date.parse(b.value.created_at ?? ''),
  )
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
      {snapshots?.character?.image_id && (
        <OriginalImage
          className="character-portrait"
          identity={snapshots.character.image_id}
          alt={snapshots.character.name}
        />
      )}
      {!narrow && <h2>{messages.currentState}</h2>}
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
        <details>
          <summary>{messages.peopleDetails}</summary>
          <div className="details-content">
            {(['player', 'character'] as const).map((role) => (
              <div key={role}>
                <span>
                  {role === 'player' ? messages.you : messages.character}
                </span>
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
          </div>
        </details>
        {!!conversation.state?.elapsed_minutes && (
          <p>
            {formatMessage('elapsedTime', {
              minutes: conversation.state.elapsed_minutes,
            })}
          </p>
        )}
      </div>
      <details>
        <summary>{messages.actionHistory}</summary>
        <div className="details-content">
          {conversation.actions?.map((record) => (
            <ActionDescription
              key={record.request.operation_id}
              action={record.request.action}
            />
          ))}
        </div>
      </details>
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
        <div className="details-content">
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
        </div>
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
          <Popover.Root>
            <Popover.Trigger render={<Button variant="ghost" />}>
              {messages.conversationMenu}
            </Popover.Trigger>
            <Popup label={messages.conversationMenu} keepMounted>
              <ConversationNotes conversation={conversation} />
            </Popup>
          </Popover.Root>
          <Button
            aria-label={showState ? messages.closeState : messages.showState}
            onClick={() => setShowState(!showState)}
          >
            <PanelRight size={18} />
          </Button>
        </header>
        <div
          className="turns"
          role="log"
          aria-label={messages.conversationHistory}
          tabIndex={0}
          ref={scrollRef}
        >
          <div ref={contentRef}>
            {snapshots?.story?.image_id && (
              <div className="conversation-banner">
                <OriginalImage
                  identity={snapshots.story.image_id}
                  alt={snapshots.story.name}
                />
              </div>
            )}
            {conversation.state?.character_place !==
              conversation.state?.place && (
              <p className="presence-notice">
                {formatMessage('companionAway', {
                  place:
                    conversation.state?.character_place ||
                    messages.notSpecified,
                })}
              </p>
            )}
            {(drawings.data ?? [])
              .filter((job) => !job.turn_id)
              .map((job) => (
                <div className="turn" key={job.id}>
                  <Drawing job={job} />
                </div>
              ))}
            {timeline.length === 0 && (
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
            {timeline.map((entry) => {
              if (entry.kind === 'input')
                return (
                  <AcceptedTurn key={entry.value.id} operation={entry.value} />
                )
              const turn = entry.value
              return (
                <article className="turn" key={turn.operation_id}>
                  {turn.user && (
                    <div className="user-message">
                      <span className="message-label">{messages.you}</span>
                      <p>{turn.user}</p>
                    </div>
                  )}
                  {(conversation.proposals ?? [])
                    .filter(
                      (proposal) => proposal.turn_id === turn.operation_id,
                    )
                    .map((proposal) => (
                      <div className="action-preview" key={proposal.id}>
                        <ActionDescription action={proposal.action} />
                        {proposal.state === 'pending' ? (
                          <Button
                            onClick={() => {
                              setActionOpen(true)
                            }}
                          >
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
                    <div className="user-action">
                      <span className="message-label">
                        {messages.myActions}
                      </span>
                      <ActionDescription action={turn.action} />
                    </div>
                  )}
                  {!turn.action &&
                    turn.action_id &&
                    conversation.actions?.find(
                      (record) =>
                        record.request.operation_id === turn.action_id,
                    ) && (
                      <div className="user-action">
                        <span className="message-label">
                          {messages.myActions}
                        </span>
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
                    <StoryReply
                      narration={turn.narration}
                      action={turn.action_narration}
                      dialogue={turn.assistant}
                      name={
                        snapshots?.character?.name ??
                        conversation.definition.character?.name ??
                        messages.character
                      }
                    />
                    <SceneActions
                      conversation={conversation}
                      turnId={turn.operation_id}
                    />
                    {turn.image_id && <ImageResult identity={turn.image_id} />}
                    {(drawings.data ?? [])
                      .filter((job) => job.turn_id === turn.operation_id)
                      .map((job) => (
                        <Drawing key={job.id} job={job} />
                      ))}
                  </div>
                </article>
              )
            })}
            <div className="visual-review">
              {needsReview || reviewControls.prepared ? (
                <VisualReviewForm
                  conversation={conversation}
                  busy={!!busy}
                  controls={reviewControls}
                />
              ) : null}
            </div>
            {replyPending && (
              <p className="pending" role="status">
                {send.isPending
                  ? messages.requestSending
                  : prepared && send.isError
                    ? messages.receiptUnknown
                    : messages.pending}
              </p>
            )}
          </div>
        </div>
        <div className="composer-area">
          {!isAtBottom && (conversation.turn_count ?? 0) > 0 && (
            <Button
              className="latest-reply"
              onClick={() => void scrollToBottom('instant')}
            >
              {messages.latestReply}
            </Button>
          )}
          <div className="composer-options" ref={composerOptions}>
            {send.error && (
              <p role="alert" className="error">
                {failure(send.error)}
                {prepared && (
                  <Button
                    onClick={() => void operation.refetch()}
                    disabled={operation.isFetching}
                  >
                    {messages.retryRequest}
                  </Button>
                )}
              </p>
            )}
            {latestOperation?.error && !latestOperation.input && (
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
            {actionControls.queued && (
              <div className="queued-action">
                <ActionDescription action={actionControls.queued.action} />
                {actionControls.queued.revision !== conversation.revision && (
                  <div role="alert">
                    <p>{messages.reviewAction}</p>
                    <Button
                      disabled={!!busy}
                      onClick={actionControls.confirmRevision}
                    >
                      {messages.confirmCurrentAction}
                    </Button>
                  </div>
                )}
                <Button
                  disabled={!!busy}
                  aria-label={messages.editQueuedAction}
                  onClick={() => {
                    actionControls.edit()
                    setActionOpen(true)
                  }}
                >
                  {messages.edit}
                </Button>
                <Button
                  disabled={!!busy}
                  aria-label={messages.removeQueuedAction}
                  onClick={actionControls.clear}
                >
                  {messages.cancel}
                </Button>
              </div>
            )}

            {actionControls.queued && (
              <p className="subtle composer-hint">
                {messages.actionWithMessage}
              </p>
            )}
            <div className="composer-tools">
              <Popover.Root>
                <Popover.Trigger render={<Button />}>
                  {messages.drawingSettings}
                </Popover.Trigger>
                <Popup label={messages.drawingSettings}>
                  {actionControls.queued && (
                    <p className="subtle">{messages.unsentDrawing}</p>
                  )}
                  <label>
                    {messages.drawingSubjects}
                    <select
                      value={subjects}
                      onChange={(event) => {
                        setSubjects(event.target.value as typeof subjects)
                        if (event.target.value === 'pair')
                          setViewpoint('observer')
                      }}
                    >
                      <option value="character">{messages.partnerOnly}</option>
                      <option value="pair">{messages.bothPeople}</option>
                      <option value="environment">
                        {messages.environmentOnly}
                      </option>
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
                </Popup>
              </Popover.Root>
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
                <Feather size={18} aria-hidden="true" />
                <span>{messages.drawScene}</span>
              </Button>
              <Popover.Root open={actionOpen} onOpenChange={setActionOpen}>
                <Popover.Trigger render={<Button className="action-editor" />}>
                  {messages.addAction}
                </Popover.Trigger>
                <Popup
                  label={messages.addAction}
                  ref={actionEditor}
                  initialFocus={() =>
                    actionEditor.current?.querySelector('select') ?? true
                  }
                  finalFocus={messageInput}
                >
                  <PlayerActions
                    conversation={conversation}
                    busy={!!busy || needsReview}
                    controls={actionControls}
                    onQueued={() => {
                      setActionOpen(false)
                    }}
                  />
                </Popup>
              </Popover.Root>
              {!busy &&
                (conversation.turn_count ?? 0) > 0 &&
                !latestOperation?.error && (
                  <p className="saved-indicator">
                    {messages.conversationSaved}
                  </p>
                )}
            </div>
          </div>
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
                void scrollToBottom('instant')
                send.mutate({ path: { identity }, body })
              }
            }}
          >
            <textarea
              ref={messageInput}
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
              type="submit"
              className="send-button"
              variant="primary"
              aria-label={messages.send}
              disabled={!canSend}
            >
              <Send size={18} />
              <span>{messages.send}</span>
            </Button>
          </form>
        </div>
      </section>
      {narrow ? (
        <Dialog.Root open={showState} onOpenChange={setShowState}>
          <Modal
            label={messages.currentState}
            closeLabel={messages.closeState}
            className="state-dialog"
          >
            {statePanel}
          </Modal>
        </Dialog.Root>
      ) : (
        showState && statePanel
      )}
    </div>
  )
}
