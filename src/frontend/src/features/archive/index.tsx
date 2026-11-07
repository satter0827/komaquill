import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router'
import * as q from '../../api/generated/@tanstack/react-query.gen'
import { StoryReply } from '../../shared/story/reply'
import { useMessages } from '../../shared/preferences'
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
  if (query.error && !query.data) return notice
  if (!query.data) return <p>{m.loading}</p>
  return (
    <section className="page archive-page">
      {notice}
      <h1>{query.data.title}</h1>
      <p className="subtle">{m.archivedNotice}</p>
      {!query.data.readable && <p role="alert">{m.unreadableConversation}</p>}
      {query.data.turns.map((turn, index) => (
        <article className="turn" key={index}>
          <div className="user-message">
            <div className="message-label">{m.you}</div>
            <p>{turn.user}</p>
          </div>
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
