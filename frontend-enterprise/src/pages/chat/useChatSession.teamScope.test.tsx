// @vitest-environment jsdom

import { cleanup, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
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
  updated_at: '2026-08-01T00:00:00Z',
};

const employeeSession: ChatSession = {
  id: 'session-emp-1',
  tenant_id: 'tenant_demo',
  agent_id: 'agent-1',
  status: 'active',
  updated_at: '2026-08-01T00:00:00Z',
};

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
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
) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <I18nProvider>
      <MemoryRouter initialEntries={[initialPath]}>
        <Routes>
          <Route path="/workspace/chat/:sessionId" element={<>{children}</>} />
          <Route path="/workspace/chat" element={<>{children}</>} />
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
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe('useChatSession team scope', () => {
  it('keeps global employee scope unchanged for an embedded team room', async () => {
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, 'agent-1');
    stubChatFetch([employeeSession]);
    const { result } = renderChatSession('/workspace/chat', {
      sessionId: teamSession.id,
      embedded: true,
    });

    await waitFor(() => {
      expect(result.current.currentSession?.id).toBe(teamSession.id);
    });
    expect(window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY)).toBe('agent-1');
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

  it('never exposes team sessions in the global chat sidebar', async () => {
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, 'team:team-1');
    stubChatFetch([teamSession, employeeSession]);
    const { result } = renderChatSession('/workspace/chat');

    await waitFor(() => {
      expect(result.current.sessionsLoading).toBe(false);
    });
    expect(result.current.visibleSidebarSessions).toEqual([]);
  });
});
