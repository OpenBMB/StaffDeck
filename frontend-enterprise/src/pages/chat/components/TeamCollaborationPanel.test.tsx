// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AgentProfileRead, ChatMessage, TeamConversationRead, TeamRead } from '@/types';
import { I18nProvider } from '@/i18n';

import TeamCollaborationPanel, {
  collaborationQuestion,
  mergeTeamChatTimeline,
} from './TeamCollaborationPanel';

const agents: AgentProfileRead[] = [
  {
    id: 'agent-leader',
    tenant_id: 'tenant_demo',
    name: '人事',
    is_overall: false,
    status: 'active',
    metadata: { employee_profile: { avatar_text: '人', avatar_preset: 'ops-grid' } },
    resources: [],
    created_at: '2026-08-15T00:00:00Z',
    updated_at: '2026-08-15T00:00:00Z',
  },
  {
    id: 'agent-admin',
    tenant_id: 'tenant_demo',
    name: '行政',
    is_overall: false,
    status: 'active',
    metadata: { employee_profile: { avatar_text: '行', avatar_preset: 'after-sales-seal' } },
    resources: [],
    created_at: '2026-08-15T00:00:00Z',
    updated_at: '2026-08-15T00:00:00Z',
  },
];

const team: TeamRead = {
  id: 'team-1',
  tenant_id: 'tenant_demo',
  name: '运营团队',
  owner_user_id: 'user-1',
  config: {},
  status: 'active',
  members: [
    {
      id: 'member-leader',
      team_id: 'team-1',
      agent_id: 'agent-leader',
      agent_name: '人事',
      role: 'leader',
      created_at: '2026-08-15T00:00:00Z',
    },
    {
      id: 'member-admin',
      team_id: 'team-1',
      agent_id: 'agent-admin',
      agent_name: '行政',
      role: 'member',
      created_at: '2026-08-15T00:00:00Z',
    },
  ],
  created_at: '2026-08-15T00:00:00Z',
  updated_at: '2026-08-15T00:00:00Z',
};

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body),
  } as Response;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('TeamCollaborationPanel', () => {
  it('renders leader mentions and expands only the member reply inline', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/stream')) {
        return jsonResponse({
          status: 'completed',
          content: '季度报告已经整理完成。',
          updated_at: '2026-08-15T00:01:00Z',
        });
      }
      if (url.includes('/messages')) {
        return jsonResponse([
          {
            id: 'message-1',
            role: 'user',
            content: '你是团队「运营团队」的成员,请完成以下团队任务。\n任务标题:季度报告',
            created_at: '2026-08-15T00:00:00Z',
          },
          {
            id: 'message-2',
            role: 'assistant',
            content: '季度报告已经整理完成。',
            created_at: '2026-08-15T00:01:00Z',
          },
        ]);
      }
      return jsonResponse({
        team_id: team.id,
        team_name: team.name,
        tl: { agent_id: 'agent-leader', agent_name: '人事', session_id: 'session-group' },
        conversations: [
          {
            session_id: 'session-task',
            kind: 'member_task',
            agent_id: 'agent-admin',
            agent_name: '行政',
            task_id: 'task-1',
            title: '团队任务:季度报告',
            preview: '季度报告已经整理完成。',
            created_at: '2026-08-15T00:00:30Z',
            updated_at: '2026-08-15T00:01:00Z',
          },
        ],
      });
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();

    render(<TeamCollaborationPanel team={team} agents={agents} />);

    expect(await screen.findByText('@行政')).toBeTruthy();
    expect(screen.getByText('，请处理「季度报告」')).toBeTruthy();
    expect(screen.getByText('项目领导')).toBeTruthy();
    expect(screen.getAllByLabelText(/员工头像/).length).toBe(2);
    expect(screen.getByText('行政回复：季度报告已经整理完成。')).toBeTruthy();

    const reply = screen.getByRole('button', { name: '展开行政的回复' });
    expect(reply.getAttribute('aria-expanded')).toBe('false');

    await user.click(reply);

    expect(await screen.findByRole('button', { name: '收起行政的回复' })).toBeTruthy();
    expect(screen.getAllByText('季度报告已经整理完成。').length).toBe(1);
    expect(screen.queryByText(/你是团队/)).toBeNull();
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/session-task/messages'),
      expect.any(Object),
    );
  });

  it('formats collaboration as a direct mention from the project leader', () => {
    expect(collaborationQuestion({
      session_id: 'session-task',
      kind: 'member_task',
      agent_id: 'agent-admin',
      agent_name: '行政',
      task_id: 'task-1',
      title: '团队任务:季度报告',
      preview: '',
      created_at: '2026-08-15T00:00:30Z',
      updated_at: '2026-08-15T00:01:00Z',
    })).toBe('@行政，请处理「季度报告」');
  });

  it('lets the user answer a member question and resume the same team task', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ status: 'rework' }));
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    const conversation: TeamConversationRead = {
      session_id: 'session-needs-input',
      kind: 'member_task',
      agent_id: 'agent-admin',
      agent_name: '行政',
      task_id: 'task-purchase',
      task_status: 'escalated',
      needs_input: true,
      pending_question: '请提供员工工号和物品清单。',
      title: '团队任务:采购物品',
      preview: '请提供员工工号和物品清单。',
      created_at: '2026-08-15T00:00:30Z',
      updated_at: '2026-08-15T00:01:00Z',
    };

    render(
      <TeamCollaborationPanel team={team} agents={agents} conversation={conversation} />,
    );

    expect(screen.getByText('行政需要补充信息')).toBeTruthy();
    expect(screen.getByText('请提供员工工号和物品清单。')).toBeTruthy();
    const answer = screen.getByRole('textbox', { name: '回复行政的补充问题' });
    await user.type(answer, '工号 001，需要 A4 纸 2 包。');
    await user.click(screen.getByRole('button', { name: '补充并继续' }));

    expect(await screen.findByText('已补充，任务正在继续执行')).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/enterprise/teams/team-1/tasks/task-purchase/resume',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          tenant_id: 'tenant_demo',
          answer: '工号 001，需要 A4 纸 2 包。',
        }),
      }),
    );
  });

  it('shows incremental member output while an expanded reply is running', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/stream')) {
        return jsonResponse({
          status: 'running',
          content: '正在整理采购清单',
          phase: '正在生成回复',
          updated_at: '2026-08-15T00:01:00Z',
        });
      }
      if (url.includes('/messages')) return jsonResponse([]);
      return jsonResponse({});
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    const conversation: TeamConversationRead = {
      session_id: 'session-running-task',
      kind: 'member_task',
      agent_id: 'agent-admin',
      agent_name: '行政',
      task_id: 'task-running',
      task_status: 'in_progress',
      title: '团队任务:整理采购清单',
      preview: '',
      created_at: '2026-08-15T00:00:30Z',
      updated_at: '2026-08-15T00:01:00Z',
    };

    render(
      <TeamCollaborationPanel team={team} agents={agents} conversation={conversation} />,
    );
    await user.click(screen.getByRole('button', { name: '展开行政的回复' }));

    expect(await screen.findByText('正在整理采购清单')).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/session-running-task/stream'),
      expect.any(Object),
    );
  });

  it('reuses standalone Markdown, citations, and artifact cards for member replies', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/stream')) {
        return jsonResponse({
          status: 'completed',
          content: '## 制度结论\n\n请参考制度 [1]。\n\n```json\n{"blackboard_suggestions": []}\n```',
          updated_at: '2026-08-15T00:01:00Z',
        });
      }
      if (url.includes('/messages')) {
        return jsonResponse([
          {
            id: 'message-structured',
            role: 'assistant',
            content: '## 制度结论\n\n请参考制度 [1]。',
            metadata: {
              knowledge_citations: [
                { id: '1', label: '1', title: '报销制度', excerpt: '制度正文' },
              ],
              harness_artifacts: [
                {
                  type: 'workspace_file',
                  task_frame_id: 'frame-1',
                  path: 'results/policy.md',
                },
              ],
            },
            created_at: '2026-08-15T00:01:00Z',
          },
        ]);
      }
      return jsonResponse({});
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    const onOpenCitation = vi.fn();
    const conversation: TeamConversationRead = {
      session_id: 'session-structured',
      kind: 'member_task',
      agent_id: 'agent-admin',
      agent_name: '行政',
      task_id: 'task-structured',
      task_status: 'done',
      title: '团队任务:整理制度',
      preview: '请参考制度。',
      created_at: '2026-08-15T00:00:30Z',
      updated_at: '2026-08-15T00:01:00Z',
    };

    render(
      <TeamCollaborationPanel
        team={team}
        agents={agents}
        conversation={conversation}
        onOpenCitation={onOpenCitation}
      />,
    );
    await user.click(screen.getByRole('button', { name: '展开行政的回复' }));

    expect(await screen.findByRole('heading', { name: '制度结论' })).toBeTruthy();
    expect(screen.getByRole('button', { name: /报销制度/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /下载文件 policy.md/ })).toBeTruthy();
    expect(screen.queryByText(/blackboard_suggestions/)).toBeNull();

    await user.click(screen.getByRole('button', { name: /报销制度/ }));
    expect(onOpenCitation).toHaveBeenCalledWith(
      expect.objectContaining({ id: '1', title: '报销制度' }),
    );
  });

  it('externalizes each member safe execution record in the collaboration card', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/trace')) {
        return jsonResponse([
          {
            turn_id: 'turn-member-1',
            started_at: '2026-08-15T00:00:40Z',
            completed_at: '2026-08-15T00:01:00Z',
            lines: [
              {
                id: 'line-plan',
                kind: 'decision',
                text: '已拆分资料检索与制度比对',
                state: 'completed',
              },
              {
                id: 'line-tool',
                kind: 'tool',
                text: '已调用制度知识库',
                detail: '返回 3 条可引用结果',
                state: 'completed',
              },
            ],
          },
        ]);
      }
      if (url.includes('/stream')) {
        return jsonResponse({
          status: 'completed',
          content: '制度比对完成。',
          updated_at: '2026-08-15T00:01:00Z',
        });
      }
      if (url.includes('/messages')) {
        return jsonResponse([
          {
            id: 'message-traced',
            role: 'assistant',
            content: '制度比对完成。',
            created_at: '2026-08-15T00:01:00Z',
          },
        ]);
      }
      return jsonResponse({});
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    const conversation: TeamConversationRead = {
      session_id: 'session-traced',
      kind: 'member_task',
      agent_id: 'agent-admin',
      agent_name: '行政',
      task_id: 'task-traced',
      task_status: 'done',
      title: '团队任务:制度比对',
      preview: '制度比对完成。',
      created_at: '2026-08-15T00:00:30Z',
      updated_at: '2026-08-15T00:01:00Z',
    };

    render(
      <I18nProvider>
        <TeamCollaborationPanel team={team} agents={agents} conversation={conversation} />
      </I18nProvider>,
    );
    await user.click(screen.getByRole('button', { name: '展开行政的回复' }));

    const record = await screen.findByRole('button', { name: /执行记录/ });
    await user.click(record);
    expect(screen.getByText('已拆分资料检索与制度比对')).toBeTruthy();
    expect(screen.getByText('已调用制度知识库')).toBeTruthy();
    expect(screen.getByText('返回 3 条可引用结果')).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/session-traced/trace'),
      expect.any(Object),
    );
  });

  it('inserts collaboration exchanges at their original position in the chat timeline', () => {
    const messages: ChatMessage[] = [
      { id: 'm1', role: 'user', content: '开始', created_at: '2026-08-15T00:00:00Z' },
      { id: 'm2', role: 'assistant', content: '结束', created_at: '2026-08-15T00:10:00Z' },
    ];
    const conversations: TeamConversationRead[] = [
      {
        session_id: 'late',
        kind: 'member_task',
        agent_id: 'agent-admin',
        agent_name: '行政',
        title: '团队任务:后续任务',
        preview: '完成',
        created_at: '2026-08-15T00:11:00Z',
        updated_at: '2026-08-15T00:12:00Z',
      },
      {
        session_id: 'middle',
        kind: 'member_task',
        agent_id: 'agent-admin',
        agent_name: '行政',
        title: '团队任务:中间任务',
        preview: '完成',
        created_at: '2026-08-15T00:05:00Z',
        updated_at: '2026-08-15T00:06:00Z',
      },
    ];

    expect(mergeTeamChatTimeline(messages, conversations).map((entry) => (
      entry.kind === 'message' ? entry.message.id : entry.conversation.session_id
    ))).toEqual(['m1', 'middle', 'm2', 'late']);
  });
});
