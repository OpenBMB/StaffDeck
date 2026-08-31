import { SaveOutlined } from '../icons';
import { useEffect, useState, type ReactNode } from 'react';
import { Button as UIButton, Card, CardContent, CardHeader, CardTitle, Input, Switch, Textarea, notify } from '@/components/ui';
import { api, TENANT_ID } from '../api/client';
import type { EnterpriseAuthUser } from '../auth';
import AccountApiKeyDialog from '../components/AccountApiKeyDialog';
import type { UIConfigRead } from '../types';
import { BrainCircuit, KeyRound, RotateCcw, ShieldCheck } from 'lucide-react';
import { apiErrorMessage } from '../lib/apiErrorMessages';

type UiConfigForm = {
  show_thinking_trace: boolean;
  show_skill_trace: boolean;
  show_tool_trace: boolean;
  reflection_max_rounds: string;
  agent_loop_max_actions: string;
  context_token_budget: string;
  context_compaction_trigger_ratio: string;
  context_recent_round_limit: string;
  context_long_summary_token_budget: string;
  context_medium_summary_token_budget: string;
  context_allowed_roles: Array<'user' | 'assistant'>;
  context_long_summary_prefix: string;
  context_medium_summary_prefix: string;
  sandbox_enabled: boolean;
  harness_storage_path: string;
  sandbox_network_mode: 'all' | 'allowlist' | 'deny';
  sandbox_allowed_domains: string;
};

const DEFAULT_UI_CONFIG: UiConfigForm = {
  show_thinking_trace: true,
  show_skill_trace: true,
  show_tool_trace: true,
  reflection_max_rounds: '1',
  agent_loop_max_actions: '32',
  context_token_budget: '32000',
  context_compaction_trigger_ratio: '0.70',
  context_recent_round_limit: '6',
  context_long_summary_token_budget: '4000',
  context_medium_summary_token_budget: '4000',
  context_allowed_roles: ['user', 'assistant'],
  context_long_summary_prefix: '历史的信息可以被总结为：',
  context_medium_summary_prefix: '近期的历史信息总结为：',
  sandbox_enabled: false,
  harness_storage_path: '',
  sandbox_network_mode: 'all',
  sandbox_allowed_domains: '',
};

