import { api, TENANT_ID } from '@/api/client';
import type {
  AuditCaseCoverageRead,
  AuditCaseMaterialRead,
  AuditCaseRead,
} from '@/types';

export type AuditCaseMaterialSummary = {
  ready: number;
  pending: number;
  failed: string[];
  total: number;
};

export function auditCaseSummary(
  materials: AuditCaseMaterialRead[],
): AuditCaseMaterialSummary {
  const current = materials.filter((material) => material.is_current !== false);
  const ready = current.filter((material) => (
    material.extraction_status === 'succeeded'
    && material.processing_status === 'succeeded'
  ));
  const failedMaterials = current.filter((material) => (
    material.extraction_status === 'failed'
    || material.processing_status === 'failed'
  ));
  const failed = failedMaterials
    .map((material) => material.filename)
    .filter((filename, index, names) => names.indexOf(filename) === index);
  return {
    ready: ready.length,
    pending: Math.max(0, current.length - ready.length - failedMaterials.length),
    failed,
    total: current.length,
  };
}

export function listAuditCases(tenantId = TENANT_ID): Promise<AuditCaseRead[]> {
  return api.get<AuditCaseRead[]>(`/api/audit-cases?tenant_id=${encodeURIComponent(tenantId)}`);
}

export function loadAuditCaseMaterials(
  caseId: string,
  tenantId = TENANT_ID,
): Promise<AuditCaseMaterialRead[]> {
  return api.get<AuditCaseMaterialRead[]>(
    `/api/audit-cases/${encodeURIComponent(caseId)}/materials?tenant_id=${encodeURIComponent(tenantId)}`,
  );
}

export function loadAuditCaseCoverage(
  caseId: string,
  tenantId = TENANT_ID,
): Promise<AuditCaseCoverageRead> {
  return api.get<AuditCaseCoverageRead>(
    `/api/audit-cases/${encodeURIComponent(caseId)}/coverage?tenant_id=${encodeURIComponent(tenantId)}`,
  );
}
