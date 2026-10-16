import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { Upload, Search, Save, X, RefreshCw, FileText, FolderPlus, Plus, Pencil, Archive } from 'lucide-react';
import { KnowledgeClient, type BasicDocument as Document, type BasicBucket as Bucket, type BasicChunk as Chunk, type BasicJob as Job, type BasicSearchResult as SearchResult } from './client';

export type Capabilities = { modules: Record<string, boolean>; operations: Record<string, { method: string; path: string }>; identity_required: boolean };
const ConceptsPanel = lazy(() => import('./panels/ConceptsPanel'));
const DiscoveryPanel = lazy(() => import('./panels/DiscoveryPanel'));
const VersionsPanel = lazy(() => import('./panels/VersionsPanel'));

export default function MinimalPage({ client, capabilities }: { client: KnowledgeClient; capabilities: Capabilities }) {
  const [documents, setDocuments] = useState<Document[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [contentRevision, setContentRevision] = useState(0);
  const terminalJobs = useRef('');
  const [selected, setSelected] = useState<Document>();
  const [content, setContent] = useState('');
  const [buckets, setBuckets] = useState<Bucket[]>([]);
  const [chunks, setChunks] = useState<Chunk[]>([]);
  const [layer, setLayer] = useState<{ kind: 'buckets' | 'chunks'; row: Bucket | Chunk; text: string }>();
  const [query, setQuery] = useState('');
  const [result, setResult] = useState<SearchResult>();
  const [error, setError] = useState('');
  const [progress, setProgress] = useState<number>();
  const uploadController = useRef<AbortController>();
  const [citation, setCitation] = useState<Chunk>();
  const [permissions, setPermissions] = useState<string[]>([]);
  const [space, setSpace] = useState('');
  const [base, setBase] = useState('');
  const [spaces, setSpaces] = useState<Array<{ id: string; name: string }>>([]);
  const [bases, setBases] = useState<Array<{ id: string; name: string }>>([]);
  const [models, setModels] = useState<Array<{ id: string; name: string }>>([]);
  const [modelId, setModelId] = useState('');
  const [useModel, setUseModel] = useState(true);
  const scope = { ...(capabilities.modules.spaces && space ? { space_id: space } : {}),
    ...(capabilities.modules.catalog && base ? { knowledge_base_id: base } : {}) };
  const suffix = Object.keys(scope).length ? '?' + new URLSearchParams(scope).toString() : '';
  const writable = permissions.includes('write');
  const scopeReady = !capabilities.modules.spaces || Boolean(space);
  function fail(error: unknown) { setError(error instanceof Error ? error.message : String(error)); }
  async function refresh() {
    if (capabilities.modules.spaces && !space) return;
    const [rows, tasks] = await Promise.all([client.request<Document[]>('GET', '/documents' + suffix), client.request<Job[]>('GET', '/jobs' + suffix)]);
    setDocuments(rows.filter(row => row.status !== 'archived')); setJobs(tasks);
    const terminal = tasks.filter(row => ['succeeded', 'failed', 'cancelled'].includes(row.status)).map(row => row.id + ':' + row.status).sort().join(',');
    if (terminal !== terminalJobs.current) { terminalJobs.current = terminal; setContentRevision(value => value + 1); }
    setSelected(current => current && rows.some(row => row.id === current.id) ? current : undefined);
  }
  useEffect(() => {
    void client.request<{ permissions: string[] }>('GET', '/identity').then(row => setPermissions(row.permissions)).catch(fail);
    if (capabilities.modules.spaces) void client.request<Array<{ id: string; name: string }>>('GET', '/spaces').then(rows => { setSpaces(rows); setSpace(rows[0]?.id || ''); }).catch(fail);
  }, [client, capabilities]);
  useEffect(() => {
    if (capabilities.modules.spaces && !space) return;
    setSelected(undefined); setBase('');
    if (capabilities.modules.catalog) void client.request<Array<{ id: string; name: string }>>('GET', '/knowledge-bases' + (space ? '?space_id=' + encodeURIComponent(space) : '')).then(setBases).catch(fail);
  }, [client, space, capabilities]);
  useEffect(() => {
    if (capabilities.modules.spaces && !space) return;
    void client.request<Array<{ id: string; name: string; default?: boolean }>>('GET', '/models' + (space ? '?space_id=' + encodeURIComponent(space) : ''))
      .then(rows => { setModels(rows); setModelId(rows.find(row => row.default)?.id || rows[0]?.id || ''); }).catch(fail);
  }, [client, space, capabilities]);
  useEffect(() => { void refresh().catch(fail); const timer = setInterval(() => { void refresh().catch(fail); }, 1000); return () => clearInterval(timer); }, [client, space, base, capabilities]);
  async function open(row: Document) {
    const [doc, topics, evidence] = await Promise.all([
      client.request<Document>('GET', '/documents/' + row.id + suffix),
      client.request<Bucket[]>('GET', '/documents/' + row.id + '/buckets' + suffix),
      client.request<Chunk[]>('GET', '/documents/' + row.id + '/chunks' + suffix),
    ]);
    setSelected(doc); setContent(doc.metadata.raw_text || ''); setBuckets(topics); setChunks(evidence); setLayer(undefined);
  }
  async function upload(file: File) {
    setProgress(0);
    const controller = new AbortController(); uploadController.current = controller;
    try { await client.uploadBasic(file, scope, setProgress, controller.signal, modelId || undefined); await refresh(); }
    catch (error) { if (!controller.signal.aborted) throw error; }
    finally { setProgress(undefined); uploadController.current = undefined; }
  }
  useEffect(() => () => uploadController.current?.abort(), []);
  async function save() {
    if (!selected) return;
    const row = await client.request<Document>('PUT', '/documents/' + selected.id + suffix,
      { expected_updated_at: selected.updated_at, content_md: content });
    await open(row); await refresh(); setContentRevision(value => value + 1);
  }
  async function saveLayer() {
    if (!layer || !selected) return;
    await client.request('PUT', '/' + layer.kind + '/' + layer.row.id + suffix,
      { expected_updated_at: layer.row.updated_at, content_md: layer.text });
    await open(selected); await refresh(); setContentRevision(value => value + 1);
  }
  return <main className="minimal-km">
    <header><h1>Knowledge</h1><div className="km-actions">
      {capabilities.modules.spaces && <select aria-label="Space" value={space} onChange={event => setSpace(event.target.value)}><option value="">Select space</option>{spaces.map(row => <option key={row.id} value={row.id}>{row.name}</option>)}</select>}
      {capabilities.modules.spaces && writable && <button aria-label="Create space" title="Create space" onClick={() => { const name = prompt('Space name'); if (name) void client.request<{ id: string; name: string }>('POST', '/spaces', { id: crypto.randomUUID(), name }).then(row => { setSpaces(rows => [...rows, row]); setSpace(row.id); }).catch(fail); }}><Plus size={18} /></button>}
      {capabilities.modules.catalog && <select aria-label="Knowledge base" value={base} onChange={event => setBase(event.target.value)}><option value="">All documents</option>{bases.map(row => <option key={row.id} value={row.id}>{row.name}</option>)}</select>}
      {capabilities.modules.catalog && writable && <button aria-label="Create knowledge base" title="Create knowledge base" onClick={() => { const name = prompt('Knowledge base name'); if (name) void client.request<{ id: string; name: string }>('POST', '/knowledge-bases', { ...(space ? { space_id: space } : {}), name }).then(row => { setBases(rows => [...rows, row]); setBase(row.id); }).catch(fail); }}><FolderPlus size={18} /></button>}
      {models.length > 0 && <select aria-label="Model" value={modelId} onChange={event => setModelId(event.target.value)}>{models.map(row => <option key={row.id} value={row.id}>{row.name}</option>)}</select>}
      <button title="Refresh" aria-label="Refresh" onClick={() => void refresh().catch(fail)}><RefreshCw size={18} /></button>
      {writable && <label className="upload-button"><Upload size={18} />Upload<input type="file" aria-label="Upload document" disabled={progress !== undefined} onChange={event => { const file = event.target.files?.[0]; if (file) void upload(file).catch(fail); event.target.value = ''; }} /></label>}
    </div></header>
    {error && <div role="alert">{error}<button title="Dismiss" aria-label="Dismiss" onClick={() => setError('')}><X size={16} /></button></div>}
    {progress !== undefined && <div className="km-actions"><progress aria-label="Upload progress" value={progress} max={100} /><button title="Cancel upload" aria-label="Cancel upload" onClick={() => uploadController.current?.abort()}><X size={18} /></button></div>}
    <form className="km-search" onSubmit={event => { event.preventDefault(); void client.request<SearchResult>('POST', '/query', { ...scope, query, use_model: useModel, ...(modelId ? { model_id: modelId } : {}) }).then(setResult).catch(fail); }}>
      <input aria-label="Search knowledge" value={query} onChange={event => setQuery(event.target.value)} placeholder="Search knowledge" required />{models.length > 0 && <label><input type="checkbox" checked={useModel} onChange={event => setUseModel(event.target.checked)} />Model</label>}<button title="Search" aria-label="Search"><Search size={18} /></button>
    </form>
    <div className="km-workspace"><aside><h2>Documents</h2>{documents.map(row => <button className={selected?.id === row.id ? 'active' : ''} key={row.id} onClick={() => void open(row).catch(fail)}><FileText size={16} /><span>{row.title || row.filename}</span><small>{row.chunk_count} chunks</small></button>)}
      <h2>Tasks</h2>{jobs.slice(0, 8).map(row => <div className="km-task" key={row.id}><span>{row.filename}</span><small>{row.status}</small>{['queued', 'running'].includes(row.status) && writable && <button title="Cancel task" aria-label="Cancel task" onClick={() => void client.request('POST', '/jobs/' + row.id + '/cancel' + suffix).then(refresh).catch(fail)}><X size={16} /></button>}{row.error && <p role="alert">{row.error}</p>}</div>)}</aside>
      <section>{selected ? <><div className="km-editor-header"><h2>{selected.title || selected.filename}</h2>{writable && <div className="km-actions"><button title="Archive document" aria-label="Archive document" onClick={() => void client.request('DELETE', '/documents/' + selected.id + suffix).then(refresh).catch(fail)}><Archive size={18} /></button><button title="Save document" aria-label="Save document" onClick={() => void save().catch(fail)}><Save size={18} /></button></div>}</div><textarea aria-label="Document content" value={content} readOnly={!writable} onChange={event => setContent(event.target.value)} />
        <h2>Buckets</h2>{buckets.map(row => <article key={row.id}><div className="km-editor-header"><h3>{row.title}</h3>{writable && <button title="Edit bucket" aria-label="Edit bucket" onClick={() => setLayer({ kind: 'buckets', row, text: row.metadata.content || row.summary })}><Pencil size={16} /></button>}</div><p>{row.summary}</p></article>)}<h2>Chunks</h2>{chunks.map(row => <article key={row.id}><div className="km-editor-header"><small>{row.source_ref}</small>{writable && <button title="Edit chunk" aria-label="Edit chunk" onClick={() => setLayer({ kind: 'chunks', row, text: row.content })}><Pencil size={16} /></button>}</div><p>{row.content}</p></article>)}</> : <h2>Documents</h2>}
        {result && <div className="km-results"><h2>Results</h2>{result.chunks.map(row => <article key={row.id}><p>{row.content}</p><button onClick={() => void client.request<Chunk>('GET', '/citations/' + (row.citation_id || row.id) + suffix).then(setCitation).catch(fail)}>Source: {row.source_ref}</button></article>)}</div>}
      </section></div>
    {layer && <div className="km-source-overlay"><section role="dialog" aria-modal="true" aria-label={layer.kind === 'buckets' ? 'Edit bucket' : 'Edit chunk'}><header><h2>{layer.kind === 'buckets' ? 'Bucket' : 'Chunk'}</h2><div className="km-actions"><button title="Save content" aria-label="Save content" onClick={() => void saveLayer().catch(fail)}><Save size={18} /></button><button title="Close editor" aria-label="Close editor" onClick={() => setLayer(undefined)}><X size={18} /></button></div></header><textarea aria-label="Layer content" value={layer.text} onChange={event => setLayer({ ...layer, text: event.target.value })} /></section></div>}
    {citation && <div className="km-source-overlay" onClick={() => setCitation(undefined)}><section role="dialog" aria-modal="true" aria-label="Citation source" onClick={event => event.stopPropagation()}><header><h2>Citation source</h2><button title="Close source" aria-label="Close source" autoFocus onClick={() => setCitation(undefined)}><X size={18} /></button></header><small>{citation.source_ref}</small><p>{citation.content}</p></section></div>}
    {scopeReady && <Suspense fallback={<div role="status">Loading</div>}>
      {capabilities.modules.versions && base && <VersionsPanel client={client} suffix={suffix} scope={scope} writable={writable} report={fail} revision={contentRevision} onChange={async () => { await refresh(); setContentRevision(value => value + 1); }} />}
      {capabilities.modules.concepts && <ConceptsPanel client={client} suffix={suffix} scope={{ ...scope, ...(selected ? { document_id: selected.id } : {}) }} writable={writable} report={fail} revision={contentRevision} onChange={async () => setContentRevision(value => value + 1)} />}
      {capabilities.modules.discovery && <DiscoveryPanel client={client} suffix={suffix} scope={scope} writable={writable} report={fail} revision={contentRevision} publication={Boolean(capabilities.operations.publish_discovery)} />}
    </Suspense>}
  </main>;
}
