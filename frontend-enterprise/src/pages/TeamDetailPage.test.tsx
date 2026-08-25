// @vitest-environment jsdom

import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import type { AgentProfileRead, TeamBlackboardEntryRead, TeamEventRead, TeamRead, TeamTaskBidRead, TeamTaskRead } from '@/types';

import TeamDetailPage from './TeamDetailPage';

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

function makeTask(overrides: Partial<TeamTaskRead>): TeamTaskRead {
  return {
    id: 'task-1',
    team_id: 'team-1',
    tenant_id: 'tenant_demo',
    parent_task_id: null,
    title: '写周报',
    description: '汇总本周数据',
    priority: 'high',
    status: 'review',
    created_by_user_id: null,
    created_by_tl: true,
    assignee_agent_id: 'agent-2',
    session_id: null,
    report: { summary: '周报已完成' },
    review: {},
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
    ...overrides,
  };
}

const tasks: TeamTaskRead[] = [
  makeTask({ id: 'task-1', title: '写周报', status: 'review' }),
  makeTask({ id: 'task-2', title: '整理线索', status: 'pending', priority: 'low', assignee_agent_id: null }),
  makeTask({ id: 'task-3', title: '投放分析', status: 'in_progress', priority: 'medium' }),
  makeTask({ id: 'task-4', title: '竞标方案', status: 'bidding', assignee_agent_id: null }),
  makeTask({ id: 'task-5', title: '竞标已裁决', status: 'pending', assignee_agent_id: 'agent-1' }),
];

function makeBid(overrides: Partial<TeamTaskBidRead>): TeamTaskBidRead {
  return {
    id: 'bid-1',
    task_id: 'task-4',
    agent_id: 'agent-1',
    agent_name: '小艾',
    round: 1,
    kind: 'statement',
    content: '我擅长数据分析',
    score: null,
    score_rationale: null,
    created_at: '2026-08-02T00:00:00Z',
    ...overrides,
  };
}

const awardedBids: TeamTaskBidRead[] = [
  makeBid({ id: 'bid-1', round: 1, kind: 'statement', content: '我擅长数据分析', score: 8, score_rationale: '方案具体' }),
  makeBid({ id: 'bid-2', agent_id: 'agent-2', agent_name: '小北', round: 1, kind: 'statement', content: '我可以快速交付', score: 6, score_rationale: null }),
  makeBid({ id: 'bid-3', round: 2, kind: 'rebuttal', content: '对方缺少落地案例', score: 7, score_rationale: null }),
];

const agents: AgentProfileRead[] = [
  {
    id: 'agent-3',
    tenant_id: 'tenant_demo',
    name: '小丙',
    is_overall: false,
    status: 'active',
    metadata: {},
    resources: [],
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
  },
];

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
    blob: async () => new Blob([JSON.stringify(body ?? {})], { type: 'application/json' }),
  } as Response;
}

function makeEntry(overrides: Partial<TeamBlackboardEntryRead>): TeamBlackboardEntryRead {
  return {
    id: 'entry-1',
    team_id: 'team-1',
    tenant_id: 'tenant_demo',
    content: 'entry content',
    tags: [],
    source_type: 'human',
    source_agent_id: null,
    source_task_id: null,
    citation: {},
    status: 'active',
    pinned: false,
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-02T00:00:00Z',
    ...overrides,
  };
}

