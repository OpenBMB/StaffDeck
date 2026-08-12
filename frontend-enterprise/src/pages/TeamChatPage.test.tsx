// @vitest-environment jsdom

import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import type { AgentProfileRead } from '@/types';

import TeamChatPage, {
  type TeamChatMessage,
  type TeamConversationsResponse,
} from './TeamChatPage';

function minutesAgo(minutes: number): string {
  return new Date(Date.now() - minutes * 60000).toISOString();
}

const agents: AgentProfileRead[] = [
  {
    id: 'agent-1',
    tenant_id: 'tenant_demo',
    name: '小艾',
    is_overall: false,
    status: 'active',
    metadata: {},
    resources: [],
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
  },
  {
    id: 'agent-2',
    tenant_id: 'tenant_demo',
    name: '小北',
    is_overall: false,
    status: 'active',
    metadata: {},
    resources: [],
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
  },
];

function makeConversations(tlSessionId: string | null = 'session-tl-1'): TeamConversationsResponse {
  return {
    team_id: 'team-1',
    team_name: '增长团队',
    tl: { agent_id: 'agent-1', agent_name: '小艾', session_id: tlSessionId },
    conversations: [
      ...(tlSessionId
        ? [
            {
              session_id: tlSessionId,
              kind: 'tl_chat' as const,
              agent_id: 'agent-1',
              agent_name: '小艾',
              task_id: null,
              title: 'TL 对话',
              preview: '在的，请讲',
              updated_at: minutesAgo(2),
            },
          ]
        : []),
      {
        session_id: 'session-task-1',
        kind: 'member_task' as const,
        agent_id: 'agent-2',
        agent_name: '小北',
        task_id: 'task-1',
        title: '写周报',
        preview: '周报已完成，请验收',
        updated_at: minutesAgo(5),
      },
      {
        session_id: 'session-bid-1',
        kind: 'member_bid' as const,
        agent_id: 'agent-2',
        agent_name: '小北',
        task_id: 'task-2',
        title: '竞标方案',
        preview: '我擅长数据分析',
        updated_at: minutesAgo(30),
      },
      {
        session_id: 'session-review-1',
        kind: 'tl_review' as const,
        agent_id: 'agent-2',
        agent_name: '小北',
        task_id: 'task-1',
        title: '验收：写周报',
        preview: '验收通过',
        updated_at: minutesAgo(60),
      },
    ],
  };
}

const tlMessages: TeamChatMessage[] = [
  { id: 'm-1', role: 'user', content: '请拆解本月目标', created_at: minutesAgo(4) },
  { id: 'm-2', role: 'assistant', content: '好的，我来拆解', created_at: minutesAgo(2) },
];

const taskMessages: TeamChatMessage[] = [
  { id: 'm-3', role: 'assistant', content: '周报已完成，请验收', created_at: minutesAgo(5) },
];

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

function stubChatFetch(overrides?: {
  tlSessionId?: string | null;
  createdSessionId?: string;
  reply?: string;
}) {
  const tlSessionId = overrides && 'tlSessionId' in overrides ? overrides.tlSessionId : 'session-tl-1';
  const createdSessionId = overrides?.createdSessionId || 'session-tl-created';
  const reply = overrides?.reply ?? '已收到，开始执行';
  const messagesBySession: Record<string, TeamChatMessage[]> = {
    'session-tl-1': tlMessages,
    'session-task-1': taskMessages,
    [createdSessionId]: [],
  };
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method || 'GET').toUpperCase();
    if (url.includes('/tl/session')) {
      return jsonResponse({ session_id: createdSessionId });
    }
    if (url.includes('/api/chat/turn')) {
      return jsonResponse({ reply, session_id: tlSessionId || createdSessionId });
    }
    const messagesMatch = url.match(/\/conversations\/([^/?]+)\/messages/);
    if (messagesMatch) {
      return jsonResponse(messagesBySession[messagesMatch[1]] ?? []);
    }
    if (url.includes('/conversations')) {
      return jsonResponse(makeConversations(tlSessionId ?? null));
    }
    if (url.includes('/api/enterprise/agents')) return jsonResponse(agents);
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function LocationEcho() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}</div>;
}

