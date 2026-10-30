import { useState } from 'react'
import { useParams } from 'react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowUp, PanelRight, Sparkles } from 'lucide-react'
import {
  applyCommand,
  getConversation,
  getConversationOperations,
  getOperation,
  sendTurn,
} from '../api/generated/sdk.gen'
import type { SendInput } from '../api/generated/types.gen'
import { POLL_INTERVAL_MS } from '../api/client'
import { Button } from '../components/ui/button'
import {
  useFailure,
  useFormatMessage,
  useMessages,
  usePreferences,
} from '../app/preferences'
import { ImageResult } from './images'

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
  const [includeImage, setIncludeImage] = useState(false)
  const [text, setText] = useState('')
  const [allowReal, setAllowReal] = useState(false)
  const [operationId, setOperationId] = useState<string | null>(null)
  const [prepared, setPrepared] = useState<SendInput | null>(null)
  const [stateDraft, setStateDraft] = useState<{
    value: string
    revision: number
  } | null>(null)
  const [showState, setShowState] = useState(
    () => window.matchMedia?.('(min-width: 851px)').matches ?? true,
  )
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
      setOperationId(result.id)
      setText((current) => (current === submitted.text ? '' : current))
      setAllowReal(false)
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
      let state: Record<string, string>
      try {
        const parsed: unknown = JSON.parse(stateDraft?.value ?? '{}')
        if (
          !parsed ||
          typeof parsed !== 'object' ||
          Array.isArray(parsed) ||
          Object.values(parsed).some((value) => typeof value !== 'string')
        )
          throw new Error()
        state = parsed as Record<string, string>
      } catch {
        throw new Error(messages.invalidState)
      }
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
  const fake = conversation.definition.llm_profile === 'fake'
  const canSend =
    !busy &&
    !(send.isError && prepared !== null) &&
    !!text.trim() &&
    (fake || allowReal)
  const latestOperation = operation.data ?? savedOperations.data?.[0]
  const snapshots = conversation.definition.presets
  const stateLabels = {
    place: messages.currentPlace,
    time: messages.currentTime,
    goal: messages.goal,
    relationship: messages.relationship,
  }
  const visibleState = Object.entries(conversation.state ?? {}).filter(
    ([key]) => key in stateLabels,
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
  return (
    <div className={`conversation-layout ${showState ? '' : 'state-hidden'}`}>
      <section className="conversation-main">
        <header className="conversation-header">
          <div>
            <span className="eyebrow">{messages.conversationLabel}</span>
            <h2>{conversation.definition.title}</h2>
          </div>
          <Button
            aria-label={messages.showState}
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
                <p>{turn.assistant}</p>
                {turn.image_id && (
                  <ImageResult
                    identity={turn.image_id}
                    operationId={turn.operation_id}
                  />
                )}
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
          {fake ? (
            <p className="sample-hint">
              {messages.sampleNotice}{' '}
              <Button onClick={() => setText(messages.sampleInput)}>
                {messages.sampleAction}
              </Button>
            </p>
          ) : (
            <label className="checkbox permission">
              <input
                type="checkbox"
                checked={allowReal}
                disabled={!!busy}
                onChange={(event) => setAllowReal(event.target.checked)}
              />
              {messages.allowReal}
            </label>
          )}
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
          {preferences.value.image_frequency === 'manual' && !fake && (
            <label className="checkbox">
              <input
                type="checkbox"
                checked={includeImage}
                onChange={(event) => setIncludeImage(event.target.checked)}
              />
              {messages.includeNextImage}
            </label>
          )}
          <form
            className="composer"
            onSubmit={(event) => {
              event.preventDefault()
              if (canSend) {
                const body = {
                  operation_id: crypto.randomUUID(),
                  text,
                  allow_real_llm: allowReal,
                  locale: preferences.value.locale,
                  response_length: preferences.value.response_length,
                  images:
                    preferences.value.image_frequency === 'automatic' ||
                    (preferences.value.image_frequency === 'manual' &&
                      includeImage),
                }
                setPrepared(body)
                setOperationId(body.operation_id)
                setAllowReal(false)
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
          <p className="composer-footer">{messages.local}</p>
        </div>
      </section>
      <aside className="state-panel">
        {snapshots?.character?.image_id && (
          <img
            className="character-portrait"
            src={`/api/v1/images/${snapshots.character.image_id}/files/0`}
            alt={snapshots.character.name}
          />
        )}
        <div className="eyebrow">{messages.context}</div>
        <h2>{messages.currentState}</h2>
        <div className="state-items">
          {visibleState.map(([key, value]) => (
            <div key={key}>
              <span>{stateLabels[key as keyof typeof stateLabels]}</span>
              <p>{value}</p>
            </div>
          ))}
          {!visibleState.length && <p className="subtle">{messages.noState}</p>}
        </div>
        <h3>{messages.scenario}</h3>
        <p className="state-description">
          {content(conversation.definition.scenario) ||
            messages.openConversation}
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
                  value: JSON.stringify(conversation.state, null, 2),
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
              <label>
                {messages.stateValues}
                <textarea
                  rows={6}
                  value={stateDraft.value}
                  onChange={(event) =>
                    setStateDraft({ ...stateDraft, value: event.target.value })
                  }
                />
              </label>
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
    </div>
  )
}
