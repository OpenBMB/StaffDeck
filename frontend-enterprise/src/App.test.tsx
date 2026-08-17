// @vitest-environment jsdom

import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ENTERPRISE_AGENT_STORAGE_KEY } from '@/lib/agent-scope-storage';
import { I18nProvider } from '@/i18n';
import type { AgentProfileRead, TeamRead } from '@/types';

import App from './App';

const AUTH_STORAGE_KEY = 'ultrarag_auth';

const authUser = {
  id: 'user-1',
  tenant_id: 'tenant_demo',
  username: 'demo',
  role: 'admin',
};

const agent: AgentProfileRead = {
  id: 'agent-1',
  tenant_id: 'tenant_demo',
  name: '小艾',
  is_overall: false,
  status: 'active',
  metadata: {},
  resources: [],
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
};

const team: TeamRead = {
  id: 'team-1',
  tenant_id: 'tenant_demo',
  name: '增长团队',
  description: '',
  owner_user_id: 'user-1',
  config: {},
  status: 'active',
  members: [],
  created_at: '2026-08-01T00:00:00Z',
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

// 提供一个可用模型配置，避免聊天页弹出模型配置引导（jsdom 下其内部数据为空会报错）。
const modelConfig = {
  id: 'model-1',
  tenant_id: 'tenant_demo',
  name: '默认模型',
  provider: 'openai',
  api_protocol: 'openai_chat_completions',
  api_key_masked: 'sk-***',
  model: 'gpt-test',
  temperature: 0.7,
  max_output_tokens: 1024,
  extra_body: {},
  protocol_options: {},
  legacy_unmapped_options: {},
  trust_status: 'verified',
  verification_attempt_status: 'idle',
  config_revision: 1,
  security_revision: 1,
  is_default: true,
  enabled: true,
};

function stubAppFetch() {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method || 'GET').toUpperCase();
    if (method === 'POST' && url.includes('/tl/session')) {
      return jsonResponse({ session_id: 'session-tl-1' });
    }
    if (url.includes('/api/auth/me')) return jsonResponse(authUser);
    if (url.includes('/api/enterprise/agents')) return jsonResponse([agent]);
    if (/\/api\/enterprise\/teams\/team-1\/(tasks|blackboard|events)/.test(url)) {
      return jsonResponse([]);
    }
    if (url.includes('/api/enterprise/teams/team-1')) return jsonResponse(team);
    if (url.includes('/api/enterprise/teams')) return jsonResponse([team]);
    if (url.includes('/api/enterprise/model-configs')) return jsonResponse([modelConfig]);
    if (url.includes('/api/chat/')) return jsonResponse([]);
    if (url.includes('/api/enterprise/')) return jsonResponse([]);
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function stubBrowserApis() {
  if (!window.matchMedia) {
    window.matchMedia = ((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    })) as typeof window.matchMedia;
  }
  if (!Element.prototype.hasPointerCapture) {
    Element.prototype.hasPointerCapture = () => false;
  }
  if (!Element.prototype.releasePointerCapture) {
    Element.prototype.releasePointerCapture = () => {};
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = () => {};
  }
}

beforeEach(() => {
  stubBrowserApis();
  window.localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify({ token: 'token-1', user: authUser }));
  window.localStorage.setItem('staffdeck_onboarding_guide_seen', '1');
  window.localStorage.setItem('staffdeck_quick_start_guide_seen', '1');
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
  window.history.pushState({}, '', '/');
});

describe('App team scope selection', () => {
  it('opens the team group in the chat app when a team is selected', async () => {
    const user = userEvent.setup();
    const fetchMock = stubAppFetch();
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, 'agent-1');
    window.history.pushState({}, '', '/enterprise/agents');
    render(<I18nProvider><App /></I18nProvider>);

    const switcher = await screen.findByLabelText('切换当前员工');
    await user.click(switcher);
    const menu = await screen.findByRole('menu');
    const teamItem = within(menu)
      .getAllByRole('menuitem')
      .find((item) => item.textContent?.includes('增长团队'));
    expect(teamItem).toBeTruthy();
    await user.click(teamItem!);

    await waitFor(() => {
      expect(window.location.pathname).toBe('/workspace/chat/session-tl-1');
    });
    const postCall = fetchMock.mock.calls.find(([, init]) => (
      (init?.method || '').toUpperCase() === 'POST'
    ));
    expect(String(postCall?.[0])).toContain('/api/enterprise/teams/team-1/tl/session');
    expect(window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY)).toBe('team:team-1');
  });

  it('keeps a preset team scope instead of resetting it to an employee on agents load', async () => {
    stubAppFetch();
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, 'team:team-1');
    window.history.pushState({}, '', '/enterprise/agents');
    render(<I18nProvider><App /></I18nProvider>);

    const switcher = await screen.findByLabelText('切换当前员工');
    await waitFor(() => {
      expect(switcher.textContent).toContain('当前团队');
      expect(switcher.textContent).toContain('增长团队');
    });
    expect(window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY)).toBe('team:team-1');
  });
});
