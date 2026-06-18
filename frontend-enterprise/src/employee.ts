import type { AgentProfileRead, AgentResourceBindingRead } from './types';

export type EmployeeProfile = {
  roleKey: string;
  roleName: string;
  avatarText: string;
  avatarTone: string;
  onboardedAt: string;
  workStyles: string[];
  expertiseTags: string[];
  workModes: string[];
};

export type EmployeeTemplate = {
  key: string;
  roleName: string;
  avatarText: string;
  avatarTone: string;
  description: string;
  workStyles: string[];
  expertiseTags: string[];
  workModes: string[];
};

export const EMPLOYEE_TEMPLATES: EmployeeTemplate[] = [
  {
    key: 'service-specialist',
    roleName: '在线客服员工',
    avatarText: '客',
    avatarTone: 'teal',
    description: '负责接待用户咨询、识别意图、推进购买和基础售后问题。',
    workStyles: ['事实先行', '流程推进', '及时追问'],
    expertiseTags: ['用户接待', '购买引导', '售后分诊'],
    workModes: ['先确认诉求', '调用 SOP', '必要时补齐信息'],
  },
  {
    key: 'after-sales',
    roleName: '售后处理员工',
    avatarText: '售',
    avatarTone: 'copper',
    description: '负责退款、换货、履约异常和会员权益补偿类处理。',
    workStyles: ['证据优先', '风险克制', '留痕复盘'],
    expertiseTags: ['退款', '换货', '权益核对'],
    workModes: ['查订单', '核规则', '给结论'],
  },
  {
    key: 'knowledge-operator',
    roleName: '知识运营员工',
    avatarText: '知',
    avatarTone: 'olive',
    description: '负责维护业务资料、沉淀证据片段并推动 SOP 学习。',
    workStyles: ['结构化整理', '可追溯', '持续学习'],
    expertiseTags: ['资料维护', '证据片段', 'SOP 学习'],
    workModes: ['解析文档', '组织结构', '发现缺口'],
  },
  {
    key: 'commerce-guide',
    roleName: '商品导购员工',
    avatarText: '导',
    avatarTone: 'blue',
    description: '负责商品咨询、价格比较、购买确认和用户偏好复用。',
    workStyles: ['偏好敏感', '主动比较', '确认后执行'],
    expertiseTags: ['商品比价', '购买流程', '偏好记忆'],
    workModes: ['理解需求', '比较选项', '确认下单'],
  },
];

const DEFAULT_WORK_STYLES = ['目标明确', '证据优先', '动作可追溯'];
const DEFAULT_EXPERTISE = ['业务问答', 'SOP 执行', '工具调用'];
const DEFAULT_WORK_MODES = ['识别意图', '补齐信息', '执行并复盘'];

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String).filter(Boolean) : [];
}

function stringFromMeta(metadata: Record<string, unknown>, key: string): string {
  const value = metadata[key];
  return typeof value === 'string' ? value : '';
}

export function employeeProfile(agent?: AgentProfileRead | null): EmployeeProfile {
  const metadata = agent?.metadata || {};
  const template = EMPLOYEE_TEMPLATES.find((item) => item.key === metadata.role_key) || EMPLOYEE_TEMPLATES[0];
  const isOverall = Boolean(agent?.is_overall);
  return {
    roleKey: stringFromMeta(metadata, 'role_key') || template.key,
    roleName: isOverall ? '组织资源库' : stringFromMeta(metadata, 'role_name') || template.roleName,
    avatarText: isOverall ? '组' : stringFromMeta(metadata, 'avatar_text') || template.avatarText,
    avatarTone: isOverall ? 'overall' : stringFromMeta(metadata, 'avatar_tone') || template.avatarTone,
    onboardedAt: stringFromMeta(metadata, 'onboarded_at') || agent?.created_at?.slice(0, 10) || '-',
    workStyles: asStringArray(metadata.work_styles).length ? asStringArray(metadata.work_styles) : DEFAULT_WORK_STYLES,
    expertiseTags: asStringArray(metadata.expertise_tags).length ? asStringArray(metadata.expertise_tags) : DEFAULT_EXPERTISE,
    workModes: asStringArray(metadata.work_modes).length ? asStringArray(metadata.work_modes) : DEFAULT_WORK_MODES,
  };
}

export function employeeDisplayName(agent?: AgentProfileRead | null): string {
  if (!agent) return '数字员工';
  if (agent.is_overall) return '组织资源库';
  return (agent.name || '数字员工').replace(/智能体/g, '员工');
}

export function resourceCount(resources: AgentResourceBindingRead[] | undefined, type: AgentResourceBindingRead['resource_type']): number {
  return (resources || []).filter((item) => item.resource_type === type && item.status !== 'deleted').length;
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
    onboarded_at: currentMetadata.onboarded_at || new Date().toISOString().slice(0, 10),
    work_styles: template.workStyles,
    expertise_tags: template.expertiseTags,
    work_modes: template.workModes,
  };
}
