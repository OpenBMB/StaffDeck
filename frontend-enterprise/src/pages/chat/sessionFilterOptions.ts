import {
  employeeDisplayName,
  employeeDisplayNameWithCreator,
} from '@/employee';
import { toTeamScope } from '@/lib/agent-scope-storage';
import type { AgentProfileRead, ChatSession } from '@/types';

export type SessionFilterOption = {
  value: string;
  label: string;
};

export function buildSessionFilterOptions(
  agents: readonly AgentProfileRead[],
  sessions: readonly ChatSession[],
  activeDraftAgentId = '',
): SessionFilterOption[] {
  const counts = new Map<string, number>();
  const teamCounts = new Map<string, { name: string; count: number }>();
  sessions.forEach((session) => {
    if (session.agent_id) {
      counts.set(session.agent_id, (counts.get(session.agent_id) || 0) + 1);
    }
    if (session.team_id) {
      const entry = teamCounts.get(session.team_id);
      const name = session.team_name || entry?.name || '团队';
      teamCounts.set(session.team_id, { name, count: (entry?.count || 0) + 1 });
    }
  });

  const rows = agents
    .filter((agent) => (counts.get(agent.id) || 0) > 0 || agent.id === activeDraftAgentId)
    .sort((left, right) => (
      employeeDisplayName(left).localeCompare(employeeDisplayName(right), 'zh-Hans-CN')
    ));

  const teamRows = [...teamCounts.entries()]
    .sort((left, right) => left[1].name.localeCompare(right[1].name, 'zh-Hans-CN'))
    .map(([teamId, entry]) => ({
      value: toTeamScope(teamId),
      label: `${entry.name} · ${entry.count}`,
    }));

  return [
    { value: 'all', label: `全部会话 · ${sessions.length}` },
    ...rows.map((agent) => {
      const count = counts.get(agent.id) || 0;
      return {
        value: agent.id,
        // Keep an active zero-session draft selectable without exposing a noisy “· 0” item.
        label: count > 0
          ? `${employeeDisplayNameWithCreator(agent)} · ${count}`
          : employeeDisplayNameWithCreator(agent),
      };
    }),
    ...teamRows,
  ];
}
