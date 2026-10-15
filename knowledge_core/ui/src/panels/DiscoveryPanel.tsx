import { useEffect, useState } from 'react';
import { Check, Download, Send, X } from 'lucide-react';
import type { Suggestion } from '../client';
import { download, type Props } from './shared';

export default function DiscoveryPanel({ client, suffix, writable, report, publication, revision }: Props & { publication: boolean }) {
  const [rows, setRows] = useState<Suggestion[]>([]);
  const refresh = () => client.request<Suggestion[]>('GET', '/suggestions' + suffix).then(setRows);
  useEffect(() => { void refresh().catch(report); }, [client, suffix, revision]);
  function action(row: Suggestion, operation: string) {
    void client.request('POST', '/suggestions/' + row.id + '/' + operation + suffix).then(value => {
      if (operation === 'export') download(new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' }), row.id + '.json');
      return refresh();
    }).catch(report);
  }
  return <section className="km-optional"><h2>Suggestions</h2>{rows.map(row => <article key={row.id}><header><h3>{row.title}</h3><small>{row.status}</small></header><p>{row.reason}</p><div className="km-actions">
    <button title="Export suggestion" aria-label="Export suggestion" onClick={() => action(row, 'export')}><Download size={18} /></button>
    {writable && row.status !== 'confirmed' && <><button title="Review suggestion" aria-label="Review suggestion" onClick={() => action(row, 'review')}><Check size={18} /></button><button title="Reject suggestion" aria-label="Reject suggestion" onClick={() => action(row, 'reject')}><X size={18} /></button></>}
    {publication && writable && row.status === 'reviewed' && <button title="Publish suggestion" aria-label="Publish suggestion" onClick={() => action(row, 'publish')}><Send size={18} /></button>}
  </div></article>)}</section>;
}
