import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router'
import * as q from '../../api/generated/@tanstack/react-query.gen'
import { isMissing } from '../../api/client'
import { StoryReply } from '../../shared/story/reply'
import { useMessages } from '../../shared/preferences'
import { useNavigate } from 'react-router'
import { WorkActions } from '../../shared/ui/work-actions'
import { QueryNotice } from '../../shared/ui/query-notice'

export function ArchivedConversations() {
  const m = useMessages()
  const query = useQuery({
    ...q.listArchivedConversationsOptions(),
  })
  const notice = (
    <QueryNotice
      error={query.error}
      retained={!!query.data}
      pending={query.isFetching}
      onRetry={() => void query.refetch()}
    />
  )
  if (query.error && !query.data) return notice
  if (!query.data?.length) return notice
  return (
    <section className="home-section">
      {notice}
      <h2>{m.archivedTitle}</h2>
      <ul className="archived-list">
        {query.data.map((item) => (
          <li key={item.id}>
            <Link to={`/archive/conversations/${item.id}`}>{item.title}</Link>
          </li>
        ))}
      </ul>
    </section>
  )
}

export function ArchivedConversationPage() {
  const { identity = '' } = useParams()
  const navigate = useNavigate()
  const m = useMessages()
  const query = useQuery({
    ...q.getArchivedConversationOptions({ path: { identity } }),
  })
  const notice = (
    <QueryNotice
      error={query.error}
      retained={!!query.data}
      pending={query.isFetching}
      onRetry={() => void query.refetch()}
    />
  )
  if (query.error && (!query.data || isMissing(query.error))) return notice
  if (!query.data)
    return (
      <p role="status" className="page">
        {m.loading}
      </p>
    )
  return (
    <section className="page archive-page">
      {notice}
      <h1>{query.data.title}</h1>
      <WorkActions
        target={{ kind: 'archived', id: identity }}
        afterTrash={() => navigate('/')}
      />
      <p className="subtle">{m.archivedNotice}</p>
      {!query.data.readable && <p role="alert">{m.unreadableConversation}</p>}
      {query.data.turns.map((turn, index) => (
        <article className="turn" key={index}>
          {turn.user.trim() && (
            <div className="user-message">
              <div className="message-label">{m.you}</div>
              <p>{turn.user}</p>
            </div>
          )}
          <div className="assistant-message">
            <StoryReply
              narration={turn.narration}
              dialogue={turn.assistant}
              name={m.character}
            />
          </div>
        </article>
      ))}
      <Link className="button" to="/new">
        {m.newConversation}
      </Link>
    </section>
  )
}
