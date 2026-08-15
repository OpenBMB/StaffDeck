// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AgentProfileRead, TeamRead } from '@/types';

import TeamCollaborationPanel from './TeamCollaborationPanel';

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
  it('shows leader-to-member cards and summarizes injected prompts in the dialog', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
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
            updated_at: '2026-08-15T00:01:00Z',
          },
        ],
      });
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();

    render(<TeamCollaborationPanel team={team} agents={agents} />);

    const card = await screen.findByRole('button', { name: /季度报告/ });
    expect(screen.getByText('人事 → 行政')).toBeTruthy();
    expect(screen.getAllByLabelText(/员工头像/).length).toBe(2);

    await user.click(card);

    expect(await screen.findByText('委派任务「季度报告」')).toBeTruthy();
    expect(screen.getByText('季度报告已经整理完成。')).toBeTruthy();
    expect(screen.queryByText(/你是团队/)).toBeNull();
  });
});