function stubDetailFetch(overrides?: {
  entries?: TeamBlackboardEntryRead[];
  events?: TeamEventRead[];
  teamOverride?: TeamRead;
  taskList?: TeamTaskRead[];
  onTlSession?: () => { session_id: string };
  taskDetails?: Record<string, TeamTaskRead>;
}) {
  let boardRows = [...(overrides?.entries ?? [])];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes('/blackboard')) {
      const method = (init?.method || 'GET').toUpperCase();
      if (url.includes('/archive')) {
        const entryId = url.split('/blackboard/')[1]?.split('/')[0];
        boardRows = boardRows.filter((row) => row.id !== entryId);
        return jsonResponse({});
      }
      if (url.includes('/promote')) {
        const entryId = url.split('/blackboard/')[1]?.split('/')[0];
        boardRows = boardRows.map((row) =>
          row.id === entryId ? { ...row, citation: { ...row.citation, knowledge_base_id: 'kb-1' } } : row,
        );
        return jsonResponse(boardRows.find((row) => row.id === entryId) ?? {});
      }
      if (method === 'POST') {
        const body = JSON.parse(String(init?.body)) as { content: string; tags?: string[] };
        const created = makeEntry({ id: 'entry-new', content: body.content, tags: body.tags ?? [] });
        boardRows = [...boardRows, created];
        return jsonResponse(created);
      }
      if (method === 'PUT') {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        const entryId = url.split('/blackboard/')[1]?.split('?')[0];
        boardRows = boardRows.map((row) => (row.id === entryId ? { ...row, ...body } : row));
        return jsonResponse(boardRows.find((row) => row.id === entryId) ?? {});
      }
      return jsonResponse(boardRows);
    }
    if (url.includes('/tl/session')) {
      return jsonResponse(overrides?.onTlSession?.() ?? { session_id: 'session-1' });
    }
    if (url.includes('/award-override')) {
      return jsonResponse(makeTask({ status: 'pending', assignee_agent_id: 'agent-1' }));
    }
    if (url.includes('/override')) return jsonResponse(makeTask({ status: 'done' }));
    const detailMatch = url.match(/\/tasks\/([^/?]+)\?/);
    if (detailMatch && overrides?.taskDetails?.[detailMatch[1]]) {
      return jsonResponse(overrides.taskDetails[detailMatch[1]]);
    }
    if (url.includes('/tasks/task-1')) {
      return jsonResponse(
        makeTask({
          events: [
            {
              id: 'event-1',
              task_id: 'task-1',
              team_id: 'team-1',
              actor_type: 'tl',
              actor_id: 'agent-1',
              event_type: 'submitted',
              payload: {},
              created_at: '2026-08-02T00:00:00Z',
            },
          ],
        }),
      );
    }
    if (url.includes('/tasks')) {
      const method = (init?.method || 'GET').toUpperCase();
      if (method === 'POST') {
        const body = JSON.parse(String(init?.body)) as { title: string; assignee_agent_id?: string };
        return jsonResponse(
          makeTask({
            id: 'task-new',
            title: body.title,
            status: body.assignee_agent_id ? 'pending' : 'bidding',
            assignee_agent_id: body.assignee_agent_id ?? null,
          }),
        );
      }
      return jsonResponse(overrides?.taskList ?? tasks);
    }
    if (url.includes('/api/enterprise/agents')) return jsonResponse(agents);
    if (url.includes('/events')) return jsonResponse(overrides?.events ?? []);
    if (url.includes('/api/enterprise/teams/team-1')) {
      const method = (init?.method || 'GET').toUpperCase();
      const currentTeam = overrides?.teamOverride ?? team;
      if (method === 'PUT') {
        const body = JSON.parse(String(init?.body)) as { config?: Record<string, unknown> };
        return jsonResponse(body.config ? { ...currentTeam, config: body.config } : currentTeam);
      }
      return jsonResponse(currentTeam);
    }
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function LocationEcho() {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>;
}

