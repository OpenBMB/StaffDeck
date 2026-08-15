import { describe, expect, it } from 'vitest';

import type { AgentProfileRead, ChatSession } from '@/types';

import { buildSessionFilterOptions } from './sessionFilterOptions';

function agent(id: string, name: string): AgentProfileRead {
  return {
    id,
    tenant_id: 'tenant-demo',
    name,
    is_overall: false,
    status: 'active',
    metadata: {},
    resources: [],
    created_at: '2026-08-02T00:00:00Z',
    updated_at: '2026-08-02T00:00:00Z',
  };
}

function session(id: string, agentId: string): ChatSession {
  return {
    id,
    tenant_id: 'tenant-demo',
    agent_id: agentId,
    status: 'completed',
    updated_at: '2026-08-02T00:00:00Z',
  };
}

describe('chat session filter options', () => {
  it('keeps the aggregate option and excludes agents with no sessions', () => {
    const agents = [agent('agent-c', 'Gamma'), agent('agent-b', 'Beta'), agent('agent-a', 'Alpha')];
    const originalOrder = agents.map((item) => item.id);
    const sessions = [
      session('session-a1', 'agent-a'),
      session('session-a2', 'agent-a'),
      session('session-b1', 'agent-b'),
    ];

    expect(buildSessionFilterOptions(agents, sessions)).toEqual([
      { value: 'all', label: '全部会话 · 3' },
      { value: 'agent-a', label: 'Alpha · 2' },
      { value: 'agent-b', label: 'Beta · 1' },
    ]);
    expect(agents.map((item) => item.id)).toEqual(originalOrder);
  });

  it('keeps an active draft selectable without showing a zero count', () => {
    const options = buildSessionFilterOptions(
      [agent('agent-a', 'Alpha'), agent('agent-draft', 'Draft Agent')],
      [session('session-a1', 'agent-a')],
      'agent-draft',
    );

    expect(options).toContainEqual({ value: 'agent-draft', label: 'Draft Agent' });
    expect(options.some((option) => option.label.includes('· 0'))).toBe(false);
  });

  it('counts team groups separately from the project leader employee', () => {
    const group = {
      ...session('session-team', 'agent-a'),
      team_id: 'team-1',
      team_name: '增长团队',
    };

    expect(buildSessionFilterOptions([agent('agent-a', 'Alpha')], [group])).toEqual([
      { value: 'all', label: '全部会话 · 1' },
      { value: 'team:team-1', label: '增长团队 · 1' },
    ]);
  });
});
