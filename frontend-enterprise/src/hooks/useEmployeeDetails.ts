import { useEffect, useState } from 'react';
import { api } from '../api/client';
import type { AgentProfileRead } from '../types';
import { notify } from '../components/ui/app-toast';

/** Editors hydrate full details; a directory summary is never written back as a resource. */
export function useEmployeeDetails(input: AgentProfileRead | null | undefined, open: boolean) {
  const [detail, setDetail] = useState<AgentProfileRead | null>(null);
  useEffect(() => {
    setDetail(null);
    if (!open || !input?.metadata?.directory_summary) return;
    const controller = new AbortController();
    api.get<AgentProfileRead>(`/api/enterprise/agents/${encodeURIComponent(input.id)}?tenant_id=${encodeURIComponent(input.tenant_id)}`, { signal: controller.signal })
      .then((row) => { if (!controller.signal.aborted) setDetail(row); })
      .catch((error) => { if (!controller.signal.aborted) notify.error(error instanceof Error ? error.message : '员工详情加载失败'); });
    return () => controller.abort();
  }, [input, open]);
  return input?.metadata?.directory_summary ? (detail?.id === input.id && detail?.tenant_id === input.tenant_id ? detail : null) : input;
}
