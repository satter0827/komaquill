import { parseObjects } from './physical-objects'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import * as q from '../api/generated/@tanstack/react-query.gen'
import type {
  ActionRequest,
  Conversation,
  ProposalDismissal,
  VisualReview,
  VisualPerson,
} from '../api/generated/types.gen'
import {
  useFailure,
  useFormatMessage,
  useMessages,
  usePreferences,
} from '../app/preferences'
import { Button } from '../components/ui/button'
type PlayerAction = ActionRequest['action']

export function ActionDescription({ action }: { action: PlayerAction }) {
  const m = useMessages()
  const format = useFormatMessage()
  if (action.kind === 'move')
    return (
      <p>
        {format('actionMoveDescription', { destination: action.destination })}
      </p>
    )
  if (action.kind === 'wait')
    return <p>{format('actionWaitDescription', { minutes: action.minutes })}</p>
  if (action.kind === 'intention') return <p>{action.text || m.noIntention}</p>
  const labels = {
    clothing: m.clothing,
    pose: m.pose,
    expression: m.expression,
    held_objects: m.heldObjects,
  }
  return (
    <dl>
      {(Object.keys(labels) as Array<keyof typeof labels>)
        .filter((field) => action[field] != null)
        .map((field) => (
          <div key={field}>
            <dt>{labels[field]}</dt>
            <dd>
              {(Array.isArray(action[field])
                ? action[field].map((item) => item.name).join(', ')
                : action[field]) || m.emptyValue}
            </dd>
          </div>
        ))}
    </dl>
  )
}

export function usePlayerActions({
  identity,
  conversation,
  onReaction,
}: {
  identity: string
  conversation: Conversation | undefined
  onReaction: (identity: string) => void
}) {
  const preferences = usePreferences()
  const cache = useQueryClient()
  const [kind, setKind] = useState<PlayerAction['kind']>('move')
  const [value, setValue] = useState('')
  const [prepareField, setPrepareField] = useState<
    'clothing' | 'pose' | 'expression' | 'held_objects'
  >('clothing')
  const [prepared, setPrepared] = useState<ActionRequest | null>(null)
  const [dismissal, setDismissal] = useState<{
    proposal_id: string
    body: ProposalDismissal
  } | null>(null)
  const draftRevisionRef = useRef<number | null>(null)
  const refresh = () =>
    cache.invalidateQueries({
      queryKey: q.getConversationQueryKey({ path: { identity } }),
    })
  const action = useMutation({
    ...q.performActionMutation(),
    onError: (error) => {
      const code =
        error && typeof error === 'object' && 'code' in error
          ? error.code
          : null
      if (
        [
          'invalid_request',
          'operation_conflict',
          'operation_busy',
          'visual_review_required',
        ].includes(String(code))
      ) {
        setPrepared(null)
        void refresh()
      }
    },
    onSuccess: (record) => {
      setPrepared(null)
      draftRevisionRef.current = null
      setValue('')
      onReaction(record.reaction_id)
      void refresh()
      void cache.invalidateQueries({
        queryKey: q.getConversationOperationsQueryKey({ path: { identity } }),
      })
    },
  })
  const dismiss = useMutation({
    ...q.dismissProposalMutation(),
    onSuccess: () => {
      setDismissal(null)
      void refresh()
    },
    onError: (error) => {
      if (
        error &&
        typeof error === 'object' &&
        'code' in error &&
        ['operation_conflict', 'operation_busy', 'invalid_request'].includes(
          String(error.code),
        )
      )
        setDismissal(null)
    },
  })
  const submit = (selected: PlayerAction, proposal_id?: string) => {
    const body = prepared ?? {
      operation_id: crypto.randomUUID(),
      expected_revision: proposal_id
        ? (conversation?.revision ?? 0)
        : (draftRevisionRef.current ?? conversation?.revision ?? 0),
      action: selected,
      proposal_id,
      locale: preferences.value.locale,
      allow_real_llm: true,
    }
    setPrepared(body)
    action.mutate({ path: { identity }, body })
  }
  return {
    kind,
    setKind,
    value,
    setValue,
    prepareField,
    setPrepareField,
    prepared,
    setPrepared,
    draftRevisionRef,
    action,
    dismiss,
    dismissal,
    setDismissal,
    submit,
    refresh,
    unresolved: !!prepared || !!dismissal,
    cache,
    onReaction,
    identity,
  }
}

