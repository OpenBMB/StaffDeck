import type { HarnessLogEntry } from '../api/harness';
import { engineLabel, hookHandlerLabel, hookPointLabel, moduleLabel, operationLabel, toolLabel } from './harnessLabels';
import { parseBackendDateTime } from './timezone';

/** 把一条执行日志翻译成一句人话，外加级别和可选的补充说明。 */

export type LogLevel = 'info' | 'success' | 'warn' | 'error' | 'muted';

export type FormattedLog = {
  title: string;
  detail?: string;
  level: LogLevel;
  /** 右侧附注（耗时、tokens 等） */
  meta?: string;
};

const str = (v: unknown, max = 160): string => {
  if (v === null || v === undefined) return '';
  const s = typeof v === 'string' ? v : JSON.stringify(v);
  return s.length > max ? `${s.slice(0, max)}…` : s;
};

const num = (v: unknown): number | undefined => (typeof v === 'number' && Number.isFinite(v) ? v : undefined);

const ms = (v: unknown): string => {
  const n = num(v);
  if (n === undefined) return '';
  return n >= 1000 ? `${(n / 1000).toFixed(1)}s` : `${Math.round(n)}ms`;
};

const STATUS_TEXT: Record<string, string> = {
  completed: '成功',
  success: '成功',
  failed: '失败',
  outcome_unknown: '结果待确认',
  denied: '无权限',
  cancelled: '已取消',
  started: '进行中',
  handoff: '需要人工处理',
  interrupted: '已中断',
  error: '出错',
};

const STATUS_LEVEL: Record<string, LogLevel> = {
  completed: 'success',
  success: 'success',
  failed: 'error',
  outcome_unknown: 'warn',
  denied: 'warn',
  cancelled: 'muted',
  handoff: 'warn',
  error: 'error',
};

const PLAN_DECISION: Record<string, string> = {
  answer_only: '直接回答',
  run_task: '执行任务',
  run_sop: '按流程执行',
  clarify: '需要追问',
  handoff: '转人工',
};

const TASK_KIND: Record<string, string> = {
  conversation: '对话',
  sop: '流程',
  skill: '流程',
  tool: '工具',
};

function errorMessage(v: unknown): string {
  if (!v) return '';
  if (typeof v === 'string') return v;
  if (typeof v === 'object') {
    const o = v as Record<string, unknown>;
    if (typeof o.message === 'string') return o.message;
    if (o.error && typeof o.error === 'object') return errorMessage(o.error);
  }
  return str(v);
}

