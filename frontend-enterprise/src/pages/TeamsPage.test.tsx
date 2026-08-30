// @vitest-environment jsdom

import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import type { TeamRead, TeamThreadRead } from '@/types';

import TeamsPage from './TeamsPage';

const team: TeamRead = {
  id: 'team-1',
  tenant_id: 'tenant_demo',
  name: '增长团队',
  description: '负责增长实验',
  owner_user_id: 'user-1',
  config: {},
  status: 'active',
  members: [
    {
      id: 'member-1',
      team_id: 'team-1',
      agent_id: 'agent-1',
      role: 'leader',
      agent_name: '小艾',
      created_at: '2026-08-01T00:00:00Z',
    },
    {
      id: 'member-2',
      team_id: 'team-1',
      agent_id: 'agent-2',
      role: 'member',
      agent_name: '小北',
      created_at: '2026-08-01T00:00:00Z',
    },
  ],
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
};

const sharedKnowledgeBase = {
  id: 'kb-shared-1',
  tenant_id: 'tenant_demo',
  name: '共享制度库',
  mode: 'shared',
  status: 'active',
  version: '1.0.0',
  document_count: 0,
  bucket_count: 0,
  chunk_count: 0,
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

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('TeamsPage', () => {
  it('renders the team management list with member count and project leader', async () => {
    const fetchMock = vi.fn(async () => jsonResponse([team]));
    vi.stubGlobal('fetch', fetchMock);

    render(
      <I18nProvider>
        <MemoryRouter>
          <TeamsPage />
        </MemoryRouter>
      </I18nProvider>,
    );

    expect(await screen.findByText('增长团队')).toBeTruthy();
    expect(screen.getByText('负责增长实验')).toBeTruthy();
    expect(screen.getAllByText('2 名成员').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('项目领导：小艾')).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/enterprise/teams?tenant_id='),
      expect.anything(),
    );
  });

  it('creates a team through the dialog', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === 'POST') return jsonResponse({ ...team, id: 'team-2', name: '新团队' });
      return jsonResponse(url.includes('/teams') ? [team] : []);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <I18nProvider>
        <MemoryRouter>
          <TeamsPage />
        </MemoryRouter>
      </I18nProvider>,
    );

    await screen.findByText('增长团队');
    await user.click(screen.getByRole('button', { name: /创建新团队/ }));
    await user.type(screen.getByLabelText('团队名称'), '新团队');
    await user.click(screen.getByRole('button', { name: '创建' }));

    await waitFor(() => {
      const createCall = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST');
      expect(createCall).toBeTruthy();
      expect(String(createCall?.[0])).toContain('/api/enterprise/teams');
      const body = JSON.parse(String(createCall?.[1]?.body)) as Record<string, unknown>;
      expect(body.name).toBe('新团队');
      expect(body.tenant_id).toBeTruthy();
    });
  });

  it('creates a team with selected shared knowledge and one default target', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === 'POST') return jsonResponse({ ...team, id: 'team-2' });
      if (url.includes('/knowledge-bases')) return jsonResponse([sharedKnowledgeBase]);
      if (url.includes('/team-threads')) return jsonResponse([]);
      return jsonResponse([team]);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <I18nProvider>
        <MemoryRouter>
          <TeamsPage />
        </MemoryRouter>
      </I18nProvider>,
    );

    await screen.findByText('增长团队');
    await user.click(screen.getByRole('button', { name: /创建新团队/ }));
    await user.type(screen.getByLabelText('团队名称'), '知识团队');
    const knowledgeStep = await screen.findByLabelText('团队知识库配置');
    await user.click(within(knowledgeStep).getByRole('checkbox', { name: '选择共享制度库' }));
    await user.click(within(knowledgeStep).getByRole('radio', { name: '设为默认 共享制度库' }));
    await user.click(screen.getByRole('button', { name: '创建' }));

    await waitFor(() => {
      const createCall = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST');
      const body = JSON.parse(String(createCall?.[1]?.body)) as {
        knowledge_bases?: Array<Record<string, unknown>>;
      };
      expect(body.knowledge_bases).toEqual([
        { existing_knowledge_base_id: 'kb-shared-1', is_default: true },
      ]);
    });
  });

  it('deletes a team after confirmation', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'DELETE') return jsonResponse({ ok: true });
      return jsonResponse([team]);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <I18nProvider>
        <MemoryRouter>
          <TeamsPage />
        </MemoryRouter>
      </I18nProvider>,
    );

    await screen.findByText('增长团队');
    await user.click(screen.getByRole('button', { name: '删除团队 增长团队' }));
    await user.click(await screen.findByRole('button', { name: '删除' }));

    await waitFor(() => {
      const deleteCall = fetchMock.mock.calls.find(([, init]) => init?.method === 'DELETE');
      expect(deleteCall).toBeTruthy();
      expect(String(deleteCall?.[0])).toContain('/api/enterprise/teams/team-1');
    });
  });

  it('starts the persistent team conversation from the management card', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes('/tl/session') && init?.method === 'POST') {
        return jsonResponse({ session_id: 'team-session-1' });
      }
      if (url.includes('/team-threads')) return jsonResponse([]);
      return jsonResponse(url.includes('/teams') ? [team] : []);
    });
    vi.stubGlobal('fetch', fetchMock);

    renderTeamsWithRoutes();
    await user.click(await screen.findByRole('button', { name: '开始与团队 增长团队 对话' }));

    expect((await screen.findByTestId('location')).textContent).toBe('/workspace/chat/team-session-1');
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/enterprise/teams/team-1/tl/session'),
      expect.objectContaining({ method: 'POST' }),
    );
  });
});

