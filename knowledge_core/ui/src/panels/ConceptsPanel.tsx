import { useEffect, useState } from 'react';
import { Download, Upload } from 'lucide-react';
import type { Concept } from '../client';
import { download, type Props } from './shared';
import KnowledgeGraphCanvas from '../KnowledgeGraphCanvas';

export default function ConceptsPanel({ client, suffix, scope, writable, report, revision, onChange }: Props) {
  const [rows, setRows] = useState<Concept[]>([]);
  const [selected, setSelected] = useState<Concept>();
  const [text, setText] = useState('');
  const [issues, setIssues] = useState<Array<{ message: string }>>([]);
  const refresh = () => client.request<Concept[]>('GET', '/concepts' + suffix).then(setRows);
  async function importBundle(file: File) {
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = '';
    for (let start = 0; start < bytes.length; start += 16384) binary += String.fromCharCode(...bytes.subarray(start, start + 16384));
    await client.request('POST', '/okf' + suffix, { filename: file.name, content_base64: btoa(binary),
      ...(scope.document_id ? { document_id: scope.document_id } : {}) });
    await refresh();
    await onChange?.();
  }
  useEffect(() => { void refresh().catch(report); setSelected(undefined); }, [client, suffix, revision]);
  return <section className="km-optional"><header><h2>Concepts</h2><div className="km-actions">
    <button title="Export OKF" aria-label="Export OKF" onClick={() => void client.download('/okf' + suffix).then(blob => download(blob, 'knowledge.okf.zip')).catch(report)}><Download size={18} /></button>
    {writable && (!scope.space_id || scope.document_id) && <label className="upload-button" title="Import OKF"><Upload size={18} /><input aria-label="Import OKF" type="file" accept=".zip,.md,.markdown" onChange={event => { const file = event.target.files?.[0]; if (file) void importBundle(file).catch(report); event.target.value = ''; }} /></label>}
    <button onClick={() => void client.request<{ issues: Array<{ message: string }> }>('POST', '/okf/lint' + suffix).then(row => setIssues(row.issues)).catch(report)}>Lint</button>
  </div></header><div className="km-concepts">{rows.map(row => <button key={row.id} onClick={() => { setSelected(row); setText(row.content_md); }}>{row.title}</button>)}</div>
    <KnowledgeGraphCanvas concepts={rows} onSelectConcept={row => { setSelected(row); setText(row.content_md); }} />
    {selected && <><h3>{selected.title}</h3><textarea aria-label="Concept content" value={text} readOnly={!writable} onChange={event => setText(event.target.value)} />{writable && <button onClick={() => void client.request<Concept>('PUT', '/concepts/' + selected.concept_id.split('/').map(encodeURIComponent).join('/') + suffix,
      { content_md: text, document_id: selected.document_id, expected_updated_at: selected.updated_at }).then(async row => { setSelected(row); await refresh(); await onChange?.(); }).catch(report)}>Save</button>}</>}
    {issues.map((row, index) => <p key={index}>{row.message}</p>)}
  </section>;
}
