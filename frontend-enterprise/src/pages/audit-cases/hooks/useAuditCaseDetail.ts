import { useCallback, useEffect, useState } from 'react';

import type {
  AuditCaseCoverageRead,
  AuditCaseEventRead,
  AuditCaseManagementOptions,
  AuditCaseMaterialRead,
  AuditCaseRead,
} from '@/types';

import {
  loadAuditCase,
  loadAuditCaseCoverage,
  loadAuditCaseEvents,
  loadAuditCaseManagementOptions,
  loadAuditCaseMaterials,
} from '../auditCaseApi';
import { auditCaseErrorMessage } from '../auditCaseErrors';

export type AuditCaseDetailState = {
  project: AuditCaseRead | null;
  materials: AuditCaseMaterialRead[];
  coverage: AuditCaseCoverageRead | null;
  events: AuditCaseEventRead[];
  options: AuditCaseManagementOptions;
  loading: boolean;
  error: string;
};

const EMPTY_OPTIONS: AuditCaseManagementOptions = {
  knowledge_versions: [],
  supported_extensions: [],
  max_material_bytes: 50 * 1024 * 1024,
};

export function useAuditCaseDetail(caseId: string | undefined): AuditCaseDetailState & {
  reload: () => Promise<void>;
  reloadMaterials: () => Promise<void>;
} {
  const [state, setState] = useState<AuditCaseDetailState>({
    project: null,
    materials: [],
    coverage: null,
    events: [],
    options: EMPTY_OPTIONS,
    loading: true,
    error: '',
  });

  const reload = useCallback(async () => {
    if (!caseId) return;
    setState((previous) => ({ ...previous, loading: true, error: '' }));
    try {
      const [project, materials, coverage, events, options] = await Promise.all([
        loadAuditCase(caseId),
        loadAuditCaseMaterials(caseId),
        loadAuditCaseCoverage(caseId),
        loadAuditCaseEvents(caseId),
        loadAuditCaseManagementOptions(),
      ]);
      setState({ project, materials, coverage, events, options, loading: false, error: '' });
    } catch (error) {
      setState((previous) => ({ ...previous, loading: false, error: auditCaseErrorMessage(error, '加载认证项目详情失败') }));
    }
  }, [caseId]);

  const reloadMaterials = useCallback(async () => {
    if (!caseId) return;
    try {
      const [materials, coverage, events] = await Promise.all([
        loadAuditCaseMaterials(caseId),
        loadAuditCaseCoverage(caseId),
        loadAuditCaseEvents(caseId),
      ]);
      setState((previous) => ({ ...previous, materials, coverage, events }));
    } catch (error) {
      setState((previous) => ({ ...previous, error: auditCaseErrorMessage(error, '刷新材料状态失败') }));
    }
  }, [caseId]);

  useEffect(() => {
    let active = true;
    setState({ project: null, materials: [], coverage: null, events: [], options: EMPTY_OPTIONS, loading: true, error: '' });
    if (!caseId) return () => { active = false; };
    void Promise.all([
      loadAuditCase(caseId),
      loadAuditCaseMaterials(caseId),
      loadAuditCaseCoverage(caseId),
      loadAuditCaseEvents(caseId),
      loadAuditCaseManagementOptions(),
    ])
      .then(([project, materials, coverage, events, options]) => {
        if (active) setState({ project, materials, coverage, events, options, loading: false, error: '' });
      })
      .catch((error) => {
        if (active) setState((previous) => ({ ...previous, loading: false, error: auditCaseErrorMessage(error, '加载认证项目详情失败') }));
      });
    return () => { active = false; };
  }, [caseId]);

  return { ...state, reload, reloadMaterials };
}
