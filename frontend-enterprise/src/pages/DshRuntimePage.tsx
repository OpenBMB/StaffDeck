import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Button as UIButton, Select, SelectContent, SelectItem, SelectTrigger, SelectValue, Tabs, TabsList, TabsTrigger, notify } from '@/components/ui';
import { cn } from '@/lib/utils';
import AppHeader from '@/components/AppHeader';
import { StatCard } from '@/components/StatCard';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import ModuleTree from '@/components/dsh/ModuleTree';
import { api, TENANT_ID } from '../api/client';
import { dshApi, type DshEngineChoice, type DshLedgerRow, type DshSnapshot, type DshStaffEngine, type DshStatus, type DshTreeBig } from '../api/dsh';
import type { AgentProfileRead } from '../types';
import type { EnterpriseAuthUser } from '../auth';
import IconRefresh from '../assets/icons/refresh.svg?react';
import IconAgents from '../assets/icons/nav-agents.svg?react';
import IconTable from '../assets/icons/table.svg?react';
import IconMasonry from '../assets/icons/view-masonry.svg?react';
import IconSearch from '../assets/icons/search.svg?react';
import IconClear from '../assets/icons/field-clear.svg?react';
import IconSuccess from '../assets/icons/success-fill.svg?react';
import IconWarning from '../assets/icons/warning-fill.svg?react';
import IconError from '../assets/icons/error-fill.svg?react';

type TabKey = 'overview' | 'modules' | 'snapshot' | 'ledger';

const TABS: Array<{ key: TabKey; label: string }> = [
  { key: 'overview', label: '总览' },
  { key: 'modules', label: '模块装配' },
  { key: 'snapshot', label: '组成快照' },
  { key: 'ledger', label: '调用台账' },
];

const STATUS_LABEL: Record<string, { text: string; className: string }> = {
  completed: { text: '已完成', className: 'bg-[#e9f7ef] text-[#2cb360]' },
  failed: { text: '失败', className: 'bg-[#fce7e7] text-[#d20b0b]' },
  outcome_unknown: { text: '结果未知', className: 'bg-[#fff4e5] text-[#c2740c]' },
  denied: { text: '权限拒绝', className: 'bg-[#f3f4f6] text-[#757f9c]' },
  cancelled: { text: '已取消', className: 'bg-[#f3f4f6] text-[#757f9c]' },
  started: { text: '执行中', className: 'bg-[#e8f1ff] text-[#2f6fdb]' },
};

function Pill({ status }: { status: string }) {
  const meta = STATUS_LABEL[status] ?? { text: status, className: 'bg-[#f3f4f6] text-[#757f9c]' };
  return <span className={cn('inline-flex h-[22px] items-center rounded-full px-[9px] text-[11px] leading-none', meta.className)}>{meta.text}</span>;
}

function SectionLabel({ icon, children }: { icon: ReactNode; children: ReactNode }) {
  return (
    <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
      <span className="grid size-[14px] shrink-0 place-items-center [&>svg]:size-[14px]">{icon}</span>
      <span className="text-[14px] font-normal leading-none">{children}</span>
    </div>
  );
}

