import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router'
import * as q from '../../api/generated/@tanstack/react-query.gen'
import { StoryReply } from '../../shared/story/reply'
import { useFailure, useMessages } from '../../shared/preferences'

export function ArchivedConversations() {
  const m = useMessages()
  const failure = useFailure()
  const query = useQuery({
    ...q.listArchivedConversationsOptions(),
  })
  if (query.error)
    return (
      <p role="alert" className="error">
        {failure(query.error)}
      </p>
    )
  if (!query.data?.length) return null
  return (
    <section>
      <h2>{m.archivedTitle}</h2>
      <div className="recent-cards">
        {query.data.map((item) => (
          <Link key={item.id} to={`/archive/conversations/${item.id}`}>
            {item.title}
          </Link>
        ))}
      </div>
    </section>
  )
}

export function ArchivedConversationPage() {
  const { identity = '' } = useParams()
  const m = useMessages()
  const failure = useFailure()
  const query = useQuery({
    ...q.getArchivedConversationOptions({ path: { identity } }),
  })
  if (query.error)
    return (
      <p role="alert" className="error">
        {failure(query.error)}
      </p>
    )
  if (!query.data) return <p>{m.loading}</p>
  return (
    <section className="page">
      <h1>{query.data.title}</h1>
      <p>{m.archivedNotice}</p>
      <Link to="/new">{m.newConversation}</Link>
      {!query.data.readable && <p role="alert">{m.unreadableConversation}</p>}
      {query.data.turns.map((turn, index) => (
        <article className="turn" key={index}>
          <div className="user-message">
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
    </section>
  )
}
