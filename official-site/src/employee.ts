import type { AgentProfileRead, AgentResourceBindingRead, AgentResourceType } from './types';
import {
  isEmployeeOwnedBy,
  isEnterpriseAdmin,
  isGalleryEmployee,
  type EnterpriseAuthUser,
} from './auth';

import avatarAfterSales from './assets/staffdeck/staffdeck-avatar-after-sales.png';
import avatarCommerce from './assets/staffdeck/staffdeck-avatar-commerce.png';
import avatarDefault from './assets/staffdeck/staffdeck-avatar-default.png';
import avatarKnowledge from './assets/staffdeck/staffdeck-avatar-knowledge.png';
import avatarOps from './assets/staffdeck/staffdeck-avatar-ops.png';
import avatarOverall from './assets/staffdeck/staffdeck-avatar-overall.png';
import avatarQuality from './assets/staffdeck/staffdeck-avatar-quality.png';
import avatarService from './assets/staffdeck/staffdeck-avatar-service.png';

export type EmployeeProfile = {
  roleKey: string;
  roleName: string;
  avatarText: string;
  avatarTone: string;
  avatarKind: 'preset' | 'upload';
  avatarPreset: string;
  avatarImage: string;
  onboardedAt: string;
  workStyles: string[];
  expertiseTags: string[];
  workModes: string[];
};

export type EmployeeAvatarPreset = {
  key: string;
  label: string;
  text: string;
  tone: string;
};

export type EmployeeTemplate = {
  key: string;
  roleName: string;
  avatarText: string;
  avatarTone: string;
  avatarPreset: string;
  description: string;
  workStyles: string[];
  expertiseTags: string[];
  workModes: string[];
};

type EmployeeAgentLike = {
  id?: string;
  name?: string;
  is_overall?: boolean;
  metadata?: Record<string, unknown>;
};

export const EMPLOYEE_AVATAR_PRESETS: EmployeeAvatarPreset[] = [
  { key: 'service-orbit', label: '研发员工', text: '研', tone: 'teal' },
  { key: 'after-sales-seal', label: '行政员工', text: '行', tone: 'copper' },
  { key: 'knowledge-node', label: '知识运营员工', text: '知', tone: 'olive' },
  { key: 'commerce-compass', label: '财务员工', text: '财', tone: 'blue' },
  { key: 'ops-grid', label: '人事员工', text: '人', tone: 'ink' },
  { key: 'quality-star', label: '法务员工', text: '法', tone: 'gold' },
];

export const DEFAULT_AVATAR_PRESET = 'service-orbit';

const PRESET_AVATAR_IMAGES: Record<string, string> = {
  'service-orbit': avatarService,
  'after-sales-seal': avatarAfterSales,
  'knowledge-node': avatarKnowledge,
  'commerce-compass': avatarCommerce,
  'ops-grid': avatarOps,
  'quality-star': avatarQuality,
  overall: avatarOverall,
};

type AvatarSource = Pick<EmployeeProfile, 'avatarKind' | 'avatarImage' | 'avatarPreset'>;

export function isUploadedAvatar(profile: AvatarSource): boolean {
  return profile.avatarKind === 'upload' && Boolean(profile.avatarImage);
}

/** Resolve the image URL for an employee avatar (uploaded image or preset illustration). */
export function employeeAvatarImage(profile: AvatarSource): string {
  if (isUploadedAvatar(profile)) return profile.avatarImage;
  return PRESET_AVATAR_IMAGES[profile.avatarPreset || DEFAULT_AVATAR_PRESET] || avatarDefault;
}

