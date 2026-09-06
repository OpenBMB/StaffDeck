import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import type { EnterpriseAuthUser } from '@/auth';
import AppHeader from '@/components/AppHeader';
import type { AuditCaseRead } from '@/types';
import { listAuditCases } from '../chat/auditCaseModel';
import { loadWorkbenchInbox, type WorkbenchInbox } from './workbenchApi';
import './workbench.css';

export default function AuditWorkbenchInbox({ currentUser, onLogout }: { currentUser?: EnterpriseAuthUser; onLogout?: () => void }) {
  const [inbox, setInbox] = useState<WorkbenchInbox>({ items: [], issues: [] });
  const [projects, setProjects] = useState<AuditCaseRead[]>([]); const [error, setError] = useState(''); const [loading, setLoading] = useState(true);
  useEffect(() => {
    let active = true;
    void Promise.all([loadWorkbenchInbox(), listAuditCases()]).then(([next, cases]) => { if (active) { setInbox(next); setProjects(cases); } }).catch((e) => { if (active) setError(e instanceof Error ? e.message : '加载工作台失败'); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);
  return <div className="wb-page"><AppHeader left={<h1>认证工作台</h1>} userName={currentUser?.display_name || currentUser?.username} onLogout={onLogout} /><main className="wb-inbox"><p className="wb-muted">个人待办按实际负责人和复核人分配。项目文件复核属于内部协作。</p>
    {error && <p className="wb-error" role="alert">{error}</p>}{loading && <p>正在加载待办…</p>}
    <section className="wb-card"><h2>我的待办 · {inbox.items.length}</h2>{!loading && !inbox.items.length && <p className="wb-muted">目前没有分配给你的待办</p>}{inbox.items.map((item) => <Link className="wb-inbox-row" key={item.id} to={`/enterprise/audit-cases/${encodeURIComponent(item.audit_case_id)}/workbench?document=${encodeURIComponent(item.document_id)}&process=${item.process_number}`}><span><strong>{item.organization_name}</strong> · {item.title}</span><span>{({ submit: '待提交', review: '待复核', respond: '待回应' })[item.task_reason]} →</span></Link>)}</section>
    <section className="wb-card"><h2>待处理问题 · {inbox.issues.length}</h2>{inbox.issues.map((issue) => issue.audit_case_id ? <Link className="wb-inbox-row" key={issue.id} to={`/enterprise/audit-cases/${encodeURIComponent(issue.audit_case_id)}/workbench?document=${encodeURIComponent(issue.document_id)}&item=${encodeURIComponent(issue.work_item_id)}`}><span>{issue.organization_name} · {issue.title}</span><span>{issue.task_reason === 'verify' ? '待验证关闭' : '待回应'} →</span></Link> : <p key={issue.id}>{issue.title} · {issue.status}</p>)}</section>
    <section className="wb-card"><h2>我的项目</h2>{!loading && !projects.length && <p className="wb-muted">暂无可访问项目</p>}{projects.map((project) => <Link className="wb-inbox-row" key={project.id} to={`/enterprise/audit-cases/${encodeURIComponent(project.id)}/workbench`}><span>{project.organization_name}</span><span>{project.report_type} →</span></Link>)}</section>
  </main></div>;
}
