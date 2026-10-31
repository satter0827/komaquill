import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { useParams } from 'react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowUp, PanelRight, Sparkles } from 'lucide-react'
import {
  applyCommand,
  getConversation,
  getConversationOperations,
  getOperation,
  sendTurn,
  listConversationImages,
} from '../api/generated/sdk.gen'
import type {
  CreationJob,
  OperationView,
  SendInput,
} from '../api/generated/types.gen'
import { POLL_INTERVAL_MS } from '../api/client'
import { Button } from '../components/ui/button'
import {
  useFailure,
  useFormatMessage,
  useMessages,
  usePreferences,
} from '../app/preferences'
import { ImageResult } from './images'
import { useCreation, CreationStatus } from './creation'

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
  const [operationId, setOperationId] = useState<string | null>(null)
  const [prepared, setPrepared] = useState<SendInput | null>(null)
  const [stateDraft, setStateDraft] = useState<{
    value: Record<string, string>
    revision: number
  } | null>(null)
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
    queryKey: ['conversation', identity],
    queryFn: async () => (await getConversation({ path: { identity } })).data!,
    refetchInterval: POLL_INTERVAL_MS,
  })
  const savedOperations = useQuery({
    queryKey: ['conversation-operations', identity],
    queryFn: async () =>
      (await getConversationOperations({ path: { identity } })).data!,
    refetchInterval: POLL_INTERVAL_MS,
  })
  const operation = useQuery({
    queryKey: ['operation', operationId],
    enabled: !!operationId,
    queryFn: async () =>
      (await getOperation({ path: { identity: operationId! } })).data!,
    refetchInterval: (query) =>
      ['complete', 'failed', 'interrupted'].includes(
        query.state.data?.state ?? '',
      )
        ? false
        : POLL_INTERVAL_MS,
  })
  const send = useMutation({
    mutationFn: async (body: SendInput) =>
      (await sendTurn({ path: { identity }, body })).data!,
    onSuccess: (result, submitted) => {
      cache.setQueryData<OperationView>(['operation', result.id], (previous) =>
        previous &&
        ['complete', 'failed', 'interrupted'].includes(previous.state)
          ? previous
          : result,
      )
      setOperationId(result.id)
      setText((current) => (current === submitted.text ? '' : current))
      setPrepared(null)
      void cache.invalidateQueries({ queryKey: ['conversation', identity] })
    },
  })
  const reset = useMutation({
    mutationFn: async () =>
      (
        await applyCommand({
          path: { identity },
          body: {
            command: 'reset_state',
            expected_revision: detail.data!.revision ?? 0,
          },
        })
      ).data!,
    onSuccess: () =>
      cache.invalidateQueries({ queryKey: ['conversation', identity] }),
  })
  const updateState = useMutation({
    mutationFn: async () => {
      const state = stateDraft!.value
      return (
        await applyCommand({
          path: { identity },
          body: {
            command: 'set_state',
            expected_revision: stateDraft!.revision,
            state,
          },
        })
      ).data!
    },
    onSuccess: async () => {
      setStateDraft(null)
      await cache.invalidateQueries({ queryKey: ['conversation', identity] })
    },
  })
  const drawing = useCreation()
  const drawings = useQuery({
    queryKey: ['conversation-images', identity],
    queryFn: async () =>
      (await listConversationImages({ path: { identity } })).data!,
    refetchInterval: POLL_INTERVAL_MS,
  })
  const dialog = useRef<HTMLDialogElement>(null)
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
  const conversation = detail.data
  const busy =
    send.isPending ||
    ['accepted', 'running'].includes(operation.data?.state ?? '') ||
    savedOperations.data?.some((value) =>
      ['accepted', 'running'].includes(value.state),
    )
  const canSend = !busy && !(send.isError && prepared !== null) && !!text.trim()
  const latestOperation = operation.data ?? savedOperations.data?.[0]
  const snapshots = conversation.definition.presets
  const stateLabels = {
    place: messages.currentPlace,
    time: messages.currentTime,
    goal: messages.goal,
    relationship: messages.relationship,
  }
  const displayedState = {
    ...conversation.state,
    place:
      conversation.state?.place || conversation.state?.character_location || '',
  }
  const visibleState = Object.entries(displayedState).filter(
    ([key, value]) => key in stateLabels && value,
  )
  const content = (raw: string | undefined) => {
    try {
      const value = JSON.parse(raw ?? '{}') as Record<string, string>
      return (
        value.opening ||
        value.description ||
        value.world ||
        value.appearance ||
        value.role ||
        value.name ||
        raw ||
        ''
      )
    } catch {
      return raw ?? ''
    }
  }
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
      <div className="eyebrow">{messages.context}</div>
      <h2>{messages.currentState}</h2>
      {conversation.turns?.at(-1)?.narration && (
        <p className="state-description">
          {conversation.turns.at(-1)!.narration}
        </p>
      )}
      <h3>{messages.scenario}</h3>
      <p className="state-description">
        {content(conversation.definition.scenario) || messages.openConversation}
      </p>
      {conversation.definition.character && (
        <>
          <h3>{messages.character}</h3>
          <p className="state-description">
            {content(conversation.definition.character)}
          </p>
        </>
      )}
      <details className="story-details">
        <summary>{messages.details}</summary>
        <div className="state-items">
          {visibleState.map(([key, value]) => (
            <div key={key}>
              <span>{stateLabels[key as keyof typeof stateLabels]}</span>
              <p>{value}</p>
            </div>
          ))}
          {!visibleState.length && !conversation.turns?.at(-1)?.narration && (
            <p className="subtle">{messages.noState}</p>
          )}
        </div>

        <span className="revision">
          {formatMessage('revision', {
            revision: conversation.revision ?? 0,
          })}
        </span>
        {stateDraft === null ? (
          <Button
            disabled={busy}
            onClick={() =>
              setStateDraft({
                value: { ...conversation.state },
                revision: conversation.revision ?? 0,
              })
            }
          >
            {messages.editState}
          </Button>
        ) : (
          <form
            onSubmit={(event) => {
              event.preventDefault()
              updateState.mutate()
            }}
          >
            {Object.entries(stateLabels).map(([key, label]) => (
              <label key={key}>
                {label}
                <input
                  value={stateDraft.value[key] ?? ''}
                  onChange={(event) =>
                    setStateDraft({
                      ...stateDraft,
                      value: { ...stateDraft.value, [key]: event.target.value },
                    })
                  }
                />
              </label>
            ))}
            <Button type="submit" disabled={busy || updateState.isPending}>
              {messages.saveState}
            </Button>
            <Button onClick={() => setStateDraft(null)}>
              {messages.cancel}
            </Button>
          </form>
        )}
        {updateState.error && (
          <p role="alert" className="error">
            {failure(updateState.error)}
          </p>
        )}
        <Button
          onClick={() => reset.mutate()}
          disabled={busy || reset.isPending}
        >
          {messages.resetState}
        </Button>
        {reset.error && (
          <p role="alert" className="error">
            {failure(reset.error)}
          </p>
        )}
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
            <img
              src={`/api/v1/images/${snapshots.story.image_id}/files/0`}
              alt={snapshots.story.name}
            />
          </div>
        )}
        <div className="turns" aria-live="polite">
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
          {(conversation.turns ?? []).map((turn) => (
            <article className="turn" key={turn.operation_id}>
              <div className="user-message">
                <span className="message-label">{messages.you}</span>
                <p>{turn.user}</p>
              </div>
              <div className="assistant-message">
                <span className="message-label">
                  <Sparkles size={14} />
                  {snapshots?.character?.name ?? messages.character}
                </span>
                <p className="scene-narration">{turn.narration}</p>
                <p>{turn.assistant}</p>
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
                    if (prepared) send.mutate(prepared)
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
              {failure({ code: latestOperation.error })}
            </p>
          )}
          <div className="scene-actions">
            <Button
              onClick={() =>
                drawing.start({
                  kind: 'conversation_image',
                  target_id: identity,
                  expected_revision: conversation.revision,
                  locale: preferences.value.locale,
                  llm_profile: conversation.definition.llm_profile,
                  allow_real_llm: true,
                })
              }
              disabled={!!busy || drawing.busy}
            >
              {messages.drawScene}
            </Button>
            <CreationStatus creation={drawing} />
          </div>
          <form
            className="composer"
            onSubmit={(event) => {
              event.preventDefault()
              if (canSend) {
                const body = {
                  operation_id: crypto.randomUUID(),
                  text,
                  allow_real_llm: true,
                  locale: preferences.value.locale,
                }
                setPrepared(body)
                send.mutate(body)
              }
            }}
          >
            <textarea
              aria-label={messages.messageLabel}
              placeholder={messages.placeholder}
              rows={2}
              value={text}
              onChange={(event) => setText(event.target.value)}
            />
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
