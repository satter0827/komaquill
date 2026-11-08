import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { Send, PanelRight, Sparkles } from 'lucide-react'
import { DrawIcon } from '../../shared/ui/atelier-icons'
import {
  useCallback,
  useId,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from 'react'
import { useStickToBottom } from 'use-stick-to-bottom'
import { Link, useNavigate, useParams } from 'react-router'
import { isMissing, isRejectedRequest } from '../../api/client'
import * as q from '../../api/generated/@tanstack/react-query.gen'
import type {
  CreationJob,
  OperationView,
  PersonDefinition,
  SendInput,
  StoryDefinition,
  Turn,
} from '../../api/generated/types.gen'
import {
  useFailure,
  useMessages,
  usePreferences,
} from '../../shared/preferences'
import { WorkActions } from '../../shared/ui/work-actions'
import { Button } from '../../shared/ui/button'
import { QueryNotice } from '../../shared/ui/query-notice'
import { Dialog, Modal, Popover, Popup } from '../../shared/ui/popup'
import { useMediaQuery } from '../../shared/viewport'
import { StoryReply } from '../../shared/story/reply'
import { CreationStatus, useCreation } from '../../shared/creation'
import {
  recheckAvailability,
  useAvailability,
  useAvailabilityRecovery,
} from '../../shared/availability'
import { StartedSettings, CurrentSituation } from './situation'
import { ConversationNotes, SceneActions } from './tools'
import { ImageResult } from '../../shared/images/result'
import { OriginalImage } from '../../shared/images/original'
import { ActionDescription, PlayerActions, usePlayerActions } from './actions'
import {
  creationOptions,
  conversationOperationsOptions,
  retainOperation,
  operationOptions,
  watchReceipt,
} from '../../shared/operations'

const MAX_DESTINATION_LENGTH = 1000

type ResumeReceipt = { operationId: string; expectedCount: number }

function MeetingDetails({
  meeting,
}: {
  meeting: NonNullable<Turn['meeting']>
}) {
  const m = useMessages()
  return (
    <section aria-label={m.meetingDetails}>
      <p className="message-label">{m.meetingDetails}</p>
      {meeting.status === 'confirmed' ? (
        <dl>
          <dt>{m.meetingDate}</dt>
          <dd>{meeting.date ?? m.meetingUndecided}</dd>
          <dt>{m.meetingTime}</dt>
          <dd>{meeting.time ?? m.meetingUndecided}</dd>
          <dt>{m.meetingPlace}</dt>
          <dd>{meeting.place ?? m.meetingUndecided}</dd>
        </dl>
      ) : (
        <p>
          {meeting.status === 'cancelled'
            ? m.meetingCancelled
            : m.meetingUnknown}
        </p>
      )}
    </section>
  )
}

function Drawing({
  job,
  excluded,
}: {
  job: CreationJob
  excluded: (slot: string, image: string) => number[]
}) {
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
    <ImageResult
      identity={value.image_id}
      excluded={excluded('job:' + job.id, value.image_id)}
    />
  ) : (
    <p className="subtle">
      {value.error ? failure({ code: value.error }) : messages.imagePreparing}
    </p>
  )
}

function ReplyStatus({ text }: { text: string }) {
  return (
    <p className="pending" role="status">
      {text}
    </p>
  )
}

