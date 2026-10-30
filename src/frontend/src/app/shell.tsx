import { useState } from 'react'
import { NavLink, Route, Routes, useNavigate } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import {
  ArrowUpRight,
  Image,
  Menu,
  MessageSquare,
  Plus,
  Settings2,
  Sparkles,
} from 'lucide-react'
import { listConversations } from '../api/generated/sdk.gen'
import { Button } from '../components/ui/button'
import { ConversationPage, NewConversation } from '../features/conversations'
import { ImagesPage } from '../features/images'
import { RuntimePage } from '../features/runtime'
import messages from './messages.json'

export function App() {
  const navigate = useNavigate()
  const [navigation, setNavigation] = useState(false)
  const conversations = useQuery({
    queryKey: ['conversations'],
    queryFn: async () => (await listConversations()).data!,
  })
  return (
    <div className="workspace">
      <aside className={`navigation ${navigation ? 'is-open' : ''}`}>
        <NavLink to="/" className="brand">
          <span className="brand-icon">
            <Sparkles size={19} />
          </span>
          {messages.brand}
        </NavLink>
        <Button
          className="primary new-conversation"
          onClick={() => {
            navigate('/new')
            setNavigation(false)
          }}
        >
          <Plus size={17} />
          {messages.newConversation}
        </Button>
        <div className="section-label">{messages.workspace}</div>
        <nav onClick={() => setNavigation(false)}>
          <NavLink to="/" end>
            <MessageSquare size={17} />
            {messages.conversations}
          </NavLink>
          <NavLink to="/images">
            <Image size={17} />
            {messages.images}
          </NavLink>
          <NavLink to="/runtime">
            <Settings2 size={17} />
            {messages.runtime}
          </NavLink>
        </nav>
        <div className="section-label recent-label">{messages.recent}</div>
        <div className="recent-list">
          {conversations.data?.map((conversation) => (
            <NavLink
              key={conversation.id}
              to={`/conversations/${conversation.id}`}
              onClick={() => setNavigation(false)}
            >
              <MessageSquare size={14} />
              <span>{conversation.definition.title}</span>
              <ArrowUpRight size={13} />
            </NavLink>
          ))}
        </div>
        <div className="local-indicator">
          <span />
          {messages.local}
          <small>{messages.onComputer}</small>
        </div>
      </aside>
      <main className="main-area">
        <header className="mobile-header">
          <Button
            aria-label={messages.showNavigation}
            onClick={() => setNavigation(!navigation)}
          >
            <Menu size={19} />
          </Button>
          <span>{messages.brand}</span>
        </header>
        {conversations.error && (
          <p role="alert" className="error">
            {messages.failed}
          </p>
        )}
        <Routes>
          <Route path="/" element={<Welcome />} />
          <Route path="/new" element={<NewConversation />} />
          <Route
            path="/conversations/:identity"
            element={<ConversationPage />}
          />
          <Route path="/images" element={<ImagesPage />} />
          <Route path="/runtime" element={<RuntimePage />} />
          <Route path="*" element={<Welcome />} />
        </Routes>
      </main>
    </div>
  )
}

function Welcome() {
  const navigate = useNavigate()
  return (
    <div className="welcome">
      <div className="eyebrow">{messages.welcomeEyebrow}</div>
      <div className="welcome-symbol">
        <Sparkles size={38} strokeWidth={1.2} />
      </div>
      <h1>{messages.emptyTitle}</h1>
      <p>{messages.emptyBody}</p>
      <Button className="primary" onClick={() => navigate('/new')}>
        <Plus size={18} />
        {messages.newConversation}
      </Button>
      <div className="welcome-cards">
        <div>
          <MessageSquare size={21} />
          <h3>{messages.followTitle}</h3>
          <p>{messages.followBody}</p>
        </div>
        <div>
          <Image size={21} />
          <h3>{messages.pictureTitle}</h3>
          <p>{messages.pictureBody}</p>
        </div>
      </div>
      <span className="welcome-footer">{messages.tagline}</span>
    </div>
  )
}
