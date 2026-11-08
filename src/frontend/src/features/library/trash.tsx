import { useQuery } from '@tanstack/react-query'
import { listTrashOptions } from '../../api/generated/@tanstack/react-query.gen'
import { useMessages } from '../../shared/preferences'
import { QueryNotice } from '../../shared/ui/query-notice'
import { RestoreWork } from '../../shared/ui/work-actions'

export function TrashPage() {
  const m = useMessages()
  const trash = useQuery(listTrashOptions())
  const labels = {
    conversation: m.conversationLabel,
    archived: m.archivedTitle,
    character: m.character,
    story: m.stories,
    bookmark: m.savedScenes,
    image: m.images,
  }
  return (
    <section className="page">
      <h1>{m.trashTitle}</h1>
      <QueryNotice
        error={trash.error}
        pending={trash.isFetching}
        retained={trash.data !== undefined}
        onRetry={() => void trash.refetch()}
      />
      {trash.isSuccess && !trash.data.length && <p>{m.emptyTrash}</p>}
      <ul className="work-list">
        {trash.data?.map((item) => (
          <li
            className="work-row"
            key={`${item.kind}:${item.id}:${item.index}`}
          >
            <div>
              <span className="subtle">{labels[item.kind]}</span>
              <div>{item.name}</div>
            </div>
            <RestoreWork item={item} />
          </li>
        ))}
      </ul>
    </section>
  )
}
