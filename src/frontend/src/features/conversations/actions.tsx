import { objectLines, parseObjects } from '../../shared/forms/physical-objects'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useCallback, useRef, useState } from 'react'
import * as q from '../../api/generated/@tanstack/react-query.gen'
import type {
  SendInput,
  Conversation,
  ProposalDismissal,
} from '../../api/generated/types.gen'
import {
  useFailure,
  useFormatMessage,
  useMessages,
} from '../../shared/preferences'
import { Button } from '../../shared/ui/button'
type PlayerAction = NonNullable<SendInput['action']>
type EditableAction = Exclude<PlayerAction, { kind: 'move' }>
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
  const [kind, setKind] = useState<EditableAction['kind']>('prepare')
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
  const queue = (
    action: PlayerAction,
    proposal_id?: string,
    revision?: number,
  ) =>
    setQueued({
      action,
      proposal_id,
      revision:
        revision ??
        (proposal_id
          ? (conversation!.revision ?? 0)
          : (draftRevisionRef.current ?? conversation!.revision ?? 0)),
    })
  return {
    edit: () => {
      if (!queued || queued.action.kind === 'move') return
      const action = queued.action
      setEditingAction(action)
      draftRevisionRef.current = queued.revision
      setKind(action.kind)
      if (action.kind === 'wait') setValue(String(action.minutes))
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
    confirmRevision: () => {
      if (!queued) return
      draftRevisionRef.current = conversation?.revision ?? 0
      setQueued({ ...queued, revision: conversation?.revision ?? 0 })
    },
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
            kind === 'wait'
              ? { kind, minutes: Number(value) }
              : kind === 'intention'
                ? { kind, text: value }
                : {
                    ...(editingAction?.kind === 'prepare' ? editingAction : {}),
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
                  setKind(selected as EditableAction['kind'])
                  setEditingAction(null)
                  setValue('')
                  draftRevisionRef.current = null
                }
              }}
            >
              <option value="wait">{m.actionWait}</option>
              <option value="clothing">{m.playerClothing}</option>
              <option value="pose">{m.pose}</option>
              <option value="expression">{m.expression}</option>
              <option value="held_objects">{m.heldObjects}</option>
              <option value="intention">{m.actionIntention}</option>
            </select>
          </label>

          <label>
            {kind === 'wait'
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
                required
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
