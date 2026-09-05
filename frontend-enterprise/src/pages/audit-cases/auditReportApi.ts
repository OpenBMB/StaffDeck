import { api, TENANT_ID } from '@/api/client';
import type { AuditReportRead } from '@/types';

function reportPath(caseId: string, suffix = ''): string {
  return `/api/audit-cases/${encodeURIComponent(caseId)}/reports${suffix}?tenant_id=${encodeURIComponent(TENANT_ID)}`;
}

export function listAuditCaseReports(caseId: string): Promise<AuditReportRead[]> {
  return api.get<AuditReportRead[]>(reportPath(caseId));
}

export function createAuditCaseReport(
  caseId: string,
  modelConfigId?: string,
): Promise<AuditReportRead> {
  return api.post<AuditReportRead>(reportPath(caseId), {
    model_config_id: modelConfigId,
    publish: false,
  });
}

export function publishAuditCaseReport(
  caseId: string,
  reportId: string,
): Promise<AuditReportRead> {
  return api.post<AuditReportRead>(
    `${reportPath(caseId, `/${encodeURIComponent(reportId)}/publish`)}`,
    {},
  );
}

export function downloadAuditCaseReport(caseId: string, reportId: string): Promise<Blob> {
  return api.blob(reportPath(caseId, `/${encodeURIComponent(reportId)}/download`));
}