function renderChat() {
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={['/enterprise/teams/team-1/chat']}>
        <Routes>
          <Route path="/enterprise/teams/:teamId/chat" element={<TeamChatPage />} />
          <Route path="/enterprise/teams/:teamId" element={<LocationEcho />} />
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

describe('TeamChatPage', () => {
  it('renders the ownership header with team name, TL identity and avatar', async () => {
    stubChatFetch();
    renderChat();

    expect(await screen.findByRole('heading', { name: '增长团队' })).toBeTruthy();
    expect(screen.getByText('TL：小艾')).toBeTruthy();
    expect(screen.getAllByLabelText(/员工头像/).length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: '返回团队' })).toBeTruthy();
  });

  it('navigates back to the team detail page', async () => {
    const user = userEvent.setup();
    stubChatFetch();
    renderChat();

    await screen.findByRole('heading', { name: '增长团队' });
    await user.click(screen.getByRole('button', { name: '返回团队' }));

    expect((await screen.findByTestId('location')).textContent).toBe('/enterprise/teams/team-1');
  });

  it('lists the TL conversation first with member conversations grouped below', async () => {
    const user = userEvent.setup();
    stubChatFetch();
    renderChat();

    const list = await screen.findByLabelText('会话列表');
    const tlItem = await within(list).findByRole('button', { name: /TL 对话/ });
    const taskItem = within(list).getByRole('button', { name: /员工头像 写周报/ });
    // TL 对话固定在最上
    expect(tlItem.compareDocumentPosition(taskItem) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(tlItem).getByText('TL')).toBeTruthy();
    // kind 中文标签
    expect(within(list).getByText('任务执行')).toBeTruthy();
    expect(within(list).getByText('竞标')).toBeTruthy();
    expect(within(list).getByText('验收')).toBeTruthy();
    // preview 与相对时间
    expect(within(taskItem).getByText('周报已完成，请验收')).toBeTruthy();
    expect(within(taskItem).getByText('5 分钟前')).toBeTruthy();
    // 默认选中 TL 对话，点击后切换高亮
    expect(tlItem.getAttribute('aria-current')).toBe('true');
    await user.click(taskItem);
    expect(taskItem.getAttribute('aria-current')).toBe('true');
    expect(tlItem.getAttribute('aria-current')).toBeNull();
  });

  it('renders user messages on the right and agent replies on the left with avatar', async () => {
    stubChatFetch();
    renderChat();

    const area = await screen.findByLabelText('消息区');
    expect(await within(area).findByText('请拆解本月目标')).toBeTruthy();
    const userRow = within(area).getByText('请拆解本月目标').closest('[data-role]') as HTMLElement;
    expect(userRow.getAttribute('data-role')).toBe('user');
    expect(userRow.className).toContain('justify-end');

    const agentRow = within(area).getByText('好的，我来拆解').closest('[data-role]') as HTMLElement;
    expect(agentRow.getAttribute('data-role')).toBe('agent');
    expect(agentRow.className).toContain('justify-start');
    expect(within(agentRow).getByLabelText(/员工头像/)).toBeTruthy();
  });

  it('sends a TL message via /api/chat/turn and appends both messages', async () => {
    const user = userEvent.setup();
    const fetchMock = stubChatFetch({ reply: '收到，马上安排' });
    renderChat();

    const input = await screen.findByLabelText('输入消息');
    await user.type(input, '帮我规划下周任务');
    await user.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => {
      const turnCall = fetchMock.mock.calls.find(([input, init]) =>
        String(input).includes('/api/chat/turn') && String(init?.method || '').toUpperCase() === 'POST',
      );
      expect(turnCall).toBeTruthy();
      const body = JSON.parse(String(turnCall?.[1]?.body)) as Record<string, unknown>;
      expect(body.session_id).toBe('session-tl-1');
      expect(body.message).toBe('帮我规划下周任务');
      expect(body.tenant_id).toBeTruthy();
    });
    expect(await screen.findByText('帮我规划下周任务')).toBeTruthy();
    expect(await screen.findByText('收到，马上安排')).toBeTruthy();
    expect((screen.getByLabelText('输入消息') as HTMLInputElement).value).toBe('');
  });

  it('shows a read-only notice instead of the composer for member conversations', async () => {
    const user = userEvent.setup();
    stubChatFetch();
    renderChat();

    const list = await screen.findByLabelText('会话列表');
    await user.click(within(list).getByRole('button', { name: /员工头像 写周报/ }));

    expect(await screen.findByText('任务会话仅可查看')).toBeTruthy();
    expect(screen.queryByLabelText('输入消息')).toBeNull();
    // 只读会话仍能查看历史消息
    const area = screen.getByLabelText('消息区');
    expect(await within(area).findByText('周报已完成，请验收')).toBeTruthy();
  });

  it('creates the TL session first when tl.session_id is null', async () => {
    const fetchMock = stubChatFetch({ tlSessionId: null, createdSessionId: 'session-tl-created' });
    renderChat();

    await waitFor(() => {
      const createCall = fetchMock.mock.calls.find(([input, init]) =>
        String(input).includes('/teams/team-1/tl/session')
        && String(init?.method || '').toUpperCase() === 'POST',
      );
      expect(createCall).toBeTruthy();
      const body = JSON.parse(String(createCall?.[1]?.body)) as Record<string, unknown>;
      expect(body.tenant_id).toBeTruthy();
    });
    // 创建成功后才拉取新会话的消息
    await waitFor(() => {
      const messagesCall = fetchMock.mock.calls.find(([input]) =>
        String(input).includes('/conversations/session-tl-created/messages'),
      );
      expect(messagesCall).toBeTruthy();
    });
    // 会话就绪后输入框可用
    await waitFor(() => {
      expect((screen.getByLabelText('输入消息') as HTMLInputElement).disabled).toBe(false);
    });
  });
});
