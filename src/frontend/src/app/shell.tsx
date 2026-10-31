import { useState } from 'react'
import { Link, NavLink, Route, Routes } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import {
  BookOpen,
  Image,
  Menu,
  MessageSquare,
  Plus,
  Settings2,
  Sparkles,
  UserRound,
  Users,
} from 'lucide-react'
import { listConversations } from '../api/generated/sdk.gen'
import { Button } from '../components/ui/button'
import { ConversationPage } from '../features/conversations'
import { NewConversation } from '../features/start'
import {
  LibraryPage,
  PlayerPage,
  loadPresets,
  PresetCard,
} from '../features/library'
import { ImagesPage } from '../features/images'
import { PreferencesPage } from '../features/preferences'
import { RuntimePage } from '../features/runtime'
import { useFailure, useMessages, usePreferences } from './preferences'

export function App() {
  const m = useMessages()
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
          {m.brand}
        </NavLink>
        <Link
          className="button primary new-conversation"
          to="/new"
          onClick={() => setNavigation(false)}
        >
          <Plus size={17} />
          {m.newConversation}
        </Link>
        <div className="section-label">{m.workspace}</div>
        <nav onClick={() => setNavigation(false)}>
          <NavLink to="/" end>
            <MessageSquare size={17} />
            {m.play}
          </NavLink>
          <NavLink to="/characters">
            <Users size={17} />
            {m.characters}
          </NavLink>
          <NavLink to="/stories">
            <BookOpen size={17} />
            {m.stories}
          </NavLink>
          <NavLink to="/player">
            <UserRound size={17} />
            {m.players}
          </NavLink>
        </nav>
        <div className="section-label recent-label">{m.recent}</div>
        <div className="recent-list">
          {conversations.data?.map((conversation) => (
            <NavLink
              key={conversation.id}
              to={`/conversations/${conversation.id}`}
              onClick={() => setNavigation(false)}
            >
              <MessageSquare size={14} />
              <span>{conversation.definition.title}</span>
            </NavLink>
          ))}
        </div>
        <nav
          className="secondary-navigation"
          onClick={() => setNavigation(false)}
        >
          <NavLink to="/images">
            <Image size={17} />
            {m.images}
          </NavLink>
          <NavLink to="/settings">
            <Settings2 size={17} />
            {m.preferences}
          </NavLink>
        </nav>
        <div className="local-indicator">
          <span />
          {m.local}
          <small>{m.onComputer}</small>
        </div>
      </aside>
      <main className="main-area">
        <header className="mobile-header">
          <Button
            aria-label={m.showNavigation}
            onClick={() => setNavigation(!navigation)}
          >
            <Menu size={19} />
          </Button>
          <span>{m.brand}</span>
        </header>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/new" element={<NewConversation />} />
          <Route
            path="/conversations/:identity"
            element={<ConversationPage />}
          />
          <Route path="/player" element={<PlayerPage />} />
          {(['character', 'story'] as const).map((kind) => (
            <Route
              key={kind}
              path={`/${kind === 'story' ? 'stories' : 'characters'}`}
            >
              <Route index element={<LibraryPage kind={kind} />} />
              <Route path=":identity" element={<LibraryPage kind={kind} />} />
            </Route>
          ))}
          <Route path="/settings" element={<PreferencesPage />} />
          <Route path="/images" element={<ImagesPage />} />
          <Route path="/runtime" element={<RuntimePage />} />
          <Route path="*" element={<Home />} />
        </Routes>
      </main>
    </div>
  )
}

function Home() {
  const m = useMessages()
  const failure = useFailure()
  const { value } = usePreferences()
  const stories = useQuery({
    queryKey: ['stories'],
    queryFn: () => loadPresets('story'),
  })
  const conversations = useQuery({
    queryKey: ['conversations'],
    queryFn: async () => (await listConversations()).data!,
  })
  const visibleStories = stories.data?.filter(
    (item) => !item.sample || item.sample_locale === value.locale,
  )
  return (
    <section className="page home-page">
      <div className="home-intro">
        <div className="eyebrow">{m.welcomeEyebrow}</div>
        <h1>{m.emptyTitle}</h1>
        <p>{m.emptyBody}</p>
        <Link className="button primary" to="/new">
          <Plus size={17} />
          {m.newConversation}
        </Link>
      </div>
      {[stories, conversations].map(
        (query, index) =>
          query.error && (
            <div key={index}>
              <p role="alert" className="error">
                {failure(query.error)}
              </p>
              <Button
                onClick={() => void query.refetch()}
                disabled={query.isFetching}
              >
                {m.refresh}
              </Button>
            </div>
          ),
      )}
      {!!conversations.data?.length && (
        <div className="home-section">
          <h2>{m.continueStory}</h2>
          <div className="continue-grid">
            {conversations.data.map((item) => (
              <Link
                className="continue-card"
                key={item.id}
                to={`/conversations/${item.id}`}
              >
                <MessageSquare size={20} />
                <strong>{item.definition.title}</strong>
                <span>{item.turns?.at(-1)?.assistant}</span>
              </Link>
            ))}
          </div>
        </div>
      )}
      <div className="home-section">
        <div className="eyebrow">{m.yourStories}</div>
        {visibleStories?.length === 0 && (
          <div className="empty-card">
            <p>{m.noPresets}</p>
            <Link className="text-button" to="/stories/new">
              {m.new} →
            </Link>
          </div>
        )}
        <div className="preset-grid">
          {visibleStories?.map((story) => (
            <PresetCard key={story.id} preset={story} kind="story" />
          ))}
        </div>
      </div>
      <p className="welcome-footer">{m.welcomeFooter}</p>
    </section>
  )
}
