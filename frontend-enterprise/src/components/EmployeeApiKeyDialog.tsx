import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  notify,
} from '@/components/ui';
import {
  Ban,
  Check,
  Copy,
  KeyRound,
  LoaderCircle,
  RefreshCw,
  ShieldCheck,
  TerminalSquare,
} from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';

import { api, TENANT_ID } from '../api/client';
import { employeeDisplayName } from '../employee';
import type { AgentProfileRead } from '../types';

type KeyAccess = 'runtime' | 'full_access';

type AgentApiCredential = {
  id: string;
  agent_id: string;
  name: string;
  access: KeyAccess;
  key_prefix: string;
  scopes: string[];
  status: string;
  expires_at?: string | null;
  last_used_at?: string | null;
  created_at: string;
  revoked_at?: string | null;
};

type AgentApiCredentialCreated = AgentApiCredential & {
  api_key: string;
};

const ACCESS_META: Record<KeyAccess, { label: string; summary: string; detail: string }> = {
  runtime: {
    label: '运行密钥',
    summary: '用于外部系统发起对话和任务',
    detail: '可创建会话、运行任务、读取 Trace 与产物；不能读取员工的全部配置资源。',
  },
  full_access: {
    label: '员工全量密钥',
    summary: '读取这个员工的全部开放内容',
    detail: '包含运行权限，并可读取 SOP、知识、技能、工具和定时任务；不能跨员工，也不能读取供应商密钥。',
  },
};

