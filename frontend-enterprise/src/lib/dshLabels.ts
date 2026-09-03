/**
 * 运行时与模块页面的用户语言。
 *
 * 后端返回的是稳定的技术标识（操作名、插槽名、模块 id、Hook 名）。这里统一翻译成
 * 使用者看得懂的说法；技术标识只在「显示技术信息」打开时作为补充展示。
 */

export const ENGINE_LABEL: Record<string, string> = {
  dsh: 'Harness v3',
  legacy: 'Harness v2',
  harness_v2: 'Harness v2',
};

export const ENGINE_LONG_LABEL: Record<string, string> = {
  dsh: 'Harness v3 引擎',
  legacy: 'Harness v2 引擎',
  harness_v2: 'Harness v2 引擎',
};

export function engineLabel(engine: string | null | undefined, long = false): string {
  if (!engine) return '—';
  return (long ? ENGINE_LONG_LABEL : ENGINE_LABEL)[engine] ?? engine;
}

export const PROFILE_LABEL: Record<string, { short: string; long: string; hint: string }> = {
  OSS_LOCAL: {
    short: '开源版 · 本地权限',
    long: '开源版 · 本地权限',
    hint: '按租户、角色、员工归属和绑定关系判断谁能使用什么。',
  },
  BUSINESS_BASE: {
    short: '企业版 · 统一权限中心',
    long: '企业版 · 统一权限中心',
    hint: '由企业权限中心统一判定；权限中心不可用时拒绝访问，不会放行。',
  },
};

export function profileLabel(profile: string | null | undefined): { short: string; long: string; hint: string } {
  if (!profile) return { short: '—', long: '—', hint: '' };
  return PROFILE_LABEL[profile] ?? { short: profile, long: profile, hint: '' };
}

export const KIND_META: Record<string, { label: string; tone: string; hint: string }> = {
  A: { label: '可替换', tone: 'bg-[#e8f1ff] text-[#2f6fdb]', hint: '可以换成其他实现，例如接入第三方服务' },
  C: { label: '可配置', tone: 'bg-[#fff4e5] text-[#c2740c]', hint: '通过配置员工、流程、技能等内容改变行为，不需要改代码' },
  T: { label: '平台服务', tone: 'bg-[#eaf7ef] text-[#1f9d55]', hint: '平台提供的固定服务，不可替换' },
  K: { label: '核心', tone: 'bg-[#f0eef9] text-[#6a4fc7]', hint: '平台核心组成部分，始终存在' },
};

const OPERATION_LABEL: Record<string, string> = {
  'knowledge.search': '知识检索',
  'general_skill.consume': '读取技能',
  'tool.invoke': '调用工具',
  'mcp.invoke': '调用 MCP 服务',
  'a2a.invoke': '智能体协作',
  'sandbox.execute': '执行命令 / 文件',
  'artifact.publish': '发布工作产物',
  'memory.read': '读取记忆',
  'memory.write': '写入记忆',
  'hook.contribute': '介入对话流程',
  'handoff.request': '发起转人工',
  'handoff.assign': '指派处理人',
  'handoff.reply': '接收人工回复',
  'team.delegate': '委派团队成员',
  'runtime.turn': '处理对话',
  'staff.use': '使用数字员工',
  'staff.manage': '管理数字员工',
  'model.use': '使用模型',
  'sop.execute': '执行流程',
  'sop.read': '读取流程',
  'sop.advance': '推进流程',
  'sop.resume': '恢复流程',
  'channel.receive': '接收渠道消息',
  'channel.send': '发送渠道消息',
  'event.observe': '记录运行事件',
};

/** `knowledge.search/v1` → 知识检索 */
export function operationLabel(op: string): string {
  const base = op.split('/')[0];
  return OPERATION_LABEL[base] ?? base;
}

const TOOL_LABEL: Record<string, string> = {
  knowledge_search: '知识检索',
  capability_describe: '查看可用能力',
  capability_search: '查找能力',
  general_skill_read: '读取技能',
  tool_invoke: '调用工具',
  sandbox_execute: '执行命令 / 文件',
  finish_task: '提交结果',
  exec_command: '执行命令',
  read_file: '读取文件',
  write_file: '写入文件',
  list_directory: '查看目录',
  run_skill_script: '运行技能脚本',
};

/** 调用记录里的工具名：新引擎是 `knowledge:knowledge.search/v1`，经典引擎是 `exec_command` 或用户自定义的工具名。 */
export function toolLabel(name: string): string {
  if (name.includes(':')) return operationLabel(name.slice(name.indexOf(':') + 1));
  if (TOOL_LABEL[name]) return TOOL_LABEL[name];
  if (name.startsWith('general_skill.')) return `技能 · ${name.slice('general_skill.'.length)}`;
  if (name.startsWith('mcp.')) return `MCP 服务 · ${name.slice(4)}`;
  return name;
}

/** 员工在对话中能直接使用的操作（新引擎暴露给模型的工具）。 */
export function proxyToolLabel(name: string): string {
  return TOOL_LABEL[name] ?? name;
}

