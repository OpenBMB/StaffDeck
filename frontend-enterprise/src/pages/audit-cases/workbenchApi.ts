import { api, TENANT_ID } from '@/api/client';
import type { AuditCaseDocumentRead } from '@/types';
import type { PublishedRuleVersionOption } from './ruleBindingApi';

export type ProjectRole = 'project_admin' | 'reviewer' | 'editor' | 'viewer';
export type WorkbenchMember = { user_id: string; display_name: string; role: ProjectRole };
export type VersionReference = { document_id: string; document_version_id: string };
export type WorkItem = {
  id: string; audit_case_id: string; document_id: string; document_version_id: string;
  process_number: number; title: string; status: 'draft' | 'submitted' | 'changes_requested' | 'approved';
  revision: number; assigned_to_user_id: string; reviewer_user_id: string; submitted_by_user_id: string | null;
  approved_version_id: string | null; reference_versions: VersionReference[]; stale: boolean;
  created_at: string; updated_at: string;
};
export type WorkIssue = {
  audit_case_id?: string; organization_name?: string; task_reason?: 'respond' | 'verify';
  id: string; work_item_id: string; document_id: string; document_version_id: string;
  kind: 'document_check' | 'nonconformity' | 'review'; title: string; detail: string;
  blocking: boolean; status: 'open' | 'responded' | 'closed'; assigned_to_user_id: string | null;
  response: string | null; response_version_id: string | null; created_by_user_id: string;
  created_at: string; updated_at: string;
};
export type WorkbenchSnapshot = {
  case_id: string; role: ProjectRole; members: WorkbenchMember[];
  processes: { number: number; name: string; stage: number; enabled: boolean }[];
  work_items: WorkItem[]; issues: WorkIssue[];
};
export type WorkbenchEvent = { id: string; work_item_id: string; event_type: string; actor_user_id: string; detail: unknown; created_at: string };
export type CheckJob = {
  id: string; status: 'queued' | 'running' | 'completed' | 'failed'; document_id: string;
  document_version_id: string; reference_versions: VersionReference[]; stale: boolean;
  findings: { code: string; severity: string; title: string; detail: string; document_id: string; document_version_id: string; evidence_excerpt: string }[];
  rule_snapshot: unknown; error_code: string | null; created_at: string; updated_at: string;
};
export type WorkbenchInbox = { items: (WorkItem & { organization_name: string; task_reason: 'submit' | 'review' | 'respond' })[]; issues: WorkIssue[] };

const encode = encodeURIComponent;
function url(path: string, query: Record<string, string> = {}) {
  return `/api/audit-workbench${path}?${new URLSearchParams({ tenant_id: TENANT_ID, ...query })}`;
}
const casePath = (id: string) => `/cases/${encode(id)}`;
export const requestKey = (): string => crypto.randomUUID();
export const loadWorkbench = (id: string) => api.get<WorkbenchSnapshot>(url(casePath(id)));
export const loadWorkbenchRuleOptions = (id: string) => api.get<PublishedRuleVersionOption[]>(url(`${casePath(id)}/rule-options`));
export const loadWorkbenchInbox = () => api.get<WorkbenchInbox>(url('/inbox'));
export const loadWorkbenchEvents = (id: string, itemId?: string) => api.get<WorkbenchEvent[]>(url(`${casePath(id)}/events`, itemId ? { work_item_id: itemId } : {}));
export const createWorkItem = (id: string, body: { document_id: string; process_number: number; assigned_to_user_id: string; reviewer_user_id: string; reference_document_ids: string[] }) => api.post<WorkItem>(url(`${casePath(id)}/items`), body);
export const transitionWorkItem = (id: string, item: WorkItem, action: 'submit' | 'request_changes' | 'approve' | 'reopen', comment = '', key = requestKey()) => api.post<WorkItem>(url(`${casePath(id)}/items/${encode(item.id)}/transition`), { action, expected_revision: item.revision, request_key: key, comment });
export const createWorkIssue = (id: string, body: { work_item_id: string; kind: WorkIssue['kind']; title: string; detail: string; blocking: boolean; assigned_to_user_id: string }) => api.post<WorkIssue>(url(`${casePath(id)}/issues`), body);
export const transitionWorkIssue = (id: string, issueId: string, action: 'respond' | 'close' | 'reopen', response = '', key = requestKey()) => api.post<WorkIssue>(url(`${casePath(id)}/issues/${encode(issueId)}/transition`), { action, response, request_key: key });
export const setWorkbenchRole = (id: string, userId: string, role: ProjectRole) => api.put<WorkbenchMember>(url(`${casePath(id)}/members/${encode(userId)}/role`), { role });
export const loadDocumentChecks = (id: string, documentId: string) => api.get<CheckJob[]>(url(`${casePath(id)}/checks`, { document_id: documentId }));
export const createDocumentCheck = (id: string, documentId: string, references: string[], key = requestKey()) => api.post<CheckJob>(url(`${casePath(id)}/checks`), { document_id: documentId, reference_document_ids: references, request_key: key });
export const retryDocumentCheck = (id: string, jobId: string, key = requestKey()) => api.post<CheckJob>(url(`${casePath(id)}/checks/${encode(jobId)}/retry`), { request_key: key });
export const reportToWorkDocument = (id: string, reportId: string) => api.post<AuditCaseDocumentRead>(url(`${casePath(id)}/reports/${encode(reportId)}/work-document`));
