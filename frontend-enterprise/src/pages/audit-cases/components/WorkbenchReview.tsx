import { useEffect, useState } from 'react';
import type { AuditCaseDocumentRead } from '@/types';
import { createWorkItem, createWorkIssue, transitionWorkItem, transitionWorkIssue, type ProcessGate, type WorkbenchSnapshot, type WorkIssue, type WorkItem } from '../workbenchApi';
import { ReferenceSelector } from './WorkbenchChecks';

export const ROLE_LABELS = { project_admin: '项目管理员', reviewer: '复核人', editor: '编辑人', viewer: '查看人' };
export const ITEM_LABELS = { draft: '草稿', submitted: '待复核', changes_requested: '退回修改', approved: '内部复核通过' };
export const ISSUE_LABELS = { document_check: '文档检查问题', nonconformity: '正式不符合项', review: '复核意见' };

export function WorkbenchReview({ caseId, userId, snapshot, documentId, documents, processNumber, gate, item, blocked, run, onDirtyChange }: { caseId: string; userId: string; snapshot: WorkbenchSnapshot; documentId: string; documents: AuditCaseDocumentRead[]; processNumber: number; gate?: ProcessGate; item?: WorkItem; blocked: boolean; run: (action: () => Promise<unknown>) => Promise<void>; onDirtyChange?: (value: boolean) => void }) {
  const [assignee, setAssignee] = useState(''); const [reviewer, setReviewer] = useState(''); const [references, setReferences] = useState<string[]>([]);
  const [comment, setComment] = useState(''); const [kind, setKind] = useState<WorkIssue['kind']>('document_check'); const [title, setTitle] = useState(''); const [detail, setDetail] = useState(''); const [blocking, setBlocking] = useState(true);
  const [responses, setResponses] = useState<Record<string, string>>({});
  useEffect(() => { onDirtyChange?.(Boolean(comment || title || detail || Object.values(responses).some(Boolean) || (!item && (reviewer || references.length)))); }, [comment, title, detail, responses, reviewer, references, item, onDirtyChange]);
  const perform = (action: () => Promise<unknown>) => run(async () => {
    await action(); setComment(''); setTitle(''); setDetail(''); setResponses({}); setReviewer(''); setReferences([]);
  });
  const admin = snapshot.role === 'project_admin'; const canReview = admin || snapshot.role === 'reviewer'; const canEdit = snapshot.role !== 'viewer';
  const process = snapshot.processes.find((p) => p.number === processNumber);
  const issues = snapshot.issues.filter((issue) => issue.work_item_id === item?.id);
  const blockingOpen = issues.some((issue) => issue.blocking && issue.status !== 'closed');
  const canSubmit = item && item.assigned_to_user_id === userId && canEdit;
  const isReviewer = item?.reviewer_user_id === userId && item.submitted_by_user_id !== userId && canReview;
  const gateBlocksCreation = !gate || gate.blockers.some((blocker) => !blocker.code.startsWith('CHECK_'));
  const gateBlocksSubmit = gate?.blockers.some((blocker) => !blocker.code.startsWith('CHECK_')) ?? false;
  useEffect(() => { setAssignee(snapshot.members.find((m) => m.user_id === userId && m.role !== 'viewer')?.user_id || ''); setReviewer(''); setReferences([]); setComment(''); setTitle(''); setDetail(''); setResponses({}); }, [documentId, processNumber, userId]);
  const editableMembers = snapshot.members.filter((m) => m.role !== 'viewer');
  return <>
    <section className="wb-card"><h2>责任与内部复核</h2><p className="wb-muted">本区处理项目文档内部审批，不执行认证决定、证书签署或监管报送。</p>
      {!process?.enabled && <p>该流程当前为目录条目，后续阶段开放。</p>}
      {process?.enabled && gate && <section className={`wb-process-gate ${gate.ready ? 'is-ready' : 'is-blocked'}`} aria-label="流程门槛"><h3>流程门槛</h3><p>{process.guidance || '按当前流程要求完成前置资料和内部复核。'}</p>{gate.predecessors.map((predecessor) => <p key={predecessor.number}>{predecessor.approved ? '✓' : '○'} 流程 {predecessor.number} · {predecessor.status === 'approved' ? '已内部复核通过' : '尚未完成'}</p>)}{gate.required_reference_document_ids.length > 0 && <p>需关联前置文件：{gate.required_reference_document_ids.join('、')}</p>}{gate.check && <p>文件检查：{gate.check.status === 'completed' && !gate.check.stale && !gate.check.has_errors ? '已完成且无错误' : `状态 ${gate.check.status}`}</p>}{gate.blockers.map((blocker) => <p className="wb-error" key={`${blocker.code}-${blocker.process_number || ''}`}>{blocker.message}</p>)}</section>}
      {item ? <>
        <p><strong>{ITEM_LABELS[item.status]}</strong> · 修订 {item.revision}</p>
        <p className="wb-muted">负责人：{snapshot.members.find((m) => m.user_id === item.assigned_to_user_id)?.display_name || item.assigned_to_user_id}<br />复核人：{snapshot.members.find((m) => m.user_id === item.reviewer_user_id)?.display_name || item.reviewer_user_id}</p>
        <p className="wb-muted">送审快照：{item.document_version_id}</p>{item.approved_version_id && <p className="wb-muted">历史审批快照：{item.approved_version_id}</p>}
        {item.stale && <p className="wb-error">主文件或关联文件版本已变化。需退回或重新打开后再次提交。</p>}
        {item.reference_versions.length > 0 && <details><summary>关联版本快照</summary>{item.reference_versions.map((ref) => <p className="wb-muted" key={ref.document_id}>{documents.find((d) => d.id === ref.document_id)?.title || ref.document_id} · {ref.document_version_id}</p>)}</details>}
        <label>流转说明<textarea value={comment} onChange={(e) => setComment(e.target.value)} /></label>
        <div className="wb-actions">
          {canSubmit && (item.status === 'draft' || item.status === 'changes_requested') && <button disabled={blocked || gateBlocksSubmit} onClick={() => void perform(() => transitionWorkItem(caseId, item, 'submit', comment))}>提交复核</button>}
          {isReviewer && item.status === 'submitted' && <><button disabled={blocked || !comment.trim()} onClick={() => void perform(() => transitionWorkItem(caseId, item, 'request_changes', comment))}>退回修改</button><button disabled={blocked || gate?.ready !== true || item.stale || blockingOpen} onClick={() => void perform(() => transitionWorkItem(caseId, item, 'approve', comment))}>内部复核通过</button></>}
          {isReviewer && item.status === 'approved' && <button disabled={blocked} onClick={() => void perform(() => transitionWorkItem(caseId, item, 'reopen', comment))}>重新打开修改</button>}
        </div>{blockingOpen && <p className="wb-muted">仍有未关闭的阻断问题，不能通过复核。</p>}
      </> : process?.enabled && <>
        <p className="wb-muted">为当前文件创建该流程的工作项，指定负责人及独立复核人。</p>
        <label>工作项负责人<select aria-label="工作项负责人" value={assignee} onChange={(e) => setAssignee(e.target.value)} disabled={!canEdit || blocked}><option value="">请选择</option>{editableMembers.map((m) => <option key={m.user_id} value={m.user_id}>{m.display_name} · {ROLE_LABELS[m.role]}</option>)}</select></label>
        <label>工作项复核人<select aria-label="工作项复核人" value={reviewer} onChange={(e) => setReviewer(e.target.value)} disabled={!canEdit || blocked}><option value="">请选择</option>{snapshot.members.filter((m) => m.user_id !== userId && m.user_id !== assignee && (m.role === 'reviewer' || m.role === 'project_admin')).map((m) => <option key={m.user_id} value={m.user_id}>{m.display_name}</option>)}</select></label>
        <ReferenceSelector documents={documents} selectedId={documentId} values={references} onChange={setReferences} disabled={!canEdit || blocked} />
        <button disabled={!canEdit || blocked || gateBlocksCreation || !documentId || !assignee || !reviewer || assignee === reviewer} onClick={() => void perform(() => createWorkItem(caseId, { document_id: documentId, process_number: processNumber, assigned_to_user_id: assignee, reviewer_user_id: reviewer, reference_document_ids: references }))}>建立工作项</button>
      </>}
    </section>
    {item && <section className="wb-card"><h2>问题闭环 · {issues.length}</h2>
      {issues.map((issue) => <article className="wb-issue" key={issue.id}><strong>{ISSUE_LABELS[issue.kind]} · {issue.title}</strong><p className="wb-muted">{({ open: '待回应', responded: '已回应待验证', closed: '已关闭' })[issue.status]}{issue.blocking ? ' · 阻断' : ' · 提示'}</p><p>{issue.detail}</p><small>发现版本：{issue.document_version_id}</small>{issue.response && <p>回应：{issue.response}<br /><small>{issue.response_version_id}</small></p>}
        {issue.status !== 'closed' && issue.assigned_to_user_id === userId && canEdit && <><label>问题回应<textarea aria-label={`回应 ${issue.title}`} value={responses[issue.id] || ''} onChange={(e) => setResponses((values) => ({ ...values, [issue.id]: e.target.value }))} /></label><button disabled={blocked || !responses[issue.id]?.trim()} onClick={() => void perform(() => transitionWorkIssue(caseId, issue.id, 'respond', responses[issue.id]))}>提交回应</button></>}
        {issue.status === 'responded' && issue.response_version_id !== documents.find((d) => d.id === issue.document_id)?.active_version_id && <p className="wb-error">回应版本已过期，请负责人基于当前版本重新回应。</p>}
        {canReview && issue.assigned_to_user_id !== userId && <div className="wb-actions">{issue.status === 'responded' && <button disabled={blocked || issue.response_version_id !== documents.find((d) => d.id === issue.document_id)?.active_version_id} onClick={() => void run(() => transitionWorkIssue(caseId, issue.id, 'close'))}>验证并关闭</button>}{issue.status === 'closed' && <button disabled={blocked} onClick={() => void run(() => transitionWorkIssue(caseId, issue.id, 'reopen'))}>重新打开问题</button>}</div>}
      </article>)}
      {canEdit && <details><summary>登记人工问题</summary><label>问题类型<select value={kind} onChange={(e) => setKind(e.target.value as WorkIssue['kind'])}><option value="document_check">文档检查问题</option>{canReview && <><option value="review">复核意见</option><option value="nonconformity">正式不符合项</option></>}</select></label><label>问题标题<input value={title} onChange={(e) => setTitle(e.target.value)} /></label><label>问题依据<textarea value={detail} onChange={(e) => setDetail(e.target.value)} /></label><label><input type="checkbox" checked={blocking} onChange={(e) => setBlocking(e.target.checked)} />阻断内部复核</label><button disabled={blocked || !title.trim()} onClick={() => void run(async () => { await createWorkIssue(caseId, { work_item_id: item.id, kind, title: title.trim(), detail, blocking, assigned_to_user_id: item.assigned_to_user_id }); setTitle(''); setDetail(''); })}>登记问题</button></details>}
    </section>}
  </>;
}
