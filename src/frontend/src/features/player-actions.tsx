import { objectLines, parseObjects } from './physical-objects'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useCallback, useRef, useState } from 'react'
import * as q from '../api/generated/@tanstack/react-query.gen'
import type {
  SendInput,
  Conversation,
  ProposalDismissal,
  VisualReview,
  VisualPerson,
} from '../api/generated/types.gen'
import { useFailure, useFormatMessage, useMessages } from '../app/preferences'
import { Button } from '../components/ui/button'
type PlayerAction = NonNullable<SendInput['action']>
const PREPARATION_FIELDS = [
  'clothing',
  'pose',
  'expression',
  'held_objects',
] as const

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
}: {
  identity: string
  conversation: Conversation | undefined
}) {
  const cache = useQueryClient()
  const [kind, setKind] = useState<PlayerAction['kind']>('move')
  const [value, setValue] = useState('')
  const [editingAction, setEditingAction] = useState<PlayerAction | null>(null)
  const [prepareField, setPrepareField] = useState<
    'clothing' | 'pose' | 'expression' | 'held_objects'
  >('clothing')
  const [queued, setQueued] = useState<{
    action: PlayerAction
    proposal_id?: string
    revision: number
  } | null>(null)
  const [dismissal, setDismissal] = useState<{
    proposal_id: string
    body: ProposalDismissal
  } | null>(null)
  const draftRevisionRef = useRef<number | null>(null)
  const refresh = () =>
    cache.invalidateQueries({
      queryKey: q.getConversationQueryKey({ path: { identity } }),
    })
  const clear = useCallback(() => {
    setQueued(null)
    setEditingAction(null)
    setValue('')
    draftRevisionRef.current = null
  }, [])
  const dismiss = useMutation({
    ...q.dismissProposalMutation(),
    onSuccess: () => {
      setDismissal(null)
      clear()
      void refresh()
    },
  })
  const queue = (action: PlayerAction, proposal_id?: string) =>
    setQueued({
      action,
      proposal_id,
      revision: proposal_id
        ? (conversation!.revision ?? 0)
        : (draftRevisionRef.current ?? conversation!.revision ?? 0),
    })
  return {
    edit: () => {
      if (!queued) return
      const action = queued.action
      setEditingAction(action)
      draftRevisionRef.current = queued.revision
      setKind(action.kind)
      if (action.kind === 'move') setValue(action.destination)
      else if (action.kind === 'wait') setValue(String(action.minutes))
      else if (action.kind === 'intention') setValue(action.text)
      else {
        const field =
          PREPARATION_FIELDS.find((key) => action[key] != null) ?? 'clothing'
        setPrepareField(field)
        setValue(
          field === 'held_objects'
            ? objectLines(action.held_objects ?? [])
            : String(action[field] ?? ''),
        )
      }
    },
    kind,
    setKind,
    value,
    setValue,
    editingAction,
    setEditingAction,
    prepareField,
    setPrepareField,
    queued,
    setQueued,
    draftRevisionRef,
    clear,
    queue,
    dismiss,
    dismissal,
    setDismissal,
    identity,
    refresh,
    cache,
    unresolved: !!dismissal,
  }
}