export const EMPLOYEE_TEMPLATES: EmployeeTemplate[] = [
  {
    key: 'service-specialist',
    roleName: '研发',
    avatarText: '研',
    avatarTone: 'teal',
    avatarPreset: 'service-orbit',
    description: '负责研发资料查询、代码任务拆解、SOP 执行和交付记录沉淀。',
    workStyles: ['目标明确', '证据优先', '动作可追溯'],
    expertiseTags: ['研发协作', '代码检索', 'SOP 执行'],
    workModes: ['理解需求', '检索资料', '推进执行'],
  },
  {
    key: 'after-sales',
    roleName: '行政',
    avatarText: '行',
    avatarTone: 'copper',
    avatarPreset: 'after-sales-seal',
    description: '负责会议纪要、资料归档、跨部门事务跟进和结果同步。',
    workStyles: ['流程推进', '及时追问', '留痕复盘'],
    expertiseTags: ['资料归档', '会议纪要', '事务跟进'],
    workModes: ['确认事项', '拆解步骤', '同步结果'],
  },
  {
    key: 'knowledge-operator',
    roleName: '知识运营',
    avatarText: '知',
    avatarTone: 'olive',
    avatarPreset: 'knowledge-node',
    description: '负责知识库检索、资料结构化归档、信息核对和答案沉淀。',
    workStyles: ['证据优先', '结构清晰', '持续沉淀'],
    expertiseTags: ['知识检索', '资料归档', '信息结构化'],
    workModes: ['查资料', '做归档', '给答案'],
  },
  {
    key: 'commerce-guide',
    roleName: '财务',
    avatarText: '财',
    avatarTone: 'blue',
    avatarPreset: 'commerce-compass',
    description: '负责报销核对、预算口径、财务资料检索和风险提示。',
    workStyles: ['证据优先', '口径统一', '风险克制'],
    expertiseTags: ['报销核对', '预算口径', '数据复盘'],
    workModes: ['查规则', '核凭证', '给结论'],
  },
];

export function staffdeckDisplayText(value: string): string {
  return value;
}

export function isDefaultEmployeeAgent(agent?: EmployeeAgentLike | null): boolean {
  if (!agent || agent.is_overall) return false;
  const metadata = agent.metadata || {};
  return metadata.is_default_employee === true;
}

export function preferredEmployeeAgent<T extends EmployeeAgentLike>(agents: T[]): T | undefined {
  return agents.find(isDefaultEmployeeAgent) || agents.find((item) => !item.is_overall);
}

export type EmployeeVisibilityOptions = {
  activeOnly?: boolean;
  excludeAgentId?: string;
  includeDefault?: boolean;
  includeOverall?: boolean;
};

export function canAccessEmployeeAgent(
  agent: AgentProfileRead,
  user?: EnterpriseAuthUser | null,
  options: EmployeeVisibilityOptions = {},
): boolean {
  if (options.excludeAgentId && agent.id === options.excludeAgentId) return false;
  if (options.activeOnly && agent.status !== 'active') return false;

  const includeOverall = options.includeOverall ?? false;
  if (isEnterpriseAdmin(user)) return includeOverall || !agent.is_overall;
  if (agent.is_overall) return false;

  const includeDefault = options.includeDefault ?? false;
  return (
    (includeDefault && isDefaultEmployeeAgent(agent))
    || isEmployeeOwnedBy(agent, user)
    || isGalleryEmployee(agent)
  );
}

export function isEmployeeUsedByCurrentUser(agent: AgentProfileRead): boolean {
  const metadata = agent.metadata || {};
  return metadata.used_by_current_user === true || metadata.chat_used_by_current_user === true;
}

export function canSelectCurrentEmployeeAgent(
  agent: AgentProfileRead,
  user?: EnterpriseAuthUser | null,
  options: EmployeeVisibilityOptions = {},
): boolean {
  if (options.excludeAgentId && agent.id === options.excludeAgentId) return false;
  if (options.activeOnly && agent.status !== 'active') return false;

  const includeOverall = options.includeOverall ?? false;
  if (isEnterpriseAdmin(user)) {
    if (agent.is_overall) return includeOverall;
    if (isGalleryEmployee(agent) && !isEmployeeOwnedBy(agent, user)) {
      return isEmployeeUsedByCurrentUser(agent);
    }
    return true;
  }
  if (agent.is_overall) return false;

  const includeDefault = options.includeDefault ?? false;
  return (
    (includeDefault && isDefaultEmployeeAgent(agent))
    || isEmployeeOwnedBy(agent, user)
    || (isGalleryEmployee(agent) && isEmployeeUsedByCurrentUser(agent))
  );
}

