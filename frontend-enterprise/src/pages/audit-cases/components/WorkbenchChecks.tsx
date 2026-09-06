import { useCallback, useEffect, useState } from 'react';
import type { AuditCaseDocumentRead } from '@/types';
import { createDocumentCheck, loadDocumentChecks, retryDocumentCheck, type CheckJob } from '../workbenchApi';

export function ReferenceSelector({ documents, selectedId, values, onChange, disabled = false }: { documents: AuditCaseDocumentRead[]; selectedId: string; values: string[]; onChange: (ids: string[]) => void; disabled?: boolean }) {
  return <fieldset className="wb-references" disabled={disabled}><legend>关联文件（显式选择当前保存版本）</legend>
    {documents.filter((d) => d.id !== selectedId && d.active_version_id).map((d) => <label key={d.id}><input type="checkbox" checked={values.includes(d.id)} onChange={(e) => onChange(e.target.checked ? [...values, d.id] : values.filter((id) => id !== d.id))} />{d.title} · v{d.active_version?.version ?? '?'}{d.status === 'archived' ? ' · 已冻结' : ''}</label>)}
    {documents.filter((d) => d.id !== selectedId && d.active_version_id).length === 0 && <p className="wb-muted">暂无其他版本可关联</p>}
  </fieldset>;
}

export function WorkbenchChecks({ caseId, documentId, documents, canRun, onRegister }: { caseId: string; documentId: string; documents: AuditCaseDocumentRead[]; canRun: boolean; onRegister?: (finding: CheckJob['findings'][number]) => Promise<void> }) {
  const [jobs, setJobs] = useState<CheckJob[]>([]); const [references, setReferences] = useState<string[]>([]);
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const reload = useCallback(async () => { const list = await loadDocumentChecks(caseId, documentId); setJobs(list); }, [caseId, documentId]);
  useEffect(() => {
    let active = true;
    setJobs([]); setReferences([]); setError('');
    void loadDocumentChecks(caseId, documentId).then((list) => { if (active) setJobs(list); }).catch((e) => { if (active) setError(e instanceof Error ? e.message : '加载检查失败'); });
    return () => { active = false; };
  }, [caseId, documentId]);
  const pending = jobs.some((j) => j.status === 'queued' || j.status === 'running');
  useEffect(() => {
    if (!pending) return;
    const timer = window.setInterval(() => { void reload().catch((e) => setError(e instanceof Error ? e.message : '刷新检查失败')); }, 3000);
    return () => window.clearInterval(timer);
  }, [pending, reload]);
  async function run(action: () => Promise<unknown>) {
    setBusy(true); setError('');
    try { await action(); await reload(); } catch (e) { setError(e instanceof Error ? e.message : '检查操作失败'); } finally { setBusy(false); }
  }
  const names = new Map(documents.map((d) => [d.id, d.title]));
  return <section className="wb-card"><h2>持久化文件检查</h2><p className="wb-muted">检查已保存版本、绑定规则及明确关联文件中的标注字段。语义或模型规则需要人工复核；检查结果不等同于正式不符合项。</p>
    {error && <p role="alert" className="wb-error">{error}</p>}
    <ReferenceSelector documents={documents} selectedId={documentId} values={references} onChange={setReferences} disabled={!canRun || busy} />
    <div className="wb-actions"><button disabled={!canRun || busy} onClick={() => void run(() => createDocumentCheck(caseId, documentId, references))}>检查保存版本</button><button disabled={busy} onClick={() => void run(reload)}>刷新检查</button></div>
    {!jobs.length && <p className="wb-muted">尚无检查记录</p>}
    {jobs.map((job) => <article className="wb-check" key={job.id}><div className="wb-actions"><strong>{({ queued: '排队中', running: '执行中', completed: '检查完成', failed: '检查失败' })[job.status]}</strong><span>{job.stale ? '版本已变化，需重新检查' : '快照与当前版本一致'}</span></div>
      <small>{job.created_at} · {job.document_version_id}</small>
      <details><summary>输入快照与规则依据</summary><ul>{job.reference_versions.map((ref) => <li key={ref.document_id}>{names.get(ref.document_id) || ref.document_id} · {ref.document_version_id}</li>)}</ul><pre className="wb-json">{JSON.stringify(job.rule_snapshot, null, 2)}</pre></details>
      {job.error_code && <p role="alert">{job.error_code}</p>}
      {job.status === 'failed' && <button disabled={!canRun || busy || job.stale} onClick={() => void run(() => retryDocumentCheck(caseId, job.id))}>重试或恢复检查</button>}
      {job.findings.map((finding, index) => <div className="wb-finding" key={`${finding.code}-${index}`}><strong>{finding.severity} · {finding.title}</strong><p>{finding.detail}</p><small>来源：{names.get(finding.document_id) || finding.document_id} · {finding.document_version_id}</small>{finding.evidence_excerpt && <blockquote>{finding.evidence_excerpt}</blockquote>}{onRegister && <button disabled={busy || job.stale} onClick={() => void run(() => onRegister(finding))}>登记为检查问题</button>}</div>)}
      {job.status === 'completed' && !job.findings.length && <p>本次检查范围内未发现问题。</p>}
    </article>)}
  </section>;
}
