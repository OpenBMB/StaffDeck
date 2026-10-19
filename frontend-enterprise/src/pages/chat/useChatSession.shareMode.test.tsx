// @vitest-environment jsdom

import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import type { AgentProfileRead, ChatSession } from '@/types';

import { useChatSession } from './useChatSession';

const AUTH_STORAGE_KEY = 'ultrarag_auth';

const shareSession: ChatSession = {
  id: 'session-share-1',
  tenant_id: 'tenant_demo',
  agent_id: 'agent-share-1',
  status: 'active',
  title: '分享会话',
  updated_at: '2026-08-01T00:00:00Z',
};

const shareAgent = {
  id: 'agent-share-1',
  tenant_id: 'tenant_demo',
  name: '分享员工',
  is_overall: false,
  status: 'active',
  metadata: {},
  resources: [],
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
} as unknown as AgentProfileRead;

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

function stubShareFetch(agent: AgentProfileRead = shareAgent) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/api/chat/agents')) return jsonResponse([agent]);
    if (url.includes('/api/chat/sessions')) return jsonResponse([shareSession]);
    if (url.includes('/api/chat/')) return jsonResponse([]);
    if (url.includes('/api/enterprise/')) return jsonResponse([]);
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function renderShareChatSession() {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <I18nProvider>
      <MemoryRouter initialEntries={['/share/tok']}>
        <Routes>
          <Route path="/share/:token" element={<>{children}</>} />
          <Route path="*" element={<>{children}</>} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>
  );
  return renderHook(
    () => useChatSession({ anonymous: true, shareMode: true }),
    { wrapper },
  );
}

beforeEach(() => {
  window.localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify({
    token: 'share-token-1',
    user: { id: 'share_guest_x', tenant_id: 'tenant_demo', username: 'share_guest_x', role: 'member' },
  }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
  vi.restoreAllMocks();
});

describe('useChatSession shareMode send guard', () => {
  it('loadSessions 确认过的会话（如分享 promoted 会话）不会触发「从待回答列表回复」守卫', async () => {
    const fetchMock = stubShareFetch();
    const { result } = renderShareChatSession();

    await waitFor(() => {
      expect(result.current.agents.some((item) => item.id === 'agent-share-1')).toBe(true);
    }, { timeout: 3000 });

    act(() => {
      result.current.setInput('第二条问题');
    });
    await act(async () => {
      await result.current.send();
    });

    const urls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(urls.some((url) => url.includes('/api/chat/stream'))).toBe(true);
  });

  it('免费员工也不向分享访客提示「你还没有自己的模型」', async () => {
    const freeAgent = {
      ...shareAgent,
      free_model_enabled: true,
      free_model_remaining: 7,
    } as unknown as AgentProfileRead;
    stubShareFetch(freeAgent);
    const { result } = renderShareChatSession();

    await waitFor(() => {
      expect(result.current.displayedAgent?.id).toBe('agent-share-1');
    }, { timeout: 3000 });

    // 分享链接里的模型在创建分享时已锁定，与访客自己的模型/免费额度无关
    expect(result.current.freeModelNoticeText).toBe('');
  });
});
