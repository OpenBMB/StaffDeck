// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import { TooltipProvider } from '@/components/ui/tooltip';
import { ENTERPRISE_AGENT_STORAGE_KEY } from '@/lib/agent-scope-storage';
import type { AgentProfileRead } from '@/types';

import AgentsPage from './AgentsPage';

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

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

beforeEach(() => {
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
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe('AgentsPage team scope compatibility', () => {
  it('renders gracefully when the stored scope is a team', async () => {
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, 'team:team-1');
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/enterprise/agents')) return jsonResponse([agent]);
      return jsonResponse([]);
    }));

    render(
      <I18nProvider>
        <TooltipProvider>
          <MemoryRouter>
            <AgentsPage
              currentUser={{ id: 'user-1', tenant_id: 'tenant_demo', username: 'demo', role: 'admin' }}
            />
          </MemoryRouter>
        </TooltipProvider>
      </I18nProvider>,
    );

    // 团队作用域匹配不到任何员工：不高亮、不报错，员工列表照常渲染。
    expect((await screen.findByText('小艾')).textContent).toBeTruthy();
  });
});
