export const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';
export const SELECTED_AGENT_STORAGE_KEY = ENTERPRISE_AGENT_STORAGE_KEY;
export const SESSION_FILTER_STORAGE_PREFIX = 'skill_agent_session_filter';

export function sessionFilterStorageKey(userId: string): string {
  return `${SESSION_FILTER_STORAGE_PREFIX}:${userId || 'anonymous'}`;
}

export function persistSharedAgentScope(agentId: string, userId?: string): void {
  void userId;
  if (!agentId) return;
  window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, agentId);
}

export function clearSharedAgentScope(userId?: string): void {
  void userId;
  window.localStorage.removeItem(ENTERPRISE_AGENT_STORAGE_KEY);
}

// Team scopes share the same storage slot as employee agent ids, prefixed so
// readers can tell "current team" apart from "current employee".
export const TEAM_SCOPE_PREFIX = 'team:';

export function toTeamScope(teamId: string): string {
  return teamId ? `${TEAM_SCOPE_PREFIX}${teamId}` : '';
}

export function isTeamScope(value: string | null | undefined): boolean {
  return typeof value === 'string'
    && value.startsWith(TEAM_SCOPE_PREFIX)
    && value.length > TEAM_SCOPE_PREFIX.length;
}

export function teamIdFromScope(value: string | null | undefined): string {
  return isTeamScope(value) ? String(value).slice(TEAM_SCOPE_PREFIX.length) : '';
}

/** 读取共享作用域；团队作用域对员工向页面视为"未选员工"，返回空串。 */
export function readEmployeeScope(): string {
  const raw = window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '';
  return isTeamScope(raw) ? '' : raw;
}

export function emitAgentScopeChange(agentId: string): void {
  window.dispatchEvent(
    new CustomEvent('ultrarag-enterprise-agent-scope-change', {
      detail: { agentId },
    }),
  );
}