const HOOK_POINT_LABEL: Record<string, string> = {
  pre_step: '回答前',
  pre_tool: '调用能力前',
  post_tool: '调用能力后',
  turn_stopping: '回答结束前',
};

const HOOK_HANDLER_LABEL: Record<string, string> = {
  persona: '载入人设',
  'memory.recall': '回忆相关记忆',
  'sop.execution_slice': '载入当前流程步骤',
  'activation.allowlist': '限定本轮可用能力',
  'capability.pep': '检查权限',
  'ledger.record': '记录调用',
  'citations.collect': '整理引用来源',
  'sop.output_supervisor': '检查输出是否符合流程',
  'handoff.detect': '判断是否需要转人工',
};

export function hookPointLabel(point: string): string {
  return HOOK_POINT_LABEL[point] ?? point;
}

export function hookHandlerLabel(handler: string): string {
  return HOOK_HANDLER_LABEL[handler] ?? handler;
}

/** `pre_step:persona` → 回答前 · 载入人设 */
export function hookLabel(entry: string): string {
  const [point, handler] = entry.split(':');
  if (!handler) return hookHandlerLabel(point);
  return `${hookPointLabel(point)} · ${hookHandlerLabel(handler)}`;
}

const SLOT_LABEL: Record<string, string> = {
  'runtime.engine': '执行引擎',
  'runtime.kernel': '运行内核',
  'security.pep': '权限控制',
  'tenant.staff': '租户员工',
  'staff.sop': '员工流程',
  'staff.capability': '员工能力',
  'staff.model_route': '员工模型',
  'staff.team': '员工团队',
  'staff.interaction': '对话介入',
  'staff.channel': '员工渠道',
  'staff.ingress': '请求入口',
  'sop.slot.knowledge': '流程 · 知识',
  'sop.slot.skill': '流程 · 技能',
  'sop.slot.action': '流程 · 动作',
  'sop.slot.control': '流程 · 控制',
  'handoff.assignment': '转人工 · 指派',
  'handoff.notifier': '转人工 · 通知',
  'handoff.reply_endpoint': '转人工 · 回复',
  'knowledge.import.source': '知识导入来源',
  'event.observer': '运行事件',
};

export function slotLabel(slot: string): string {
  return SLOT_LABEL[slot] ?? slot;
}

const RESOURCE_TYPE_LABEL: Record<string, string> = {
  knowledge_base: '知识库',
  general_skill: '技能',
  tool: '工具',
  capability: '能力',
  handoff: '转人工',
  skill: '流程',
  agent: '数字员工',
  mcp_server: 'MCP 服务',
};

export function resourceTypeLabel(type: string): string {
  return RESOURCE_TYPE_LABEL[type] ?? type;
}

const MODEL_ROLE_LABEL: Record<string, string> = {
  default: '默认',
  router: '路由',
  step: '分步',
};

export function modelRoleLabel(role: string): string {
  return MODEL_ROLE_LABEL[role] ?? role;
}

/** 对话轨迹里出现的模块 id → 名称（与后端注册表一致）。 */
const MODULE_LABEL: Record<string, string> = {
  'knowledge.local': '知识库检索',
  'general_skill.local': '通用技能',
  'tool.local': '业务工具调用',
  'sandbox.local': '受控执行环境',
  'engine.dsh': 'Harness v3 引擎',
  'engine.legacy': 'Harness v2 引擎',
};

export function moduleLabel(moduleId: string): string {
  return MODULE_LABEL[moduleId] ?? moduleId;
}

export const LEDGER_STATUS_LABEL: Record<string, { text: string; className: string; hint?: string }> = {
  completed: { text: '成功', className: 'bg-[#e9f7ef] text-[#2cb360]' },
  failed: { text: '失败', className: 'bg-[#fce7e7] text-[#d20b0b]' },
  outcome_unknown: { text: '待确认', className: 'bg-[#fff4e5] text-[#c2740c]', hint: '已发出调用但没有收到结果确认' },
  denied: { text: '无权限', className: 'bg-[#f3f4f6] text-[#757f9c]' },
  cancelled: { text: '已取消', className: 'bg-[#f3f4f6] text-[#757f9c]' },
  started: { text: '进行中', className: 'bg-[#e8f1ff] text-[#2f6fdb]' },
};

export const PLACEMENT_SOURCE_LABEL: Record<string, string> = {
  override: '管理员指定',
  taxonomy: '平台预设',
  manifest: '模块自带分类',
  slot: '按接入点归类',
  none: '未归类',
};

export function placementSourceLabel(source: string | undefined): string {
  return PLACEMENT_SOURCE_LABEL[source ?? 'none'] ?? source ?? '';
}

export const TECH_MODE_STORAGE_KEY = 'staffdeck_dsh_tech_mode';

export function readTechMode(): boolean {
  try {
    return window.localStorage.getItem(TECH_MODE_STORAGE_KEY) === '1';
  } catch {
    return false;
  }
}

export function writeTechMode(on: boolean): void {
  try {
    window.localStorage.setItem(TECH_MODE_STORAGE_KEY, on ? '1' : '0');
  } catch {
    /* ignore */
  }
}
