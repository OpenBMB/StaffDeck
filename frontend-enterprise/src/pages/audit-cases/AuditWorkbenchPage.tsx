import { useCallback, useEffect, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import type { EnterpriseAuthUser } from '@/auth';
import AppHeader from '@/components/AppHeader';
import type { AuditCaseCoverageRead, AuditCaseDocumentRead, AuditCaseRead } from '@/types';
import { loadAuditCase, loadAuditCaseCoverage, loadAuditCaseDocuments } from './auditCaseApi';
import { createWorkIssue, loadProcessGates, loadWorkbench, loadWorkbenchEvents, setWorkbenchRole, type ProcessGate, type ProjectRole, type WorkbenchEvent, type WorkbenchSnapshot } from './workbenchApi';
import { ProjectDocumentWorkspace } from './components/ProjectDocumentWorkspace';
import { RuleBindingPanel } from './components/RuleBindingPanel';
import { EvidenceReportPanel } from './components/EvidenceReportPanel';
import { DocumentVersionDiff } from './components/DocumentVersionDiff';
import { WorkbenchChecks } from './components/WorkbenchChecks';
import { ITEM_LABELS, ROLE_LABELS, WorkbenchReview } from './components/WorkbenchReview';
import { useWorkbenchDirtyGuard } from './hooks/useWorkbenchDirtyGuard';
import './workbench.css';

type Tab = 'content' | 'versions' | 'rules' | 'checks' | 'reports' | 'new';
const EVENT_LABELS: Record<string, string> = { 'item.created': '建立工作项', 'item.submit': '提交复核', 'item.approve': '内部复核通过', 'item.request_changes': '退回修改', 'item.reopen': '重新打开修改', 'issue.created': '登记问题', 'issue.respond': '提交回应', 'issue.close': '验证并关闭', 'issue.reopen': '重新打开问题', 'member.role_changed': '调整成员角色' };
export default function AuditWorkbenchPage({ currentUser, onLogout }: { currentUser?: EnterpriseAuthUser; onLogout?: () => void }) {
  const { caseId = '' } = useParams(); const [search] = useSearchParams();
  const [snapshot, setSnapshot] = useState<WorkbenchSnapshot | null>(null); const [project, setProject] = useState<AuditCaseRead | null>(null);
  const [documents, setDocuments] = useState<AuditCaseDocumentRead[]>([]); const [coverage, setCoverage] = useState<AuditCaseCoverageRead | null>(null); const [processGates, setProcessGates] = useState<ProcessGate[]>([]);
  const [selectedId, setSelectedId] = useState(search.get('document') || ''); const [process, setProcess] = useState(Number(search.get('process')) || 15);
  const [tab, setTab] = useState<Tab>('content'); const [dirty, setDirty] = useState(false); const [reviewDirty, setReviewDirty] = useState(false); const [reviewKey, setReviewKey] = useState(0); const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [events, setEvents] = useState<WorkbenchEvent[]>([]);
  const confirmLeave = useWorkbenchDirtyGuard(dirty || reviewDirty);
  const reload = useCallback(async () => {
    const [next, files, info] = await Promise.all([loadWorkbench(caseId), loadAuditCaseDocuments(caseId), loadAuditCase(caseId)]);
    setSnapshot(next); setDocuments(files); setProject(info); setSelectedId((current) => files.some((d) => d.id === current) ? current : files[0]?.id || '');
  }, [caseId]);
  useEffect(() => {
    let active = true;
    setSnapshot(null); setProject(null); setDocuments([]); setError(''); setDirty(false);
    void Promise.all([loadWorkbench(caseId), loadAuditCaseDocuments(caseId), loadAuditCase(caseId)]).then(([next, files, info]) => {
      if (active) {
        setSnapshot(next); setDocuments(files); setProject(info);
        const target = next.work_items.find((workItem) => workItem.id === search.get('item'));
        if (target) { setSelectedId(target.document_id); setProcess(target.process_number); }
        else setSelectedId((current) => files.some((d) => d.id === current) ? current : files[0]?.id || '');
      }
    }).catch((e) => { if (active) setError(e instanceof Error ? e.message : '加载工作台失败'); });
    return () => { active = false; };
  }, [caseId]);
  const document = documents.find((d) => d.id === selectedId);
  const item = snapshot?.work_items.find((i) => i.document_id === selectedId && i.process_number === process);
  const processGate = processGates.find((gate) => gate.process_number === process);
  useEffect(() => {
    let active = true;
    if (!selectedId) { setProcessGates([]); return () => { active = false; }; }
    void loadProcessGates(caseId, selectedId).then((gates) => { if (active) setProcessGates(gates); }).catch((e) => { if (active) setError(e instanceof Error ? e.message : '加载流程门槛失败'); });
    return () => { active = false; };
  }, [caseId, selectedId]);
  const locked = snapshot?.work_items.some((i) => i.document_id === selectedId && (i.status === 'submitted' || i.status === 'approved'));
  useEffect(() => {
    let active = true; setEvents([]);
    if (snapshot) void loadWorkbenchEvents(caseId, item?.id).then((values) => { if (active) setEvents(values); }).catch((e) => { if (active) setError(e instanceof Error ? e.message : '加载流转记录失败'); });
    return () => { active = false; };
  }, [caseId, item?.id, snapshot]);
  useEffect(() => {
    if (tab !== 'reports') return;
    let active = true;
    void loadAuditCaseCoverage(caseId).then((next) => { if (active) setCoverage(next); }).catch((e) => { if (active) setError(e instanceof Error ? e.message : '加载覆盖率失败'); });
    return () => { active = false; };
  }, [caseId, tab]);
  function switchContext(action: () => void) { if (!confirmLeave()) return; setDirty(false); setReviewDirty(false); setReviewKey((key) => key + 1); action(); }
  async function run(action: () => Promise<unknown>) {
    if (busy) return;
    setBusy(true); setError('');
    try { await action(); await reload(); } catch (e) { setError(e instanceof Error ? e.message : '工作台操作失败'); } finally { setBusy(false); }
  }
  if (!snapshot || !project) return <div className="wb-page wb-inbox">{error ? <p className="wb-error" role="alert">{error}</p> : <p>正在加载项目工作台…</p>}<Link to="/enterprise/audit-workbench">返回我的待办</Link></div>;
  const canEdit = snapshot.role !== 'viewer' && project.status !== 'archived'; const canReview = snapshot.role === 'reviewer' || snapshot.role === 'project_admin';
  const fileReadOnly = !canEdit || Boolean(locked) || document?.status === 'archived';
  return <div className="wb-page"><AppHeader left={<div><h1>{project.organization_name}</h1><p className="wb-muted">认证项目工作台 · {ROLE_LABELS[snapshot.role]}</p></div>} userName={currentUser?.display_name || currentUser?.username} onLogout={() => { if (confirmLeave()) onLogout?.(); }} />
    <div className="wb-summary"><Link to="/enterprise/audit-workbench">← 我的待办</Link><span>{project.report_type}</span><span className="wb-badge">{snapshot.processes.length} 个流程</span>{snapshot.role === 'project_admin' && currentUser?.role === 'admin' && <Link to={`/enterprise/audit-cases/${encodeURIComponent(caseId)}`}>项目设置</Link>}{(dirty || reviewDirty) && <strong>有未保存修改</strong>}</div>
    {error && <p role="alert" className="wb-error">{error}</p>}
    <main className="wb-shell"><aside className="wb-sidebar"><section className="wb-card wb-documents"><h2>项目文件</h2>{canEdit && <button onClick={() => switchContext(() => setTab('new'))}>＋ 新建工作文档</button>}{documents.map((d) => <button key={d.id} aria-pressed={d.id === selectedId} onClick={() => switchContext(() => { setSelectedId(d.id); if (tab === 'new') setTab('content'); })}>{d.title}<small>{d.zone} · v{d.active_version?.version ?? '?'} · {d.status === 'archived' ? '已冻结' : '工作中'}</small></button>)}{!documents.length && <p className="wb-muted">尚无工作文档</p>}</section>
      <section className="wb-card wb-directory"><h2>36 流程目录</h2>{[1, 2, 3].map((stage) => <details key={stage} open={snapshot.processes.find((p) => p.number === process)?.stage === stage}><summary>阶段 {stage}</summary>{snapshot.processes.filter((p) => p.stage === stage).map((p) => <button key={p.number} aria-pressed={p.number === process} onClick={() => switchContext(() => setProcess(p.number))}>{p.number}. {p.name}<small>{p.number === 26 ? '当前仅内部文件复核' : p.enabled ? '文档协作已开放' : '后续阶段'}</small></button>)}</details>)}</section>
    </aside><section className="wb-content"><section className="wb-card"><h2>{document?.title || '请选择或创建工作文档'}</h2><p className="wb-muted">流程 {process} · {snapshot.processes.find((p) => p.number === process)?.name}</p>{item && <span className="wb-badge">{ITEM_LABELS[item.status]}</span>}{locked && <p className="wb-muted">存在已提交或已通过的工作项，文件已锁定。退回或重新打开相关工作项后可修改。</p>}</section>
      <nav className="wb-tabs" role="tablist" aria-label="工作台内容">{([['content', '文件内容'], ['versions', '版本差异'], ['rules', '绑定规则'], ['checks', '文件检查'], ['reports', '证据与报告']] as const).map(([value, label]) => <button role="tab" aria-selected={tab === value} key={value} onClick={() => switchContext(() => setTab(value))}>{label}</button>)}</nav>
      {process === 26 && <p className="wb-muted">流程 26 当前仅开放内部文件复核；正式认证决定仍为后续阶段。</p>}
      {tab === 'new' && <ProjectDocumentWorkspace key={`new-${reviewKey}`} caseId={caseId} documents={[]} selectedDocumentId="" onSelectedDocumentChange={(id) => { setSelectedId(id); setTab('content'); setDirty(false); }} disabled={!canEdit || busy} onDirtyChange={setDirty} onChanged={reload} />}
      {document && tab === 'content' && <ProjectDocumentWorkspace key={`${document.id}-${reviewKey}`} compact caseId={caseId} documents={documents} selectedDocumentId={selectedId} onSelectedDocumentChange={(id) => switchContext(() => setSelectedId(id))} disabled={fileReadOnly || busy} onChanged={reload} onDirtyChange={setDirty} />}
      {document && tab === 'versions' && <DocumentVersionDiff caseId={caseId} documentId={selectedId} approvedVersionId={item?.approved_version_id} />}
      {document && tab === 'rules' && <RuleBindingPanel key={`${document.id}-${reviewKey}`} caseId={caseId} documents={documents} selectedDocumentId={selectedId} disabled={fileReadOnly || busy || !canReview} onDirtyChange={setDirty} />}
      {document && tab === 'checks' && <WorkbenchChecks key={`${selectedId}-${document.active_version_id}`} caseId={caseId} documentId={selectedId} documents={documents} canRun={canEdit && !busy && document.status !== 'archived'} onRegister={canEdit && item ? async (finding) => { await run(() => createWorkIssue(caseId, { work_item_id: item.id, kind: 'document_check', title: finding.title, detail: `${finding.detail}\n${finding.evidence_excerpt}\n${finding.document_id} / ${finding.document_version_id}`, blocking: finding.severity === 'error', assigned_to_user_id: item.assigned_to_user_id })); } : undefined} />}
      {document && tab === 'reports' && <EvidenceReportPanel caseId={caseId} documents={documents} selectedDocumentId={selectedId} coverage={coverage} disabled={!canEdit || busy || document.status === 'archived'} canPublish={canReview} onWorkDocumentCreated={async (created) => { await reload(); setSelectedId(created.id); setProcess(26); setTab('content'); }} onChanged={async () => { setCoverage(await loadAuditCaseCoverage(caseId)); await reload(); }} />}
    </section><aside className="wb-review"><WorkbenchReview key={reviewKey} caseId={caseId} userId={currentUser?.id || ''} snapshot={snapshot} documentId={selectedId} documents={documents} processNumber={process} gate={processGate} item={item} blocked={busy || dirty || !canEdit || document?.status === 'archived'} run={run} onDirtyChange={setReviewDirty} />
      {snapshot.role === 'project_admin' && <section className="wb-card"><h2>项目成员角色</h2><p className="wb-muted">仅现有项目成员。所有者及待办复核的约束由服务端校验。</p>{snapshot.members.map((member) => <label key={member.user_id}>{member.display_name}<select aria-label={`角色 ${member.display_name}`} value={member.role} disabled={busy || project.status === 'archived'} onChange={(e) => void run(() => setWorkbenchRole(caseId, member.user_id, e.target.value as ProjectRole))}>{Object.entries(ROLE_LABELS).map(([role, label]) => <option key={role} value={role}>{label}</option>)}</select></label>)}</section>}
      <section className="wb-card"><h2>流转记录</h2>{events.length === 0 && <p className="wb-muted">尚无流转记录</p>}{events.map((event) => {
        const detail = (event.detail && typeof event.detail === 'object' ? event.detail : {}) as Record<string, unknown>;
        return <article className="wb-issue" key={event.id}><strong>{EVENT_LABELS[event.event_type] || event.event_type}</strong><p className="wb-muted">{event.created_at} · {snapshot.members.find((m) => m.user_id === event.actor_user_id)?.display_name || event.actor_user_id}</p>{typeof detail.comment === 'string' && detail.comment && <p>{detail.comment}</p>}{typeof detail.status === 'string' && <p>{ITEM_LABELS[detail.status as keyof typeof ITEM_LABELS] || detail.status}</p>}<details><summary>快照详情</summary><pre className="wb-json">{JSON.stringify(event.detail, null, 2)}</pre></details></article>;
      })}</section>
    </aside></main>
  </div>;
}

