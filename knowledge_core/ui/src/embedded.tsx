import { lazy, Suspense, useEffect, useState } from 'react';
import type { KnowledgeHost } from './KnowledgePage';
import { KnowledgeClient, type UploadTransport, type Space } from './client';
import MinimalPage, { type Capabilities } from './MinimalPage';

const protocol = 'knowledge-frame/v1';
const KnowledgePage = lazy(() => import('./KnowledgePage'));
export function EmbeddedKnowledge() {
  const [host, setHost] = useState<KnowledgeHost>();
  const [error, setError] = useState('');
  const [capabilities, setCapabilities] = useState<Capabilities>();
  useEffect(() => {
    const channel = new URLSearchParams(location.hash.slice(1)).get('channel');
    const origin = location.origin;
    const pending = new Map<string, { resolve: (value: Response) => void; reject: (error: Error) => void; progress?: (percent: number) => void; cleanup: () => void }>();
    const post = (value: Record<string, unknown>) => parent.postMessage({ protocol, channel, ...value }, origin);
    function request(url: string, options: RequestInit = {}, progress?: (percent: number) => void): Promise<Response> {
      return new Promise((resolve, reject) => {
        const id = crypto.randomUUID();
        const abort = () => { post({ type: 'cancel', id }); pending.get(id)?.cleanup(); pending.delete(id); reject(new DOMException('Request cancelled', 'AbortError')); };
        if (options.signal?.aborted) { reject(options.signal.reason); return; }
        options.signal?.addEventListener('abort', abort, { once: true });
        pending.set(id, { resolve, reject, progress, cleanup: () => options.signal?.removeEventListener('abort', abort) });
        post({ type: 'request', id, path: url, method: options.method || 'GET', body: options.body, upload: Boolean(progress) });
      });
    }
    const transport: typeof fetch = (url, options) => request(String(url), options);
    const upload: UploadTransport = (url, body, _headers, progress, signal) => request(url, { method: 'POST', body, signal }, progress);
    const listener = (event: MessageEvent) => {
      if (event.source !== parent || event.origin !== origin || event.data?.channel !== channel) return;
      const value = event.data;
      if (value.protocol !== protocol) { setError('Knowledge bridge protocol is incompatible'); return; }
      if (value.type === 'init') {
        clearTimeout(timeout);
        const spaces: Space[] = value.spaces;
        if (value.capabilities) setCapabilities(value.capabilities);
        if (!Array.isArray(spaces) || !spaces.every(row => typeof row?.id === 'string' && typeof row?.name === 'string')
          || !Array.isArray(value.models) || !value.models.every((row: { id?: unknown; name?: unknown }) => typeof row?.id === 'string' && typeof row?.name === 'string')) { setError('Invalid Knowledge host configuration'); return; }
        setHost({ client: new KnowledgeClient('', undefined, transport, '/api/km/v1', undefined, upload),
          spaceDirectory: async () => spaces, modelOptions: async space => value.models.filter((row: { space_id?: string }) => !row.space_id || row.space_id === space),
          canWrite: space => space?.can_write === true, manageSpaces: false, useModel: true,
          publishSuggestions: value.publishSuggestions === true,
          initialSpaceId: spaces.some(row => row.id === value.initialSpaceId) ? value.initialSpaceId : undefined,
          navigate: path => post({ type: 'navigate', path }),
          notify: (message, error) => post({ type: 'notify', message, error }),
        });
      }
      const item = pending.get(value.id);
      if (!item) return;
      if (value.type === 'progress' && Number.isFinite(value.percent)) { item.progress?.(Math.max(0, Math.min(100, value.percent))); return; }
      if (value.type !== 'response') return;
      pending.delete(value.id); item.cleanup();
      if (value.error) item.reject(new Error(value.error));
      else item.resolve(new Response(value.body, { status: value.status, headers: value.headers }));
    };
    addEventListener('message', listener);
    const timeout = setTimeout(() => setError('Knowledge host bridge did not connect'), 15000);
    post({ type: 'ready' });
    return () => { clearTimeout(timeout); removeEventListener('message', listener); pending.forEach((item, id) => { post({ type: 'cancel', id }); item.cleanup(); item.reject(new Error('Knowledge frame unmounted')); }); pending.clear(); };
  }, []);
  if (error) return <div role="alert">{error}</div>;
  if (host && capabilities?.modules && !Object.entries(capabilities.modules).filter(([name]) => name !== 'authentication').every(([, enabled]) => enabled))
    return <MinimalPage client={host.client} capabilities={capabilities} />;
  return host ? <Suspense fallback={<div role="status">Loading</div>}><KnowledgePage host={host} /></Suspense> : <div role="status">Loading</div>;
}