export function canManageEmployeeAgent(
  agent: AgentProfileRead,
  user?: EnterpriseAuthUser | null,
): boolean {
  if (agent.is_overall) return isEnterpriseAdmin(user);
  return isEnterpriseAdmin(user) || isEmployeeOwnedBy(agent, user);
}

export function isMyEmployeeAgent(
  agent: AgentProfileRead,
  user?: EnterpriseAuthUser | null,
): boolean {
  return !agent.is_overall && isEmployeeOwnedBy(agent, user);
}

export function visibleEmployeeAgents(
  rows: AgentProfileRead[],
  user?: EnterpriseAuthUser | null,
  options: EmployeeVisibilityOptions = {},
): AgentProfileRead[] {
  return rows.filter((agent) => canAccessEmployeeAgent(agent, user, options));
}

export function currentEmployeeAgents(
  rows: AgentProfileRead[],
  user?: EnterpriseAuthUser | null,
  options: EmployeeVisibilityOptions = {},
): AgentProfileRead[] {
  return rows.filter((agent) => canSelectCurrentEmployeeAgent(agent, user, options));
}

export function openGalleryAgent(rows: AgentProfileRead[]): AgentProfileRead | undefined {
  return rows.find((agent) => agent.is_overall);
}

export function openGalleryAgentId(rows: AgentProfileRead[]): string {
  return openGalleryAgent(rows)?.id || '';
}

export function openGalleryImportSourceOptions(
  rows: AgentProfileRead[],
  label: string,
): Array<{ value: string; label: string }> {
  const agentId = openGalleryAgentId(rows);
  return agentId ? [{ value: agentId, label }] : [];
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String).filter(Boolean) : [];
}

function stringFromMeta(metadata: Record<string, unknown>, key: string): string {
  const value = metadata[key];
  return typeof value === 'string' ? value : '';
}

function firstString(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return '';
}

export function creatorNameFromMetadata(
  metadata?: Record<string, unknown> | null,
  fallback = '',
): string {
  const meta = metadata || {};
  const creator = firstString(
    meta.creator_name,
    meta.created_by,
    meta.created_by_display_name,
    meta.created_by_username,
    meta.owner_display_name,
    meta.owner_username,
    meta.gallery_published_by,
    meta.created_by_user_id,
    meta.owner_user_id,
  );
  if (!creator) return fallback;
  const normalized = creator.trim();
  return normalized || fallback;
}

export function displayNameWithCreator(name: string, creator?: string): string {
  const cleanName = name.trim() || '未命名';
  const cleanCreator = (creator || '').trim();
  if (!cleanCreator) return cleanName;
  return `${cleanName} @${cleanCreator}`;
}

export function employeeProfile(agent?: AgentProfileRead | null): EmployeeProfile {
  const metadata = agent?.metadata || {};
  const template = EMPLOYEE_TEMPLATES.find((item) => item.key === metadata.role_key);
  const preset = EMPLOYEE_AVATAR_PRESETS.find((item) => item.key === metadata.avatar_preset)
    || (template ? EMPLOYEE_AVATAR_PRESETS.find((item) => item.key === template.avatarPreset) : undefined)
    || EMPLOYEE_AVATAR_PRESETS[0];
  const isOverall = Boolean(agent?.is_overall);
  const avatarKind = stringFromMeta(metadata, 'avatar_kind') === 'upload' && stringFromMeta(metadata, 'avatar_image')
    ? 'upload'
    : 'preset';
  return {
    roleKey: stringFromMeta(metadata, 'role_key') || template?.key || '',
    roleName: isOverall ? '开放广场' : stringFromMeta(metadata, 'role_name') || template?.roleName || '待补充岗位',
    avatarText: isOverall ? '广' : stringFromMeta(metadata, 'avatar_text') || preset.text || template?.avatarText || '员',
    avatarTone: isOverall ? 'overall' : stringFromMeta(metadata, 'avatar_tone') || preset.tone || template?.avatarTone || 'teal',
    avatarKind: isOverall ? 'preset' : avatarKind,
    avatarPreset: isOverall ? 'overall' : stringFromMeta(metadata, 'avatar_preset') || preset.key,
    avatarImage: isOverall ? '' : stringFromMeta(metadata, 'avatar_image'),
    onboardedAt: stringFromMeta(metadata, 'onboarded_at') || agent?.created_at?.slice(0, 10) || '-',
    workStyles: asStringArray(metadata.work_styles),
    expertiseTags: asStringArray(metadata.expertise_tags),
    workModes: asStringArray(metadata.work_modes),
  };
}

