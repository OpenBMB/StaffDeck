// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { employeeProfile } from '@/employee';
import { I18nProvider } from '@/i18n';
import type { AgentProfileRead, ChatSession, TeamRead } from '@/types';

import type { UseChatSession } from '../useChatSession';
import ChatEmptyState from './ChatEmptyState';

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
  description: '负责增长实验与内容投放',
  owner_user_id: 'user-1',
  config: {},
  status: 'active',
  members: [
    { id: 'm-1', team_id: 'team-1', agent_id: 'agent-1', role: 'leader', agent_name: '小艾', created_at: '2026-08-01T00:00:00Z' },
    { id: 'm-2', team_id: 'team-1', agent_id: 'agent-2', role: 'member', agent_name: '小北', created_at: '2026-08-01T00:00:00Z' },
    { id: 'm-3', team_id: 'team-1', agent_id: 'agent-3', role: 'member', agent_name: '小南', created_at: '2026-08-01T00:00:00Z' },
    { id: 'm-4', team_id: 'team-1', agent_id: 'agent-4', role: 'member', agent_name: '小西', created_at: '2026-08-01T00:00:00Z' },
  ],
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
};

function buildChat(session: Partial<ChatSession>, extra: Record<string, unknown> = {}): UseChatSession {
  return {
    currentSession: {
      id: 'session-1',
      tenant_id: 'tenant_demo',
      status: 'active',
      updated_at: '2026-08-01T00:00:00Z',
      ...session,
    } as ChatSession,
    ...extra,
  } as unknown as UseChatSession;
}

function renderEmptyState(chat: UseChatSession) {
  return render(
    <I18nProvider>
      <ChatEmptyState chat={chat} />
    </I18nProvider>,
  );
}

afterEach(() => {
  cleanup();
});

describe('ChatEmptyState team card', () => {
  it('renders the team card for team sessions', () => {
    renderEmptyState(buildChat(
      { team_id: 'team-1', team_name: '增长团队' },
      { displayedTeam: team, agents: [agent], teamEmptyStats: { tasks: 2, blackboard: 3 } },
    ));

    expect(screen.getByText(/Hello 我们是/).textContent).toContain('增长团队');
    expect(screen.getByText('我们来做什么？')).toBeTruthy();
    expect(screen.getByText('负责增长实验与内容投放')).toBeTruthy();
    // 成员名标签，TL 带后缀标识
    expect(screen.getByText(/小艾 · TL/)).toBeTruthy();
    expect(screen.getByText(/小北/)).toBeTruthy();
    // 统计格：成员数 / 任务数 / 黑板条目数
    expect(screen.getByText('成员数')).toBeTruthy();
    expect(screen.getByText('4')).toBeTruthy();
    expect(screen.getByText('任务数')).toBeTruthy();
    expect(screen.getByText('2')).toBeTruthy();
    expect(screen.getByText('黑板条目数')).toBeTruthy();
    expect(screen.getByText('3')).toBeTruthy();
  });

  it('falls back to team_name and a member-count summary when the team is not loaded', () => {
    renderEmptyState(buildChat(
      { team_id: 'team-1', team_name: '增长团队' },
      { displayedTeam: null, agents: [], teamEmptyStats: { tasks: 0, blackboard: 0 } },
    ));

    expect(screen.getByText(/Hello 我们是/).textContent).toContain('增长团队');
    expect(screen.getByText(/团队由 0 名成员组成/)).toBeTruthy();
  });

  it('still renders the employee card for regular sessions', () => {
    renderEmptyState(buildChat(
      { agent_id: 'agent-1' },
      {
        displayedAgent: agent,
        displayedProfile: employeeProfile(agent),
        emptyRoleSummary: 'role summary',
        emptyProfileTags: ['结构化整理'],
        emptyStats: [
          { label: '资料', value: 1 },
          { label: '技能', value: 2 },
          { label: 'SOP', value: 3 },
        ],
        displayedTeam: null,
        agents: [agent],
        teamEmptyStats: { tasks: 0, blackboard: 0 },
      },
    ));

    expect(screen.getByText(/Hello 我是/).textContent).toContain('小艾');
    expect(screen.queryByText(/Hello 我们是/)).toBeNull();
    expect(screen.getByText('资料')).toBeTruthy();
  });
});
