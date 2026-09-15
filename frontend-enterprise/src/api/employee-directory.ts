import { api, TENANT_ID } from './client';
import { getEnterpriseAuthSession } from '../auth';
import type { AgentProfileRead } from '../types';

const pending = new Map<string, Promise<AgentProfileRead[]>>();

/** Share overlapping shell/page reads only. No settled roster or permission cache. */
export function loadEmployeeDirectory(signal?: AbortSignal): Promise<AgentProfileRead[]> {
  if (signal?.aborted) return Promise.reject(new DOMException('Aborted', 'AbortError'));
  const token = getEnterpriseAuthSession()?.token || '';
  const key = JSON.stringify([TENANT_ID, token]);
  let result = pending.get(key);
  if (!result) {
    result = api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}&view=summary`)
      .finally(() => { if (pending.get(key) === result) pending.delete(key); });
    pending.set(key, result);
  }
  if (!signal) return result;
  // One unmounted consumer must not cancel the shell's shared request.
  return new Promise((resolve, reject) => {
    const abort = () => reject(new DOMException('Aborted', 'AbortError'));
    signal.addEventListener('abort', abort, { once: true });
    result!.then((rows) => { if (!signal.aborted) resolve(rows); }, reject)
      .finally(() => signal.removeEventListener('abort', abort));
  });
}
