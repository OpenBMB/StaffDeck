import { useEffect, useState } from 'react';
import { Copy, RefreshCw, Send } from 'lucide-react';
import type { Props } from './shared';

type Version = { id: string; name: string; created_at: string };
export default function VersionsPanel({ client, scope, writable, report, onChange, revision }: Props) {
  const [rows, setRows] = useState<Version[]>([]);
  const [selected, setSelected] = useState('');
  const [branch, setBranch] = useState<{ updated_at: string }>();
  const path = '/knowledge-bases/' + scope.knowledge_base_id;
  const suffix = scope.space_id ? '?space_id=' + encodeURIComponent(scope.space_id) : '';
  async function refresh() {
    const [versions, current] = await Promise.all([client.request<Version[]>('GET', path + '/versions' + suffix), client.request<{ updated_at: string }>('GET', path + '/branch' + suffix)]);
    setRows(versions); setBranch(current); setSelected(value => versions.some(row => row.id === value) ? value : versions.at(-1)?.id || '');
  }
  useEffect(() => { void refresh().catch(report); }, [client, path, suffix, revision]);
  async function action(operation: string) {
    if (!branch) return;
    await client.request('POST', path + '/versions/' + operation + suffix, { expected_updated_at: branch.updated_at, ...(operation !== 'copy' ? { version_id: selected } : {}) });
    await refresh();
    await onChange?.();
  }
  return <section className="km-optional"><header><h2>Versions</h2><div className="km-actions"><select aria-label="Version" value={selected} onChange={event => setSelected(event.target.value)}><option value="">Select version</option>{rows.map(row => <option key={row.id} value={row.id}>{row.name} {row.created_at}</option>)}</select>
    {writable && <><button title="Snapshot" aria-label="Snapshot" disabled={!branch} onClick={() => void action('copy').catch(report)}><Copy size={18} /></button><button title="Rollback" aria-label="Rollback" disabled={!selected} onClick={() => void action('rollback').catch(report)}><RefreshCw size={18} /></button><button title="Publish version" aria-label="Publish version" disabled={!selected} onClick={() => void action('publish').catch(report)}><Send size={18} /></button></>}
  </div></header></section>;
}
