import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
  Button as UIButton, Select, SelectContent, SelectItem, SelectTrigger, SelectValue, Switch, Tabs, TabsList, TabsTrigger, notify,
} from '@/components/ui';
import { cn } from '@/lib/utils';
import AppHeader from '@/components/AppHeader';
import { StatCard } from '@/components/StatCard';
import ModuleTree from '@/components/harness/ModuleTree';
import SessionLog from '@/components/harness/SessionLog';
import BaseConnectionPanel from '@/components/harness/BaseConnectionPanel';
import ExternalModulesPanel from '@/components/harness/ExternalModulesPanel';
import { api, TENANT_ID } from '../../api/client';
import { harnessApi, type HarnessAssembly, type HarnessAssemblyState, type HarnessAssemblyUpdate, type HarnessBaseConnectionUpdate, type HarnessEngineChoice, type HarnessLedgerRow, type HarnessLogEntry, type HarnessSessionSummary, type HarnessSnapshot, type HarnessStaffEngine, type HarnessStatus, type HarnessTreeBig, type HarnessTreeOption } from '../../api/harness';
import type { AgentProfileRead, ChannelBindingRead, ModelConfigRead, TeamRead } from '../../types';
import type { EnterpriseAuthUser } from '../../auth';
import { EnterpriseRoute } from '../../enums/routes';
import { formatClientDateTime } from '../../lib/timezone';
import { engineLabel, hookHandlerLabel, hookPointLabel, modelRoleLabel, operationLabel, profileLabel, proxyToolLabel, readTechMode, resourceTypeLabel, toolLabel, writeTechMode } from '../../lib/harnessLabels';
import IconRefresh from '../../assets/icons/refresh.svg?react';
import IconSuccess from '../../assets/icons/success-fill.svg?react';
import IconWarning from '../../assets/icons/warning-fill.svg?react';
import IconError from '../../assets/icons/error-fill.svg?react';
import IconBack from '../../assets/icons/chevron-down.svg?react';

type TabKey = 'overview' | 'modules' | 'staff' | 'log';

/** Pseudo-session id for the admin audit trail (assembly saves, connection tests, restarts). */
const ADMIN_AUDIT_SESSION = '__admin_audit__';

const TABS: Array<{ key: TabKey; label: string }> = [
  { key: 'overview', label: '运行状态' },
  { key: 'modules', label: '功能模块' },
  { key: 'staff', label: '员工配置' },
  { key: 'log', label: '执行日志' },
];

