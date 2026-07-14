import type { ReactNode } from 'react';
import {
  MemoryRouter,
  Route,
  Routes,
  UNSAFE_LocationContext,
  UNSAFE_NavigationContext,
  UNSAFE_RouteContext,
} from 'react-router-dom';

import { cn } from '@/lib/utils';

import { CHAT_MAIN_CLASS } from './chatPageStyles';
import { useChatSession } from './useChatSession';
import Composer from './components/Composer';
import MessageList from './components/MessageList';
import ChatDialogs from './components/ChatDialogs';

/**
 * The chat conversation surface (empty state + messages + composer) without the
 * app sidebar/header, reusing the exact same components as the full ChatPage.
 */
function EmbeddedChatSurface() {
  const chat = useChatSession({ anonymous: true });
  return (
    <div className={cn(CHAT_MAIN_CLASS, 'h-full w-full')}>
      <MessageList chat={chat} />
      <Composer chat={chat} />
      <ChatDialogs chat={chat} />
    </div>
  );
}

/**
 * Resets React Router's context so a nested router is allowed to mount. React
 * Router forbids rendering a <Router> inside another <Router>; clearing the
 * location/navigation/route contexts makes the inner MemoryRouter look like a
 * fresh top-level router, and lets its <Routes> match from "/" (not relative to
 * the surrounding /site route).
 */
function IsolatedRouterBoundary({ children }: { children: ReactNode }) {
  return (
    <UNSAFE_NavigationContext.Provider value={null as never}>
      <UNSAFE_LocationContext.Provider value={null as never}>
        <UNSAFE_RouteContext.Provider
          value={{ outlet: null, matches: [], isDataRoute: false } as never}
        >
          {children}
        </UNSAFE_RouteContext.Provider>
      </UNSAFE_LocationContext.Provider>
    </UNSAFE_NavigationContext.Provider>
  );
}

/**
 * Anonymous, embeddable chat used on the marketing site's last screen. It runs
 * inside its own in-memory router so starting a conversation (draft → session)
 * only mutates this isolated history — the browser URL never leaves the site.
 */
export default function EmbeddedChat({ agentId }: { agentId: string }) {
  return (
    <IsolatedRouterBoundary>
      <MemoryRouter initialEntries={[`/workspace/chat/draft/${agentId}`]}>
        <Routes>
          <Route
            path="/workspace/chat/draft/:draftAgentId"
            element={<EmbeddedChatSurface />}
          />
          <Route
            path="/workspace/chat/:sessionId"
            element={<EmbeddedChatSurface />}
          />
          <Route path="*" element={<EmbeddedChatSurface />} />
        </Routes>
      </MemoryRouter>
    </IsolatedRouterBoundary>
  );
}