const threads: TeamThreadRead[] = [
  {
    team_id: 'team-1',
    team_name: '增长团队',
    kind: 'tl_chat',
    session_id: 'session-1',
    task_id: null,
    title: 'planning chat',
    task_status: null,
    updated_at: new Date().toISOString(),
  },
  {
    team_id: 'team-1',
    team_name: '增长团队',
    kind: 'task',
    session_id: 'session-2',
    task_id: 'task-9',
    title: '写周报',
    task_status: 'in_progress',
    updated_at: new Date().toISOString(),
  },
];

function LocationEcho() {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>;
}

function renderTeamsWithRoutes() {
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={['/enterprise/teams']}>
        <Routes>
          <Route path="/enterprise/teams" element={<TeamsPage />} />
          <Route path="/enterprise/teams/:teamId" element={<LocationEcho />} />
          <Route path="/workspace/chat/:sessionId" element={<LocationEcho />} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  );
}

function stubThreadsFetch() {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/team-threads')) return jsonResponse(threads);
    return jsonResponse([team]);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

describe('TeamsPage team activity', () => {
  it('renders the activity tree grouped by team and task', async () => {
    stubThreadsFetch();
    renderTeamsWithRoutes();

    const activity = await screen.findByLabelText('团队动态');
    // 团队节点只出现一次，线程按任务分组收拢在节点下（默认展开最新团队）
    expect(within(activity).getAllByText('增长团队').length).toBe(1);
    expect(within(activity).getByText('项目领导对话')).toBeTruthy();
    expect(within(activity).getByText('planning chat')).toBeTruthy();
    expect(within(activity).getAllByText('写周报').length).toBeGreaterThanOrEqual(1);
    expect(within(activity).getByText('进行中')).toBeTruthy();
    expect(within(activity).getByText(/1 任务 · 2 线程/)).toBeTruthy();
  });

  it('collapses and expands a team node', async () => {
    const user = userEvent.setup();
    stubThreadsFetch();
    renderTeamsWithRoutes();

    const activity = await screen.findByLabelText('团队动态');
    await user.click(within(activity).getByLabelText('收起团队 增长团队'));
    expect(within(activity).queryByText('planning chat')).toBeNull();

    await user.click(within(activity).getByLabelText('展开团队 增长团队'));
    expect(within(activity).getByText('planning chat')).toBeTruthy();
  });

  it('navigates to the task detail when the thread has a task_id', async () => {
    const user = userEvent.setup();
    stubThreadsFetch();
    renderTeamsWithRoutes();

    const activity = await screen.findByLabelText('团队动态');
    await user.click(within(activity).getAllByRole('button', { name: /写周报/ })[0]);

    expect((await screen.findByTestId('location')).textContent).toBe(
      '/enterprise/teams/team-1?task=task-9',
    );
  });

  it('opens team group conversations in the chat app', async () => {
    const user = userEvent.setup();
    stubThreadsFetch();
    renderTeamsWithRoutes();

    const activity = await screen.findByLabelText('团队动态');
    await user.click(within(activity).getByRole('button', { name: /planning chat/ }));

    expect((await screen.findByTestId('location')).textContent).toBe('/workspace/chat/session-1');
  });
});

describe('relativeTimeLabel', () => {
  it('treats naive backend timestamps as UTC when computing relative time', async () => {
    const { relativeTimeLabel } = await import('./TeamsPage');
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-11T12:00:00Z'));
    try {
      expect(relativeTimeLabel('2026-08-11T11:50:00')).toBe('10 分钟前');
      expect(relativeTimeLabel('2026-08-11T11:00:00Z')).toBe('1 小时前');
      expect(relativeTimeLabel('')).toBe('');
    } finally {
      vi.useRealTimers();
    }
  });
});
