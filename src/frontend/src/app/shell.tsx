import { useQuery } from '@tanstack/react-query'
import {
  BookOpen,
  Menu,
  MessageSquare,
  Plus,
  Settings2,
  EyeOff,
  UserRound,
  Users,
} from 'lucide-react'
import { useRef, useState } from 'react'
import { Dialog, Modal } from '../shared/ui/popup'
import { useMediaQuery } from '../shared/viewport'
import { Link, NavLink, Route, Routes, useLocation } from 'react-router'
import * as q from '../api/generated/@tanstack/react-query.gen'
import { Button } from '../shared/ui/button'
import { AlbumIcon, AtelierMark } from '../shared/ui/atelier-icons'
import {
  ArchivedConversationPage,
  ArchivedConversations,
} from '../features/archive/index'
import { ConversationPage } from '../features/conversations/index'
import { ImagesPage } from '../features/images/index'
import { LibraryPage, PlayerPage, PresetCard } from '../features/library/index'
import { PreferencesPage } from '../features/preferences/index'
import { ImageActivityProvider } from '../shared/image-activity'
import { presetsForLocale } from '../shared/story/presets'
import { ConnectionSummary, useConnectionSummary } from '../shared/availability'
import { RuntimePage } from '../features/runtime/index'
import { NewConversation } from '../features/conversations/start'
import { useFailure, useMessages, usePreferences } from '../shared/preferences'

export function App() {
  return (
    <ImageActivityProvider>
      <Application />
    </ImageActivityProvider>
  )
}

function Application() {
  const m = useMessages()
  const connectionSummary = useConnectionSummary()
  const [navigation, setNavigation] = useState(false)
  const [privateScreen, setPrivateScreen] = useState(false)
  const [privacyHandle] = useState(() => Dialog.createHandle())
  const navigationTrigger = useRef<HTMLButtonElement>(null)
  const narrow = useMediaQuery('(max-width: 850px)')
  return (
    <Dialog.Root
      open={navigation && narrow && !privateScreen}
      onOpenChange={setNavigation}
    >
      <div className="application">
        <div className="workspace">
          {!narrow && (
            <Navigation
              onNavigate={() => setNavigation(false)}
              privacyHandle={privacyHandle}
            />
          )}
          <main className="main-area">
            <header className="mobile-header">
              <Dialog.Trigger
                ref={navigationTrigger}
                render={
                  <Button variant="ghost" aria-label={m.showNavigation} />
                }
              >
                <Menu size={20} />
                <span
                  className="connection-indicator"
                  data-warning={connectionSummary.failed || undefined}
                />
              </Dialog.Trigger>
              {narrow && (
                <Modal
                  label={m.workspace}
                  closeLabel={m.closePanel}
                  className="navigation-modal"
                >
                  <Navigation
                    onNavigate={() => setNavigation(false)}
                    privacyHandle={privacyHandle}
                  />
                </Modal>
              )}
              <span className="mobile-brand">
                <AtelierMark size={18} />
                {m.brand}
              </span>
            </header>
            <Routes>
              <Route path="/" element={<Home />} />
              <Route path="/new" element={<NewConversation />} />
              <Route
                path="/conversations/:identity"
                element={<ConversationPage screenHidden={privateScreen} />}
              />
              <Route
                path="/archive/conversations/:identity"
                element={<ArchivedConversationPage />}
              />
              <Route path="/player" element={<PlayerPage />} />
              {(['character', 'story'] as const).map((kind) => (
                <Route
                  key={kind}
                  path={`/${kind === 'story' ? 'stories' : 'characters'}`}
                >
                  <Route index element={<LibraryPage kind={kind} />} />
                  <Route
                    path=":identity"
                    element={<LibraryPage kind={kind} />}
                  />
                </Route>
              ))}
              <Route
                path="/settings"
                element={
                  <PreferencesPage connections={<RuntimePage embedded />} />
                }
              />
              <Route path="/images" element={<ImagesPage />} />
              <Route path="/runtime" element={<RuntimePage />} />
              <Route path="*" element={<Home />} />
            </Routes>
          </main>
        </div>
        <Dialog.Root
          open={privateScreen}
          handle={privacyHandle}
          disablePointerDismissal
          onOpenChange={(open, details) => {
            if (!open && details.reason !== 'close-press') {
              details.cancel()
              return
            }
            setPrivateScreen(open)
          }}
        >
          <Dialog.Portal>
            <Dialog.Popup
              className="privacy-overlay"
              finalFocus={() =>
                document.querySelector<HTMLButtonElement>('.privacy-button') ??
                navigationTrigger.current ??
                true
              }
            >
              <Dialog.Title className="sr-only">{m.hideScreen}</Dialog.Title>
              <Dialog.Close render={<Button variant="primary" />}>
                {m.showScreen}
              </Dialog.Close>
            </Dialog.Popup>
          </Dialog.Portal>
        </Dialog.Root>
      </div>
    </Dialog.Root>
  )
}