export function employeeDisplayName(agent?: AgentProfileRead | null): string {
  if (!agent) return '数字员工';
  if (agent.is_overall) return '开放广场';
  return agent.name || '数字员工';
}

export function employeeCreatorName(agent?: AgentProfileRead | null): string {
  return creatorNameFromMetadata(agent?.metadata);
}

export function employeeDisplayNameWithCreator(agent?: AgentProfileRead | null): string {
  return displayNameWithCreator(employeeDisplayName(agent), employeeCreatorName(agent));
}

export function resourceCreatorName(resource?: { metadata?: Record<string, unknown> } | null): string {
  return creatorNameFromMetadata(resource?.metadata);
}

export function resourceDisplayNameWithCreator(
  name: string,
  resource?: { metadata?: Record<string, unknown> } | null,
): string {
  return displayNameWithCreator(name, resourceCreatorName(resource));
}

export function resourceCount(resources: AgentResourceBindingRead[] | undefined, type: AgentResourceBindingRead['resource_type']): number {
  return (resources || []).filter((item) => (
    item.resource_type === type
    && item.status !== 'deleted'
    && item.status !== 'inactive'
  )).length;
}

/** Employees selectable in the chat sidebar: active employees visible to the current user. */
export function visibleChatEmployees(
  rows: AgentProfileRead[],
  user?: EnterpriseAuthUser | null,
): AgentProfileRead[] {
  return currentEmployeeAgents(rows, user, { activeOnly: true });
}

export function agentResourceCount(agent: AgentProfileRead, resourceType: AgentResourceType): number {
  return (agent.resources || []).filter((resource) => (
    resource.resource_type === resourceType
    && resource.status !== 'deleted'
    && resource.status !== 'inactive'
  )).length;
}

export function activeResourceCount(resources: AgentResourceBindingRead[] | undefined): number {
  return (resources || []).filter((item) => item.status === 'active').length;
}

export function employeeMetadataFromTemplate(templateKey: string, currentMetadata: Record<string, unknown> = {}): Record<string, unknown> {
  const template = EMPLOYEE_TEMPLATES.find((item) => item.key === templateKey) || EMPLOYEE_TEMPLATES[0];
  return {
    ...currentMetadata,
    role_key: template.key,
    role_name: template.roleName,
    avatar_text: template.avatarText,
    avatar_tone: template.avatarTone,
    avatar_kind: 'preset',
    avatar_preset: template.avatarPreset,
    onboarded_at: currentMetadata.onboarded_at || new Date().toISOString().slice(0, 10),
    work_styles: template.workStyles,
    expertise_tags: template.expertiseTags,
    work_modes: template.workModes,
  };
}

export function employeeBlankMetadata(currentMetadata: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    ...currentMetadata,
    blank_onboarding: true,
    role_key: stringFromMeta(currentMetadata, 'role_key'),
    role_name: stringFromMeta(currentMetadata, 'role_name') || '待补充职位',
    avatar_text: stringFromMeta(currentMetadata, 'avatar_text') || '员',
    avatar_tone: stringFromMeta(currentMetadata, 'avatar_tone') || 'teal',
    avatar_kind: stringFromMeta(currentMetadata, 'avatar_kind') || 'preset',
    avatar_preset: stringFromMeta(currentMetadata, 'avatar_preset') || EMPLOYEE_AVATAR_PRESETS[0].key,
    onboarded_at: currentMetadata.onboarded_at || new Date().toISOString().slice(0, 10),
    work_styles: asStringArray(currentMetadata.work_styles),
    expertise_tags: asStringArray(currentMetadata.expertise_tags),
    work_modes: asStringArray(currentMetadata.work_modes),
  };
}