function renderDetail(initialEntry = '/enterprise/teams/team-1') {
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/enterprise/teams/:teamId" element={<TeamDetailPage />} />
          <Route path="/enterprise/teams/:teamId/chat" element={<LocationEcho />} />
          <Route path="/workspace/chat/:sessionId" element={<LocationEcho />} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

beforeAll(() => {
  // Radix Select 在 jsdom 中需要 pointer capture API
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
  window.HTMLElement.prototype.hasPointerCapture = vi.fn();
  window.HTMLElement.prototype.releasePointerCapture = vi.fn();
});

describe('TeamDetailPage', () => {
  it('downloads the complete team execution log', async () => {
    const user = userEvent.setup();
    const fetchMock = stubDetailFetch();
    const createObjectURL = vi.fn(() => 'blob:team-log');
    const revokeObjectURL = vi.fn();
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });
    renderDetail();

    await user.click(await screen.findByRole('button', { name: '下载完整日志' }));

    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([input]) =>
        String(input).includes('/api/enterprise/teams/team-1/export?tenant_id=tenant_demo'),
      )).toBe(true);
      expect(createObjectURL).toHaveBeenCalledTimes(1);
      expect(click).toHaveBeenCalledTimes(1);
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:team-log');
    });
  });

  it('renders members and groups kanban tasks by status', async () => {
    stubDetailFetch();
    renderDetail();

    expect(await screen.findByText('增长团队')).toBeTruthy();

    const members = screen.getByLabelText('成员管理');
    expect(within(members).getByText('小艾')).toBeTruthy();
    expect(within(members).getByText('项目领导')).toBeTruthy();
    expect(within(members).getByText('小北')).toBeTruthy();

    const board = screen.getByLabelText('任务看板');
    expect(await within(board).findByText('写周报')).toBeTruthy();
    const reviewColumn = within(board).getByText('待验收').closest('div')?.parentElement as HTMLElement;
    expect(within(reviewColumn).getByText('写周报')).toBeTruthy();
    const pendingColumn = within(board).getByText('待认领').closest('div')?.parentElement as HTMLElement;
    expect(within(pendingColumn).getByText('整理线索')).toBeTruthy();
    expect(within(pendingColumn).getByText('未分配')).toBeTruthy();
    const progressColumn = within(board).getByText('进行中').closest('div')?.parentElement as HTMLElement;
    expect(within(progressColumn).getByText('投放分析')).toBeTruthy();
  });

  it('submits an override verdict from the task detail dialog', async () => {
    const user = userEvent.setup();
    const fetchMock = stubDetailFetch();
    renderDetail();

    const board = screen.getByLabelText('任务看板');
    await user.click(await within(board).findByText('写周报'));

    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByText('周报已完成')).toBeTruthy();
    expect(within(dialog).getByText('submitted')).toBeTruthy();

    await user.click(within(dialog).getByRole('button', { name: '通过' }));

    await waitFor(() => {
      const overrideCall = fetchMock.mock.calls.find(([input]) =>
        String(input).includes('/tasks/task-1/override'),
      );
      expect(overrideCall).toBeTruthy();
      const body = JSON.parse(String(overrideCall?.[1]?.body)) as Record<string, unknown>;
      expect(body.verdict).toBe('approve');
    });
  });

  it('keeps chat content out of the management workspace', async () => {
    stubDetailFetch();
    renderDetail();

    expect(await screen.findByText('增长团队')).toBeTruthy();
    expect(screen.queryByLabelText('团队群聊')).toBeNull();
  });

  it('starts the persistent team conversation from team details', async () => {
    const user = userEvent.setup();
    const fetchMock = stubDetailFetch({ onTlSession: () => ({ session_id: 'team-session-2' }) });
    renderDetail();

    await user.click(await screen.findByRole('button', { name: '开始对话' }));

    expect((await screen.findByTestId('location')).textContent).toBe('/workspace/chat/team-session-2');
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/enterprise/teams/team-1/tl/session'),
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it('renders blackboard entries pinned first with tags and sources', async () => {
    stubDetailFetch({
      entries: [
        makeEntry({
          id: 'entry-1',
          content: 'member note',
          tags: ['okr'],
          source_type: 'member',
          source_agent_id: 'agent-2',
          citation: { task_title: '写周报' },
        }),
        makeEntry({ id: 'entry-2', content: 'pinned note', pinned: true, source_type: 'leader' }),
      ],
    });
    renderDetail();

    const board = screen.getByLabelText('团队黑板');
    const pinned = await within(board).findByText('pinned note');
    const plain = within(board).getByText('member note');
    expect(pinned.compareDocumentPosition(plain) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(board).getByText('okr')).toBeTruthy();
    expect(within(board).getByText((content) => content.startsWith('项目领导'))).toBeTruthy();
    expect(within(board).getByText((content) => content.startsWith('小北'))).toBeTruthy();
    expect(within(board).getByText(/关联任务：写周报/)).toBeTruthy();
    expect(within(board).getAllByText('置顶').length).toBeGreaterThan(0);
  });

  it('shows an empty placeholder when the blackboard has no entries', async () => {
    stubDetailFetch();
    renderDetail();

    const board = screen.getByLabelText('团队黑板');
    expect(await within(board).findByText('暂无黑板条目')).toBeTruthy();
  });

  it('submits a human-written blackboard entry with tags', async () => {
    const user = userEvent.setup();
    const fetchMock = stubDetailFetch();
    renderDetail();

    const board = screen.getByLabelText('团队黑板');
    await user.type(within(board).getByLabelText('输入黑板内容'), 'release risk');
    await user.type(within(board).getByLabelText('标签（逗号分隔，可选）'), 'risk, launch');
    await user.click(within(board).getByRole('button', { name: '添加' }));

    expect(await within(board).findByText('release risk')).toBeTruthy();
    const postCall = fetchMock.mock.calls.find(
      ([input, init]) =>
        String(input).includes('/blackboard') && String(init?.method || '').toUpperCase() === 'POST',
    );
    expect(postCall).toBeTruthy();
    const body = JSON.parse(String(postCall?.[1]?.body)) as Record<string, unknown>;
    expect(body.content).toBe('release risk');
    expect(body.tags).toEqual(['risk', 'launch']);
  });

  it('archives a blackboard entry after confirmation', async () => {
    const user = userEvent.setup();
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const fetchMock = stubDetailFetch({
      entries: [makeEntry({ id: 'entry-1', content: 'stale note' })],
    });
    renderDetail();

    const board = screen.getByLabelText('团队黑板');
    await within(board).findByText('stale note');
    await user.click(within(board).getByRole('button', { name: '归档' }));

    await waitFor(() => {
      const archiveCall = fetchMock.mock.calls.find(([input]) =>
        String(input).includes('/blackboard/entry-1/archive'),
      );
      expect(archiveCall).toBeTruthy();
    });
    await waitFor(() => {
      expect(within(board).queryByText('stale note')).toBeNull();
    });
    expect(within(board).getByText('暂无黑板条目')).toBeTruthy();
  });

  it('renders the bidding column with bidding tasks', async () => {
    stubDetailFetch();
    renderDetail();

    const board = screen.getByLabelText('任务看板');
    const biddingColumn = within(board).getByText('竞标中').closest('div')?.parentElement as HTMLElement;
    expect(await within(biddingColumn).findByText('竞标方案')).toBeTruthy();
  });

  it('renders the bidding arena with HP bars and the winner crown', async () => {
    const user = userEvent.setup();
    stubDetailFetch({
      taskDetails: {
        'task-5': makeTask({
          id: 'task-5',
          title: '竞标已裁决',
          status: 'pending',
          assignee_agent_id: 'agent-1',
          bids: awardedBids,
        }),
      },
    });
    renderDetail();

    const board = screen.getByLabelText('任务看板');
    await user.click(await within(board).findByText('竞标已裁决'));

    const dialog = await screen.findByRole('dialog');
    const arena = await within(dialog).findByLabelText('竞标竞技场');
    // 小艾：第 1 轮 8 分扣 6，第 2 轮 7 分扣 9 → HP 85；小北：6 分扣 12 → HP 88
    expect(within(arena).getByText('HP 85')).toBeTruthy();
    expect(within(arena).getByText('HP 88')).toBeTruthy();
    expect(within(arena).getByTestId('arena-hp-agent-1').style.width).toBe('85%');
    expect(within(arena).getByTestId('arena-hp-agent-2').style.width).toBe('88%');
    expect(within(arena).getByText('胜者为王')).toBeTruthy();
    // 每个候选人卡片各有一份轮次标签：两人都有第 1 轮，只有小艾有第 2 轮
    expect(within(arena).getAllByText('第 1 轮').length).toBe(2);
    expect(within(arena).getAllByText('第 2 轮').length).toBe(1);
    expect(within(arena).getAllByText('陈述').length).toBe(2);
    expect(within(arena).getByText('反驳')).toBeTruthy();
    expect(within(arena).getByText('我擅长数据分析')).toBeTruthy();
    expect(within(arena).getByText('对方缺少落地案例')).toBeTruthy();
    expect(within(arena).getByText('得分：8')).toBeTruthy();
    expect(within(arena).getByText('方案具体')).toBeTruthy();
  });

  it('marks eliminated candidates and shows full HP for legacy bids without scores', async () => {
    const user = userEvent.setup();
    stubDetailFetch({
      taskDetails: {
        'task-4': makeTask({
          id: 'task-4',
          title: '竞标方案',
          status: 'bidding',
          assignee_agent_id: null,
          bids: [
            ...[1, 2, 3, 4].map((round) =>
              makeBid({ id: `bid-w${round}`, agent_id: 'agent-1', agent_name: '小艾', round, score: 0 }),
            ),
            makeBid({ id: 'bid-legacy', agent_id: 'agent-2', agent_name: '小北', round: 1, score: null }),
          ],
        }),
      },
    });
    renderDetail();

    const board = screen.getByLabelText('任务看板');
    await user.click(await within(board).findByText('竞标方案'));

    const dialog = await screen.findByRole('dialog');
    const arena = await within(dialog).findByLabelText('竞标竞技场');
    // 4 轮 0 分扣尽 HP → 淘汰；无得分的历史数据保持满血
    expect(within(arena).getByText('HP 0')).toBeTruthy();
    expect(within(arena).getByText('淘汰')).toBeTruthy();
    expect(within(arena).getByText('HP 100')).toBeTruthy();
    expect(within(arena).queryByText('胜者为王')).toBeNull();
  });

  it('creates a task into the bidding pool without an assignee', async () => {
    const user = userEvent.setup();
    const fetchMock = stubDetailFetch();
    renderDetail();

    const board = screen.getByLabelText('任务看板');
    await user.click(within(board).getByRole('button', { name: '新建任务' }));

    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText('任务标题'), '池化任务');
    await user.click(within(dialog).getByRole('button', { name: '创建' }));

    await waitFor(() => {
      const postCall = fetchMock.mock.calls.find(
        ([input, init]) =>
          String(input).includes('/tasks') && String(init?.method || '').toUpperCase() === 'POST',
      );
      expect(postCall).toBeTruthy();
      const body = JSON.parse(String(postCall?.[1]?.body)) as Record<string, unknown>;
      expect(body.title).toBe('池化任务');
      expect('assignee_agent_id' in body).toBe(false);
    });
  });

  it('creates a task with direct assignment to a member', async () => {
    const user = userEvent.setup();
    const fetchMock = stubDetailFetch();
    renderDetail();

    const board = screen.getByLabelText('任务看板');
    await user.click(within(board).getByRole('button', { name: '新建任务' }));

    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText('任务标题'), '直派任务');
    await user.click(within(dialog).getByRole('combobox', { name: '执行者' }));
    await user.click(await screen.findByRole('option', { name: '小北' }));
    await user.click(within(dialog).getByRole('button', { name: '创建' }));

    await waitFor(() => {
      const postCall = fetchMock.mock.calls.find(
        ([input, init]) =>
          String(input).includes('/tasks') && String(init?.method || '').toUpperCase() === 'POST',
      );
      expect(postCall).toBeTruthy();
      const body = JSON.parse(String(postCall?.[1]?.body)) as Record<string, unknown>;
      expect(body.title).toBe('直派任务');
      expect(body.assignee_agent_id).toBe('agent-2');
    });
  });

  it('submits an award override for a bidding task', async () => {
    const user = userEvent.setup();
    const fetchMock = stubDetailFetch({
      taskDetails: {
        'task-4': makeTask({
          id: 'task-4',
          title: '竞标方案',
          status: 'bidding',
          assignee_agent_id: null,
          bids: awardedBids,
        }),
      },
    });
    renderDetail();

    const board = screen.getByLabelText('任务看板');
    await user.click(await within(board).findByText('竞标方案'));

    const dialog = await screen.findByRole('dialog');
    const awardSection = await within(dialog).findByLabelText('改判执行者');
    await user.click(within(awardSection).getByRole('combobox', { name: '选择执行者' }));
    await user.click(await screen.findByRole('option', { name: '小艾' }));
    await user.type(within(awardSection).getByLabelText('改判说明（可选）'), '更信任小艾');
    await user.click(within(awardSection).getByRole('button', { name: '确认改判' }));

    await waitFor(() => {
      const overrideCall = fetchMock.mock.calls.find(([input]) =>
        String(input).includes('/tasks/task-4/award-override'),
      );
      expect(overrideCall).toBeTruthy();
      const body = JSON.parse(String(overrideCall?.[1]?.body)) as Record<string, unknown>;
      expect(body.agent_id).toBe('agent-1');
      expect(body.comment).toBe('更信任小艾');
    });
  });

  it('opens the task detail dialog from the ?task= query param', async () => {
    stubDetailFetch();
    renderDetail('/enterprise/teams/team-1?task=task-1');

    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByText('周报已完成')).toBeTruthy();
    expect(within(dialog).getByText('submitted')).toBeTruthy();
  });

  it('saves team settings via PUT with the merged config', async () => {
    const user = userEvent.setup();
    const fetchMock = stubDetailFetch({
      teamOverride: { ...team, config: { member_concurrency: 2, custom_flag: 'keep' } },
    });
    renderDetail();

    const settings = await screen.findByLabelText('团队设置');
    expect((within(settings).getByLabelText('成员并发上限') as HTMLInputElement).value).toBe('2');
    const timeoutInput = within(settings).getByLabelText('任务超时分钟') as HTMLInputElement;
    expect(timeoutInput.value).toBe('30');
    expect((within(settings).getByLabelText('竞标反驳轮数') as HTMLInputElement).value).toBe('1');

    await user.clear(timeoutInput);
    await user.type(timeoutInput, '45');
    await user.click(within(settings).getByRole('button', { name: '保存设置' }));

    await waitFor(() => {
      const putCall = fetchMock.mock.calls.find(
        ([input, init]) =>
          String(input).includes('/api/enterprise/teams/team-1') &&
          String(init?.method || '').toUpperCase() === 'PUT',
      );
      expect(putCall).toBeTruthy();
      const body = JSON.parse(String(putCall?.[1]?.body)) as { config: Record<string, unknown> };
      expect(body.config.member_concurrency).toBe(2);
      expect(body.config.task_timeout_minutes).toBe(45);
      expect(body.config.bid_rebuttal_rounds).toBe(1);
      expect(body.config.custom_flag).toBe('keep');
    });
  });

  it('promotes a blackboard entry and disables already promoted entries', async () => {
    const user = userEvent.setup();
    const fetchMock = stubDetailFetch({
      entries: [
        makeEntry({ id: 'entry-1', content: 'fresh note' }),
        makeEntry({ id: 'entry-2', content: 'old note', citation: { knowledge_base_id: 'kb-9' } }),
      ],
    });
    renderDetail();

    const board = screen.getByLabelText('团队黑板');
    await within(board).findByText('fresh note');
    const promotedButton = within(board).getByRole('button', { name: '已沉淀' }) as HTMLButtonElement;
    expect(promotedButton.disabled).toBe(true);

    await user.click(within(board).getByRole('button', { name: '沉淀到知识库' }));

    await waitFor(() => {
      const promoteCall = fetchMock.mock.calls.find(([input]) =>
        String(input).includes('/blackboard/entry-1/promote'),
      );
      expect(promoteCall).toBeTruthy();
      const body = JSON.parse(String(promoteCall?.[1]?.body)) as Record<string, unknown>;
      expect(body.tenant_id).toBeTruthy();
    });
    await waitFor(() => {
      expect(within(board).getAllByRole('button', { name: '已沉淀' }).length).toBe(2);
    });
  });

  it('groups team activity by task and opens the task from the group header', async () => {
    const user = userEvent.setup();
    const now = Date.now();
    stubDetailFetch({
      events: [
        {
          id: 'event-1',
          task_id: 'task-1',
          task_title: '写周报',
          actor_type: 'agent',
          actor_id: 'agent-2',
          event_type: 'task_reported',
          payload: {},
          created_at: new Date(now - 60000).toISOString(),
        },
        {
          id: 'event-3',
          task_id: 'task-1',
          task_title: '写周报',
          actor_type: 'tl',
          actor_id: 'agent-1',
          event_type: 'tl_review_rework',
          payload: {},
          created_at: new Date(now - 120000).toISOString(),
        },
        {
          id: 'event-4',
          task_id: 'task-3',
          task_title: '投放分析',
          actor_type: 'agent',
          actor_id: 'agent-2',
          event_type: 'task_started',
          payload: {},
          created_at: new Date(now).toISOString(),
        },
        {
          id: 'event-2',
          task_id: null,
          task_title: null,
          actor_type: 'system',
          actor_id: null,
          event_type: 'tl_review_skipped',
          payload: {},
          created_at: new Date(now - 30000).toISOString(),
        },
      ],
    });
    renderDetail();

    const activity = await screen.findByLabelText('团队动态');
    // 同任务事件聚合到一张分组卡片下
    expect(await within(activity).findByText('提交报告')).toBeTruthy();
    expect(within(activity).getByText('项目领导退回重做')).toBeTruthy();
    expect(within(activity).getByText('项目领导免验收')).toBeTruthy();
    expect(within(activity).getByText('其他')).toBeTruthy();
    // 组间按最新事件倒序：投放分析 > 其他 > 写周报
    const headerNewest = within(activity).getByRole('button', { name: '投放分析' });
    const headerOldest = within(activity).getByRole('button', { name: '写周报' });
    expect(
      headerNewest.compareDocumentPosition(headerOldest) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    // 组头点击打开任务详情
    await user.click(headerOldest);
    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByText('周报已完成')).toBeTruthy();
  });

  it('renders the review verdict as a prominent banner with the comment quote', async () => {
    const user = userEvent.setup();
    stubDetailFetch({
      taskDetails: {
        'task-1': makeTask({
          review: { verdict: 'rework', comment: '数据不完整，请补充来源' },
          events: [],
        }),
      },
    });
    renderDetail();

    const board = screen.getByLabelText('任务看板');
    await user.click(await within(board).findByText('写周报'));

    const dialog = await screen.findByRole('dialog');
    const verdict = await within(dialog).findByLabelText('验收结论');
    expect(within(verdict).getByText('退回重做')).toBeTruthy();
    const quote = within(verdict).getByText('数据不完整，请补充来源');
    expect(quote.tagName).toBe('BLOCKQUOTE');
  });

  it('renders an approve banner and hides the section when there is no verdict', async () => {
    const user = userEvent.setup();
    stubDetailFetch({
      taskDetails: {
        'task-1': makeTask({ review: { verdict: 'approve' }, events: [] }),
        'task-3': makeTask({ id: 'task-3', title: '投放分析', status: 'in_progress', review: {}, events: [] }),
      },
    });
    renderDetail();

    const board = screen.getByLabelText('任务看板');
    await user.click(await within(board).findByText('写周报'));
    let dialog = await screen.findByRole('dialog');
    const verdict = await within(dialog).findByLabelText('验收结论');
    expect(within(verdict).getByText('验收通过')).toBeTruthy();
    await user.click(within(dialog).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());

    await user.click(within(board).getByText('投放分析'));
    dialog = await screen.findByRole('dialog');
    expect(within(dialog).queryByLabelText('验收结论')).toBeNull();
  });

  it('keeps internal execution records inside the task detail', async () => {
    const user = userEvent.setup();
    stubDetailFetch({
      taskDetails: {
        'task-1': makeTask({ session_id: 'session-exec-1', events: [] }),
      },
    });
    renderDetail();

    const board = screen.getByLabelText('任务看板');
    await user.click(await within(board).findByText('写周报'));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('内部执行记录已归档')).toBeTruthy();
    expect(within(dialog).queryByRole('button', { name: '查看执行会话' })).toBeNull();
  });

  it('shows creation timestamps on kanban cards sorted newest first', async () => {
    stubDetailFetch({
      taskList: [
        makeTask({ id: 'task-old', title: '旧任务', status: 'in_progress', created_at: '2026-08-01T09:00:00' }),
        makeTask({ id: 'task-new', title: '新任务', status: 'in_progress', created_at: '2026-08-02T09:00:00' }),
      ],
    });
    renderDetail();

    const board = screen.getByLabelText('任务看板');
    const progressColumn = within(board).getByText('进行中').closest('div')?.parentElement as HTMLElement;
    expect(await within(progressColumn).findByText('新任务')).toBeTruthy();
    expect(within(progressColumn).getAllByText(/创建于/).length).toBe(2);
    const newer = within(progressColumn).getByText('新任务');
    const older = within(progressColumn).getByText('旧任务');
    expect(newer.compareDocumentPosition(older) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('renders the member tree with employee avatars', async () => {
    stubDetailFetch();
    renderDetail();

    const members = screen.getByLabelText('成员管理');
    await within(members).findByText('小艾');
    expect(within(members).getAllByLabelText(/员工头像/).length).toBe(2);
    expect(within(members).getByText('项目领导')).toBeTruthy();
    expect(within(members).getByText('成员')).toBeTruthy();
    const promoteButton = within(members).getByRole('button', { name: '设为项目领导' });
    const removeButton = within(members).getByRole('button', { name: '移除成员 小北' });
    expect(promoteButton.className).toContain('whitespace-nowrap');
    expect(removeButton.className).toContain('whitespace-nowrap');
  });
});
