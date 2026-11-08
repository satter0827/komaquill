import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import * as q from '../../api/generated/@tanstack/react-query.gen'
import type { WorkItem, WorkRef } from '../../api/generated/types.gen'
import { useFailure, useMessages } from '../preferences'
import { Button } from './button'
import { Dialog, Modal } from './popup'

const workQueries = new Set([
  'getWork',
  'listTrash',
  'listConversations',
  'getConversation',
  'listArchivedConversations',
  'getArchivedConversation',
  'listStories',
  'getStory',
  'listCharacters',
  'getCharacter',
  'listImageOutputs',
  'listImageUses',
  'getDetachedImages',
  'getBookmarks',
])

function useRefreshWorks() {
  const cache = useQueryClient()
  return (removed?: WorkItem) =>
    cache.invalidateQueries({
      predicate: ({ queryKey }) =>
        queryKey.some(
          (part) =>
            !!part &&
            typeof part === 'object' &&
            '_id' in part &&
            workQueries.has(String(part._id)) &&
            !(
              removed &&
              'path' in part &&
              part.path &&
              typeof part.path === 'object' &&
              'identity' in part.path &&
              part.path.identity === removed.id
            ),
        ),
    })
}

export function RestoreWork({ item }: { item: WorkItem }) {
  const m = useMessages()
  const failure = useFailure()
  const refresh = useRefreshWorks()
  const restore = useMutation({
    ...q.changeWorkMutation(),
    onSuccess: () => refresh(),
  })
  return (
    <>
      <Button
        disabled={restore.isPending}
        onClick={() =>
          restore.mutate({
            body: {
              target: { kind: item.kind, id: item.id, index: item.index },
              action: 'restore',
              operation_id: crypto.randomUUID(),
              expected_revision: item.revision ?? 0,
            },
          })
        }
      >
        {m.restoreWork}
      </Button>
      {restore.error && <p role="alert">{failure(restore.error)}</p>}
    </>
  )
}

export function WorkActions({
  target,
  afterTrash,
  canRename = true,
  contentRevision,
  screenHidden = false,
}: {
  target: WorkRef
  afterTrash?: () => void
  canRename?: boolean
  contentRevision?: number
  screenHidden?: boolean
}) {
  const m = useMessages()
  const failure = useFailure()
  const [open, setOpen] = useState(false)
  const [name, setName] = useState<string | null>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const cancel = useRef<HTMLButtonElement>(null)
  const refresh = useRefreshWorks()
  const detail = useQuery({
    ...q.getWorkOptions({
      path: { kind: target.kind, identity: target.id },
      query: { index: target.index },
    }),
    enabled: open,
  })
  const uses = useQuery({
    ...q.listImageUsesOptions({
      path: { identity: target.id, index: target.index ?? 0 },
    }),
    enabled: open && target.kind === 'image',
  })
  const change = useMutation({
    ...q.changeWorkMutation(),
    onSuccess: async (item) => {
      setOpen(false)
      if (item.trashed) afterTrash?.()
      await refresh(item.trashed ? item : undefined)
    },
    onError: () => refresh(),
  })
  const detach = useMutation({
    ...q.detachImageMutation(),
    onSuccess: () => refresh(),
  })
  const active = change.isPending || detach.isPending
  const submit = (action: 'rename' | 'trash') => {
    if (!detail.data) return
    change.mutate({
      body: {
        target,
        action,
        operation_id: crypto.randomUUID(),
        expected_revision: detail.data.revision ?? 0,
        expected_content_revision: contentRevision,
        name: action === 'rename' ? name?.trim() : undefined,
      },
    })
  }
  return (
    <Dialog.Root
      open={open}
      onOpenChange={(value) => {
        setOpen(value)
        if (value) {
          setName(null)
          change.reset()
          detach.reset()
        }
      }}
    >
      <Dialog.Trigger render={<Button ref={trigger} variant="ghost" />}>
        {m.manageWork}
      </Dialog.Trigger>
      {!screenHidden && (
        <Modal
          label={m.manageWork}
          closeLabel={m.closePanel}
          initialFocus={cancel}
          finalFocus={trigger}
        >
          <div className="details-content">
            {!detail.data && detail.isPending && (
              <p role="status">{m.loading}</p>
            )}
            {detail.data && canRename && (
              <form
                className="details-content"
                onSubmit={(event) => {
                  event.preventDefault()
                  submit('rename')
                }}
              >
                <label>
                  {m.name}
                  <input
                    maxLength={120}
                    value={name ?? detail.data.name}
                    onChange={(event) => setName(event.target.value)}
                  />
                </label>
                <Button
                  type="submit"
                  disabled={
                    active || !name?.trim() || name.trim() === detail.data.name
                  }
                >
                  {m.renameWork}
                </Button>
              </form>
            )}
            {target.kind === 'image' && uses.isPending && (
              <p role="status">{m.loading}</p>
            )}
            {!!uses.data?.length && (
              <section className="details-content">
                <h3>{m.imageUses}</h3>
                {uses.data.map((use) => (
                  <div className="work-row" key={`${use.owner}:${use.slot}`}>
                    <span>
                      {use.name} ·{' '}
                      {use.slot === 'cover' || use.slot === 'story'
                        ? m.imageUseCover
                        : use.slot === 'character'
                          ? m.imageUsePortrait
                          : m.imageUseScene}
                    </span>
                    <Button
                      disabled={active}
                      onClick={() =>
                        detach.mutate({ body: { image: target, use } })
                      }
                    >
                      {m.detachImage}
                    </Button>
                  </div>
                ))}
              </section>
            )}
            {(detail.error || uses.error || change.error || detach.error) && (
              <p role="alert" className="error">
                {failure(
                  detail.error || uses.error || change.error || detach.error,
                )}
              </p>
            )}
            <p className="subtle">{m.trashDescription}</p>
            <div className="actions">
              <Dialog.Close render={<Button ref={cancel} />}>
                {m.cancel}
              </Dialog.Close>
              <Button
                variant="danger"
                disabled={
                  !detail.data ||
                  active ||
                  (target.kind === 'image' &&
                    (!uses.isSuccess || uses.data.length > 0))
                }
                onClick={() => submit('trash')}
              >
                {m.moveToTrash}
              </Button>
            </div>
          </div>
        </Modal>
      )}
    </Dialog.Root>
  )
}