function KV({ label, children, mono }: { label: string; children: ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-start gap-[16px] py-[10px] [&:not(:last-child)]:border-b-[0.5px] [&:not(:last-child)]:border-[#eef1f6]">
      <div className="w-[140px] shrink-0 text-[12px] leading-[20px] text-[#9aa0ad]">{label}</div>
      <div className={cn('min-w-0 flex-1 text-[13px] leading-[20px] text-[#18181a]', mono && 'break-all font-mono text-[12px]')}>{children}</div>
    </div>
  );
}

function StatusDot({ ok, warn }: { ok: boolean; warn?: boolean }) {
  if (ok) return <IconSuccess className="size-[14px] text-[#2cb360]" />;
  if (warn) return <IconWarning className="size-[14px] text-[#c2740c]" />;
  return <IconError className="size-[14px] text-[#d20b0b]" />;
}

export default function DshRuntimePage({ currentUser, onLogout }: { currentUser: EnterpriseAuthUser; onLogout?: () => void }) {
  const [tab, setTab] = useState<TabKey>('overview');
  const [status, setStatus] = useState<DshStatus | null>(null);
  const [tree, setTree] = useState<DshTreeBig[]>([]);
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [agentId, setAgentId] = useState('');
  const [snapshot, setSnapshot] = useState<DshSnapshot | null>(null);
  const [staffEngine, setStaffEngine] = useState<DshStaffEngine | null>(null);
  const [ledgerUnknown, setLedgerUnknown] = useState<DshLedgerRow[]>([]);
  const [ledgerRecent, setLedgerRecent] = useState<DshLedgerRow[]>([]);
  const [sessionFilter, setSessionFilter] = useState('');
  const [loading, setLoading] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const [st, t, agts] = await Promise.all([dshApi.status(TENANT_ID), dshApi.modulesTree(TENANT_ID), api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`)]);
      setStatus(st);
      setTree(t);
      setAgents(agts);
      if (!agentId && agts[0]) setAgentId(agts.find((a) => !a.is_overall)?.id ?? agts[0].id);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载失败');
    } finally {
      setLoading(false);
    }
  }

  async function loadSnapshot(id = agentId) {
    if (!id) return;
    setLoading(true);
    try {
      const [snap, eng] = await Promise.all([dshApi.snapshot(TENANT_ID, id), dshApi.staffEngine(TENANT_ID, id)]);
      setSnapshot(snap);
      setStaffEngine(eng);
    } catch (error) {
      setSnapshot(null);
      notify.error(error instanceof Error ? error.message : '快照编译失败');
    } finally {
      setLoading(false);
    }
  }

  async function setEngine(engine: DshEngineChoice) {
    if (!agentId) return;
    try {
      const row = await dshApi.setStaffEngine(TENANT_ID, agentId, engine);
      setStaffEngine(row);
      notify.success(`已切换到${row.effective_engine === 'dsh' ? ' DSH' : ' Harness v2'}，下一轮对话生效`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '切换失败');
    }
  }

  async function loadLedger() {
    try {
      const [unknown, recent] = await Promise.all([dshApi.ledgerUnknown(TENANT_ID), dshApi.ledgerRecent(TENANT_ID, sessionFilter || undefined, 100)]);
      setLedgerUnknown(unknown);
      setLedgerRecent(recent);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载台账失败');
    }
  }

  async function reconcile(id: string, outcome: 'completed' | 'failed') {
    try {
      await dshApi.reconcile(TENANT_ID, id, outcome);
      notify.success(outcome === 'completed' ? '已记为成功' : '已记为未发生');
      await loadLedger();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '结算失败');
    }
  }

  useEffect(() => { void load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (tab === 'snapshot' && agentId) void loadSnapshot(agentId); }, [tab, agentId]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (tab === 'ledger') void loadLedger(); }, [tab, sessionFilter]); // eslint-disable-line react-hooks/exhaustive-deps

  const modulesTotal = status?.modules_total ?? 0;
  const modulesEnabled = status?.modules_enabled ?? 0;
  const bigCount = tree.length;
  const subCount = tree.reduce((n, b) => n + b.subs.length, 0);

  const ledgerColumns = useMemo<DataTableColumn<DshLedgerRow>[]>(() => [
    { key: 'time', title: '时间', width: 160, render: (r) => <span className="text-[12px] text-[#757f9c]">{new Date(r.started_at).toLocaleString('zh-CN', { hour12: false })}</span> },
    { key: 'call', title: '调用', render: (r) => <span className="block"><span className="block text-[13px] text-[#18181a]">{r.tool_name}</span><span className="block font-mono text-[11px] text-[#9aa0ad]">{r.session_id}</span></span> },
    { key: 'status', title: '状态', width: 110, render: (r) => <Pill status={r.status} /> },
    { key: 'key', title: '副作用键', width: 150, render: (r) => r.side_effect_key ? <code className="rounded-[6px] bg-[#f6f6f6] px-[6px] py-[2px] font-mono text-[11px] text-[#464c5e]">{r.side_effect_key.slice(0, 16)}…</code> : <span className="text-[#c0c6d4]">—</span> },
    { key: 'engine', title: '引擎', width: 90, render: (r) => <span className="text-[12px] text-[#464c5e]">{r.engine === 'dsh' ? 'DSH' : r.engine ?? 'Harness'}</span> },
  ], []);

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader className="items-center" onLogout={onLogout} userName={currentUser?.username} title="运行时与插件" description="DSH 引擎、模块装配、员工组成快照与调用台账" />

      <div className="mt-[20px] flex flex-wrap items-end justify-between gap-[12px]">
        <Tabs value={tab} onValueChange={(v) => setTab(v as TabKey)}>
          <TabsList aria-label="运行时与插件分区" className="h-[35px]! gap-2 rounded-none bg-transparent p-0">
            {TABS.map((t) => (
              <TabsTrigger
                key={t.key}
                value={t.key}
                className="h-[35px] min-w-[112px] gap-[7px] rounded-t-lg rounded-b-none border-0 px-[16px] text-[14px] font-bold text-[#8b94aa] hover:text-[#202226] data-[state=active]:bg-white data-[state=active]:text-[#202226] data-[state=active]:shadow-[0_-12px_28px_rgba(21,26,38,0.04)]"
              >
                {t.label}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
        <UIButton
          variant="outline"
          onClick={() => void load()}
          disabled={loading}
          className="mb-[6px] h-[34px] gap-[4px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[20px] text-[12px] font-normal text-[#757f9c] hover:border-[#cbd3e6] hover:bg-white hover:text-[#18181a]"
        >
          <IconRefresh className={cn('size-[14px]', loading && 'animate-spin')} />
          刷新
        </UIButton>
      </div>

      <div className="flex flex-col gap-[24px] rounded-[20px_20px_0_0] bg-white p-[18px_18px_24px_18px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        <div className="flex flex-wrap items-stretch gap-[20px]" aria-label="运行时统计">
          <StatCard label="默认引擎" value={status ? (status.default_engine === 'dsh' ? 'DSH' : 'Harness v2') : '-'} valueClassName="text-[18px] leading-[26px]" />
          <StatCard label="安全配置" value={status?.security_profile ?? '-'} valueClassName="text-[18px] leading-[26px]" />
          <StatCard label="模块已启用" value={`${modulesEnabled}/${modulesTotal}`} tone={modulesEnabled > 0 ? 'green' : 'default'} />
          <StatCard label="DSH 子进程" value={status ? (status.dsh_enabled ? (status.runtime_ok ? '运行中' : '不可用') : '未启用') : '-'} tone={status?.dsh_enabled ? (status.runtime_ok ? 'green' : 'red') : 'default'} valueClassName="text-[18px] leading-[26px]" />
        </div>

        {tab === 'overview' && (
          <div className="flex flex-col gap-[18px]">
            <SectionLabel icon={<IconMasonry />}>运行时状态</SectionLabel>
            <div className="rounded-[14px] border-[0.5px] border-[#e3e7f1] px-[18px]">
              {status ? (
                <>
                  <KV label="执行引擎">
                    <span className="inline-flex items-center gap-[8px]">
                      <StatusDot ok={status.dsh_enabled} warn={!status.dsh_enabled} />
                      {status.dsh_enabled ? 'DSH（DeepSeek Harness）为默认引擎' : 'Harness v2（进程内）为默认引擎，DSH 未开启'}
                    </span>
                  </KV>
                  <KV label="DSH 子进程">
                    <span className="inline-flex items-center gap-[8px]">
                      <StatusDot ok={status.dsh_enabled && status.runtime_ok} warn={!status.dsh_enabled} />
                      {status.dsh_enabled ? (status.runtime_ok ? `运行中，当前 ${status.live_activations} 个激活` : `不可用：${status.runtime_error?.message ?? '未知错误'}`) : '未启用'}
                    </span>
                  </KV>
                  {status.mcp_url && <KV label="能力回调（MCP）" mono>{status.mcp_url}</KV>}
                  <KV label="安全配置">
                    {status.security_profile}
                    <span className="ml-[8px] text-[12px] text-[#9aa0ad]">部署级二选一；OSS 本地权限并非空实现，企业版 Base 不可用时拒绝而不回退</span>
                  </KV>
                  <KV label="模块注册表">{modulesEnabled} / {modulesTotal} 个启用 · {bigCount} 个大模块 · {subCount} 个子模块 · Generation {status.registry_generation}</KV>
                  <KV label="灰度名单">{status.staff_allowlist.length ? status.staff_allowlist.join('、') : <span className="text-[#9aa0ad]">未设置（全部员工遵循默认引擎，可在「组成快照」按员工覆盖）</span>}</KV>
                  <KV label="失败回退">{status.fallback_to_legacy ? 'DSH 不可用时回退到 Harness v2' : '不回退（DSH 不可用即失败）'}</KV>
                  <KV label="DSH 路径" mono>{status.dsh_root || '—'}</KV>
                </>
              ) : (
                <div className="py-[28px] text-center text-[13px] text-[#858b9c]">加载中…</div>
              )}
            </div>
          </div>
        )}

        {tab === 'modules' && (
          <div className="flex flex-col gap-[18px]">
            <SectionLabel icon={<IconMasonry />}>模块装配 · 大模块 → 子模块 → 插件</SectionLabel>
            <ModuleTree tree={tree} loading={loading} />
          </div>
        )}

        {tab === 'snapshot' && (
          <div className="flex flex-col gap-[18px]">
            <SectionLabel icon={<IconAgents />}>数字员工组成快照</SectionLabel>
            <div className="flex flex-wrap items-center gap-[10px]">
              <Select value={agentId || undefined} onValueChange={setAgentId}>
                <SelectTrigger className="h-[34px] w-[260px] rounded-[10px] border-[0.5px] border-[#e3e7f1] text-[12px]"><SelectValue placeholder="选择数字员工" /></SelectTrigger>
                <SelectContent>{agents.map((a) => <SelectItem key={a.id} value={a.id}>{a.name}{a.is_overall ? '（整体）' : ''}</SelectItem>)}</SelectContent>
              </Select>
              {staffEngine && (
                <Select value={staffEngine.engine} onValueChange={(v) => void setEngine(v as DshEngineChoice)}>
                  <SelectTrigger className="h-[34px] w-[240px] rounded-[10px] border-[0.5px] border-[#e3e7f1] text-[12px]"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="default">跟随默认（当前 {staffEngine.effective_engine === 'dsh' ? 'DSH' : 'Harness v2'}）</SelectItem>
                    <SelectItem value="dsh">此员工强制 DSH</SelectItem>
                    <SelectItem value="legacy">此员工强制 Harness v2</SelectItem>
                  </SelectContent>
                </Select>
              )}
              <span className="text-[12px] text-[#9aa0ad]">切换存到该员工元数据，下一轮对话生效</span>
            </div>

            {snapshot ? (
              <div className="grid gap-[16px] lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
                <div className="rounded-[14px] border-[0.5px] border-[#e3e7f1] px-[18px]">
                  <KV label="快照 ID"><span title={snapshot.snapshot_id} className="font-mono text-[12px] text-[#464c5e]">{snapshot.snapshot_id.slice(0, 16)}…{snapshot.snapshot_id.slice(-8)}</span><span className="ml-[8px] text-[12px] text-[#9aa0ad]">绑定变化即换新 ID，下一轮生效</span></KV>
                  <KV label="Persona">{snapshot.persona_preview ? <span className="line-clamp-3 whitespace-pre-line">{snapshot.persona_preview}</span> : <span className="text-[#9aa0ad]">（无）</span>}</KV>
                  <KV label="模型路由">{Object.keys(snapshot.model_route).length ? Object.entries(snapshot.model_route).map(([k, v]) => <span key={k} className="mr-[10px]"><span className="text-[#9aa0ad]">{k}</span> <code className="rounded-[6px] bg-[#f6f6f6] px-[6px] py-[2px] font-mono text-[11px]">{v}</code></span>) : <span className="text-[#d20b0b]">未配置默认模型</span>}</KV>
                  <KV label="Hook 计划">
                    <div className="flex flex-col gap-[4px]">
                      {Object.entries(snapshot.hooks).map(([point, handlers]) => (
                        <div key={point} className="flex flex-wrap items-center gap-[6px] text-[12px]"><span className="w-[100px] shrink-0 font-mono text-[#9aa0ad]">{point}</span>{handlers.map((h, i) => <span key={h} className="inline-flex items-center gap-[6px]">{i > 0 && <span className="text-[#c0c6d4]">→</span>}<code className="rounded-[6px] bg-[#f6f6f6] px-[6px] py-[2px] font-mono text-[11px]">{h}</code></span>)}</div>
                      ))}
                    </div>
                  </KV>
                  <KV label="DSH 暴露的工具"><div className="flex flex-wrap gap-[6px]">{snapshot.proxy_tools.map((t) => <code key={t} className="rounded-[6px] bg-[#f0eef9] px-[6px] py-[2px] font-mono text-[11px] text-[#6a4fc7]">mcp__staffdeck__{t}</code>)}</div></KV>
                  <KV label="渠道 / 团队">{snapshot.channels.length ? `${snapshot.channels.length} 个渠道绑定` : '无渠道'}{snapshot.team_id ? ` · 团队 ${snapshot.team_id}` : ''}</KV>
                </div>
                <div className="flex flex-col gap-[14px]">
                  <div className="rounded-[14px] border-[0.5px] border-[#e3e7f1] p-[14px]">
                    <div className="mb-[10px] flex items-center justify-between"><span className="text-[13px] text-[#18181a]">可激活能力</span><span className="text-[12px] text-[#9aa0ad]">{snapshot.grants.length} 项</span></div>
                    <div className="flex max-h-[240px] flex-col gap-[6px] overflow-auto pr-[4px]">
                      {snapshot.grants.map((g, i) => (
                        <div key={i} className="flex items-center gap-[8px] rounded-[8px] bg-[#fafbfd] px-[10px] py-[6px] text-[12px]">
                          <span className={cn('shrink-0 rounded-[6px] px-[6px] py-[2px] text-[11px] leading-none', g.scope === 'sop_specific' ? 'bg-[#fff4e5] text-[#c2740c]' : 'bg-[#e8f1ff] text-[#2f6fdb]')}>{g.scope === 'sop_specific' ? 'SOP' : '直接'}</span>
                          <span className="min-w-0 flex-1 truncate text-[#18181a]">{g.name}</span>
                          <span className="shrink-0 font-mono text-[11px] text-[#9aa0ad]">{g.operation.split('/')[0]}</span>
                        </div>
                      ))}
                      {snapshot.grants.length === 0 && <div className="py-[16px] text-center text-[12px] text-[#9aa0ad]">该员工尚未绑定任何能力</div>}
                    </div>
                  </div>
                  <div className="rounded-[14px] border-[0.5px] border-[#e3e7f1] p-[14px]">
                    <div className="mb-[10px] flex items-center justify-between"><span className="text-[13px] text-[#18181a]">SOP 与逻辑槽</span><span className="text-[12px] text-[#9aa0ad]">{snapshot.sops.length} 个</span></div>
                    <div className="flex max-h-[240px] flex-col gap-[8px] overflow-auto pr-[4px]">
                      {snapshot.sops.map((s) => (
                        <div key={s.skill_id} className="rounded-[8px] bg-[#fafbfd] px-[10px] py-[8px]">
                          <div className="flex items-center justify-between text-[12px]"><span className="truncate text-[#18181a]">{s.name}</span><span className="shrink-0 font-mono text-[11px] text-[#9aa0ad]">v{s.version}</span></div>
                          {s.resolved_slots.length > 0 && <ul className="mt-[4px] flex flex-col gap-[2px] text-[11px] text-[#757f9c]">{s.resolved_slots.map((slot, i) => <li key={i} className="truncate">{slot.slot} → {slot.resource_type}:{slot.resource_id}{slot.required ? ' · 必选' : ''}</li>)}</ul>}
                          {s.sub_sop_ids.length > 0 && <div className="mt-[4px] text-[11px] text-[#9aa0ad]">子流程：{s.sub_sop_ids.join('、')}</div>}
                        </div>
                      ))}
                      {snapshot.sops.length === 0 && <div className="py-[16px] text-center text-[12px] text-[#9aa0ad]">未绑定 SOP</div>}
                    </div>
                  </div>
                </div>
              </div>
            ) : (
              <div className="rounded-[14px] border-[0.5px] border-dashed border-[#e3e7f1] py-[40px] text-center text-[13px] text-[#858b9c]">{loading ? '正在编译快照…' : '选择一个数字员工以编译其组成快照'}</div>
            )}
          </div>
        )}

        {tab === 'ledger' && (
          <div className="flex flex-col gap-[18px]">
            <SectionLabel icon={<IconTable />}>Invocation Ledger</SectionLabel>
            {ledgerUnknown.length > 0 && (
              <div className="rounded-[14px] border-[0.5px] border-[#f5d9a8] bg-[#fffaf0] p-[14px]">
                <div className="mb-[10px] flex items-center gap-[8px] text-[13px] text-[#18181a]"><IconWarning className="size-[14px] text-[#c2740c]" />{ledgerUnknown.length} 个调用结果未知，需核对外部系统后结算；结算前同副作用调用会被阻断而不会重放</div>
                <div className="flex flex-col gap-[8px]">
                  {ledgerUnknown.map((r) => (
                    <div key={r.id} className="flex flex-wrap items-center gap-[10px] rounded-[10px] bg-white px-[12px] py-[8px] text-[12px]">
                      <span className="min-w-0 flex-1 truncate"><span className="text-[#18181a]">{r.tool_name}</span><span className="ml-[8px] font-mono text-[11px] text-[#9aa0ad]">{r.session_id}</span></span>
                      <UIButton size="sm" variant="outline" className="h-[28px] rounded-[8px] border-[0.5px] border-[#e3e7f1] text-[12px]" onClick={() => void reconcile(r.id, 'completed')}>外部已成功</UIButton>
                      <UIButton size="sm" variant="outline" className="h-[28px] rounded-[8px] border-[0.5px] border-[#e3e7f1] text-[12px]" onClick={() => void reconcile(r.id, 'failed')}>未发生，允许重试</UIButton>
                    </div>
                  ))}
                </div>
              </div>
            )}
            <label className="flex h-[34px] w-[320px] items-center gap-[8px] overflow-hidden rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] transition-colors focus-within:border-[#18181a] max-[900px]:w-full">
              <IconSearch className="size-[14px] shrink-0 text-[#858b9c]" />
              <input value={sessionFilter} onChange={(e) => setSessionFilter(e.target.value)} placeholder="按会话 ID 过滤" className="h-full min-w-0 flex-1 bg-transparent text-[12px] text-[#17191f] outline-none placeholder:text-[#c0c6d4]" />
              {sessionFilter && <button type="button" aria-label="清除" onClick={() => setSessionFilter('')} className="grid size-[16px] place-items-center text-[#c0c6d4] hover:text-[#858b9c]"><IconClear className="size-[14px]" /></button>}
            </label>
            <DataTable aria-label="调用台账" columns={ledgerColumns} data={ledgerRecent} rowKey={(r) => r.id} size="compact" striped emptyText="暂无调用记录" />
          </div>
        )}
      </div>
    </div>
  );
}