function Navigation({
  onNavigate,
  privacyHandle,
}: {
  onNavigate: () => void
  privacyHandle: ReturnType<typeof Dialog.createHandle>
}) {
  const m = useMessages()
  const { pathname } = useLocation()
  const playing =
    pathname === '/' ||
    pathname === '/new' ||
    pathname.startsWith('/conversations/') ||
    pathname.startsWith('/archive/conversations/')
  const conversations = useQuery({
    ...q.listConversationsOptions(),
  })
  return (
    <aside className="navigation">
      <NavLink to="/" className="brand" onClick={onNavigate}>
        <span className="brand-icon">
          <AtelierMark />
        </span>
        {m.brand}
      </NavLink>
      <Link
        className="button primary new-conversation"
        to="/new"
        onClick={() => onNavigate()}
      >
        <Plus size={17} />
        {m.newConversation}
      </Link>
      <div className="section-label">{m.workspace}</div>
      <nav onClick={() => onNavigate()}>
        <Link
          to="/"
          className={playing ? 'active' : undefined}
          aria-current={playing ? 'location' : undefined}
        >
          <MessageSquare size={17} />
          {m.play}
        </Link>
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
            onClick={() => onNavigate()}
          >
            <MessageSquare size={14} />
            <span>{conversation.definition.title}</span>
          </NavLink>
        ))}
      </div>
      <ConnectionSummary onNavigate={onNavigate} />
      <nav className="secondary-navigation">
        <NavLink to="/images" onClick={onNavigate}>
          <AlbumIcon />
          {m.images}
        </NavLink>
        <NavLink to="/settings" onClick={onNavigate}>
          <Settings2 size={17} />
          {m.preferences}
        </NavLink>
        <Dialog.Trigger
          handle={privacyHandle}
          render={<Button className="privacy-button" />}
        >
          <EyeOff size={18} aria-hidden="true" />
          {m.hideScreen}
        </Dialog.Trigger>
      </nav>
    </aside>
  )
}

function Home() {
  const m = useMessages()
  const failure = useFailure()
  const { value } = usePreferences()
  const stories = useQuery({
    ...q.listStoriesOptions(),
  })
  const conversations = useQuery({
    ...q.listConversationsOptions(),
  })
  const visibleStories = presetsForLocale(stories.data, value.locale)
  return (
    <section className="page home-page">
      <div className="home-intro">
        <h1>{m.emptyTitle}</h1>
        <p>{m.emptyBody}</p>
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
      <NewConversation compact />
      <div className="home-section">
        <h2>{m.stories}</h2>
        {stories.data && visibleStories.length === 0 && (
          <div className="empty-card">
            <p>{m.noPresets}</p>
            <Link className="text-button" to="/stories/new">
              {m.new} →
            </Link>
          </div>
        )}
        <div className="preset-grid">
          {visibleStories.map((story) => (
            <PresetCard key={story.id} preset={story} kind="story" />
          ))}
        </div>
      </div>
      <ArchivedConversations />
    </section>
  )
}