export function PlayerActions({
  conversation,
  busy,
  controls,
}: {
  conversation: Conversation
  busy: boolean
  controls: ReturnType<typeof usePlayerActions>
}) {
  const m = useMessages()
  const failure = useFailure()
  const {
    kind,
    setKind,
    value,
    setValue,
    prepareField,
    setPrepareField,
    prepared,
    setPrepared,
    draftRevisionRef,
    action,
    dismiss,
    dismissal,
    setDismissal,
    submit,
    refresh,
    cache,
    onReaction,
    identity,
  } = controls
  const blocked = busy || action.isPending || dismiss.isPending || !!dismissal
  return (
    <section className="player-actions">
      <h3>{m.myActions}</h3>
      {(conversation.proposals ?? [])
        .filter((p) => p.state === 'pending')
        .map((p) => (
          <article className="action-proposal" key={p.id}>
            <ActionDescription action={p.action} />
            <Button
              disabled={
                blocked ||
                !!prepared ||
                p.revision !== (conversation.revision ?? 0)
              }
              onClick={() => submit(p.action, p.id)}
            >
              {m.confirmAction}
            </Button>
            <Button
              disabled={blocked || !!prepared}
              onClick={() => {
                const body = {
                  operation_id: crypto.randomUUID(),
                  expected_revision: conversation.revision ?? 0,
                }
                setDismissal({ proposal_id: p.id, body })
                dismiss.mutate({
                  path: { identity, proposal_id: p.id },
                  body,
                })
              }}
            >
              {m.dismissAction}
            </Button>
          </article>
        ))}
      <form
        onSubmit={(event) => {
          event.preventDefault()
          const selected: PlayerAction =
            kind === 'move'
              ? { kind, destination: value }
              : kind === 'wait'
                ? { kind, minutes: Number(value) }
                : kind === 'intention'
                  ? { kind, text: value }
                  : {
                      kind,
                      [prepareField]:
                        prepareField === 'held_objects'
                          ? parseObjects(
                              value,
                              conversation.state?.player?.held_objects ?? [],
                            )
                          : value,
                    }
          submit(selected)
        }}
      >
        <fieldset disabled={blocked || !!prepared}>
          <label>
            {m.chooseAction}
            <select
              value={kind}
              onChange={(event) => {
                setKind(event.target.value as PlayerAction['kind'])
                setValue('')
                draftRevisionRef.current = null
              }}
            >
              <option value="move">{m.actionMove}</option>
              <option value="wait">{m.actionWait}</option>
              <option value="prepare">{m.actionPrepare}</option>
              <option value="intention">{m.actionIntention}</option>
            </select>
          </label>
          {kind === 'prepare' && (
            <label>
              {m.choosePreparation}
              <select
                value={prepareField}
                onChange={(event) => {
                  setPrepareField(event.target.value as typeof prepareField)
                  setValue('')
                  draftRevisionRef.current = null
                }}
              >
                <option value="clothing">{m.clothing}</option>
                <option value="pose">{m.pose}</option>
                <option value="expression">{m.expression}</option>
                <option value="held_objects">{m.heldObjects}</option>
              </select>
            </label>
          )}
          <label>
            {kind === 'move'
              ? m.destination
              : kind === 'wait'
                ? m.waitMinutes
                : kind === 'prepare'
                  ? m.preparationValue
                  : m.currentIntention}
            {kind === 'prepare' && prepareField === 'held_objects' ? (
              <textarea
                value={value}
                rows={3}
                onChange={(event) => {
                  draftRevisionRef.current ??= conversation.revision ?? 0
                  setValue(event.target.value)
                }}
              />
            ) : (
              <input
                required={kind === 'move' || kind === 'wait'}
                type={kind === 'wait' ? 'number' : 'text'}
                min={kind === 'wait' ? 1 : undefined}
                value={value}
                onChange={(event) => {
                  draftRevisionRef.current ??= conversation.revision ?? 0
                  setValue(event.target.value)
                }}
              />
            )}
          </label>
          <Button type="submit">{m.confirmAction}</Button>
        </fieldset>
      </form>
      {action.error && (
        <p className="error" role="alert">
          {failure(action.error)}
        </p>
      )}
      {action.isError && !prepared && (
        <Button
          onClick={async () => {
            const current = await cache.fetchQuery(
              q.getConversationOptions({ path: { identity } }),
            )
            draftRevisionRef.current = current.revision ?? 0
            action.reset()
          }}
        >
          {m.reviewAction}
        </Button>
      )}
      {dismiss.error && (
        <p className="error" role="alert">
          {failure(dismiss.error)}
        </p>
      )}
      {prepared && action.isError && (
        <Button
          onClick={() => action.mutate({ path: { identity }, body: prepared })}
        >
          {m.retryRequest}
        </Button>
      )}
      {prepared && action.isError && (
        <Button
          onClick={async () => {
            const records = await cache.fetchQuery(
              q.getActionsOptions({ path: { identity } }),
            )
            const record = records.find(
              (r) => r.request.operation_id === prepared.operation_id,
            )
            if (record) {
              setPrepared(null)
              draftRevisionRef.current = null
              setValue('')
              action.reset()
              onReaction(record.reaction_id)
              await refresh()
            }
          }}
        >
          {m.checkAction}
        </Button>
      )}
      {dismissal && dismiss.isError && (
        <Button
          onClick={() =>
            dismiss.mutate({
              path: { identity, proposal_id: dismissal.proposal_id },
              body: dismissal.body,
            })
          }
        >
          {m.retryRequest}
        </Button>
      )}
      {dismissal && dismiss.isError && (
        <Button
          onClick={async () => {
            const result = await cache.fetchQuery(
              q.getConversationOptions({ path: { identity } }),
            )
            if (
              result.proposals?.some(
                (p) =>
                  p.id === dismissal.proposal_id &&
                  p.decision_id === dismissal.body.operation_id,
              )
            ) {
              setDismissal(null)
              dismiss.reset()
            }
          }}
        >
          {m.checkAction}
        </Button>
      )}
      <details>
        <summary>{m.actionHistory}</summary>
        {(conversation.actions ?? []).map((record) => (
          <ActionDescription
            key={record.request.operation_id}
            action={record.request.action}
          />
        ))}
      </details>
    </section>
  )
}

