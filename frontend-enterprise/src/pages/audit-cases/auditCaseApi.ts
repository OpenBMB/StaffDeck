import { api, TENANT_ID } from '@/api/client';
import type {
  AuditCaseManagementOptions,
  AuditCaseManagementPage,
  AuditCaseRead,
} from '@/types';

import type {
  AuditCaseCreateRequest,
  AuditCaseListParams,
  AuditCaseMemberOption,
  AuditCaseUpdateRequest,
} from './auditCaseTypes';

function managementSearchParams(params: AuditCaseListParams = {}): string {
  const search = new URLSearchParams({ tenant_id: TENANT_ID });
  if (params.query?.trim()) search.set('q', params.query.trim());
  if (params.status && params.status !== 'all') search.set('status', params.status);
  if (params.management_system?.trim()) search.set('management_system', params.management_system.trim());
  if (params.report_type?.trim()) search.set('report_type', params.report_type.trim());
  search.set('offset', String(Math.max(0, params.offset ?? 0)));
  search.set('limit', String(Math.min(100, Math.max(1, params.limit ?? 20))));
  return search.toString();
}

function cleanList(values: string[]): string[] {
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))];
}

export function listManagedAuditCases(
  params: AuditCaseListParams = {},
): Promise<AuditCaseManagementPage> {
  return api.get<AuditCaseManagementPage>(
    `/api/audit-cases/management?${managementSearchParams(params)}`,
  );
}

export function loadAuditCaseManagementOptions(): Promise<AuditCaseManagementOptions> {
  const search = new URLSearchParams({ tenant_id: TENANT_ID });
  return api.get<AuditCaseManagementOptions>(
    `/api/audit-cases/management-options?${search.toString()}`,
  );
}

export function loadAuditCaseMembers(): Promise<AuditCaseMemberOption[]> {
  const search = new URLSearchParams({ tenant_id: TENANT_ID });
  return api.get<AuditCaseMemberOption[]>(`/api/auth/users?${search.toString()}`);
}

export function createAuditCase(request: AuditCaseCreateRequest): Promise<AuditCaseRead> {
  return api.post<AuditCaseRead>('/api/audit-cases', {
    ...request,
    tenant_id: request.tenant_id.trim(),
    organization_name: request.organization_name.trim(),
    report_type: request.report_type.trim(),
    management_systems: cleanList(request.management_systems),
    knowledge_base_version_ids: cleanList(request.knowledge_base_version_ids),
    member_user_ids: cleanList(request.member_user_ids),
  });
}

export function updateAuditCase(
  caseId: string,
  request: AuditCaseUpdateRequest,
): Promise<AuditCaseRead> {
  return api.patch<AuditCaseRead>(
    `/api/audit-cases/${encodeURIComponent(caseId)}?tenant_id=${encodeURIComponent(TENANT_ID)}`,
    {
      ...request,
      organization_name: request.organization_name?.trim(),
      report_type: request.report_type?.trim(),
      management_systems: request.management_systems
        ? cleanList(request.management_systems)
        : undefined,
      knowledge_base_version_ids: request.knowledge_base_version_ids
        ? cleanList(request.knowledge_base_version_ids)
        : undefined,
    },
  );
}

export function replaceAuditCaseMembers(
  caseId: string,
  memberUserIds: string[],
): Promise<AuditCaseRead> {
  return api.put<AuditCaseRead>(
    `/api/audit-cases/${encodeURIComponent(caseId)}/members?tenant_id=${encodeURIComponent(TENANT_ID)}`,
    { member_user_ids: cleanList(memberUserIds) },
  );
}
