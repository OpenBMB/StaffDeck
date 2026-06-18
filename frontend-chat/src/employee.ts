import type { AgentProfileRead } from './types';

export type EmployeeProfile = {
  roleName: string;
  avatarText: string;
  avatarTone: string;
};

function stringFromMeta(metadata: Record<string, unknown> | undefined, key: string): string {
  const value = metadata?.[key];
  return typeof value === 'string' ? value : '';
}

export function employeeProfile(agent?: AgentProfileRead | null): EmployeeProfile {
  if (agent?.is_overall) {
    return { roleName: '组织资源库', avatarText: '组', avatarTone: 'overall' };
  }
  return {
    roleName: stringFromMeta(agent?.metadata, 'role_name') || '在线客服员工',
    avatarText: stringFromMeta(agent?.metadata, 'avatar_text') || '员',
    avatarTone: stringFromMeta(agent?.metadata, 'avatar_tone') || 'teal',
  };
}

export function employeeDisplayName(agent?: AgentProfileRead | null): string {
  if (!agent) return '数字员工';
  if (agent.is_overall) return '组织资源库';
  return (agent.name || '数字员工').replace(/智能体/g, '员工');
}
