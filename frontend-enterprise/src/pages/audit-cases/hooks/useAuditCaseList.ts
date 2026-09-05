import { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';

import { auditCaseErrorMessage } from '../auditCaseErrors';
import { listManagedAuditCases } from '../auditCaseApi';
import type { AuditCaseListParams, AuditCaseListState } from '../auditCaseTypes';

const PAGE_SIZE = 20;

function numberParam(value: string | null, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : fallback;
}

export function useAuditCaseList(): AuditCaseListState & {
  filters: AuditCaseListParams;
  page: number;
  setFilter: (key: 'query' | 'status' | 'management_system' | 'report_type', value: string) => void;
  setPage: (page: number) => void;
  reload: () => Promise<void>;
} {
  const [searchParams, setSearchParams] = useSearchParams();
  const searchKey = searchParams.toString();
  const filters = useMemo<AuditCaseListParams>(() => ({
    query: searchParams.get('q') || '',
    status: searchParams.get('status') || 'all',
    management_system: searchParams.get('management_system') || '',
    report_type: searchParams.get('report_type') || '',
    offset: Math.max(0, numberParam(searchParams.get('page'), 1) - 1) * PAGE_SIZE,
    limit: PAGE_SIZE,
  }), [searchKey]);
  const page = Math.floor((filters.offset || 0) / PAGE_SIZE) + 1;
  const [state, setState] = useState<AuditCaseListState>({
    rows: [],
    total: 0,
    loading: true,
    error: '',
  });

  const reload = useCallback(async () => {
    setState((previous) => ({ ...previous, loading: true, error: '' }));
    try {
      const result = await listManagedAuditCases(filters);
      setState({ rows: result.items, total: result.total, loading: false, error: '' });
    } catch (error) {
      setState((previous) => ({
        ...previous,
        loading: false,
        error: auditCaseErrorMessage(error, '加载认证项目失败'),
      }));
    }
  }, [filters]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const setFilter = useCallback((key: 'query' | 'status' | 'management_system' | 'report_type', value: string) => {
    const next = new URLSearchParams(searchParams);
    const paramName = key === 'query' ? 'q' : key;
    if (value && value !== 'all') next.set(paramName, value);
    else next.delete(paramName);
    next.delete('page');
    setSearchParams(next);
  }, [searchParams, setSearchParams]);

  const setPage = useCallback((nextPage: number) => {
    const next = new URLSearchParams(searchParams);
    if (nextPage <= 1) next.delete('page');
    else next.set('page', String(nextPage));
    setSearchParams(next);
  }, [searchParams, setSearchParams]);

  return { ...state, filters, page, setFilter, setPage, reload };
}

export { PAGE_SIZE as AUDIT_CASE_PAGE_SIZE };