function AcceptedTurn({
  operation,
  status,
}: {
  operation: OperationView
  status?: string
}) {
  const messages = useMessages()
  const failure = useFailure()
  const accepted = operation.input
  if (!accepted) return null
  return (
    <article className="turn" data-operation-id={operation.id}>
      {accepted.text?.trim() && (
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
      {status && <ReplyStatus text={status} />}
      {operation.error && !status && (
        <p role="alert" className="error">
          {messages.replyFailedAfterAcceptance}{' '}
          {failure({ code: operation.error })}
        </p>
      )}
    </article>
  )
}

export function ConversationPage({
  screenHidden = false,
}: {
  screenHidden?: boolean
}) {
  const { identity = '' } = useParams()
  return (
    <ConversationView
      key={identity}
      identity={identity}
      screenHidden={screenHidden}
    />
  )
}

function ConversationView({
  identity,
  screenHidden,
}: {
  identity: string
  screenHidden: boolean
}) {
  const cache = useQueryClient()
  const navigate = useNavigate()
  const messages = useMessages()
  const failure = useFailure()
  const preferences = usePreferences()
  const [text, setText] = useState('')
  const [actionOpen, setActionOpen] = useState(false)
  const [moveOpen, setMoveOpen] = useState(false)
  const [destination, setDestination] = useState('')
  const moveRevision = useRef<number | null>(null)
  const destinationInput = useRef<HTMLInputElement>(null)
  const sendFormId = useId()
  const actionEditor = useRef<HTMLDivElement>(null)
  const messageInput = useRef<HTMLTextAreaElement>(null)
  const stateTrigger = useRef<HTMLButtonElement>(null)
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
  const [pendingResume, setPendingResume] = useState<ResumeReceipt | null>(null)
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
    if (actionControls.queued && moveOpen)
      destinationInput.current?.scrollIntoView({ block: 'nearest' })
    else if (actionControls.queued && composerOptions.current)
      composerOptions.current.scrollTop = 0
  }, [actionControls.queued, moveOpen])
  useLayoutEffect(() => {
    if (moveOpen) destinationInput.current?.focus()
  }, [moveOpen])
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
  const detached = useQuery(
    q.getDetachedImagesOptions({
      path: { kind: 'conversation', identity },
    }),
  )
  const excluded = (slot: string, image: string) =>
    detached.data?.[slot + ':' + image] ?? []
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
    ...conversationOperationsOptions(identity),
  })
  const operation = useQuery({
    ...operationOptions(
      operationId ?? '',
      pendingResume?.operationId === operationId
        ? pendingResume.expectedCount
        : 0,
    ),
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
        (previous) => retainOperation(previous, result),
      )
      setOperationId(result.id)
      if (
        submittedDraft.current?.operationId === submitted.operation_id &&
        submittedDraft.current.version === draftVersion.current
      )
        setText('')
      setPrepared(null)
      clearQueuedAction()
      moveRevision.current = null
      setDestination('')
      setMoveOpen(false)
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
  const clearResumeReceipt = useCallback(
    (receipt: ResumeReceipt | undefined) => {
      if (!receipt) return
      // Unreceived retries share the expected server count; keep request identity.
      setPendingResume((current) => (current === receipt ? null : current))
    },
    [],
  )
  const checkResumeReceipt = useCallback(
    async (receipt: ResumeReceipt | undefined) => {
      if (!receipt) return
      try {
        await cache.cancelQueries(
          {
            queryKey: q.getOperationQueryKey({
              path: { identity: receipt.operationId },
            }),
            exact: true,
          },
          { revert: false },
        )
        const observed = await cache.fetchQuery({
          ...operationOptions(receipt.operationId),
          staleTime: 0,
        })
        if (observed.state === 'interrupted' && observed.can_resume)
          clearResumeReceipt(receipt)
      } catch {
        // The query retains the failure; keep read-only receipt checking available.
      }
    },
    [cache, clearResumeReceipt],
  )
  const resume = useMutation({
    ...q.resumeOperationMutation(),
    onMutate: ({ path }) => {
      const previous = cache.getQueryData<OperationView>(
        q.getOperationQueryKey({ path }),
      )
      const listed = savedOperations.data?.find(
        (value) => value.id === path.identity,
      )
      const receipt = {
        operationId: path.identity,
        expectedCount:
          Math.max(previous?.resume_count ?? 0, listed?.resume_count ?? 0) + 1,
      }
      setPendingResume(receipt)
      setOperationId(path.identity)
      return receipt
    },
    onError: async (error, { path }, receipt) => {
      if (isRejectedRequest(error)) {
        clearResumeReceipt(receipt)
        void cache.invalidateQueries({
          queryKey: q.getOperationQueryKey({ path }),
        })
      } else await checkResumeReceipt(receipt)
    },
    onSuccess: (value, _submitted, receipt) => {
      cache.setQueryData<OperationView>(
        q.getOperationQueryKey({ path: { identity: value.id } }),
        (previous) => retainOperation(previous, value),
      )
      clearResumeReceipt(receipt)
      void cache.invalidateQueries({
        queryKey: q.getConversationOperationsQueryKey({ path: { identity } }),
      })
      void cache.invalidateQueries({
        queryKey: q.getConversationQueryKey({ path: { identity } }),
      })
    },
  })
  useEffect(() => {
    if (!resume.context) return
    const receipt = resume.context
    return watchReceipt<OperationView>(
      cache,
      q.getOperationQueryKey({ path: { identity: receipt.operationId } }),
      receipt.operationId,
      () => {
        clearResumeReceipt(receipt)
      },
      (value) =>
        value.state === 'complete' ||
        (value.resume_count ?? 0) >= receipt.expectedCount,
    )
  }, [cache, resume.context, clearResumeReceipt])
  const resumeReceiptKnown =
    !!resume.context &&
    operation.data?.id === resume.context.operationId &&
    (operation.data.state === 'complete' ||
      (operation.data.resume_count ?? 0) >= resume.context.expectedCount)
  const resumeError =
    operationId === resume.context?.operationId && !resumeReceiptKnown
      ? resume.error
      : null
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
  if (detail.error && (!detail.data || isMissing(detail.error)))
    return (
      <QueryNotice
        error={detail.error}
        pending={detail.isFetching}
        onRetry={() => void detail.refetch()}
      />
    )
  if (!detail.data) return <p className="loading">{messages.loading}</p>
  const conversation = {
    ...detail.data,
    turns: history.data
      ? [...history.data.pages].reverse().flatMap((page) => page.items)
      : (detail.data.turns ?? []),
  }
  const retainedOperations = new Map(
    (savedOperations.data ?? []).map((value) => [value.id, value]),
  )
  if (operation.data)
    retainedOperations.set(
      operation.data.id,
      retainOperation(
        retainedOperations.get(operation.data.id),
        operation.data,
      ),
    )
  const completedIds = new Set(
    (conversation.turns ?? []).map((turn) => turn.operation_id),
  )
  const currentReply = [...retainedOperations.values()].find(
    (value) =>
      ['accepted', 'running'].includes(value.state) ||
      (value.result_revision ?? 0) > (conversation.revision ?? 0) ||
      (value.state === 'complete' &&
        value.id === operationId &&
        value.result_revision != null &&
        history.isFetching &&
        !completedIds.has(value.id)),
  )
  const replyStatus =
    send.isPending || (resume.isPending && pendingResume)
      ? messages.requestSending
      : (prepared && send.isError) || (pendingResume && resumeError)
        ? messages.receiptUnknown
        : currentReply?.state === 'complete'
          ? messages.replyLoading
          : currentReply?.state === 'accepted' ||
              currentReply?.state === 'running'
            ? messages.pending
            : undefined
  const statusTurn =
    currentReply?.input && !completedIds.has(currentReply.id)
      ? currentReply.id
      : undefined
  const replyPending = !!replyStatus || !!currentReply || pendingResume !== null
  const busy = actionControls.unresolved || replyPending || !!detail.error
  const canSend =
    !detail.error &&
    sendAvailability.ready &&
    (!actionControls.queued ||
      actionControls.queued.revision === conversation.revision) &&
    !busy &&
    (!!text.trim() || !!actionControls.queued)
  const latestOperation = operation.data ?? savedOperations.data?.[0]
  const acceptedOperations = [...retainedOperations.values()].filter(
    (value) =>
      value.input &&
      (value.state !== 'complete' || value.id === statusTurn) &&
      !completedIds.has(value.id),
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
    <CurrentSituation conversation={conversation} showHeading={!narrow} />
  )
  const recordedActions = new Map(
    conversation.actions?.map(({ request }) => [
      request.operation_id,
      request.action,
    ]),
  )
  return (
    <div className={`conversation-layout ${showState ? '' : 'state-hidden'}`}>
      <section className="conversation-main">
        <header className="conversation-header">
          <div>
            <span className="eyebrow">{messages.conversationLabel}</span>
            <h2>
              {conversation.display_name || conversation.definition.title}
            </h2>
          </div>
          <Popover.Root>
            <Popover.Trigger
              render={<Button variant="ghost" disabled={!!detail.error} />}
            >
              {messages.conversationMenu}
            </Popover.Trigger>
            <Popup label={messages.conversationMenu} keepMounted>
              <ConversationNotes
                conversation={conversation}
                canMutate={!detail.error}
              />
              <WorkActions
                target={{ kind: 'conversation', id: identity }}
                screenHidden={screenHidden}
                afterTrash={() => navigate('/')}
              />
              <StartedSettings
                definition={conversation.definition}
                showPortrait={
                  !snapshots?.character?.image_id ||
                  !excluded('character', snapshots.character.image_id).includes(
                    0,
                  )
                }
                screenHidden={screenHidden}
              />
            </Popup>
          </Popover.Root>
          <Button
            ref={stateTrigger}
            aria-label={showState ? messages.closeState : messages.showState}
            onClick={() => setShowState(!showState)}
          >
            <PanelRight size={18} />
          </Button>
        </header>
        <QueryNotice
          error={detail.error}
          retained
          pending={detail.isFetching}
          onRetry={() => void detail.refetch()}
        />
        <div
          className="turns"
          role="log"
          aria-label={messages.conversationHistory}
          tabIndex={0}
          ref={scrollRef}
        >
          <div ref={contentRef}>
            {snapshots?.story?.image_id &&
              !excluded('story', snapshots.story.image_id).includes(0) && (
                <div className="conversation-banner">
                  <OriginalImage
                    identity={snapshots.story.image_id}
                    alt={snapshots.story.name}
                  />
                </div>
              )}
            {(drawings.data ?? [])
              .filter((job) => !job.turn_id)
              .map((job) => (
                <div className="turn" key={job.id}>
                  <Drawing job={job} excluded={excluded} />
                </div>
              ))}
            {timeline.length === 0 && !conversation.turn_count && (
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
            {!!conversation.turn_count && history.isPending && (
              <p role="status">{messages.loading}</p>
            )}
            <QueryNotice
              error={history.error}
              retained={!!history.data}
              pending={history.isFetching}
              onRetry={() => void history.refetch()}
            />
            {timeline.map((entry) => {
              if (entry.kind === 'input')
                return (
                  <AcceptedTurn
                    key={entry.value.id}
                    operation={entry.value}
                    status={
                      entry.value.id === statusTurn ? replyStatus : undefined
                    }
                  />
                )
              const turn = entry.value
              const action =
                turn.action ??
                (turn.action_id
                  ? recordedActions.get(turn.action_id)
                  : undefined)
              return (
                <article className="turn" key={turn.operation_id}>
                  {turn.user?.trim() && (
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
                  {action && (
                    <div className="user-action">
                      <span className="message-label">
                        {messages.myActions}
                      </span>
                      <ActionDescription action={action} />
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
                    {turn.meeting && <MeetingDetails meeting={turn.meeting} />}
                    <SceneActions
                      conversation={conversation}
                      canMutate={!detail.error}
                      turnId={turn.operation_id}
                    />
                    {turn.image_id && (
                      <ImageResult
                        identity={turn.image_id}
                        excluded={excluded(
                          'turn:' + turn.operation_id,
                          turn.image_id,
                        )}
                      />
                    )}
                    {(drawings.data ?? [])
                      .filter((job) => job.turn_id === turn.operation_id)
                      .map((job) => (
                        <Drawing key={job.id} job={job} excluded={excluded} />
                      ))}
                  </div>
                </article>
              )
            })}
            {replyStatus && !statusTurn && (
              <div className="turn">
                <ReplyStatus text={replyStatus} />
              </div>
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
                disabled={!!busy}
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
            {resumeError && (
              <p role="alert" className="error">
                {failure(resumeError)}
                <Button
                  onClick={() => void checkResumeReceipt(resume.context)}
                  disabled={operation.isFetching}
                >
                  {messages.retryRequest}
                </Button>
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
                      onClick={() => {
                        actionControls.confirmRevision()
                        if (actionControls.queued?.action.kind === 'move')
                          moveRevision.current = conversation.revision ?? 0
                      }}
                    >
                      {messages.confirmCurrentAction}
                    </Button>
                  </div>
                )}
                <Button
                  disabled={!!busy}
                  aria-label={messages.editQueuedAction}
                  onClick={() => {
                    if (actionControls.queued?.action.kind === 'move') {
                      moveRevision.current = actionControls.queued.revision
                      setDestination(actionControls.queued.action.destination)
                      setMoveOpen(true)
                    } else {
                      actionControls.edit()
                      setActionOpen(true)
                    }
                  }}
                >
                  {messages.edit}
                </Button>
                <Button
                  disabled={!!busy}
                  aria-label={messages.removeQueuedAction}
                  onClick={() => {
                    actionControls.clear()
                    moveRevision.current = null
                    setDestination('')
                    setMoveOpen(false)
                  }}
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
              <Button
                aria-expanded={moveOpen}
                disabled={!!busy}
                onClick={() => setMoveOpen(!moveOpen)}
              >
                {messages.changePlace}
              </Button>
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
                  drawing.busy ||
                  sampleDrawing ||
                  !drawAvailability.ready ||
                  !!detail.error
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
                <DrawIcon />
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
                    busy={!!busy}
                    controls={actionControls}
                    onQueued={() => {
                      setActionOpen(false)
                      setMoveOpen(false)
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
            {moveOpen && (
              <label>
                {messages.destination}
                <input
                  ref={destinationInput}
                  form={sendFormId}
                  value={destination}
                  maxLength={MAX_DESTINATION_LENGTH}
                  disabled={!!busy}
                  onChange={(event) => {
                    const value = event.target.value
                    setDestination(value)
                    moveRevision.current ??= conversation.revision ?? 0
                    if (value.trim())
                      actionControls.queue(
                        { kind: 'move', destination: value },
                        undefined,
                        moveRevision.current,
                      )
                    else actionControls.setQueued(null)
                  }}
                />
              </label>
            )}
          </div>
          <form
            id={sendFormId}
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
        <Dialog.Root
          open={showState && !screenHidden}
          onOpenChange={setShowState}
        >
          <Modal
            label={messages.currentState}
            closeLabel={messages.closeState}
            className="state-dialog"
            finalFocus={stateTrigger}
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