function KV({ label, children, mono }: { label: string; children: ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-start gap-[16px] py-[10px] [&:not(:last-child)]:border-b-[0.5px] [&:not(:last-child)]:border-[#eef1f6]">
      <div className="w-[150px] shrink-0 text-[12px] leading-[20px] text-[#9aa0ad]">{label}</div>
      <div className={cn('min-w-0 flex-1 text-[13px] leading-[20px] text-[#18181a]', mono && 'break-all font-mono text-[12px]')}>{children}</div>
    </div>
  );
}

function Hint({ children }: { children: ReactNode }) {
  return <span className="ml-[8px] text-[12px] text-[#9aa0ad]">{children}</span>;
}

function StatusDot({ state }: { state: 'ok' | 'warn' | 'error' }) {
  if (state === 'ok') return <IconSuccess className="size-[14px] shrink-0 text-[#2cb360]" />;
  if (state === 'warn') return <IconWarning className="size-[14px] shrink-0 text-[#c2740c]" />;
  return <IconError className="size-[14px] shrink-0 text-[#d20b0b]" />;
}

function Panel({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn('rounded-[14px] border-[0.5px] border-[#e3e7f1] bg-white', className)}>{children}</div>;
}

/** 保存的装配和运行中的装配差在哪，用一句话说清楚。 */
function describePending(state: HarnessAssemblyState | null): string[] {
  if (!state || !state.pending) return [];
  const a = state.applied;
  const s = state.saved;
  const out: string[] = [];
  if (!a) return ['运行时尚未启动'];
  if (a.engine !== s.engine) out.push(`执行引擎：${engineLabel(a.engine)} → ${engineLabel(s.engine)}`);
  if (a.security_profile !== s.security_profile) out.push(`权限模式：${profileLabel(a.security_profile).short} → ${profileLabel(s.security_profile).short}`);
  const off = s.disabled_modules.filter((m) => !a.disabled_modules.includes(m));
  const on = a.disabled_modules.filter((m) => !s.disabled_modules.includes(m));
  if (off.length) out.push(`停用 ${off.length} 个模块`);
  if (on.length) out.push(`启用 ${on.length} 个模块`);
  const add = s.extra_modules.filter((m) => !a.extra_modules.includes(m));
  const rm = a.extra_modules.filter((m) => !s.extra_modules.includes(m));
  if (add.length) out.push(`接入 ${add.length} 个外部模块`);
  if (rm.length) out.push(`移除 ${rm.length} 个外部模块`);
  if (out.length === 0) out.push('权限中心连接设置已变化');
  return out;
}

/** 本地先改，再排队保存：连续点击不会互相覆盖，也不会因为上一次还没返回而被吞掉。 */
function applyLocally(saved: HarnessAssembly, patch: HarnessAssemblyUpdate): HarnessAssembly {
  const next: HarnessAssembly = { ...saved };
  if (patch.engine) next.engine = patch.engine;
  if (patch.security_profile) next.security_profile = patch.security_profile;
  if (patch.disabled_modules) next.disabled_modules = [...patch.disabled_modules].sort();
  if (patch.extra_modules) next.extra_modules = [...patch.extra_modules];
  if (patch.placements) {
    const pl = { ...saved.placements };
    for (const [k, v] of Object.entries(patch.placements)) { if (v) pl[k] = v; else delete pl[k]; }
    next.placements = pl;
  }
  return next;
}

export default function AdminPage({ currentUser, onLogout }: { currentUser: EnterpriseAuthUser; onLogout?: () => void }) {
  const navigate = useNavigate();
  const [tab, setTab] = useState<TabKey>('overview');
  const [tech, setTech] = useState<boolean>(() => readTechMode());
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);

  const [status, setStatus] = useState<HarnessStatus | null>(null);
  const [tree, setTree] = useState<HarnessTreeBig[]>([]);
  const [assembly, setAssembly] = useState<HarnessAssemblyState | null>(null);
  const [options, setOptions] = useState<HarnessTreeOption[]>([]);
  const [restartOpen, setRestartOpen] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const assemblyRef = useRef<HarnessAssemblyState | null>(null);
  const saveChain = useRef<Promise<void>>(Promise.resolve());
  useEffect(() => { assemblyRef.current = assembly; }, [assembly]);

  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [models, setModels] = useState<ModelConfigRead[]>([]);
  const [teams, setTeams] = useState<TeamRead[]>([]);
  const [channels, setChannels] = useState<ChannelBindingRead[]>([]);
  const [agentId, setAgentId] = useState('');
  const [snapshot, setSnapshot] = useState<HarnessSnapshot | null>(null);
  const [staffEngine, setStaffEngine] = useState<HarnessStaffEngine | null>(null);

  const [ledgerUnknown, setLedgerUnknown] = useState<HarnessLedgerRow[]>([]);
  const [unknownOpen, setUnknownOpen] = useState(false);
  const [sessions, setSessions] = useState<HarnessSessionSummary[]>([]);
  const [sessionId, setSessionId] = useState('');
  const [logEntries, setLogEntries] = useState<HarnessLogEntry[]>([]);
  const [logLoading, setLogLoading] = useState(false);

  useEffect(() => { writeTechMode(tech); }, [tech]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [st, t, cfg, agts, opts] = await Promise.all([
        harnessApi.status(TENANT_ID),
        harnessApi.modulesTree(TENANT_ID),
        harnessApi.config(TENANT_ID),
        api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`),
        harnessApi.treeOptions(TENANT_ID).catch(() => [] as HarnessTreeOption[]),
      ]);
      setStatus(st);
      setTree(t);
      setAssembly(cfg);
      setOptions(opts);
      setAgents(agts);
      setAgentId((prev) => prev || agts.find((a) => !a.is_overall)?.id || agts[0]?.id || '');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  const loadLookups = useCallback(async () => {
    const [m, tm, ch] = await Promise.all([
      api.get<ModelConfigRead[]>(`/api/enterprise/model-configs?tenant_id=${TENANT_ID}`).catch(() => [] as ModelConfigRead[]),
      api.get<TeamRead[]>(`/api/enterprise/teams?tenant_id=${TENANT_ID}`).catch(() => [] as TeamRead[]),
      api.get<ChannelBindingRead[]>(`/api/enterprise/channels?tenant_id=${TENANT_ID}`).catch(() => [] as ChannelBindingRead[]),
    ]);
    setModels(m);
    setTeams(tm);
    setChannels(ch);
  }, []);

  async function loadSnapshot(id = agentId) {
    if (!id) return;
    setLoading(true);
    try {
      const [snap, eng] = await Promise.all([harnessApi.snapshot(TENANT_ID, id), harnessApi.staffEngine(TENANT_ID, id)]);
      setSnapshot(snap);
      setStaffEngine(eng);
    } catch (error) {
      setSnapshot(null);
      notify.error(error instanceof Error ? error.message : '读取员工配置失败');
    } finally {
      setLoading(false);
    }
  }

  async function setEngine(engine: HarnessEngineChoice) {
    if (!agentId) return;
    try {
      const row = await harnessApi.setStaffEngine(TENANT_ID, agentId, engine);
      setStaffEngine(row);
      notify.success(`已切换为 ${engineLabel(row.effective_engine, true)}，下一轮对话生效`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '切换失败');
    }
  }

  const loadLog = useCallback(async (id = sessionId) => {
    if (!id) return;
    setLogLoading(true);
    try {
      const body = id === ADMIN_AUDIT_SESSION ? await harnessApi.audit(TENANT_ID) : await harnessApi.log(TENANT_ID, id);
      setLogEntries(body.entries);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '读取日志失败');
    } finally {
      setLogLoading(false);
    }
  }, [sessionId]);

  async function loadLogTab() {
    try {
      const [unknown, ss] = await Promise.all([harnessApi.ledgerUnknown(TENANT_ID), harnessApi.sessionsRecent(TENANT_ID)]);
      setLedgerUnknown(unknown);
      setSessions([{ session_id: ADMIN_AUDIT_SESSION, title: '管理操作记录', agent_id: null, agent_name: '管理后台', channel: 'admin', status: 'active', updated_at: new Date().toISOString() }, ...ss]);
      setSessionId((prev) => prev || ss[0]?.session_id || ADMIN_AUDIT_SESSION);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载失败');
    }
  }

  async function reconcile(id: string, outcome: 'completed' | 'failed') {
    try {
      await harnessApi.reconcile(TENANT_ID, id, outcome);
      notify.success(outcome === 'completed' ? '已记为成功' : '已记为未执行，之后可以重试');
      await loadLogTab();
      await loadLog();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '确认失败');
    }
  }

  /**
   * Optimistic + serialised: the UI reflects the change at once; PUTs run one after another with the
   * latest full state. Resolves to true on success, false on failure (state is re-read from the server).
   */
  function saveAssembly(patch: HarnessAssemblyUpdate): Promise<boolean> {
    const current = assemblyRef.current;
    if (current && !patch.base) {
      const optimistic = { ...current, saved: applyLocally(current.saved, patch) };
      assemblyRef.current = optimistic;
      setAssembly(optimistic);
    }
    const run = async (): Promise<boolean> => {
      setBusy(true);
      try {
        const next = await harnessApi.setConfig(TENANT_ID, patch);
        assemblyRef.current = next;
        setAssembly(next);
        setStatus((s) => (s ? { ...s, config_pending: next.pending } : s));
        return true;
      } catch (error) {
        notify.error(error instanceof Error ? error.message : '保存失败');
        try {
          const fresh = await harnessApi.config(TENANT_ID);
          assemblyRef.current = fresh;
          setAssembly(fresh);
        } catch { /* keep optimistic state; next load() will reconcile */ }
        return false;
      } finally {
        setBusy(false);
      }
    };
    const next = saveChain.current.then(run, run);
    saveChain.current = next.then(() => undefined, () => undefined);
    return next;
  }

  function toggleModule(moduleId: string, enabled: boolean) {
    const saved = assemblyRef.current?.saved;
    if (!saved) return;
    const set = new Set(saved.disabled_modules);
    if (enabled) set.delete(moduleId); else set.add(moduleId);
    void saveAssembly({ disabled_modules: [...set] });
  }

  async function placeModule(moduleId: string, subId: string | null) {
    try {
      const r = await harnessApi.setPlacement(TENANT_ID, moduleId, subId);
      setTree(r.tree);
      const current = assemblyRef.current;
      if (current) {
        const pl = { ...current.saved.placements };
        if (subId) pl[moduleId] = subId; else delete pl[moduleId];
        const next = { ...current, saved: { ...current.saved, placements: pl } };
        assemblyRef.current = next;
        setAssembly(next);
      }
      notify.success(subId ? '已归入，立即生效' : '已恢复默认位置');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '归类失败');
    }
  }

  async function inspectModule(spec: string) {
    try {
      return await harnessApi.inspectModule(TENANT_ID, spec);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '预检失败');
      return null;
    }
  }

  async function addExtraModule(spec: string): Promise<boolean> {
    const saved = assemblyRef.current?.saved;
    if (!saved || saved.extra_modules.includes(spec)) return true;
    return saveAssembly({ extra_modules: [...saved.extra_modules, spec] });
  }

  async function removeExtraModule(spec: string): Promise<boolean> {
    const saved = assemblyRef.current?.saved;
    if (!saved) return false;
    return saveAssembly({ extra_modules: saved.extra_modules.filter((x) => x !== spec) });
  }

  async function saveBase(patch: HarnessBaseConnectionUpdate): Promise<boolean> {
    const ok = await saveAssembly({ base: patch });
    if (ok) notify.success('权限中心连接已保存，请测试连接');
    return ok;
  }

  async function testBase(patch?: HarnessBaseConnectionUpdate) {
    try {
      const r = await harnessApi.baseTest(TENANT_ID, patch);
      if (r.saved) {
        const fresh = await harnessApi.config(TENANT_ID);
        assemblyRef.current = fresh;
        setAssembly(fresh);
      }
      return r;
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '测试失败');
      return null;
    }
  }

  async function restart() {
    setRestartOpen(false);
    setRestarting(true);
    setBusy(true);
    try {
      await saveChain.current;
      const res = await harnessApi.restart(TENANT_ID);
      if (res.runtime_error) notify.warning(`已重启，但 Harness v3 引擎未能启动：${res.runtime_error.message}`);
      else notify.success('运行时已重启，新的装配已生效');
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '重启失败');
      await load();
    } finally {
      setBusy(false);
      setRestarting(false);
    }
  }

  useEffect(() => { void load(); void loadLookups(); }, [load, loadLookups]);
  useEffect(() => { if (tab === 'staff' && agentId) void loadSnapshot(agentId); }, [tab, agentId]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (tab === 'log') void loadLogTab(); }, [tab]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (tab === 'log' && sessionId) void loadLog(sessionId); }, [tab, sessionId, loadLog]);

  const modelName = (id: string) => models.find((m) => m.id === id)?.name ?? id;
  const teamName = (id: string) => teams.find((t) => t.id === id)?.name ?? id;
  const channelName = (bindingId: string) => {
    const b = channels.find((c) => c.id === bindingId);
    return b ? (b.name || b.channel) : bindingId;
  };
  const agentName = (id: string) => agents.find((a) => a.id === id)?.name ?? id;
  const grantName = (type: string, id: string) => snapshot?.grants.find((g) => g.resource_type === type && g.resource_id === id)?.name ?? id;

  const pendingLines = useMemo(() => describePending(assembly), [assembly]);
  const applied = assembly?.applied ?? null;
  const engineRunning: 'ok' | 'warn' | 'error' = status?.harness_v3_enabled ? (status.runtime_ok ? 'ok' : 'error') : 'ok';
  const businessSelected = assembly?.saved.security_profile === 'BUSINESS_BASE';
  const restartingAny = restarting || Boolean(status?.restarting) || Boolean(assembly?.restarting);
  const baseReady = !businessSelected || assembly?.saved.base?.last_test_ok === true;
  const modulesTotal = status?.modules_total ?? 0;
  const modulesEnabled = status?.modules_enabled ?? 0;
  const bigCount = tree.length;
  const subCount = tree.reduce((n, b) => n + b.subs.length, 0);

  return (
    <div className="app-shell flex flex-col">
      <div className="content flex-1">
        <div className="mx-auto flex min-h-full max-w-[1280px] flex-col box-border px-[24px] pt-[10px] pb-[43px] max-[900px]:px-[8px]">
          <AppHeader
            className="items-center"
            onLogout={onLogout}
            userName={currentUser?.username}
            left={(
              <div className="flex min-w-0 items-center gap-[14px]">
                <button type="button" onClick={() => navigate(EnterpriseRoute.Gallery)} className="inline-flex h-[30px] shrink-0 items-center gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[10px] text-[12px] text-[#757f9c] hover:border-[#cbd3e6] hover:text-[#18181a]">
                  <IconBack className="size-[12px] rotate-90" />
                  返回工作台
                </button>
                <div className="min-w-0">
                  <div className="text-[20px] font-semibold leading-[28px] text-[#18181a]">管理后台</div>
                  <div className="truncate text-[13px] leading-[20px] text-[#757f9c]">查看执行引擎状态，装配功能模块，检查员工配置，回看执行日志</div>
                </div>
              </div>
            )}
          />

          <div className="mt-[20px] flex flex-wrap items-end justify-between gap-[12px]">
            <Tabs value={tab} onValueChange={(v) => setTab(v as TabKey)}>
              <TabsList aria-label="管理后台分区" className="h-[35px]! gap-2 rounded-none bg-transparent p-0">
                {TABS.map((t) => (
                  <TabsTrigger key={t.key} value={t.key} className="h-[35px] min-w-[112px] gap-[7px] rounded-t-lg rounded-b-none border-0 px-[16px] text-[14px] font-bold text-[#8b94aa] hover:text-[#202226] data-[state=active]:bg-white data-[state=active]:text-[#202226] data-[state=active]:shadow-[0_-12px_28px_rgba(21,26,38,0.04)]">
                    {t.label}
                    {t.key === 'modules' && assembly?.pending && <span className="size-[6px] rounded-full bg-[#e0a03b]" aria-label="有未生效的更改" />}
                  </TabsTrigger>
                ))}
              </TabsList>
            </Tabs>
            <div className="mb-[6px] flex items-center gap-[12px]">
              <label className="inline-flex items-center gap-[8px] text-[12px] text-[#757f9c]">
                <Switch checked={tech} onCheckedChange={setTech} aria-label="显示技术信息" />
                显示技术信息
              </label>
              <UIButton variant="outline" onClick={() => { void load(); if (tab === 'log') void loadLog(); if (tab === 'staff') void loadSnapshot(); }} disabled={loading} className="h-[34px] gap-[4px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[20px] text-[12px] font-normal text-[#757f9c] hover:border-[#cbd3e6] hover:bg-white hover:text-[#18181a]">
                <IconRefresh className={cn('size-[14px]', loading && 'animate-spin')} />
                刷新
              </UIButton>
            </div>
          </div>

          <div className="flex flex-1 flex-col gap-[20px] rounded-[20px_20px_0_0] bg-white p-[18px_18px_24px_18px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
            {assembly?.pending && (
              <div className="flex flex-wrap items-center gap-[12px] rounded-[12px] border-[0.5px] border-[#f3d28b] bg-[#fff8e8] px-[16px] py-[12px]">
                <IconWarning className="size-[16px] shrink-0 text-[#c2740c]" />
                <div className="min-w-0 flex-1 text-[13px] text-[#6f4500]">
                  <span className="font-medium">有已保存但尚未生效的更改：</span>
                  {pendingLines.join('；') || '装配已变化'}。重启运行时后生效；重启期间正在进行的对话会被中断。
                  {!baseReady && <div className="mt-[4px] text-[12px] text-[#b00c0c]">已选择企业版权限：请先在下方「企业权限中心连接」保存并通过测试连接，才能重启。</div>}
                  {assembly.last_restart_error && <div className="mt-[4px] text-[12px] text-[#b00c0c]">{assembly.last_restart_error.startsWith('无法切换') || assembly.last_restart_error.startsWith('装配无法') ? `上次重启被拒绝，运行中的装配未受影响：${assembly.last_restart_error}` : `上次重启失败：${assembly.last_restart_error}`}</div>}
                </div>
                <UIButton onClick={() => setRestartOpen(true)} disabled={busy || restarting || !baseReady} className="h-[32px] rounded-[8px] bg-[#18181a] px-[14px] text-[12px] text-white hover:bg-[#333] disabled:opacity-50">{restarting ? '正在重启…' : '重启运行时'}</UIButton>
              </div>
            )}

            {!assembly?.pending && status?.last_restart_error && (
              <div className="flex items-start gap-[10px] rounded-[12px] border-[0.5px] border-[#f3c4c4] bg-[#fff6f6] px-[16px] py-[10px] text-[12px] text-[#b00c0c]">
                <IconError className="mt-[2px] size-[14px] shrink-0" />
                <span className="min-w-0 flex-1">{status.last_restart_error.startsWith('启动时') ? status.last_restart_error : `上次重启未成功（运行中的装配未受影响）：${status.last_restart_error}`}</span>
              </div>
            )}
            {restartingAny && (
              <div className="flex items-center gap-[10px] rounded-[12px] border-[0.5px] border-[#e3e7f1] bg-[#fafbfd] px-[16px] py-[10px] text-[12px] text-[#464c5e]">
                <IconRefresh className="size-[14px] animate-spin text-[#757f9c]" />正在按已保存的装配重启运行时，请稍候…
              </div>
            )}

            <div className="flex flex-wrap items-stretch gap-[20px]" aria-label="运行概览">
              <StatCard label="执行引擎" value={applied ? engineLabel(applied.engine) : status ? engineLabel(status.default_engine) : '-'} valueClassName="text-[18px] leading-[26px]" />
              <StatCard label="引擎状态" value={status ? (restartingAny ? '重启中' : status.harness_v3_enabled ? (status.runtime_ok ? '运行中' : '异常') : '运行中') : '-'} tone={status ? (restartingAny ? 'default' : status.runtime_ok ? 'green' : 'red') : 'default'} valueClassName="text-[18px] leading-[26px]" />
              <StatCard label="权限模式" value={profileLabel(status?.security_profile).short} tone={status?.security_profile === 'BUSINESS_BASE' ? (status.base_last_test_ok === false ? 'red' : 'green') : 'default'} valueClassName="text-[18px] leading-[26px]" />
              <StatCard label="功能模块" value={`${modulesEnabled} / ${modulesTotal} 已启用`} tone={modulesEnabled > 0 ? 'green' : 'default'} valueClassName="text-[18px] leading-[26px]" />
            </div>

            {tab === 'overview' && (
              <Panel className="px-[18px]">
                {status ? (
                  <>
                    <KV label="执行引擎">
                      <span className="inline-flex flex-wrap items-center gap-[8px]">
                        <StatusDot state="ok" />
                        {engineLabel(applied?.engine ?? status.default_engine, true)}
                        {status.engine_version && status.harness_v3_enabled && <Hint>版本 {status.engine_version}</Hint>}
                      </span>
                      <div className="text-[12px] text-[#9aa0ad]">员工回答问题时，由它规划步骤、调用能力并生成回复。</div>
                    </KV>
                    <KV label="引擎状态">
                      <span className="inline-flex items-center gap-[8px]">
                        <StatusDot state={engineRunning} />
                        {status.harness_v3_enabled
                          ? (status.runtime_ok ? `运行正常，当前有 ${status.live_activations} 个对话正在使用` : `异常：${status.runtime_error?.message ?? '未知错误'}`)
                          : 'Harness v2 引擎在主进程内运行，不需要单独启动'}
                      </span>
                      {status.harness_v3_enabled && (
                        <div className="text-[12px] text-[#9aa0ad]">{status.fallback_to_legacy ? 'Harness v3 引擎异常时会自动改用 Harness v2，对话不会中断。' : '未开启自动切换：Harness v3 引擎异常时对话会失败。'}</div>
                      )}
                      {(status.fallback_count ?? 0) > 0 && (
                        <div className="mt-[4px] text-[12px] text-[#b26a00]">
                          自启动以来有 {status.fallback_count} 轮对话改用了 Harness v2
                          {status.last_fallback ? `，最近一次：${status.last_fallback.reason === 'image_attachments' ? '带图片附件' : status.last_fallback.reason === 'engine_unavailable' ? 'Harness v3 引擎不可用' : status.last_fallback.reason}${status.last_fallback.detail ? `（${status.last_fallback.detail}）` : ''}` : ''}
                        </div>
                      )}
                    </KV>
                    <KV label="权限模式">
                      {profileLabel(status.security_profile).long}
                      {status.security_profile === 'BUSINESS_BASE' && (
                        <Hint>{status.base_last_test_ok === true ? '权限中心连接测试通过' : status.base_last_test_ok === false ? '权限中心连接测试失败' : '权限中心未测试'}{assembly?.saved.base?.authz_url ? ` · ${assembly.saved.base.authz_url}` : ''}</Hint>
                      )}
                      <div className="text-[12px] text-[#9aa0ad]">{profileLabel(status.security_profile).hint}</div>
                    </KV>
                    <KV label="使用 Harness v3 的员工">
                      {!status.harness_v3_enabled
                        ? <span>当前默认引擎是 Harness v2，所有员工都使用它。<Hint>要启用 Harness v3，请在「功能模块」中选择它并重启运行时</Hint></span>
                        : status.staff_allowlist.length
                          ? <span>{status.staff_allowlist.map(agentName).join('、')}<Hint>其余员工使用 Harness v2</Hint></span>
                          : <span>全部员工<Hint>可在「员工配置」中为单个员工单独指定引擎</Hint></span>}
                    </KV>
                    <KV label="功能模块">
                      {modulesEnabled} / {modulesTotal} 已启用 · 分为 {bigCount} 大类 {subCount} 小类
                      <button type="button" onClick={() => setTab('modules')} className="ml-[8px] text-[12px] text-[#2f6fdb] hover:underline">去装配</button>
                    </KV>
                    <KV label="运行时间">
                      {status.started_at ? `自 ${formatClientDateTime(status.started_at)} 起` : '—'}
                      {status.restart_count ? <Hint>本进程内已重启 {status.restart_count} 次</Hint> : null}
                    </KV>
                    {tech && (
                      <>
                        {status.mcp_url && <KV label="能力回调地址" mono>{status.mcp_url}</KV>}
                        <KV label="引擎安装路径" mono>{status.harness_v3_root || '—'}</KV>
                        <KV label="引擎数据目录" mono>{status.harness_v3_home || '—'}</KV>
                        <KV label="装配配置文件" mono>{assembly?.config_path ?? '—'}</KV>
                        <KV label="注册表代次" mono>{status.registry_generation}</KV>
                      </>
                    )}
                  </>
                ) : (
                  <div className="py-[28px] text-center text-[13px] text-[#858b9c]">加载中…</div>
                )}
              </Panel>
            )}

            {tab === 'modules' && (
              <div className="flex flex-col gap-[16px]">
                <div className="text-[12px] text-[#757f9c]">打开或关闭开关、选择引擎和权限模式，都会先保存下来；点「重启运行时」后才真正生效。归类（模块放在哪个类目下）只影响展示，改完立即生效。</div>
                <ModuleTree
                  tree={tree}
                  loading={loading}
                  tech={tech}
                  assembly={assembly?.saved ?? null}
                  options={options}
                  busy={busy}
                  onToggleModule={toggleModule}
                  onChooseEngine={(engine) => void saveAssembly({ engine })}
                  onChooseProfile={(security_profile) => {
                    if (security_profile === 'BUSINESS_BASE' && !assembly?.saved.base?.configured) {
                      notify.warning('请先在下方「企业权限中心连接」填写地址和决策令牌并保存，再选择企业版');
                      return;
                    }
                    void saveAssembly({ security_profile });
                  }}
                  onPlaceModule={(id, sub) => void placeModule(id, sub)}
                />
                <BaseConnectionPanel base={assembly?.saved.base ?? null} selected={Boolean(businessSelected)} busy={busy} onSave={saveBase} onTest={testBase} />
                <ExternalModulesPanel assembly={assembly} options={options} tech={tech} busy={busy} onInspect={inspectModule} onAdd={addExtraModule} onRemove={removeExtraModule} onPlace={placeModule} />
              </div>
            )}

            {tab === 'staff' && (
              <div className="flex flex-col gap-[16px]">
                <div className="flex flex-wrap items-center gap-[10px]">
                  <Select value={agentId || undefined} onValueChange={setAgentId}>
                    <SelectTrigger className="h-[34px] w-[260px] rounded-[10px] border-[0.5px] border-[#e3e7f1] text-[12px]"><SelectValue placeholder="选择数字员工" /></SelectTrigger>
                    <SelectContent>{agents.map((a) => <SelectItem key={a.id} value={a.id}>{a.name}{a.is_overall ? '（整体）' : ''}</SelectItem>)}</SelectContent>
                  </Select>
                  {staffEngine && (
                    <Select value={staffEngine.engine} onValueChange={(v) => void setEngine(v as HarnessEngineChoice)}>
                      <SelectTrigger className="h-[34px] w-[280px] rounded-[10px] border-[0.5px] border-[#e3e7f1] text-[12px]"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="default">跟随系统默认（当前 {engineLabel(staffEngine.effective_engine)}）</SelectItem>
                        <SelectItem value="harness_v3">这位员工使用 Harness v3 引擎</SelectItem>
                        <SelectItem value="harness_v2">这位员工使用 Harness v2 引擎</SelectItem>
                      </SelectContent>
                    </Select>
                  )}
                  <span className="text-[12px] text-[#9aa0ad]">引擎选择保存后，下一轮对话生效</span>
                </div>
                <div className="text-[12px] text-[#757f9c]">下面是这位员工下一轮对话实际会用到的配置：人设、模型、可用能力和流程。绑定关系变化后会自动更新。</div>

                {snapshot ? (
                  <div className="grid gap-[16px] lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
                    <Panel className="px-[18px]">
                      <KV label="人设">{snapshot.persona_preview ? <span className="line-clamp-4 whitespace-pre-line">{snapshot.persona_preview}</span> : <span className="text-[#9aa0ad]">未设置</span>}</KV>
                      <KV label="使用模型">
                        {Object.keys(snapshot.model_route).length
                          ? <span className="flex flex-wrap gap-x-[14px] gap-y-[4px]">{Object.entries(snapshot.model_route).map(([role, id]) => <span key={role}><span className="text-[#9aa0ad]">{modelRoleLabel(role)}</span> {modelName(id)}{tech && <span className="ml-[4px] font-mono text-[11px] text-[#c0c6d4]">{id}</span>}</span>)}</span>
                          : <span className="text-[#d20b0b]">未配置默认模型，员工无法回答</span>}
                      </KV>
                      <KV label="可直接使用的操作">
                        <span className="flex flex-wrap gap-[6px]">{snapshot.proxy_tools.map((t) => <span key={t} className="rounded-[6px] bg-[#f0eef9] px-[6px] py-[2px] text-[11px] text-[#6a4fc7]" title={tech ? `mcp__staffdeck__${t}` : undefined}>{proxyToolLabel(t)}</span>)}</span>
                      </KV>
                      <KV label="接入渠道">{snapshot.channels.length ? snapshot.channels.map(channelName).join('、') : <span className="text-[#9aa0ad]">仅网页对话</span>}</KV>
                      <KV label="所属团队">{snapshot.team_id ? teamName(snapshot.team_id) : <span className="text-[#9aa0ad]">无</span>}</KV>
                      {tech && (
                        <>
                          <KV label="配置版本" mono><span title={snapshot.snapshot_id}>{snapshot.snapshot_id.slice(0, 16)}…{snapshot.snapshot_id.slice(-8)}</span></KV>
                          <KV label="对话介入顺序">
                            <div className="flex flex-col gap-[4px]">
                              {Object.entries(snapshot.hooks).map(([point, handlers]) => (
                                <div key={point} className="flex flex-wrap items-center gap-[6px] text-[12px]"><span className="w-[92px] shrink-0 text-[#9aa0ad]">{hookPointLabel(point)}</span>{handlers.map((h, i) => <span key={h} className="inline-flex items-center gap-[6px]">{i > 0 && <span className="text-[#c0c6d4]">→</span>}<span className="rounded-[6px] bg-[#f6f6f6] px-[6px] py-[2px] text-[11px]">{hookHandlerLabel(h)}</span></span>)}</div>
                              ))}
                            </div>
                          </KV>
                        </>
                      )}
                    </Panel>
                    <div className="flex flex-col gap-[14px]">
                      <Panel className="p-[14px]">
                        <div className="mb-[10px] flex items-center justify-between"><span className="text-[13px] text-[#18181a]">已授权的能力</span><span className="text-[12px] text-[#9aa0ad]">{snapshot.grants.length} 项</span></div>
                        <div className="flex max-h-[240px] flex-col gap-[6px] overflow-auto pr-[4px]">
                          {snapshot.grants.map((g, i) => (
                            <div key={i} className="flex items-center gap-[8px] rounded-[8px] bg-[#fafbfd] px-[10px] py-[6px] text-[12px]">
                              <span className={cn('shrink-0 rounded-[6px] px-[6px] py-[2px] text-[11px] leading-none', g.scope === 'sop_specific' ? 'bg-[#fff4e5] text-[#c2740c]' : 'bg-[#e8f1ff] text-[#2f6fdb]')} title={g.scope === 'sop_specific' ? '只在对应流程里可用' : '随时可用'}>{g.scope === 'sop_specific' ? '仅流程内' : '随时可用'}</span>
                              <span className="min-w-0 flex-1 truncate text-[#18181a]">{g.name}</span>
                              <span className="shrink-0 text-[11px] text-[#9aa0ad]">{resourceTypeLabel(g.resource_type)} · {operationLabel(g.operation)}</span>
                            </div>
                          ))}
                          {snapshot.grants.length === 0 && <div className="py-[16px] text-center text-[12px] text-[#9aa0ad]">这位员工还没有绑定任何能力</div>}
                        </div>
                      </Panel>
                      <Panel className="p-[14px]">
                        <div className="mb-[10px] flex items-center justify-between"><span className="text-[13px] text-[#18181a]">已绑定的流程</span><span className="text-[12px] text-[#9aa0ad]">{snapshot.sops.length} 个</span></div>
                        <div className="flex max-h-[240px] flex-col gap-[8px] overflow-auto pr-[4px]">
                          {snapshot.sops.map((s) => (
                            <div key={s.skill_id} className="rounded-[8px] bg-[#fafbfd] px-[10px] py-[8px]">
                              <div className="flex items-center justify-between text-[12px]"><span className="truncate text-[#18181a]">{s.name}</span><span className="shrink-0 text-[11px] text-[#9aa0ad]">版本 {s.version}</span></div>
                              {s.resolved_slots.length > 0 && <ul className="mt-[4px] flex flex-col gap-[2px] text-[11px] text-[#757f9c]">{s.resolved_slots.map((slot, i) => <li key={i} className="truncate">需要{resourceTypeLabel(slot.resource_type)}：{grantName(slot.resource_type, slot.resource_id)}{slot.required ? '（必需）' : ''}{tech && <span className="ml-[6px] font-mono text-[10px] text-[#c0c6d4]">{slot.slot}</span>}</li>)}</ul>}
                              {s.sub_sop_ids.length > 0 && <div className="mt-[4px] text-[11px] text-[#9aa0ad]">包含子流程 {s.sub_sop_ids.length} 个</div>}
                            </div>
                          ))}
                          {snapshot.sops.length === 0 && <div className="py-[16px] text-center text-[12px] text-[#9aa0ad]">没有绑定流程</div>}
                        </div>
                      </Panel>
                    </div>
                  </div>
                ) : (
                  <div className="rounded-[14px] border-[0.5px] border-dashed border-[#e3e7f1] py-[40px] text-center text-[13px] text-[#858b9c]">{loading ? '正在读取…' : '选择一位数字员工查看配置'}</div>
                )}
              </div>
            )}

            {tab === 'log' && (
              <div className="flex flex-col gap-[16px]">
                {ledgerUnknown.length > 0 && (
                  <div className="rounded-[14px] border-[0.5px] border-[#f5d9a8] bg-[#fffaf0] p-[14px]">
                    <button type="button" onClick={() => setUnknownOpen((v) => !v)} className="flex w-full items-center gap-[8px] text-left text-[13px] text-[#18181a]">
                      <IconWarning className="size-[14px] shrink-0 text-[#c2740c]" />
                      <span className="flex-1">有 {ledgerUnknown.length} 次调用没有收到结果，需要人工确认</span>
                      <span className="text-[12px] text-[#2f6fdb]">{unknownOpen ? '收起' : '查看并处理'}</span>
                    </button>
                    {unknownOpen && <div className="mt-[8px] mb-[10px] text-[12px] text-[#757f9c]">请先到对应的外部系统核对是否已经执行，再在这里确认。确认之前，相同的调用不会自动重复执行。</div>}
                    <div className={cn('flex flex-col gap-[8px]', !unknownOpen && 'hidden')}>
                      {ledgerUnknown.map((r) => (
                        <div key={r.id} className="flex flex-wrap items-center gap-[10px] rounded-[10px] bg-white px-[12px] py-[8px] text-[12px]">
                          <span className="min-w-0 flex-1 truncate">
                            <span className="text-[#18181a]">{toolLabel(r.tool_name)}</span>
                            <span className="ml-[8px] text-[11px] text-[#9aa0ad]">{formatClientDateTime(r.started_at)} · {engineLabel(r.engine ?? 'harness_v2')}</span>
                            {tech && <span className="ml-[8px] font-mono text-[11px] text-[#c0c6d4]">{r.session_id}</span>}
                          </span>
                          <button type="button" onClick={() => setSessionId(r.session_id)} className="text-[12px] text-[#2f6fdb] hover:underline">看日志</button>
                          <UIButton size="sm" variant="outline" className="h-[28px] rounded-[8px] border-[0.5px] border-[#e3e7f1] text-[12px]" onClick={() => void reconcile(r.id, 'completed')}>已执行，记为成功</UIButton>
                          <UIButton size="sm" variant="outline" className="h-[28px] rounded-[8px] border-[0.5px] border-[#e3e7f1] text-[12px]" onClick={() => void reconcile(r.id, 'failed')}>未执行，允许重试</UIButton>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                <SessionLog sessions={sessions} sessionId={sessionId} onSelectSession={setSessionId} entries={logEntries} loading={logLoading} tech={tech} onRefresh={() => void loadLog()} />
              </div>
            )}
          </div>
        </div>
      </div>

      <AlertDialog open={restartOpen} onOpenChange={setRestartOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>重启运行时？</AlertDialogTitle>
            <AlertDialogDescription>
              将按已保存的装配重新加载功能模块、权限模式和执行引擎：{pendingLines.join('；') || '无变化'}。
              重启前会先做预检（模块能否装配、权限中心是否可用），预检不通过则不会动到运行中的系统；
              预检通过后正在进行的对话会被中断；如果新的装配仍然无法启动，会自动恢复原有装配。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={() => void restart()}>确认重启</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
