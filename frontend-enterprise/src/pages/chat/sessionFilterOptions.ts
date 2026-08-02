import {
  employeeDisplayName,
  employeeDisplayNameWithCreator,
} from '@/employee';
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
  sessions.forEach((session) => {
    if (!session.agent_id) return;
    counts.set(session.agent_id, (counts.get(session.agent_id) || 0) + 1);
  });

  const rows = agents
    .filter((agent) => (counts.get(agent.id) || 0) > 0 || agent.id === activeDraftAgentId)
    .sort((left, right) => (
      employeeDisplayName(left).localeCompare(employeeDisplayName(right), 'zh-Hans-CN')
    ));

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
  ];
}
