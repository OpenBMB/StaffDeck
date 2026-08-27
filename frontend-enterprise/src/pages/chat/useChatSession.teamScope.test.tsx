// @vitest-environment jsdom

import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { type ReactNode, useEffect } from 'react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import { ENTERPRISE_AGENT_STORAGE_KEY } from '@/lib/agent-scope-storage';
import type { ChatSession } from '@/types';

import { useChatSession } from './useChatSession';

const AUTH_STORAGE_KEY = 'ultrarag_auth';

const teamSession: ChatSession = {
  id: 'session-team-1',
  tenant_id: 'tenant_demo',
  status: 'active',
  team_id: 'team-1',
  team_name: '增长团队',
  title: '团队 增长团队 · TL 对话',
  updated_at: '2026-08-01T00:00:00Z',
};

const employeeSession: ChatSession = {
  id: 'session-emp-1',
  tenant_id: 'tenant_demo',
  agent_id: 'agent-1',
  status: 'active',
  updated_at: '2026-08-01T00:00:00Z',
};

const scheduledSession: ChatSession = {
  id: 'session-scheduled-1',
  tenant_id: 'tenant_demo',
  agent_id: 'agent-1',
  status: 'active',
  is_scheduled: true,
  updated_at: '2026-08-01T00:01:00Z',
};

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

function jsonErrorResponse(status: number, body: unknown): Response {
  return {
    ok: false,
    status,
    statusText: 'Not Found',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

function stubChatFetch(sessions: ChatSession[]) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/api/chat/sessions/session-team-1?')) return jsonResponse(teamSession);
    if (url.includes('/api/chat/sessions/session-emp-1?')) return jsonResponse(employeeSession);
    if (url.includes('/api/chat/sessions?')) return jsonResponse(sessions);
    if (url.includes('/api/chat/')) return jsonResponse([]);
    if (url.includes('/api/enterprise/')) return jsonResponse([]);
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function renderChatSession(
  initialPath: string,
  options: Parameters<typeof useChatSession>[0] = {},
  onLocationChange: (pathname: string) => void = () => undefined,
) {
  function LocationReporter() {
    const location = useLocation();
    useEffect(() => {
      onLocationChange(location.pathname);
    }, [location.pathname, onLocationChange]);
    return null;
  }

  const wrapper = ({ children }: { children: ReactNode }) => (
    <I18nProvider>
      <MemoryRouter initialEntries={[initialPath]}>
        <Routes>
          <Route path="/workspace/chat/:sessionId" element={<><LocationReporter />{children}</>} />
          <Route path="/workspace/chat" element={<><LocationReporter />{children}</>} />
          <Route path="*" element={<LocationReporter />} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>
  );
  return renderHook(() => useChatSession(options), { wrapper });
}

beforeEach(() => {
  window.localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify({
    token: 'token-1',
    user: { id: 'user-1', tenant_id: 'tenant_demo', username: 'demo', role: 'admin' },
  }));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe('useChatSession team scope', () => {
  it('syncs the shared scope for an active team group', async () => {
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, 'agent-1');
    stubChatFetch([teamSession, employeeSession]);
    renderChatSession('/workspace/chat/session-team-1');

    await waitFor(() => {
      expect(window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY)).toBe('team:team-1');
    });
  });

  it('keeps the employee scope for regular employee sessions', async () => {
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, 'agent-1');
    stubChatFetch([teamSession, employeeSession]);
    renderChatSession('/workspace/chat/session-emp-1');

    await waitFor(() => {
      expect(window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY)).toBe('agent-1');
    });
    // 给员工会话留足同步窗口，确认不会被误写成团队作用域。
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY)).toBe('agent-1');
  });

  it('filters the unified session list to a selected team group', async () => {
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, 'team:team-1');
    stubChatFetch([teamSession, employeeSession]);
    const { result } = renderChatSession('/workspace/chat');

    await waitFor(() => {
      expect(result.current.sessionsLoading).toBe(false);
    });
    expect(result.current.visibleSidebarSessions.map((session) => session.id)).toEqual(['session-team-1']);
  });

  it('keeps polling messages after a team leader turn hands work to members', async () => {
    vi.useFakeTimers();
    const fetchMock = stubChatFetch([teamSession, employeeSession]);
    renderChatSession('/workspace/chat/session-team-1');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    const messageRequestCount = () => fetchMock.mock.calls.filter(([input]) => (
      String(input).includes('/api/chat/sessions/session-team-1/messages?')
    )).length;
    expect(messageRequestCount()).toBe(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_100);
    });
    expect(messageRequestCount()).toBeGreaterThan(1);
  });

  it('does not navigate away when polling discovers a scheduled background session', async () => {
    vi.useFakeTimers();
    let sessionListRequests = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/chat/sessions/session-emp-1?')) return jsonResponse(employeeSession);
      if (url.includes('/api/chat/sessions?')) {
        sessionListRequests += 1;
        return jsonResponse(sessionListRequests === 1 ? [employeeSession] : [employeeSession, scheduledSession]);
      }
      if (url.includes('/api/chat/')) return jsonResponse([]);
      if (url.includes('/api/enterprise/')) return jsonResponse([]);
      return jsonResponse({});
    }));
    const locations: string[] = [];
    renderChatSession('/workspace/chat/session-emp-1', {}, (pathname) => locations.push(pathname));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_500);
    });

    expect(locations).toEqual(['/workspace/chat/session-emp-1']);
  });

  it('keeps the current route when a failed step leaves its session unavailable', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/chat/sessions/session-emp-1/messages?')) {
        return jsonErrorResponse(404, { detail: 'Session not found' });
      }
      if (url.includes('/api/chat/sessions/session-emp-1?')) return jsonResponse(employeeSession);
      if (url.includes('/api/chat/sessions?')) return jsonResponse([employeeSession]);
      if (url.includes('/api/chat/')) return jsonResponse([]);
      if (url.includes('/api/enterprise/')) return jsonResponse([]);
      return jsonResponse({});
    }));
    const locations: string[] = [];
    renderChatSession('/workspace/chat/session-emp-1', {}, (pathname) => locations.push(pathname));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });

    expect(locations).toEqual(['/workspace/chat/session-emp-1']);
  });
});