export default function EmployeeApiKeyDialog({
  agent,
  open,
  onClose,
}: {
  agent: AgentProfileRead | null;
  open: boolean;
  onClose: () => void;
}) {
  const [credentials, setCredentials] = useState<AgentApiCredential[]>([]);
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState<KeyAccess | null>(null);
  const [actingId, setActingId] = useState<string | null>(null);
  const [revealed, setRevealed] = useState<AgentApiCredentialCreated | null>(null);
  const [copied, setCopied] = useState(false);
  const displayName = useMemo(() => (agent ? employeeDisplayName(agent) : '数字员工'), [agent]);

  async function load() {
    if (!agent) return;
    setLoading(true);
    try {
      const rows = await api.get<AgentApiCredential[]>(
        `/api/enterprise/agents/${encodeURIComponent(agent.id)}/api-credentials?tenant_id=${encodeURIComponent(TENANT_ID)}`,
      );
      setCredentials(rows);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载 API 密钥失败');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (!open || !agent) return;
    setRevealed(null);
    setCopied(false);
    void load();
  }, [agent, open]);

  async function createCredential(access: KeyAccess) {
    if (!agent) return;
    setCreating(access);
    try {
      const created = await api.post<AgentApiCredentialCreated>(
        `/api/enterprise/agents/${encodeURIComponent(agent.id)}/api-credentials`,
        {
          tenant_id: TENANT_ID,
          name: `${displayName} ${ACCESS_META[access].label}`,
          access,
        },
      );
      setRevealed(created);
      setCopied(false);
      await load();
      notify.success('API 密钥已创建，请立即复制保存');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '创建 API 密钥失败');
    } finally {
      setCreating(null);
    }
  }

  async function rotateCredential(row: AgentApiCredential) {
    if (!agent) return;
    setActingId(row.id);
    try {
      const rotated = await api.post<AgentApiCredentialCreated>(
        `/api/enterprise/agents/${encodeURIComponent(agent.id)}/api-credentials/${encodeURIComponent(row.id)}/rotate?tenant_id=${encodeURIComponent(TENANT_ID)}`,
        {},
      );
      setRevealed(rotated);
      setCopied(false);
      await load();
      notify.success('密钥已轮换，旧密钥立即失效');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '轮换 API 密钥失败');
    } finally {
      setActingId(null);
    }
  }

  async function revokeCredential(row: AgentApiCredential) {
    if (!agent || !window.confirm(`确认禁用「${row.name}」？禁用后调用会立即失败。`)) return;
    setActingId(row.id);
    try {
      await api.post(
        `/api/enterprise/agents/${encodeURIComponent(agent.id)}/api-credentials/${encodeURIComponent(row.id)}/revoke?tenant_id=${encodeURIComponent(TENANT_ID)}`,
        {},
      );
      if (revealed?.id === row.id) setRevealed(null);
      await load();
      notify.success('API 密钥已禁用');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '禁用 API 密钥失败');
    } finally {
      setActingId(null);
    }
  }

  async function copyKey() {
    if (!revealed?.api_key) return;
    try {
      await navigator.clipboard.writeText(revealed.api_key);
      setCopied(true);
      notify.success('密钥已复制');
    } catch {
      notify.error('复制失败，请手动选择密钥文本');
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next && !creating && !actingId) onClose(); }}>
      <DialogContent
        aria-describedby="employee-api-key-description"
        data-i18n-ignore
        className="flex max-h-[calc(100dvh-3rem)] w-[calc(100%-2rem)] flex-col gap-0 overflow-hidden rounded-[18px] border-0 bg-[#f7f8fa] p-0 shadow-[0_28px_80px_rgba(24,31,46,0.20)] sm:max-w-[780px]"
      >
        <DialogHeader className="border-b border-[#e9ecf2] bg-white px-[26px] py-[22px]">
          <div className="flex items-center gap-[12px]">
            <span className="grid size-[38px] place-items-center rounded-[12px] bg-[#18181a] text-white">
              <KeyRound className="size-[18px]" />
            </span>
            <div>
              <DialogTitle className="text-[16px] font-semibold text-[#18181a]">API 密钥 · {displayName}</DialogTitle>
              <DialogDescription id="employee-api-key-description" className="mt-[5px] text-[12px] text-[#757f9c]">
                密钥始终绑定当前员工。明文只展示一次，平台不会保存可再次查看的副本。
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <div className="min-h-0 flex-1 space-y-[18px] overflow-y-auto px-[26px] py-[22px]">
          <section className="grid gap-[12px] md:grid-cols-2" aria-label="创建密钥">
            {(Object.keys(ACCESS_META) as KeyAccess[]).map((access) => {
              const full = access === 'full_access';
              const meta = ACCESS_META[access];
              const Icon = full ? ShieldCheck : TerminalSquare;
              return (
                <article
                  key={access}
                  className={full
                    ? 'relative overflow-hidden rounded-[16px] border border-[#cfe7dc] bg-[#eef8f3] p-[18px]'
                    : 'rounded-[16px] border border-[#e2e6ee] bg-white p-[18px]'}
                >
                  {full && <span className="absolute right-[14px] top-[14px] rounded-full bg-[#d8efe4] px-[8px] py-[3px] text-[10px] font-semibold text-[#207451]">大密钥</span>}
                  <Icon className={full ? 'size-[20px] text-[#207451]' : 'size-[20px] text-[#5d6880]'} />
                  <h3 className="mt-[13px] text-[14px] font-semibold text-[#18181a]">{meta.label}</h3>
                  <p className="mt-[4px] text-[12px] font-medium text-[#464c5e]">{meta.summary}</p>
                  <p className="mt-[8px] min-h-[52px] text-[11px] leading-[17px] text-[#7b8498]">{meta.detail}</p>
                  <Button
                    type="button"
                    disabled={Boolean(creating || actingId)}
                    onClick={() => void createCredential(access)}
                    className={full
                      ? 'mt-[14px] h-[32px] w-full rounded-[10px] bg-[#207451] text-[12px] text-white hover:bg-[#185d40]'
                      : 'mt-[14px] h-[32px] w-full rounded-[10px] bg-[#18181a] text-[12px] text-white hover:bg-[#303033]'}
                  >
                    {creating === access && <LoaderCircle className="size-[14px] animate-spin" />}
                    创建{meta.label}
                  </Button>
                </article>
              );
            })}
          </section>

          {revealed && (
            <section className="rounded-[16px] border border-[#f0d28e] bg-[#fff9e9] p-[18px]" aria-live="polite">
              <div className="flex items-start justify-between gap-[14px]">
                <div>
                  <strong className="text-[13px] text-[#6b4d12]">请现在复制，关闭后无法再次查看</strong>
                  <p className="mt-[4px] text-[11px] text-[#96732f]">{ACCESS_META[revealed.access].label} · {revealed.name}</p>
                </div>
                <span className="rounded-full bg-[#f7e8bc] px-[8px] py-[3px] text-[10px] text-[#7b5c19]">仅显示一次</span>
              </div>
              <div className="mt-[12px] flex items-center gap-[8px] rounded-[12px] bg-[#1d2027] p-[8px] pl-[12px]">
                <code className="min-w-0 flex-1 select-all overflow-x-auto whitespace-nowrap text-[12px] text-[#e7ebf3]">{revealed.api_key}</code>
                <Button type="button" onClick={() => void copyKey()} className="h-[30px] shrink-0 rounded-[8px] bg-white px-[10px] text-[11px] text-[#18181a] hover:bg-[#edf0f5]">
                  {copied ? <Check className="size-[13px]" /> : <Copy className="size-[13px]" />}
                  {copied ? '已复制' : '复制'}
                </Button>
              </div>
            </section>
          )}

          <section>
            <div className="mb-[10px] flex items-center justify-between">
              <div>
                <h3 className="text-[13px] font-semibold text-[#18181a]">已创建密钥</h3>
                <p className="mt-[3px] text-[11px] text-[#8a92a3]">仅显示前缀、权限和使用状态。</p>
              </div>
              <Button type="button" variant="ghost" disabled={loading} onClick={() => void load()} className="h-[30px] rounded-[9px] px-[9px] text-[11px] text-[#687187] hover:bg-white">
                <RefreshCw className={loading ? 'size-[13px] animate-spin' : 'size-[13px]'} />
                刷新
              </Button>
            </div>

            <div className="overflow-hidden rounded-[16px] border border-[#e4e8ef] bg-white">
              {loading && !credentials.length ? (
                <div className="grid h-[92px] place-items-center text-[#8b93a5]"><LoaderCircle className="size-[18px] animate-spin" /></div>
              ) : credentials.length ? credentials.map((row, index) => (
                <div key={row.id} className={`flex flex-col gap-[12px] px-[16px] py-[14px] md:flex-row md:items-center ${index ? 'border-t border-[#edf0f4]' : ''}`}>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-[7px]">
                      <strong className="truncate text-[12px] text-[#252932]">{row.name}</strong>
                      <span className={row.access === 'full_access'
                        ? 'rounded-full bg-[#e5f4ec] px-[7px] py-[2px] text-[9px] font-medium text-[#207451]'
                        : 'rounded-full bg-[#edf1f8] px-[7px] py-[2px] text-[9px] font-medium text-[#56627a]'}
                      >
                        {ACCESS_META[row.access].label}
                      </span>
                      <span className={row.status === 'active'
                        ? 'rounded-full bg-[#e8f7ec] px-[7px] py-[2px] text-[9px] text-[#218546]'
                        : 'rounded-full bg-[#f1f2f5] px-[7px] py-[2px] text-[9px] text-[#8a92a2]'}
                      >
                        {row.status === 'active' ? '启用' : '已禁用'}
                      </span>
                    </div>
                    <div className="mt-[6px] flex flex-wrap gap-x-[14px] gap-y-[3px] text-[10px] text-[#8a92a3]">
                      <code>{row.key_prefix}</code>
                      <span>创建于 {formatDate(row.created_at)}</span>
                      <span>{row.last_used_at ? `最后使用 ${formatDate(row.last_used_at)}` : '尚未使用'}</span>
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-[6px]">
                    <Button type="button" variant="outline" disabled={Boolean(actingId)} onClick={() => void rotateCredential(row)} className="h-[29px] rounded-[9px] border-[#e2e6ed] px-[9px] text-[10px] text-[#5e687c] hover:bg-[#f4f6f9]">
                      <RefreshCw className={actingId === row.id ? 'size-[12px] animate-spin' : 'size-[12px]'} />
                      轮换
                    </Button>
                    <Button type="button" variant="outline" disabled={row.status !== 'active' || Boolean(actingId)} onClick={() => void revokeCredential(row)} className="h-[29px] rounded-[9px] border-[#f0d8d8] px-[9px] text-[10px] text-[#bd4141] hover:bg-[#fff1f1] hover:text-[#a62d2d]">
                      <Ban className="size-[12px]" />
                      禁用
                    </Button>
                  </div>
                </div>
              )) : (
                <div className="flex h-[92px] flex-col items-center justify-center text-[#9098a9]">
                  <KeyRound className="size-[18px]" />
                  <span className="mt-[7px] text-[11px]">还没有为这个员工创建 API 密钥</span>
                </div>
              )}
            </div>
          </section>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function formatDate(value?: string | null): string {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}
