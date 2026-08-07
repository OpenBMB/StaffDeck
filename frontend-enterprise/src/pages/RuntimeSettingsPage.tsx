import { SaveOutlined } from '../icons';
import { useEffect, useState, type ReactNode } from 'react';
import { Button as UIButton, Card, CardContent, CardHeader, CardTitle, Input, Switch, Textarea, notify } from '@/components/ui';
import { api, TENANT_ID } from '../api/client';
import type { UIConfigRead } from '../types';

type UiConfigForm = {
  show_thinking_trace: boolean;
  show_skill_trace: boolean;
  show_tool_trace: boolean;
  reflection_max_rounds: string;
  agent_loop_max_actions: string;
  sandbox_network_mode: 'all' | 'allowlist' | 'deny';
  sandbox_allowed_domains: string;
};

const DEFAULT_UI_CONFIG: UiConfigForm = {
  show_thinking_trace: true,
  show_skill_trace: true,
  show_tool_trace: true,
  reflection_max_rounds: '1',
  agent_loop_max_actions: '32',
  sandbox_network_mode: 'all',
  sandbox_allowed_domains: '',
};

function formatDateOnly(value: string): string {
  const normalized = /(?:z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? value.slice(0, 10) : date.toISOString().slice(0, 10);
}

export default function RuntimeSettingsPage() {
  const [form, setForm] = useState<UiConfigForm>(DEFAULT_UI_CONFIG);
  const [loading, setLoading] = useState(false);
  const [updatedAt, setUpdatedAt] = useState('');
  const [setupMessage, setSetupMessage] = useState('');
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
          sandbox_network_mode: row.sandbox_network_mode || 'all',
          sandbox_allowed_domains: (row.sandbox_allowed_domains || []).join('\n'),
        });
        setUpdatedAt(row.updated_at);
        setSetupMessage(row.sandbox_setup_instructions || '');
        setSandboxStatus({ sandbox_status: row.sandbox_status, sandbox_status_message: row.sandbox_status_message, sandbox_status_remediation: row.sandbox_status_remediation });
      })
      .catch((error) => notify.error(error.message));
  }, []);

  async function save() {
    const reflectionMaxRounds = Number(form.reflection_max_rounds);
    const agentLoopMaxActions = Number(form.agent_loop_max_actions);
    if (Number.isNaN(reflectionMaxRounds) || Number.isNaN(agentLoopMaxActions)) {
      notify.error('反思轮数与单轮最大动作数必须是数字');
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
        sandbox_network_mode: form.sandbox_network_mode,
        sandbox_allowed_domains: form.sandbox_allowed_domains.split(/[\n,]/).map((item) => item.trim()).filter(Boolean),
      });
      setUpdatedAt(row.updated_at);
      notify.success('运行设置已保存');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存失败');
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <div className="page-title">
        <div><h3>运行设置</h3><p className="text-[12px] text-muted-foreground">统一影响当前租户下所有数字员工的执行行为。</p></div>
        <UIButton disabled={loading} onClick={() => void save()}><SaveOutlined />保存设置</UIButton>
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
      <Card className="editor-card settings-card">
        <CardHeader><CardTitle>沙盒网络策略</CardTitle></CardHeader>
        <CardContent className="flex flex-col gap-[16px]">
          <div className={`whitespace-pre-line rounded-md border px-[12px] py-[10px] text-[12px] leading-[18px] ${sandboxStatus.sandbox_status === 'ready' ? 'border-emerald-200 bg-emerald-50 text-emerald-900' : sandboxStatus.sandbox_status === 'degraded' ? 'border-red-300 bg-red-50 text-red-900' : 'border-amber-200 bg-amber-50 text-amber-900'}`}>
            <div className="font-medium">沙盒状态：{sandboxStatus.sandbox_status === 'ready' ? '可用' : sandboxStatus.sandbox_status === 'degraded' ? '已降级为无沙盒（高风险）' : '不可用'}</div>
            {sandboxStatus.sandbox_status_message && <div>{sandboxStatus.sandbox_status_message}</div>}
            {sandboxStatus.sandbox_status_remediation && <div>{sandboxStatus.sandbox_status_remediation}</div>}
          </div>
          {setupMessage && <div className="whitespace-pre-line rounded-md border border-amber-200 bg-amber-50 px-[12px] py-[10px] text-[12px] leading-[18px] text-amber-900">{setupMessage}</div>}
          <LabeledField label="网络访问" hint="统一影响所有 Harness/SRT 执行。默认联网按运行环境放行；白名单只允许列出的域名；全拒绝禁止外网。">
            <select className="h-[36px] rounded-md border border-input bg-background px-[10px] text-[13px]" value={form.sandbox_network_mode} onChange={(e) => update({ sandbox_network_mode: e.target.value as UiConfigForm['sandbox_network_mode'] })}>
              <option value="all">默认联网</option><option value="allowlist">白名单</option><option value="deny">全拒绝</option>
            </select>
          </LabeledField>
          {form.sandbox_network_mode === 'allowlist' && <LabeledField label="允许的域名" hint="每行一个域名，也支持 *.example.com。"><Textarea rows={4} value={form.sandbox_allowed_domains} onChange={(e) => update({ sandbox_allowed_domains: e.target.value })} placeholder="api.example.com\n*.internal.example.com" /></LabeledField>}
          <p className="text-[11px] leading-[16px] text-muted-foreground">策略由运行 StaffDeck 后端的主机执行。Windows 首次安装 SRT 需要在该主机的 PowerShell 或 CMD 中以管理员身份运行一次安装命令。</p>
          {updatedAt && <span className="text-[12px] text-muted-foreground">最后更新：{formatDateOnly(updatedAt)}</span>}
        </CardContent>
      </Card>
    </>
  );
}

function LabeledField({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return <label className="flex flex-col gap-[6px]"><span className="text-[12px] font-medium text-[#464c5e]">{label}</span>{hint && <span className="text-[11px] leading-[16px] text-muted-foreground">{hint}</span>}{children}</label>;
}

function SwitchRow({ label, checked, onChange }: { label: string; checked: boolean; onChange: (next: boolean) => void }) {
  return <label className="flex items-center justify-between gap-[16px]"><span className="text-[12px] font-medium text-[#464c5e]">{label}</span><Switch checked={checked} onCheckedChange={onChange} /></label>;
}
