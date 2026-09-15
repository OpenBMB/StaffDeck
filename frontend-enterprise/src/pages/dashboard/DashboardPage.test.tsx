// @vitest-environment jsdom

import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import { TooltipProvider } from '@/components/ui/tooltip';
import { notify } from '@/components/ui';
import { ENTERPRISE_AGENT_STORAGE_KEY } from '@/lib/agent-scope-storage';
import type { AgentProfileRead } from '@/types';

import DashboardPage from './DashboardPage';

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
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe('DashboardPage team scope compatibility', () => {
  const mount = () => render(<I18nProvider><TooltipProvider><MemoryRouter>
    <DashboardPage currentUser={{ id: 'user-1', tenant_id: 'tenant_demo', username: 'demo', role: 'admin' }} isAdmin />
  </MemoryRouter></TooltipProvider></I18nProvider>);

  it('renders successful panels while another fails or remains pending, then retries', async () => {
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, agent.id);
    let attempts = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/enterprise/agents?')) return jsonResponse([agent]);
      if (url.includes('/knowledge-bases?')) {
        if (++attempts === 1) throw new Error('Knowledge unavailable');
        return jsonResponse([]);
      }
      if (url.includes('/general-skills?')) return new Promise<Response>(() => {});
      if (url.includes('/skills?')) return jsonResponse([{ id: 's', name: '可用 SOP', status: 'published' }]);
      if (url.includes('/work-record')) return jsonResponse({ events: [] });
      return jsonResponse([]);
    }));
    const view = mount();
    const panel = (name: string) => view.container.querySelector(`[data-panel="${name}"]`);
    await waitFor(() => expect(panel('skills')?.getAttribute('data-state')).toBe('ready'));
    expect(panel('skills')?.textContent).toContain('可用 SOP');
    expect(panel('generalSkills')?.getAttribute('data-state')).toBe('loading');
    expect(panel('knowledge')?.getAttribute('data-state')).toBe('error');
    expect(panel('knowledge')?.textContent).toContain('—');
    expect(panel('knowledge')?.textContent).not.toContain('暂无知识库');
    await act(async () => { screen.getByRole('button', { name: '重新加载' }).click(); });
    await waitFor(() => expect(panel('knowledge')?.getAttribute('data-state')).toBe('ready'));
    expect(attempts).toBe(2);
  });

  it('waits for the roster before sending resource requests with a remembered employee id', async () => {
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, 'agent-old-assembly');
    let resolveRoster!: (value: Response) => void;
    const roster = new Promise<Response>((resolve) => { resolveRoster = resolve; });
    const urls: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      urls.push(url);
      if (url.includes('/api/enterprise/agents?')) return roster;
      if (url.includes('/work-record')) return jsonResponse({ events: [] });
      if (url.includes('/feedback/summary')) return jsonResponse({ total_feedback: 0, up_count: 0, down_count: 0, bucket_counts: [] });
      return jsonResponse([]);
    }));
    mount();
    expect(urls.some((url) => url.includes('agent_id='))).toBe(false);
    await act(async () => { resolveRoster(jsonResponse([agent])); });
    await waitFor(() => expect(urls.some((url) => url.includes('agent_id=agent-1'))).toBe(true));
    expect(urls.some((url) => url.includes('agent-old-assembly'))).toBe(false);
    expect(window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY)).toBe('agent-1');
  });

  it('does not show an obsolete request error after leaving the management page', async () => {
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, agent.id);
    const toast = vi.spyOn(notify, 'error');
    let rejectResource!: (reason: Error) => void;
    const resource = new Promise<Response>((_, reject) => { rejectResource = reject; });
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/enterprise/agents?')) return jsonResponse([agent]);
      if (url.includes('/skills?')) return resource;
      if (url.includes('/work-record')) return jsonResponse({ events: [] });
      return jsonResponse([]);
    });
    vi.stubGlobal('fetch', fetchMock);
    const view = mount();
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/skills?'))).toBe(true));
    view.unmount();
    await act(async () => { rejectResource(new Error('Agent not found')); });
    expect(toast).not.toHaveBeenCalled();
  });

  it('cancels obsolete work-record feedback while the next employee roster is still loading', async () => {
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, agent.id);
    const toast = vi.spyOn(notify, 'error');
    let rejectOld!: (reason: Error) => void;
    const oldRecord = new Promise<Response>((_, reject) => { rejectOld = reject; });
    let resolveRoster!: (value: Response) => void;
    const nextRoster = new Promise<Response>((resolve) => { resolveRoster = resolve; });
    let rosterCalls = 0;
    const urls: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      urls.push(url);
      if (url.includes('/api/enterprise/agents?')) return ++rosterCalls === 1 ? jsonResponse([agent]) : nextRoster;
      if (url.includes('/agents/agent-1/work-record')) return oldRecord;
      if (url.includes('/work-record')) return jsonResponse({ events: [] });
      if (url.includes('/feedback/summary')) return jsonResponse({ total_feedback: 0, up_count: 0, down_count: 0, bucket_counts: [] });
      return jsonResponse([]);
    }));
    mount();
    await waitFor(() => expect(urls.some((url) => url.includes('/agents/agent-1/work-record'))).toBe(true));
    await act(async () => {
      window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, 'agent-2');
      window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId: 'agent-2' } }));
    });
    expect(urls.some((url) => url.includes('/agents/agent-2/work-record'))).toBe(false);
    await act(async () => { rejectOld(new Error('Internal Server Error')); });
    expect(toast).not.toHaveBeenCalled();
    await act(async () => { resolveRoster(jsonResponse([{ ...agent, id: 'agent-2', name: '新员工' }])); });
    await waitFor(() => expect(urls.some((url) => url.includes('/agents/agent-2/work-record'))).toBe(true));
  });

  it('still shows genuine resource failures for the active validated employee', async () => {
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, agent.id);
    const toast = vi.spyOn(notify, 'error');
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/enterprise/agents?')) return jsonResponse([agent]);
      if (url.includes('/skills?')) throw new Error('Permission denied');
      if (url.includes('/work-record')) return jsonResponse({ events: [] });
      return jsonResponse([]);
    }));
    mount();
    await waitFor(() => expect(toast).toHaveBeenCalledWith('Permission denied'));
  });

  it('never sends the team scope as an agent_id query param', async () => {
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, 'team:team-1');
    const fetchedUrls: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      fetchedUrls.push(url);
      if (url.includes('/work-record')) {
        return jsonResponse({ reply_stats: { total: 0, today: 0, by_day: {} }, events: [] });
      }
      if (url.includes('/api/enterprise/agents')) return jsonResponse([agent]);
      if (url.includes('/api/enterprise/feedback/summary')) {
        return jsonResponse({ total_feedback: 0, up_count: 0, down_count: 0, bucket_counts: [] });
      }
      return jsonResponse([]);
    }));

    render(
      <I18nProvider>
        <TooltipProvider>
          <MemoryRouter>
            <DashboardPage
              currentUser={{ id: 'user-1', tenant_id: 'tenant_demo', username: 'demo', role: 'admin' }}
              isAdmin
            />
          </MemoryRouter>
        </TooltipProvider>
      </I18nProvider>,
    );

    // 团队作用域视为未选员工：页面回落到可用员工，正常渲染且不报 "Agent not found"。
    expect((await screen.findByText('小艾')).textContent).toBeTruthy();
    await waitFor(() => expect(fetchedUrls.length).toBeGreaterThan(0));
    fetchedUrls.forEach((url) => {
      expect(url).not.toContain('agent_id=team');
      expect(url).not.toContain('team%3A');
      expect(url).not.toContain('/agents/team');
    });
  });
});
