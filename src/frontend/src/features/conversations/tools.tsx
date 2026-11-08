import { MoreHorizontal } from 'lucide-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router'
import * as q from '../../api/generated/@tanstack/react-query.gen'
import type {
  Conversation,
  NotesRequest,
  SceneRequest,
} from '../../api/generated/types.gen'
import {
  useFailure,
  useFormatMessage,
  useMessages,
} from '../../shared/preferences'
import { WorkActions } from '../../shared/ui/work-actions'
import { Button } from '../../shared/ui/button'
import { Popover, Popup } from '../../shared/ui/popup'
import { QueryNotice } from '../../shared/ui/query-notice'

export function ConversationNotes({
  conversation,
  canMutate,
}: {
  conversation: Conversation
  canMutate: boolean
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
    <div className="story-menu-content">
      <details>
        <summary>{m.pinnedNotes}</summary>
        <div className="details-content">
          <form
            onSubmit={(event) => {
              event.preventDefault()
              if (!canMutate) return
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
            <Button type="submit" disabled={!canMutate || save.isPending}>
              {prepared ? m.retrySave : m.save}
            </Button>
            {save.error && <p role="alert">{failure(save.error)}</p>}
          </form>
        </div>
      </details>
      <details>
        <summary>{m.exportConversation}</summary>
        <div className="details-content">
          <label className="checkbox">
            <input
              type="checkbox"
              checked={includeImages}
              onChange={(event) => setIncludeImages(event.target.checked)}
            />
            {m.includeImages}
          </label>
          <label className="checkbox">
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
        </div>
      </details>
      <h3>{m.savedScenes}</h3>
      {bookmarks.isPending && <p role="status">{m.loading}</p>}
      <QueryNotice
        error={bookmarks.error}
        retained={bookmarks.data !== undefined}
        pending={bookmarks.isFetching}
        onRetry={() => void bookmarks.refetch()}
      />
      {bookmarks.isSuccess && bookmarks.data.length === 0 && (
        <p className="subtle">{m.noSavedScenes}</p>
      )}
      {bookmarks.data?.map((bookmark) => (
        <div key={bookmark.id}>
          <p>{bookmark.name}</p>
          <WorkActions target={{ kind: 'bookmark', id: bookmark.id }} />
          <SceneActions
            conversation={conversation}
            canMutate={canMutate}
            turnId={bookmark.turn_id}
            defaultName={bookmark.name}
          />
        </div>
      ))}
    </div>
  )
}

export function SceneActions({
  conversation,
  canMutate,
  turnId,
  defaultName = '',
}: {
  conversation: Conversation
  canMutate: boolean
  turnId: string
  defaultName?: string
}) {
  const m = useMessages()
  const format = useFormatMessage()
  const failure = useFailure()
  const cache = useQueryClient()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [name, setName] = useState(defaultName)
  const label = defaultName
    ? format('bookmarkActions', { name: defaultName })
    : m.sceneActions
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
  const actionError = save.error || fork.error
  const errorNotice = useRef<HTMLParagraphElement>(null)
  useEffect(() => {
    if (open && actionError) errorNotice.current?.focus()
  }, [open, actionError])
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
    <div className="scene-menu">
      <Popover.Root open={open} onOpenChange={setOpen}>
        <Popover.Trigger
          render={
            <Button variant="ghost" aria-label={label} disabled={!canMutate} />
          }
        >
          <MoreHorizontal size={18} />
        </Popover.Trigger>
        <Popup label={label}>
          <label>
            {m.sceneName}
            <input
              disabled={!!prepared}
              value={name}
              maxLength={120}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          {actionError && (
            <p ref={errorNotice} tabIndex={-1} role="alert" className="error">
              {failure(actionError)}
            </p>
          )}
          <Button
            disabled={
              !canMutate ||
              save.isPending ||
              fork.isPending ||
              prepared?.kind === 'fork'
            }
            onClick={() => {
              fork.reset()
              save.mutate(request('save'))
            }}
          >
            {m.saveScene}
          </Button>
          <Button
            disabled={
              !canMutate ||
              save.isPending ||
              fork.isPending ||
              prepared?.kind === 'save'
            }
            onClick={() => {
              save.reset()
              fork.mutate(request('fork'))
            }}
          >
            {m.forkScene}
          </Button>
        </Popup>
      </Popover.Root>
    </div>
  )
}
