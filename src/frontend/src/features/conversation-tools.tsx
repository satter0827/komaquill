import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate } from 'react-router'
import * as q from '../api/generated/@tanstack/react-query.gen'
import type {
  Conversation,
  NotesRequest,
  SceneRequest,
} from '../api/generated/types.gen'
import { useFailure, useMessages } from '../app/preferences'
import { Button } from '../components/ui/button'

export function ConversationNotes({
  conversation,
}: {
  conversation: Conversation
}) {
  const m = useMessages()
  const failure = useFailure()
  const cache = useQueryClient()
  const [draft, setDraft] = useState<string | null>(null)
  const [prepared, setPrepared] = useState<NotesRequest | null>(null)
  const [includeImages, setIncludeImages] = useState(true)
  const [includeConditions, setIncludeConditions] = useState(false)
  const save = useMutation({
    ...q.saveNotesMutation(),
    onError: async (error) => {
      if (
        error &&
        typeof error === 'object' &&
        'code' in error &&
        ['operation_conflict', 'invalid_request'].includes(String(error.code))
      ) {
        setPrepared(null)
        await cache.invalidateQueries({
          queryKey: q.getConversationQueryKey({
            path: { identity: conversation.id ?? '' },
          }),
        })
      }
    },
    onSuccess: async () => {
      await cache.invalidateQueries({
        queryKey: q.getConversationQueryKey({
          path: { identity: conversation.id ?? '' },
        }),
      })
      setDraft(null)
      setPrepared(null)
    },
  })
  function prepareNotes(): NotesRequest {
    const request = prepared ?? {
      operation_id: crypto.randomUUID(),
      expected_revision: conversation.revision ?? 0,
      notes: (draft ?? conversation.notes?.join('\n') ?? '')
        .split('\n')
        .filter((line) => line.trim()),
    }
    setPrepared(request)
    return request
  }
  const bookmarks = useQuery(
    q.getBookmarksOptions({ path: { identity: conversation.id ?? '' } }),
  )
  return (
    <details>
      <summary>{m.pinnedNotes}</summary>
      <form
        onSubmit={(event) => {
          event.preventDefault()
          save.mutate({
            path: { identity: conversation.id ?? '' },
            body: {
              ...prepareNotes(),
            },
          })
        }}
      >
        <label>
          {m.notesDescription}
          <textarea
            disabled={!!prepared}
            value={draft ?? conversation.notes?.join('\n') ?? ''}
            onChange={(event) => setDraft(event.target.value)}
          />
        </label>
        <Button type="submit" disabled={save.isPending}>
          {prepared ? m.retryRequest : m.save}
        </Button>
        {save.error && <p role="alert">{failure(save.error)}</p>}
      </form>
      <details>
        <summary>{m.exportConversation}</summary>
        <label>
          <input
            type="checkbox"
            checked={includeImages}
            onChange={(event) => setIncludeImages(event.target.checked)}
          />
          {m.includeImages}
        </label>
        <label>
          <input
            type="checkbox"
            checked={includeConditions}
            onChange={(event) => setIncludeConditions(event.target.checked)}
          />
          {m.includeConditions}
        </label>
        <a
          className="button"
          download
          href={`/api/v1/conversations/${conversation.id}/export?include_images=${includeImages}&include_conditions=${includeConditions}`}
        >
          {m.downloadCopy}
        </a>
      </details>
      <h3>{m.savedScenes}</h3>
      {bookmarks.error && <p role="alert">{failure(bookmarks.error)}</p>}
      {bookmarks.data?.map((bookmark) => (
        <div key={bookmark.id}>
          <p>{bookmark.name}</p>
          <SceneActions
            conversation={conversation}
            turnId={bookmark.turn_id}
            defaultName={bookmark.name}
          />
        </div>
      ))}
    </details>
  )
}

export function SceneActions({
  conversation,
  turnId,
  defaultName = '',
}: {
  conversation: Conversation
  turnId: string
  defaultName?: string
}) {
  const m = useMessages()
  const failure = useFailure()
  const cache = useQueryClient()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [name, setName] = useState(defaultName)
  const [prepared, setPrepared] = useState<{
    kind: 'save' | 'fork'
    body: SceneRequest
  } | null>(null)
  const rejected = async (error: unknown) => {
    if (
      error &&
      typeof error === 'object' &&
      'code' in error &&
      ['operation_conflict', 'request_not_found', 'invalid_request'].includes(
        String(error.code),
      )
    ) {
      setPrepared(null)
      await cache.invalidateQueries({
        queryKey: q.getConversationQueryKey({
          path: { identity: conversation.id ?? '' },
        }),
      })
    }
  }
  const save = useMutation({
    ...q.saveBookmarkMutation(),
    onError: rejected,
    onSuccess: async () => {
      await cache.invalidateQueries({
        queryKey: q.getBookmarksQueryKey({
          path: { identity: conversation.id ?? '' },
        }),
      })
      setOpen(false)
      setPrepared(null)
    },
  })
  const fork = useMutation({
    ...q.forkConversationMutation(),
    onError: rejected,
    onSuccess: async (value) => {
      await cache.invalidateQueries({ queryKey: q.listConversationsQueryKey() })
      navigate(`/conversations/${value.id}`)
    },
  })
  const request = (kind: 'save' | 'fork') => {
    const body = prepared?.body ?? {
      operation_id: crypto.randomUUID(),
      expected_revision: conversation.revision ?? 0,
      turn_id: turnId,
      name: name.trim() || conversation.definition.title,
    }
    setPrepared({ kind, body })
    return { path: { identity: conversation.id ?? '' }, body }
  }
  return (
    <div>
      <Button onClick={() => setOpen(!open)}>{m.sceneActions}</Button>
      {open && (
        <div>
          <label>
            {m.sceneName}
            <input
              disabled={!!prepared}
              value={name}
              maxLength={120}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <Button
            disabled={
              save.isPending || fork.isPending || prepared?.kind === 'fork'
            }
            onClick={() => save.mutate(request('save'))}
          >
            {m.saveScene}
          </Button>
          <Button
            disabled={
              save.isPending || fork.isPending || prepared?.kind === 'save'
            }
            onClick={() => fork.mutate(request('fork'))}
          >
            {m.forkScene}
          </Button>
        </div>
      )}
      {(save.error || fork.error) && (
        <p role="alert">{failure(save.error || fork.error)}</p>
      )}
    </div>
  )
}
