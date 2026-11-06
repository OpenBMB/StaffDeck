import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { getEnterpriseAuthSession } from '../auth';

const pending = new Map<string, Promise<Blob>>();
let authToken = '';

export function useEmployeeAvatar(source: string) {
  const [resolved, setResolved] = useState('');
  const token = getEnterpriseAuthSession()?.token || '';
  const protectedAsset = source.startsWith('/api/chat/agents/') && source.includes('/avatar/');
  useEffect(() => {
    setResolved('');
    if (!protectedAsset || !token) return;
    if (authToken !== token) { pending.clear(); authToken = token; }
    if (!pending.has(source)) {
      const request = api.blob(source).catch((error) => { pending.delete(source); throw error; });
      if (pending.size >= 64) pending.delete(pending.keys().next().value!);
      pending.set(source, request);
    }
    let cancelled = false;
    let url = '';
    pending.get(source)!.then((blob) => {
      if (!cancelled) { url = URL.createObjectURL(blob); setResolved(url); }
    }).catch(() => undefined);
    return () => { cancelled = true; if (url) URL.revokeObjectURL(url); };
  }, [protectedAsset, source, token]);
  return protectedAsset ? resolved : source;
}