function formatDateOnly(value: string): string {
  const normalized = /(?:z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? value.slice(0, 10) : date.toISOString().slice(0, 10);
}

/** Renders tenant runtime controls, including the Harness-only workspace setting and effective root. */
export default function RuntimeSettingsPage({ currentUser }: { currentUser: EnterpriseAuthUser }) {
  const [form, setForm] = useState<UiConfigForm>(DEFAULT_UI_CONFIG);
  const [loading, setLoading] = useState(false);
  const [updatedAt, setUpdatedAt] = useState('');
  const [setupMessage, setSetupMessage] = useState('');
  const [effectiveStoragePath, setEffectiveStoragePath] = useState('');
  const [apiKeyOpen, setApiKeyOpen] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [sandboxStatus, setSandboxStatus] = useState<Pick<UIConfigRead, 'sandbox_status' | 'sandbox_status_message' | 'sandbox_status_remediation'>>({});
  const update = (patch: Partial<UiConfigForm>) => setForm((prev) => ({ ...prev, ...patch }));

  useEffect(() => {
    api.get<UIConfigRead>(`/api/enterprise/ui-config?tenant_id=${TENANT_ID}`)
      .then((row) => {
        setForm({
          show_thinking_trace: row.show_thinking_trace,
          show_skill_trace: row.show_skill_trace,
          show_tool_trace: row.show_tool_trace,
          reflection_max_rounds: String(row.reflection_max_rounds),
          agent_loop_max_actions: String(row.agent_loop_max_actions),
          context_token_budget: String(row.context_token_budget),
          context_compaction_trigger_ratio: String(row.context_compaction_trigger_ratio),
          context_recent_round_limit: String(row.context_recent_round_limit),
          context_long_summary_token_budget: String(row.context_long_summary_token_budget),
          context_medium_summary_token_budget: String(row.context_medium_summary_token_budget),
          context_allowed_roles: row.context_allowed_roles,
          context_long_summary_prefix: row.context_long_summary_prefix,
          context_medium_summary_prefix: row.context_medium_summary_prefix,
          sandbox_enabled: row.sandbox_enabled,
          harness_storage_path: row.harness_storage_path || '',
          sandbox_network_mode: row.sandbox_network_mode || 'all',
          sandbox_allowed_domains: (row.sandbox_allowed_domains || []).join('\n'),
        });
        setUpdatedAt(row.updated_at);
        setEffectiveStoragePath(row.effective_harness_storage_path || '');
        setSetupMessage(row.sandbox_setup_instructions || '');
        setSandboxStatus({ sandbox_status: row.sandbox_status, sandbox_status_message: row.sandbox_status_message, sandbox_status_remediation: row.sandbox_status_remediation });
      })
      .catch((error) => notify.error(error.message));
  }, []);

  async function save() {
    /** Saves runtime controls while preserving the compatible Harness workspace API field. */

    const reflectionMaxRounds = Number(form.reflection_max_rounds);
    const agentLoopMaxActions = Number(form.agent_loop_max_actions);
    if (Number.isNaN(reflectionMaxRounds) || Number.isNaN(agentLoopMaxActions)) {
      notify.error('反思轮数与单轮最大动作数必须是数字');
      return;
    }
    const contextError = validateContextSettings(form);
    if (contextError) {
      notify.error(contextError);
      return;
    }
    setLoading(true);
    try {
      const row = await api.put<UIConfigRead>('/api/enterprise/ui-config', {
        tenant_id: TENANT_ID,
        show_thinking_trace: form.show_thinking_trace,
        show_skill_trace: form.show_skill_trace,
        show_tool_trace: form.show_tool_trace,
        reflection_max_rounds: reflectionMaxRounds,
        agent_loop_max_actions: agentLoopMaxActions,
        context_token_budget: Number(form.context_token_budget),
        context_compaction_trigger_ratio: Number(form.context_compaction_trigger_ratio),
        context_recent_round_limit: Number(form.context_recent_round_limit),
        context_long_summary_token_budget: Number(form.context_long_summary_token_budget),
        context_medium_summary_token_budget: Number(form.context_medium_summary_token_budget),
        context_allowed_roles: form.context_allowed_roles,
        context_long_summary_prefix: form.context_long_summary_prefix.trim(),
        context_medium_summary_prefix: form.context_medium_summary_prefix.trim(),
        sandbox_enabled: form.sandbox_enabled,
        harness_storage_path: form.harness_storage_path.trim(),
        sandbox_network_mode: form.sandbox_network_mode,
        sandbox_allowed_domains: form.sandbox_allowed_domains.split(/[\n,]/).map((item) => item.trim()).filter(Boolean),
      });
      setUpdatedAt(row.updated_at);
      setEffectiveStoragePath(row.effective_harness_storage_path || '');
      if (row.restart_scheduled) {
        setRestarting(true);
        notify.success('沙盒设置已保存，StaffDeck 正在重启');
        await waitForApplicationRestart();
        window.location.reload();
        return;
      }
      notify.success('运行设置已保存');
    } catch (error) {
      notify.error(apiErrorMessage(error, '保存失败'));
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <div className="page-title">
        <div><h3>运行设置</h3><p className="text-[12px] text-muted-foreground">统一影响当前租户下所有数字员工的执行行为。</p></div>
        <UIButton disabled={loading || restarting} onClick={() => void save()}>
          {restarting ? <RotateCcw className="size-[15px] animate-spin" /> : <SaveOutlined />}
          {restarting ? '等待应用重启' : '保存设置'}
        </UIButton>
      </div>
      <Card className="editor-card settings-card">
        <CardHeader><CardTitle>执行记录与 Agent Loop</CardTitle></CardHeader>
        <CardContent className="flex flex-col gap-[16px]">
          <SwitchRow label="展示思考状态" checked={form.show_thinking_trace} onChange={(next) => update({ show_thinking_trace: next })} />
          <SwitchRow label="展示执行技能" checked={form.show_skill_trace} onChange={(next) => update({ show_skill_trace: next })} />
          <SwitchRow label="展示工具调用" checked={form.show_tool_trace} onChange={(next) => update({ show_tool_trace: next })} />
          <LabeledField label="反思轮数" hint="设为 0 时关闭反思；每轮允许模型检查当前技能和工具结果。"><Input type="number" min={0} max={5} step={1} value={form.reflection_max_rounds} onChange={(e) => update({ reflection_max_rounds: e.target.value })} /></LabeledField>
          <LabeledField label="单轮最大动作数" hint="限制一次用户输入内连续决策和工具调用的次数，避免无限循环。"><Input type="number" min={1} max={100} step={1} value={form.agent_loop_max_actions} onChange={(e) => update({ agent_loop_max_actions: e.target.value })} /></LabeledField>
        </CardContent>
      </Card>
      <Card className="editor-card settings-card overflow-hidden">
        <CardHeader className="border-b border-[#edf0f5] bg-[linear-gradient(110deg,#f8fbff_0%,#ffffff_52%,#f6f9ff_100%)]">
          <div className="flex flex-wrap items-center justify-between gap-[12px]">
            <div>
              <CardTitle className="flex items-center gap-[8px]">
                <span className="flex size-[28px] items-center justify-center rounded-[9px] bg-[#eaf2ff] text-[#1a71ff]">
                  <BrainCircuit className="size-[15px]" />
                </span>
                对话上下文与自动压缩
              </CardTitle>
              <p className="mt-[7px] text-[11px] leading-[17px] text-muted-foreground">
                租户级即时生效；单员工会话和团队成员任务共享这套参数。
              </p>
            </div>
            <UIButton
              type="button"
              variant="outline"
              className="h-[32px] gap-[6px] text-[11px]"
              onClick={() => update({
                context_token_budget: DEFAULT_UI_CONFIG.context_token_budget,
                context_compaction_trigger_ratio: DEFAULT_UI_CONFIG.context_compaction_trigger_ratio,
                context_recent_round_limit: DEFAULT_UI_CONFIG.context_recent_round_limit,
                context_long_summary_token_budget: DEFAULT_UI_CONFIG.context_long_summary_token_budget,
                context_medium_summary_token_budget: DEFAULT_UI_CONFIG.context_medium_summary_token_budget,
                context_allowed_roles: DEFAULT_UI_CONFIG.context_allowed_roles,
                context_long_summary_prefix: DEFAULT_UI_CONFIG.context_long_summary_prefix,
                context_medium_summary_prefix: DEFAULT_UI_CONFIG.context_medium_summary_prefix,
              })}
            >
              <RotateCcw className="size-[13px]" />
              恢复上下文默认值
            </UIButton>
          </div>
        </CardHeader>
        <CardContent className="flex flex-col gap-[18px] pt-[18px]">
          <div className="rounded-[11px] border border-[#dce8fb] bg-[#f7faff] px-[13px] py-[11px] text-[11px] leading-[18px] text-[#52637d]">
            当估算上下文达到预算 × 触发比例时，系统会把较早对话压缩为长期与近期摘要，并保留指定的最近轮次。数值越大，历史保留越完整，但模型输入成本也越高。
          </div>
          <div className="grid gap-[14px] md:grid-cols-2">
            <LabeledField label="上下文 Token 预算" hint="完整会话上下文的目标上限，范围 512–262144。">
              <Input type="number" min={512} max={262144} step={512} value={form.context_token_budget} onChange={(event) => update({ context_token_budget: event.target.value })} />
            </LabeledField>
            <LabeledField label="压缩触发比例" hint="达到预算的这个比例后开始压缩，范围 0.10–0.95。">
              <Input type="number" min={0.1} max={0.95} step={0.05} value={form.context_compaction_trigger_ratio} onChange={(event) => update({ context_compaction_trigger_ratio: event.target.value })} />
            </LabeledField>
            <LabeledField label="保留最近对话轮数" hint="压缩时不进入摘要的最近用户轮次，范围 1–50。">
              <Input type="number" min={1} max={50} step={1} value={form.context_recent_round_limit} onChange={(event) => update({ context_recent_round_limit: event.target.value })} />
            </LabeledField>
            <div className="grid grid-cols-2 gap-[10px]">
              <LabeledField label="长期摘要预算" hint="Token">
                <Input type="number" min={128} max={32768} step={128} value={form.context_long_summary_token_budget} onChange={(event) => update({ context_long_summary_token_budget: event.target.value })} />
              </LabeledField>
              <LabeledField label="近期摘要预算" hint="Token">
                <Input type="number" min={128} max={32768} step={128} value={form.context_medium_summary_token_budget} onChange={(event) => update({ context_medium_summary_token_budget: event.target.value })} />
              </LabeledField>
            </div>
          </div>
          <div className="rounded-[11px] border border-[#e6e9f0] bg-[#fbfbfc] px-[13px] py-[12px]">
            <p className="text-[12px] font-medium text-[#464c5e]">纳入历史上下文的角色</p>
            <p className="mt-[3px] text-[11px] leading-[16px] text-muted-foreground">至少保留一种角色；图片只随用户消息进入上下文。</p>
            <div className="mt-[10px] grid gap-[8px] sm:grid-cols-2">
              <SwitchRow label="用户消息" checked={form.context_allowed_roles.includes('user')} onChange={(checked) => update({ context_allowed_roles: toggleContextRole(form.context_allowed_roles, 'user', checked) })} />
              <SwitchRow label="数字员工回复" checked={form.context_allowed_roles.includes('assistant')} onChange={(checked) => update({ context_allowed_roles: toggleContextRole(form.context_allowed_roles, 'assistant', checked) })} />
            </div>
          </div>
          <div className="grid gap-[14px] md:grid-cols-2">
            <LabeledField label="长期摘要前缀" hint="注入长期摘要消息时使用，最多 200 字。">
              <Textarea rows={3} maxLength={200} value={form.context_long_summary_prefix} onChange={(event) => update({ context_long_summary_prefix: event.target.value })} />
            </LabeledField>
            <LabeledField label="近期摘要前缀" hint="注入近期摘要消息时使用，最多 200 字。">
              <Textarea rows={3} maxLength={200} value={form.context_medium_summary_prefix} onChange={(event) => update({ context_medium_summary_prefix: event.target.value })} />
            </LabeledField>
          </div>
        </CardContent>
      </Card>
      <Card className="editor-card settings-card">
        <CardHeader><CardTitle className="flex items-center gap-[8px]"><ShieldCheck className="size-[16px]" />执行隔离与 Harness 工作区</CardTitle></CardHeader>
        <CardContent className="flex flex-col gap-[16px]">
          <SwitchRow label="启用 SRT 沙盒" checked={form.sandbox_enabled} onChange={(next) => update({ sandbox_enabled: next })} hint="仅管理员可修改。打开或关闭后保存将自动重启 StaffDeck。默认关闭。" />
          <div className={`whitespace-pre-line rounded-md border px-[12px] py-[10px] text-[12px] leading-[18px] ${sandboxStatus.sandbox_status === 'ready' ? 'border-emerald-200 bg-emerald-50 text-emerald-900' : sandboxStatus.sandbox_status === 'degraded' ? 'border-red-300 bg-red-50 text-red-900' : sandboxStatus.sandbox_status === 'disabled' ? 'border-slate-200 bg-slate-50 text-slate-700' : 'border-amber-200 bg-amber-50 text-amber-900'}`}>
            <div className="font-medium">沙盒状态：{sandboxStatus.sandbox_status === 'ready' ? '可用' : sandboxStatus.sandbox_status === 'degraded' ? '已降级为无沙盒（高风险）' : sandboxStatus.sandbox_status === 'disabled' ? '未启用' : '不可用'}</div>
            {sandboxStatus.sandbox_status_message && <div>{sandboxStatus.sandbox_status_message}</div>}
            {sandboxStatus.sandbox_status_remediation && <div>{sandboxStatus.sandbox_status_remediation}</div>}
          </div>
          {setupMessage && <div className="whitespace-pre-line rounded-md border border-amber-200 bg-amber-50 px-[12px] py-[10px] text-[12px] leading-[18px] text-amber-900">{setupMessage}</div>}
          <div className="rounded-md border border-[#dce8fb] bg-[#f7faff] px-[12px] py-[10px] text-[12px] leading-[18px] text-[#52637d]">
            <div className="font-medium text-[#2f3442]">当前生效的 Harness 工作区</div>
            <code className="mt-[4px] block break-all text-[11px]">{effectiveStoragePath || '—'}</code>
          </div>
          {!form.sandbox_enabled && <LabeledField label="Harness 工作区目录" hint="仅用于 Harness 的任务文件和生成产物；不会配置、迁移或改变数据库、日志、上传附件和运行配置。留空使用默认工作区。"><Input value={form.harness_storage_path} onChange={(e) => update({ harness_storage_path: e.target.value })} placeholder={effectiveStoragePath || '~/.staffdeck/workspaces'} /></LabeledField>}
          {form.sandbox_enabled && <p className="rounded-md border border-amber-200 bg-amber-50 px-[12px] py-[10px] text-[12px] leading-[18px] text-amber-900">启用沙盒时，当前实际工作区以沙盒策略为准；上方路径仅显示当前生效位置。</p>}
          {form.sandbox_enabled && <LabeledField label="网络访问" hint="统一影响所有 Harness/SRT 执行。默认联网按运行环境放行；白名单只允许列出的域名；全拒绝禁止外网。">
            <select className="h-[36px] rounded-md border border-input bg-background px-[10px] text-[13px]" value={form.sandbox_network_mode} onChange={(e) => update({ sandbox_network_mode: e.target.value as UiConfigForm['sandbox_network_mode'] })}>
              <option value="all">默认联网</option><option value="allowlist">白名单</option><option value="deny">全拒绝</option>
            </select>
          </LabeledField>}
          {form.sandbox_enabled && form.sandbox_network_mode === 'allowlist' && <LabeledField label="允许的域名" hint="每行一个域名，也支持 *.example.com。"><Textarea rows={4} value={form.sandbox_allowed_domains} onChange={(e) => update({ sandbox_allowed_domains: e.target.value })} placeholder="api.example.com\n*.internal.example.com" /></LabeledField>}
          <p className="text-[11px] leading-[16px] text-muted-foreground">关闭沙盒时，命令仍受 TaskFrame 工作区、运行时长和输出大小限制，但不再使用操作系统级 SRT 隔离。</p>
          {updatedAt && <span className="text-[12px] text-muted-foreground">最后更新：{formatDateOnly(updatedAt)}</span>}
        </CardContent>
      </Card>
      <Card className="editor-card settings-card">
        <CardHeader><CardTitle className="flex items-center gap-[8px]"><KeyRound className="size-[16px]" />API 全量密钥</CardTitle></CardHeader>
        <CardContent className="flex items-center justify-between gap-[20px]">
          <div><p className="text-[13px] font-medium text-[#2f3442]">管理员账号全量访问</p><p className="mt-[4px] text-[11px] leading-[17px] text-muted-foreground">用于 API 查询当前账号可访问的数字员工与资源。明文密钥只在创建或轮换时展示一次。</p></div>
          <UIButton variant="outline" onClick={() => setApiKeyOpen(true)}><KeyRound className="size-[15px]" />管理密钥</UIButton>
        </CardContent>
      </Card>
      <AccountApiKeyDialog account={currentUser} open={apiKeyOpen} onClose={() => setApiKeyOpen(false)} />
    </>
  );
}

export function validateContextSettings(form: UiConfigForm): string | null {
  const tokenBudget = Number(form.context_token_budget);
  const triggerRatio = Number(form.context_compaction_trigger_ratio);
  const recentRoundLimit = Number(form.context_recent_round_limit);
  const longSummaryBudget = Number(form.context_long_summary_token_budget);
  const mediumSummaryBudget = Number(form.context_medium_summary_token_budget);
  const integerValues = [tokenBudget, recentRoundLimit, longSummaryBudget, mediumSummaryBudget];
  if (![...integerValues, triggerRatio].every(Number.isFinite)) {
    return '上下文压缩参数必须是数字';
  }
  if (!integerValues.every(Number.isInteger)) {
    return 'Token 预算和保留轮数必须是整数';
  }
  if (tokenBudget < 512 || tokenBudget > 262_144) {
    return '上下文 Token 预算必须在 512–262144 之间';
  }
  if (triggerRatio < 0.1 || triggerRatio > 0.95) {
    return '压缩触发比例必须在 0.10–0.95 之间';
  }
  if (recentRoundLimit < 1 || recentRoundLimit > 50) {
    return '保留最近对话轮数必须在 1–50 之间';
  }
  if (
    longSummaryBudget < 128
    || longSummaryBudget > 32_768
    || mediumSummaryBudget < 128
    || mediumSummaryBudget > 32_768
  ) {
    return '长期与近期摘要预算必须在 128–32768 之间';
  }
  if (longSummaryBudget + mediumSummaryBudget > tokenBudget) {
    return '长期与近期摘要预算之和不能超过上下文预算';
  }
  if (form.context_allowed_roles.length === 0) {
    return '至少保留一种历史消息角色';
  }
  if (!form.context_long_summary_prefix.trim() || !form.context_medium_summary_prefix.trim()) {
    return '摘要前缀不能为空';
  }
  return null;
}

function toggleContextRole(
  roles: UiConfigForm['context_allowed_roles'],
  role: UiConfigForm['context_allowed_roles'][number],
  checked: boolean,
): UiConfigForm['context_allowed_roles'] {
  if (checked) return roles.includes(role) ? roles : [...roles, role];
  return roles.filter((item) => item !== role);
}

function LabeledField({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return <label className="flex flex-col gap-[6px]"><span className="text-[12px] font-medium text-[#464c5e]">{label}</span>{hint && <span className="text-[11px] leading-[16px] text-muted-foreground">{hint}</span>}{children}</label>;
}

function SwitchRow({ label, hint, checked, onChange }: { label: string; hint?: string; checked: boolean; onChange: (next: boolean) => void }) {
  return <label className="flex items-center justify-between gap-[16px]"><span><span className="block text-[12px] font-medium text-[#464c5e]">{label}</span>{hint && <span className="mt-[3px] block text-[11px] leading-[16px] text-muted-foreground">{hint}</span>}</span><Switch checked={checked} onCheckedChange={onChange} /></label>;
}

async function waitForApplicationRestart(): Promise<void> {
  await new Promise((resolve) => window.setTimeout(resolve, 1800));
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      await api.get('/api/health');
      return;
    } catch {
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
  }
  throw new Error('StaffDeck 重启超时，请稍后手动刷新页面');
}