export function useVisualReview({
  identity,
  conversation,
}: {
  identity: string
  conversation: Conversation | undefined
}) {
  const cache = useQueryClient()
  const [values, setValues] = useState<Record<
    'player' | 'character',
    VisualPerson
  > | null>(null)
  const [prepared, setPrepared] = useState<VisualReview | null>(null)
  const resolved =
    values ??
    (Object.fromEntries(
      (['player', 'character'] as const).map((role) => [
        role,
        {
          appearance: '',
          initial_clothing:
            (role === 'player'
              ? conversation?.definition.user
              : conversation?.definition.character
            )?.initial_clothing ?? null,
          current_clothing: conversation?.state?.[role]?.clothing ?? null,
        },
      ]),
    ) as Record<'player' | 'character', VisualPerson>)
  const review = useMutation({
    ...q.reviewVisualsMutation(),
    onSuccess: (result) => {
      setPrepared(null)
      cache.setQueryData(
        q.getConversationQueryKey({
          path: { identity },
        }),
        result,
      )
    },
    onError: (error) => {
      if (
        error &&
        typeof error === 'object' &&
        'code' in error &&
        ['operation_conflict', 'operation_busy', 'invalid_request'].includes(
          String(error.code),
        )
      )
        setPrepared(null)
    },
  })
  return {
    values: resolved,
    setValues,
    prepared,
    setPrepared,
    review,
    cache,
    identity,
  }
}

export function VisualReviewForm({
  conversation,
  busy,
  controls,
}: {
  conversation: Conversation
  busy: boolean
  controls: ReturnType<typeof useVisualReview>
}) {
  const m = useMessages()
  const failure = useFailure()
  const { values, setValues, prepared, setPrepared, review, cache, identity } =
    controls
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault()
        const body: VisualReview = {
          operation_id: crypto.randomUUID(),
          expected_revision: conversation.revision ?? 0,
          ...(conversation.definition.user?.visual_review_required
            ? { player: values.player }
            : {}),
          ...(conversation.definition.character?.visual_review_required
            ? { character: values.character }
            : {}),
        }
        setPrepared(body)
        review.mutate({ path: { identity }, body })
      }}
    >
      <h3>{m.reviewAppearance}</h3>
      <p>{m.reviewAppearanceHelp}</p>
      <fieldset disabled={busy || review.isPending || !!prepared}>
        {(['player', 'character'] as const)
          .filter(
            (role) =>
              (role === 'player'
                ? conversation.definition.user
                : conversation.definition.character
              )?.visual_review_required,
          )
          .map((role) => (
            <fieldset key={role}>
              <legend>{role === 'player' ? m.you : m.character}</legend>
              <p>
                {
                  (role === 'player'
                    ? conversation.definition.user
                    : conversation.definition.character
                  )?.appearance
                }
              </p>
              {(
                ['appearance', 'initial_clothing', 'current_clothing'] as const
              ).map((field) => (
                <label key={field}>
                  {field === 'appearance'
                    ? m.appearance
                    : field === 'initial_clothing'
                      ? m.initial_clothing
                      : m.clothing}
                  <input
                    value={values[role][field] ?? ''}
                    onChange={(event) =>
                      setValues({
                        ...values,
                        [role]: {
                          ...values[role],
                          [field]: event.target.value,
                        },
                      })
                    }
                  />
                </label>
              ))}
            </fieldset>
          ))}
        <Button type="submit">{m.confirmAppearance}</Button>
      </fieldset>
      {review.error && (
        <p role="alert" className="error">
          {failure(review.error)}
        </p>
      )}
      {prepared && review.isError && (
        <Button
          onClick={() => review.mutate({ path: { identity }, body: prepared })}
        >
          {m.retryRequest}
        </Button>
      )}
      {prepared && review.isError && (
        <Button
          onClick={async () => {
            const result = await cache.fetchQuery(
              q.getConversationOptions({ path: { identity } }),
            )
            if (
              result.visual_reviews?.some(
                (record) =>
                  record.request.operation_id === prepared.operation_id,
              )
            ) {
              setPrepared(null)
              review.reset()
            }
          }}
        >
          {m.checkAction}
        </Button>
      )}
    </form>
  )
}
