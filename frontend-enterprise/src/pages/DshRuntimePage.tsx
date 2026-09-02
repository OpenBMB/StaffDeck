import { useEffect, useState, type ReactNode } from 'react';
import { ReloadOutlined } from '../icons';
import { Badge, Button as UIButton, Card, CardContent, CardDescription, CardHeader, CardTitle, Input, Select, SelectContent, SelectItem, SelectTrigger, SelectValue, Switch, Tabs, TabsContent, TabsList, TabsTrigger, notify } from '@/components/ui';
import { api, TENANT_ID } from '../api/client';
import { dshApi, type DshEngineChoice, type DshLedgerRow, type DshModule, type DshSnapshot, type DshStaffEngine, type DshStatus } from '../api/dsh';
import type { AgentProfileRead } from '../types';
import type { EnterpriseAuthUser } from '../auth';
import { CircuitBoard, Plug, Boxes, ShieldCheck, ClipboardList, RefreshCw, Cpu } from 'lucide-react';

const KIND_LABEL: Record<string, string> = { A: 'A · 插件', C: 'C · 内容包', T: 'T · 可信服务', K: 'K · 内核' };

export default function DshRuntimePage({ currentUser }: { currentUser: EnterpriseAuthUser }) {
  const [status, setStatus] = useState<DshStatus | null>(null);
  const [modules, setModules] = useState<DshModule[]>([]);
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [agentId, setAgentId] = useState<string>('');
  const [snapshot, setSnapshot] = useState<DshSnapshot | null>(null);
  const [staffEngine, setStaffEngine] = useState<DshStaffEngine | null>(null);
  const [ledgerUnknown, setLedgerUnknown] = useState<DshLedgerRow[]>([]);
  const [ledgerRecent, setLedgerRecent] = useState<DshLedgerRow[]>([]);
  const [sessionId, setSessionId] = useState('');
  const [loading, setLoading] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const [st, mods, agts] = await Promise.all([dshApi.status(TENANT_ID), dshApi.modules(TENANT_ID), api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`)]);
      setStatus(st);
      setModules(mods);
      setAgents(agts);
      if (!agentId && agts[0]) setAgentId(agts[0].id);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载失败');
    } finally {
      setLoading(false);
    }
  }

  async function loadSnapshot() {
    if (!agentId) return notify.info('请先选择一个数字员工');
    setLoading(true);
    try {
      const snap = await dshApi.snapshot(TENANT_ID, agentId);
      setSnapshot(snap);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '快照编译失败');
    } finally {
      setLoading(false);
    }
  }

  async function loadStaffEngine() {
    if (!agentId) return;
    try {
      setStaffEngine(await dshApi.staffEngine(TENANT_ID, agentId));
    } catch {
      setStaffEngine(null);
    }
  }

  async function setEngine(engine: DshEngineChoice) {
    if (!agentId) return;
    try {
      const row = await dshApi.setStaffEngine(TENANT_ID, agentId, engine);
      setStaffEngine(row);
      notify.success(`已切换：${row.effective_engine === 'dsh' ? 'DSH 引擎' : '原 Harness 引擎'}（下一 Turn 生效）`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '切换失败');
    }
  }

  async function loadLedger() {
    try {
      const [unknown, recent] = await Promise.all([dshApi.ledgerUnknown(TENANT_ID), dshApi.ledgerRecent(TENANT_ID, sessionId || undefined)]);
      setLedgerUnknown(unknown);
      setLedgerRecent(recent);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载台账失败');
    }
  }

  async function reconcile(id: string, outcome: 'completed' | 'failed') {
    try {
      await dshApi.reconcile(TENANT_ID, id, outcome);
      notify.success('已确认执行结果');
      await loadLedger();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '结算失败');
    }
  }

  useEffect(() => { void load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { void loadStaffEngine(); }, [agentId]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { void loadLedger(); }, [sessionId]); // eslint-disable-line react-hooks/exhaustive-deps

  const engineLabel = (status?.default_engine ?? 'legacy') === 'dsh' ? 'DSH' : 'Harness v2';

  return (
    <>
      <div className="page-title">
        <div>
          <h3>运行时与插件</h3>
          <p className="text-[12px] text-muted-foreground">DSH 引擎、模块装配与调用台账（本页面仅影响查看与单 Staff 引擎选择，不影响未选中的员工）。</p>
        </div>
        <UIButton disabled={loading} onClick={() => void load()}><ReloadOutlined />刷新</UIButton>
      </div>

      <Tabs defaultValue="overview">
        <TabsList>
          <TabsTrigger value="overview"><Cpu className="size-[14px]" />总览</TabsTrigger>
          <TabsTrigger value="modules"><Plug className="size-[14px]" />模块装配</TabsTrigger>
          <TabsTrigger value="snapshot"><Boxes className="size-[14px]" />组成快照</TabsTrigger>
          <TabsTrigger value="ledger"><ClipboardList className="size-[14px]" />调用台账</TabsTrigger>
        </TabsList>

        {/* 总览 */}
        <TabsContent value="overview">
          <Card className="editor-card settings-card">
            <CardHeader><CardTitle><CircuitBoard className="size-[15px]" />运行时状态</CardTitle><CardDescription>引擎由部署开关决定；未在允许名单或显式选为 DSH 的员工仍走原引擎。</CardDescription></CardHeader>
            <CardContent className="flex flex-col gap-[12px]">
              {status ? (
                <>
                  <Row label="默认引擎" value={engineLabel} />
                  <Row label="安全配置" value={`${status.security_profile}${'（部署级二选一）'}`} />
                  <Row label="DSH 子进程" value={status.dsh_enabled ? (status.runtime_ok ? `运行中 · ${status.live_activations} 个激活` : `不可用：${status.runtime_error?.message ?? '未知'}`) : '未启用（dsh_enabled=false）'} />
                  {status.mcp_url && <Row label="能力回调 MCP" value={status.mcp_url} />}
                  <Row label="模块" value={`${status.modules_enabled}/${status.modules_total} 个启用（Generation ${status.registry_generation}）`} />
                  <Row label="灰度名单" value={status.staff_allowlist.length ? status.staff_allowlist.join(', ') : '未设置（全部遵循默认引擎）'} />
                  <div className="rounded-md bg-muted/40 p-[10px] text-[12px] leading-[18px] text-muted-foreground">
                    说明：每个数字员工可在「组成快照」页签单独切换引擎（存到该员工元数据，下一 Turn 生效）；未指定时按上面的默认引擎与灰度名单决定。
                  </div>
                </>
              ) : (
                <div className="text-[13px] text-muted-foreground">加载中…</div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* 模块装配 */}
        <TabsContent value="modules">
          <Card className="editor-card settings-card">
            <CardHeader><CardTitle><Plug className="size-[15px]" />模块注册表（sealed，部署级）</CardTitle><CardDescription>每个可插拔物在此登记：A/C/T/K 与插槽、能力、Hook、策略动作、是否 PEP 守卫。禁用某模块需改 DSH_DISABLED_MODULES 并重启。</CardDescription></CardHeader>
            <CardContent>
              <div className="rounded-md border">
                <table className="w-full text-left text-[12px]">
                  <thead className="bg-muted/40 text-muted-foreground">
                    <tr>
                      <th className="px-[10px] py-[8px]">模块</th>
                      <th className="px-[10px] py-[8px]">类型</th>
                      <th className="px-[10px] py-[8px]">插槽</th>
                      <th className="px-[10px] py-[8px]">提供能力</th>
                      <th className="px-[10px] py-[8px]">PEP</th>
                      <th className="px-[10px] py-[8px]">状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {modules.map((m) => (
                      <tr key={m.module_id} className="border-t">
                        <td className="px-[10px] py-[6px]"><div className="font-medium">{m.name}</div><div className="text-muted-foreground">{m.module_id} · v{m.version} · {m.contract_version}</div></td>
                        <td className="px-[10px] py-[6px]">{KIND_LABEL[m.kind] ?? m.kind}</td>
                        <td className="px-[10px] py-[6px]"><code className="rounded bg-muted/50 px-[4px]">{m.slot}</code>{m.hooks.length > 0 && <div className="text-muted-foreground">Hook: {m.hooks.join(' · ')}</div>}</td>
                        <td className="px-[10px] py-[6px]">{m.provides.map((op) => <div key={op} className="text-[11px]">{op}</div>) || <span className="text-muted-foreground">—</span>}</td>
                        <td className="px-[10px] py-[6px]">{m.guarded ? <Badge variant="secondary">PEP</Badge> : <span className="text-muted-foreground">—</span>}</td>
                        <td className="px-[10px] py-[6px]">{m.enabled ? <Badge variant="secondary">启用</Badge> : <Badge variant="outline">禁用</Badge>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        {/* 组成快照 */}
        <TabsContent value="snapshot">
          <Card className="editor-card settings-card">
            <CardHeader><CardTitle><Boxes className="size-[15px]" />数字员工组成快照</CardTitle><CardDescription>选择员工后编译其冻结的 Staff 组成：Persona、模型路由、能力授权、SOP 逻辑槽、Hook 计划与 DSH 侧暴露的代理工具。</CardDescription></CardHeader>
            <CardContent className="flex flex-col gap-[14px]">
              <div className="flex flex-wrap items-center gap-[12px]">
                <Select value={agentId || undefined} onValueChange={setAgentId}>
                  <SelectTrigger className="w-[240px]"><SelectValue placeholder="选择数字员工" /></SelectTrigger>
                  <SelectContent>{agents.map((a) => <SelectItem key={a.id} value={a.id}>{a.name}</SelectItem>)}</SelectContent>
                </Select>
                <UIButton onClick={() => void loadSnapshot()} disabled={loading}><Boxes className="size-[14px]" />编译快照</UIButton>
                {staffEngine && (
                  <div className="flex items-center gap-[8px]">
                    <span className="text-[13px]">此员工：</span>
                    <Select value={staffEngine.engine} onValueChange={(v) => void setEngine(v as DshEngineChoice)}>
                      <SelectTrigger className="w-[180px]"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="default">跟随默认（当前 {staffEngine.effective_engine === 'dsh' ? 'DSH' : 'Harness'}）</SelectItem>
                        <SelectItem value="dsh">强制 DSH</SelectItem>
                        <SelectItem value="legacy">强制 Harness v2</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                )}
              </div>
              {snapshot && (
                <div className="flex flex-col gap-[12px]">
                  <Row label="快照 ID" value={snapshot.snapshot_id} mono />
                  <Row label="Persona" value={snapshot.persona_preview ? (snapshot.persona_preview.length > 160 ? `${snapshot.persona_preview.slice(0, 160)}…` : snapshot.persona_preview) : '（无）'} />
                  <Row label="模型路由" value={Object.entries(snapshot.model_route).map(([k, v]) => `${k}: ${v}`).join(' · ') || '（无默认模型）'} />
                  <div>
                    <div className="mb-[6px] text-[12px] font-medium">可激活能力（{snapshot.grants.length}）</div>
                    <div className="flex flex-wrap gap-[6px]">
                      {snapshot.grants.map((g, i) => (
                        <Badge key={i} variant={g.scope === 'sop_specific' ? 'secondary' : 'default'}>
                          {g.operation.split('/')[0]} → {g.resource_type}:{g.resource_id}{g.scope === 'sop_specific' ? `（SOP ${g.sop_id ?? ''}）` : ''}
                        </Badge>
                      ))}
                    </div>
                  </div>
                  <div>
                    <div className="mb-[6px] text-[12px] font-medium">DSH 侧暴露的代理工具</div>
                    <div className="flex flex-wrap gap-[6px]">{snapshot.proxy_tools.map((t) => <Badge key={t} variant="outline"><code>{`mcp__staffdeck__${t}`}</code></Badge>)}</div>
                  </div>
                  <div>
                    <div className="mb-[6px] text-[12px] font-medium">SOP 与逻辑槽（{snapshot.sops.length}）</div>
                    <div className="grid gap-[8px]">
                      {snapshot.sops.map((s) => (
                        <div key={s.skill_id} className="rounded-md border p-[10px]">
                          <div className="text-[13px] font-medium">{s.name} <span className="text-muted-foreground">v{s.version}</span></div>
                          {s.resolved_slots.length > 0 && (
                            <ul className="mt-[6px] flex flex-col gap-[4px] text-[12px]">
                              {s.resolved_slots.map((slot, i) => <li key={i} className="text-muted-foreground">{slot.slot} → {slot.resource_type}:{slot.resource_id}（{slot.operation}）{slot.required ? ' · 必选' : ''}</li>)}
                            </ul>
                          )}
                          {s.sub_sop_ids.length > 0 && <div className="mt-[6px] text-[12px] text-muted-foreground">子流程：{s.sub_sop_ids.join(', ')}</div>}
                        </div>
                      ))}
                      {snapshot.sops.length === 0 && <div className="text-[12px] text-muted-foreground">未绑定 SOP。</div>}
                    </div>
                  </div>
                  <Row label="Hook 计划" value={Object.entries(snapshot.hooks).map(([k, v]) => `${k}: ${v.join('→')}`).join(' · ')} />
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* 台账 */}
        <TabsContent value="ledger">
          <Card className="editor-card settings-card">
            <CardHeader><CardTitle><ClipboardList className="size-[15px]" />Invocation Ledger</CardTitle><CardDescription>每次能力调用的幂等回执；「结果未知」需核对外部系统后手动结算，否则同副作用调用会被阻断而非重放。</CardDescription></CardHeader>
            <CardContent className="flex flex-col gap-[14px]">
              <div className="flex items-center gap-[10px]">
                <Input placeholder="按会话 ID 过滤" value={sessionId} onChange={(e) => setSessionId(e.target.value)} className="w-[300px]" />
              </div>
              {ledgerUnknown.length > 0 && (
                <div className="rounded-md border border-amber-300 bg-amber-50 p-[10px]">
                  <div className="mb-[8px] text-[13px] font-medium">待结算（{ledgerUnknown.length}）</div>
                  <div className="flex flex-col gap-[8px]">
                    {ledgerUnknown.map((r) => (
                      <div key={r.id} className="flex items-center justify-between gap-[8px] text-[12px]">
                        <div className="min-w-0 flex-1"><span className="text-muted-foreground">调用</span> {r.tool_name} <span className="text-muted-foreground">· {r.side_effect_key ? '副作用键已占位' : ''}</span></div>
                        <UIButton size="sm" variant="outline" onClick={() => void reconcile(r.id, 'completed')}>已成功</UIButton>
                        <UIButton size="sm" variant="outline" onClick={() => void reconcile(r.id, 'failed')}>未发生</UIButton>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              <div className="rounded-md border">
                <table className="w-full text-left text-[12px]">
                  <thead className="bg-muted/40 text-muted-foreground">
                    <tr><th className="px-[10px] py-[8px]">时间</th><th className="px-[10px] py-[8px]">调用</th><th className="px-[10px] py-[8px]">状态</th><th className="px-[10px] py-[8px]">副作用键</th><th className="px-[10px] py-[8px]">引擎</th></tr>
                  </thead>
                  <tbody>
                    {ledgerRecent.map((r) => (
                      <tr key={r.id} className="border-t">
                        <td className="px-[10px] py-[6px] text-muted-foreground">{new Date(r.started_at).toLocaleString()}</td>
                        <td className="px-[10px] py-[6px]">{r.tool_name}<div className="text-muted-foreground">{r.session_id.slice(0, 12)}</div></td>
                        <td className="px-[10px] py-[6px]"><Badge variant={r.status === 'completed' ? 'secondary' : r.status === 'failed' ? 'destructive' : 'outline'}>{r.status}</Badge></td>
                        <td className="px-[10px] py-[6px]">{r.side_effect_key ? <code className="text-[11px]">{r.side_effect_key.slice(0, 20)}…</code> : '—'}</td>
                        <td className="px-[10px] py-[6px]">{r.engine ?? '—'}</td>
                      </tr>
                    ))}
                    {ledgerRecent.length === 0 && <tr><td colSpan={5} className="px-[10px] py-[12px] text-muted-foreground">暂无记录。</td></tr>}
                  </tbody>
                </table>
              </div>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </>
  );
}

function Row({ label, value, mono }: { label: string; value: string | number | null | undefined; mono?: boolean }) {
  return (
    <div className="flex items-start gap-[12px]">
      <div className="w-[160px] shrink-0 text-[12px] text-muted-foreground">{label}</div>
      <div className={`min-w-0 flex-1 text-[13px] ${mono ? 'font-mono break-all' : ''}`}>{value ?? '—'}</div>
    </div>
  );
}