export function PlayerActions({
  conversation,
  busy,
  controls,
  onQueued,
}: {
  conversation: Conversation
  busy: boolean
  controls: ReturnType<typeof usePlayerActions>
  onQueued?: () => void
}) {
  const m = useMessages()
  const failure = useFailure()
  const {
    kind,
    setKind,
    value,
    setValue,
    editingAction,
    setEditingAction,
    prepareField,
    setPrepareField,
    queued,
    setQueued,
    draftRevisionRef,
    queue: queueAction,
    dismiss,
    dismissal,
    setDismissal,
    identity,
    cache,
  } = controls
  const queue = (action: PlayerAction, proposal?: string) => {
    queueAction(action, proposal)
    setEditingAction(null)
    onQueued?.()
  }
  const updateValue = (next: string) => {
    draftRevisionRef.current ??= conversation.revision ?? 0
    setValue(next)
  }
  const remaining =
    editingAction?.kind === 'prepare'
      ? { ...editingAction, [prepareField]: undefined }
      : null
  const blocked = busy || dismiss.isPending || !!dismissal
  return (
    <section className="player-actions">
      <h3>{m.myActions}</h3>
      <p className="subtle">{m.actionWithMessage}</p>
      {(conversation.proposals ?? [])
        .filter((p) => p.state === 'pending')
        .map((p) => (
          <article className="action-proposal" key={p.id}>
            <ActionDescription action={p.action} />
            <div className="actions">
              <Button
                disabled={blocked || p.revision !== conversation.revision}
                onClick={() => queue(p.action, p.id)}
              >
                {m.queueAction}
              </Button>
              <Button
                disabled={blocked}
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
            </div>
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
                      ...(editingAction?.kind === 'prepare'
                        ? editingAction
                        : {}),
                      kind,
                      [prepareField]:
                        prepareField === 'held_objects'
                          ? parseObjects(
                              value,
                              (editingAction?.kind === 'prepare'
                                ? editingAction.held_objects
                                : undefined) ??
                                conversation.state?.player?.held_objects ??
                                [],
                            )
                          : value,
                    }
          queue(selected)
        }}
      >
        <fieldset disabled={blocked}>
          <label>
            {m.chooseAction}
            <select
              value={kind === 'prepare' ? prepareField : kind}
              onChange={(e) => {
                const selected = e.target.value
                if (PREPARATION_FIELDS.some((field) => field === selected)) {
                  setKind('prepare')
                  if (editingAction?.kind !== 'prepare') {
                    setEditingAction(null)
                    draftRevisionRef.current = null
                  }
                  setPrepareField(selected as typeof prepareField)
                  const field = selected as typeof prepareField
                  setValue(
                    editingAction?.kind === 'prepare'
                      ? field === 'held_objects'
                        ? objectLines(editingAction.held_objects)
                        : String(editingAction[field] ?? '')
                      : '',
                  )
                } else {
                  setKind(selected as PlayerAction['kind'])
                  setEditingAction(null)
                  setValue('')
                  draftRevisionRef.current = null
                }
              }}
            >
              <option value="move">{m.actionMove}</option>
              <option value="wait">{m.actionWait}</option>
              <option value="clothing">{m.playerClothing}</option>
              <option value="pose">{m.pose}</option>
              <option value="expression">{m.expression}</option>
              <option value="held_objects">{m.heldObjects}</option>
              <option value="intention">{m.actionIntention}</option>
            </select>
          </label>

          <label>
            {kind === 'move'
              ? m.destination
              : kind === 'wait'
                ? m.waitMinutes
                : kind === 'prepare'
                  ? prepareField === 'held_objects'
                    ? m.heldObjectsInput
                    : m.preparationValue
                  : m.currentIntention}
            {kind === 'prepare' || kind === 'intention' ? (
              <textarea
                rows={
                  kind === 'prepare' && prepareField === 'held_objects' ? 3 : 2
                }
                value={value}
                onChange={(event) => updateValue(event.target.value)}
              />
            ) : (
              <input
                required={kind === 'move' || kind === 'wait'}
                type={kind === 'wait' ? 'number' : 'text'}
                min={kind === 'wait' ? 1 : undefined}
                value={value}
                onChange={(event) => updateValue(event.target.value)}
              />
            )}
          </label>
          {remaining &&
            PREPARATION_FIELDS.some((field) => remaining[field] != null) && (
              <div className="details-content">
                <p className="subtle">{m.additionalActionChanges}</p>
                <ActionDescription action={remaining} />
              </div>
            )}
          <Button type="submit">{m.queueAction}</Button>
        </fieldset>
      </form>
      {queued && queued.revision !== conversation.revision && (
        <div role="alert">
          <p>{m.reviewAction}</p>
          <Button
            disabled={blocked}
            onClick={() => {
              draftRevisionRef.current = conversation.revision ?? 0
              setQueued({ ...queued, revision: conversation.revision ?? 0 })
            }}
          >
            {m.confirmCurrentAction}
          </Button>
        </div>
      )}
      {dismiss.error && (
        <p className="error" role="alert">
          {failure(dismiss.error)}
        </p>
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
          {m.retrySave}
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