export function formatLogEntry(e: HarnessLogEntry): FormattedLog {
  const d = e.data ?? {};
  switch (e.type) {
    case 'session/start':
      return { title: '会话开始', level: 'muted' };
    case 'user/message':
      return { title: `用户：${str(d.message, 200) || '（空消息）'}`, level: 'info', meta: typeof d.channel === 'string' ? d.channel : undefined };
    case 'turn/plan': {
      const decision = typeof d.decision === 'string' ? PLAN_DECISION[d.decision] ?? d.decision : '';
      return { title: `规划本轮：${decision}${d.user_intent ? ` · ${str(d.user_intent, 80)}` : ''}`, detail: typeof d.reason === 'string' ? d.reason : undefined, level: 'info' };
    }
    case 'snapshot/compiled': {
      const sops = Array.isArray(d.sops) ? d.sops.length : undefined;
      const parts = [num(d.grants) !== undefined ? `${d.grants} 项能力` : '', sops !== undefined ? `${sops} 个流程` : ''].filter(Boolean);
      return { title: `已加载员工配置${parts.length ? ` · ${parts.join(' · ')}` : ''}`, level: 'info', meta: engineLabel(typeof d.execution_engine === 'string' ? d.execution_engine : e.engine) };
    }
    case 'engine/start': {
      const thinking = d.thinking === 'disabled' ? ' · 不思考' : d.thinking === 'enabled' ? ` · 思考${typeof d.reasoning_effort === 'string' && d.reasoning_effort !== 'provider_default' ? `(${d.reasoning_effort})` : ''}` : '';
      return { title: `Harness v3 引擎已就绪${d.model ? ` · 模型 ${str(d.model, 60)}` : ''}${thinking}${d.pooled ? ' · 复用暖进程' : ''}`, level: 'success', meta: d.boot_ms !== undefined ? (d.pooled ? '无需启动' : `启动 ${ms(d.boot_ms)}`) : undefined };
    }
    case 'engine/plan':
      return { title: '引擎开始规划本轮', level: 'muted' };
    case 'engine/plan_done':
      return { title: '引擎完成规划', level: 'muted', meta: d.duration_ms !== undefined ? ms(d.duration_ms) : undefined };
    case 'engine/reply':
      return { title: '引擎开始生成回复', level: 'muted' };
    case 'engine/reply_done':
      return { title: '引擎完成回复', level: 'muted', meta: d.duration_ms !== undefined ? ms(d.duration_ms) : undefined };
    case 'engine/frame':
      return { title: `引擎接管任务步骤${d.process_uses !== undefined ? `（进程第 ${d.process_uses} 次复用）` : ''}`, level: 'muted' };
    case 'engine/fallback':
      return { title: `本轮改用 Harness v2 引擎：${d.reason === 'image_attachments' ? '带图片附件，Harness v3 不读取图片' : d.reason === 'engine_unavailable' ? 'Harness v3 引擎不可用' : str(d.reason, 40)}`, detail: typeof d.detail === 'string' ? d.detail : undefined, level: 'warn' };
    case 'engine/turn':
      return { title: `引擎开始第 ${d.turn ?? '?'} 轮`, level: 'muted' };
    case 'engine/step':
      return { title: `引擎第 ${d.turn ?? '?'} 轮 · 第 ${d.step ?? '?'} 步`, level: 'muted' };
    case 'engine/request':
      return { title: `引擎发起模型请求${d.model ? ` · ${str(d.model, 60)}` : ''}`, level: 'muted' };
    case 'engine/steer':
      return { title: '引擎收到新的指令，调整执行', level: 'info' };
    case 'engine/finish': {
      const status = typeof d.status === 'string' ? d.status : '';
      return { title: `引擎提交结果 · ${STATUS_TEXT[status] ?? status}`, detail: typeof d.next_step_id === 'string' ? `下一步：${d.next_step_id}` : undefined, level: STATUS_LEVEL[status] ?? 'success' };
    }
    case 'turn/failed':
      return { title: `引擎本轮失败：${errorMessage(d.error) || errorMessage(d.reason) || '未知错误'}`, level: 'error' };
    case 'turn/end': {
      const reason = d.reason && typeof d.reason === 'object' ? (d.reason as Record<string, unknown>) : {};
      const kind = typeof reason.kind === 'string' ? reason.kind : '';
      if (kind === 'error') return { title: `引擎本轮出错：${errorMessage(reason.error) || '未知错误'}`, level: 'error' };
      return { title: `引擎第 ${d.turn ?? '?'} 轮结束${kind ? ` · ${STATUS_TEXT[kind] ?? kind}` : ''}`, level: 'muted' };
    }
    case 'task/start': {
      const name = typeof d.skill_name === 'string' && d.skill_name ? d.skill_name : TASK_KIND[String(d.kind)] ?? String(d.kind ?? '');
      return { title: `开始任务：${name}${d.step_id ? ` · 步骤 ${d.step_id}` : ''}`, level: 'info' };
    }
    case 'task/end': {
      const status = typeof d.status === 'string' ? d.status : '';
      const name = typeof d.skill_name === 'string' && d.skill_name ? d.skill_name : TASK_KIND[String(d.kind)] ?? '';
      return { title: `任务结束：${name} · ${STATUS_TEXT[status] ?? status}`, detail: errorMessage(d.error) || undefined, level: STATUS_LEVEL[status] ?? 'info', meta: num(d.action_count) !== undefined ? `${d.action_count} 个动作` : undefined };
    }
    case 'tool/route':
      return { title: `${operationLabel(String(d.operation ?? ''))} → 由「${moduleLabel(String(d.module_id ?? ''))}」处理`, level: 'muted' };
    case 'tool/call': {
      const args = d.arguments && typeof d.arguments === 'object' ? (d.arguments as Record<string, unknown>) : {};
      const preview = Object.entries(args).map(([k, v]) => `${k}=${str(v, 60)}`).join('  ');
      return { title: `调用 ${toolLabel(String(d.tool ?? ''))}`, detail: preview || undefined, level: 'info', meta: engineLabel(e.engine) };
    }
    case 'tool/result': {
      const status = typeof d.status === 'string' ? d.status : '';
      return { title: `${toolLabel(String(d.tool ?? ''))} · ${STATUS_TEXT[status] ?? status}`, detail: errorMessage(d.error) || undefined, level: STATUS_LEVEL[status] ?? 'info', meta: ms(d.duration_ms) || undefined };
    }
    case 'tool/denied':
      return { title: `无权限：${operationLabel(String(d.operation ?? ''))}`, detail: typeof d.reason === 'string' ? d.reason : undefined, level: 'warn' };
    case 'hook/result':
      return { title: `${hookPointLabel(String(d.point ?? ''))} · ${hookHandlerLabel(String(d.handler ?? ''))}${d.decision ? ` · ${str(d.decision, 40)}` : ''}`, detail: typeof d.reason === 'string' ? d.reason : undefined, level: 'muted' };
    case 'hook/failed':
      return { title: `${hookPointLabel(String(d.point ?? ''))} · ${hookHandlerLabel(String(d.handler ?? ''))} 执行失败`, detail: errorMessage(d.error) || undefined, level: 'error' };
    case 'llm/call': {
      const tokens = num(d.input_tokens) !== undefined || num(d.output_tokens) !== undefined ? `${d.input_tokens ?? 0}+${d.output_tokens ?? 0} tokens` : '';
      const model = typeof d.model_name === 'string' && d.model_name ? d.model_name : String(d.model ?? '');
      const failed = d.status === 'failed' || d.status === 'error';
      return { title: `模型调用 · ${model}${d.operation ? ` · ${d.operation}` : ''}`, level: failed ? 'error' : 'muted', meta: [ms(d.duration_ms), tokens].filter(Boolean).join(' · ') || undefined };
    }
    case 'llm/failed':
      return { title: `模型调用失败 · ${String(d.model ?? '')}`, detail: errorMessage(d.error) || (typeof d.message === 'string' ? d.message : undefined), level: 'error' };
    case 'knowledge/query': {
      const q = d.query && typeof d.query === 'object' ? (d.query as Record<string, unknown>).query : d.query;
      return { title: `知识检索「${str(q, 80)}」`, level: 'info', meta: num(d.hit_count) !== undefined ? `${d.hit_count} 条结果` : undefined };
    }
    case 'memory/recall':
      return { title: '回忆相关记忆', level: 'muted', meta: num(d.hit_count ?? d.count) !== undefined ? `${d.hit_count ?? d.count} 条` : undefined };
    case 'assistant/message':
      return { title: `员工：${str(d.reply, 200) || '（空回复）'}`, level: 'success' };
    case 'turn/complete':
      return { title: '本轮对话完成', level: 'success', meta: num(d.iteration) !== undefined ? `第 ${d.iteration} 次迭代` : undefined };
    case 'turn/cancelled':
      return { title: '用户停止了本轮生成', level: 'muted' };
    case 'error':
      return { title: `错误${d.code ? ` ${d.code}` : ''}：${str(d.message, 300)}`, level: 'error' };
    case 'handoff/created':
      return { title: `发起转人工${d.reason ? `：${str(d.reason, 120)}` : ''}`, detail: typeof d.question === 'string' ? d.question : undefined, level: 'warn' };
    case 'handoff/assigned':
      return { title: `已指派处理人${d.assignee_name ? `：${d.assignee_name}` : ''}`, level: 'info' };
    case 'handoff/notified':
      return { title: `已通知处理人${d.channel ? `（${d.channel}）` : ''}`, level: 'info' };
    case 'ledger/reconciled': {
      const status = typeof d.status === 'string' ? d.status : '';
      return { title: `人工确认调用结果：${STATUS_TEXT[status] ?? status}`, level: 'info' };
    }
    case 'admin/config':
      return { title: `管理员保存了装配 · ${engineLabel(String(d.engine ?? ''))} · ${String(d.security_profile ?? '')}${d.pending ? ' · 待重启' : ''}`, detail: [Array.isArray(d.disabled_modules) && d.disabled_modules.length ? `停用 ${d.disabled_modules.length} 个模块` : '', Array.isArray(d.extra_modules) && d.extra_modules.length ? `外部模块 ${d.extra_modules.length} 个` : ''].filter(Boolean).join(' · ') || undefined, level: 'info', meta: str(d.by, 24) };
    case 'admin/base_test':
      return { title: `测试权限中心连接 · ${d.ok ? '通过' : '未通过'}`, detail: typeof d.authz_url === 'string' ? d.authz_url : undefined, level: d.ok ? 'success' : 'warn', meta: str(d.by, 24) };
    case 'admin/placement':
      return { title: `模块归类 · ${str(d.module_id, 60)} → ${d.sub_id ? str(d.sub_id, 60) : '默认位置'}`, level: 'muted', meta: str(d.by, 24) };
    case 'admin/inspect':
      return { title: `预检外部模块 · ${str(d.spec, 80)} · ${d.ok ? '通过' : '未通过'}`, detail: Array.isArray(d.modules) && d.modules.length ? `会安装：${d.modules.join('、')}` : undefined, level: d.ok ? 'info' : 'warn', meta: str(d.by, 24) };
    case 'admin/restart':
      return { title: '管理员请求重启运行时', level: 'info', meta: str(d.by, 24) };
    case 'admin/restarted':
      return { title: `运行时已重启 · ${String(d.security_profile ?? '')} · ${d.modules ?? '?'} 个模块`, level: 'success', meta: num(d.restart_count) !== undefined ? `第 ${d.restart_count} 次` : undefined };
    case 'admin/restart_failed': {
      const err = str(d.error, 200);
      const refused = err.startsWith('无法切换') || err.startsWith('装配无法') || err.startsWith('已有一次');
      return { title: refused ? `重启前预检未通过（运行中的装配未受影响）：${err}` : `重启未成功，已恢复原有装配：${err}`, level: refused ? 'warn' : 'error', meta: str(d.by, 24) };
    }
    default:
      return { title: e.event_type ?? e.type, detail: str(d, 200), level: 'muted' };
  }
}

export function logTime(ts: string): string {
  const d = parseBackendDateTime(ts);
  if (Number.isNaN(d.getTime())) return ts;
  const pad = (n: number, w = 2) => String(n).padStart(w, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
}

export function logDate(ts: string): string {
  const d = parseBackendDateTime(ts);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' });
}
