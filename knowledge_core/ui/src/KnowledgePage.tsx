import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Archive, Check, Copy, Download, FileText, FolderPlus, GitBranch, History, LoaderCircle, Network, Pencil, Plus, RefreshCw, Save, Search, Trash2, Upload, X } from 'lucide-react';
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import KnowledgeGraphCanvas from './KnowledgeGraphCanvas';
import { KnowledgeClient, type Binding, type KnowledgeBase, type Space, type Document, type Bucket, type Concept, type Job, type Version, type Suggestion, type SearchResult, type Chunk } from './client';

export type KnowledgeHost = {
  client: KnowledgeClient;
  spaceDirectory?: () => Promise<Space[]>;
  canWrite?: (space: Space | undefined) => boolean;
  notify?: (message: string, error?: boolean) => void;
  navigate?: (path: string) => void;
  renderMarkdown?: (text: string) => ReactNode;
  publishSuggestions?: boolean;
  manageSpaces?: boolean;
  initialSpaceId?: string;
  useModel?: boolean;
  modelOptions?: (spaceId: string) => Promise<Array<{ id: string; name: string }>>;
};

function IconButton({ title, children, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { title: string }) {
  return <button className="icon-button" title={title} aria-label={title} {...props}>{children}</button>;
}

export default function KnowledgePage({ host }: { host: KnowledgeHost }) {
  const api = host.client;
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [modelOptions, setModelOptions] = useState<Array<{ id: string; name: string }>>([]);
  const [modelId, setModelId] = useState('');
  const [spaceId, setSpaceId] = useState(host.initialSpaceId ?? '');
  const [bases, setBases] = useState<KnowledgeBase[]>([]);
  const [hiddenBindings, setHiddenBindings] = useState<Binding[]>([]);
  const [baseId, setBaseId] = useState('');
  const [base, setBase] = useState<KnowledgeBase>();
  const [documents, setDocuments] = useState<Document[]>([]);
  const [document, setDocument] = useState<Document>();
  const [buckets, setBuckets] = useState<Bucket[]>([]);
  const [chunks, setChunks] = useState<Chunk[]>([]);
  const [layerEdit, setLayerEdit] = useState<{ layer: 'buckets' | 'chunks'; entity: Bucket | Chunk; text: string }>();
  const [concepts, setConcepts] = useState<Concept[]>([]);
  const [selectedConcept, setSelectedConcept] = useState<Concept>();
  const [conceptText, setConceptText] = useState('');
  const [editingConcept, setEditingConcept] = useState(false);
  const [sourceText, setSourceText] = useState('');
  const [versions, setVersions] = useState<Version[]>([]);
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [progress, setProgress] = useState<number>();
  const [tab, setTab] = useState('source');
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<SearchResult>();
  const [citation, setCitation] = useState<Chunk>();
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [newName, setNewName] = useState('');
  const [dialog, setDialog] = useState<'base' | 'space' | 'binding' | 'delete' | 'rename' | 'restore'>();
  const [targetSpace, setTargetSpace] = useState('');
  const uploadRef = useRef<HTMLInputElement>(null);
  const okfRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    let active = true;
    setBuckets([]); setChunks([]); setLayerEdit(undefined);
    if (document) void Promise.all([api.buckets(document.id, spaceId), api.chunks(document.id, spaceId)])
      .then(([nextBuckets, nextChunks]) => { if (active) { setBuckets(nextBuckets); setChunks(nextChunks); } }).catch(report);
    return () => { active = false; };
  }, [api, document, spaceId]);
  useEffect(() => {
    let active = true;
    setModelId(''); setModelOptions([]);
    if (spaceId && host.modelOptions) void host.modelOptions(spaceId)
      .then(value => {
        if (!active) return;
        setModelOptions(value);
        const saved = sessionStorage.getItem('km:model:' + spaceId);
        setModelId(value.some(model => model.id === saved) ? saved! : '');
      }).catch(report);
    return () => { active = false; };
  }, [spaceId, host.modelOptions]);
  const canWrite = host.canWrite?.(spaces.find(space => space.id === spaceId)) ?? true;
  const selection = useRef({ spaceId, baseId });
  selection.current = { spaceId, baseId };

  function report(error: unknown) {
    const text = error instanceof Error ? error.message : String(error);
    setMessage(text); host.notify?.(text, true);
  }
  async function run(action: () => Promise<void>) {
    setBusy(true); setMessage('');
    try { await action(); } catch (error) { report(error); } finally { setBusy(false); }
  }
  async function refreshSpaces() {
    const rows = await (host.spaceDirectory?.() ?? api.spaces());
    setSpaces(rows);
    if (rows.length && !rows.some(space => space.id === selection.current.spaceId)) setSpaceId(rows[0].id);
  }
  async function refreshBases() {
    if (!spaceId) return;
    const [rows, bindings] = await Promise.all([api.bases(spaceId), api.bindings(spaceId)]);
    if (selection.current.spaceId !== spaceId) return;
    setBases(rows);
    setHiddenBindings(bindings.filter(binding => binding.hidden));
    if (!rows.some(row => row.id === baseId)) setBaseId(rows[0]?.id ?? '');
  }
  async function refreshBase() {
    if (!baseId || !spaceId) { setBase(undefined); setDocuments([]); setConcepts([]); return; }
    const [next, docs, wiki, history, discoveries] = await Promise.all([
      api.base(baseId, spaceId), api.documents(baseId, spaceId), api.concepts(baseId, spaceId),
      api.versions(baseId, spaceId), api.suggestions(baseId, spaceId),
    ]);
    if (selection.current.spaceId !== spaceId || selection.current.baseId !== baseId) return;
    setBase(next); setDocuments(docs); setConcepts(wiki); setVersions(history); setSuggestions(discoveries);
    if (document && !docs.some(row => row.id === document.id)) {
      setDocument(undefined); setSourceText('');
    }
    if (selectedConcept && !wiki.some(row => row.id === selectedConcept.id)) setSelectedConcept(undefined);
  }
  useEffect(() => { void refreshSpaces().catch(report); }, [api]);
  useEffect(() => {
    setResults(undefined); setCitation(undefined); setJobs([]);
    void refreshBases().catch(report);
    if (spaceId) void api.jobs(spaceId).then(rows => {
      if (selection.current.spaceId === spaceId) setJobs(rows);
    }).catch(report);
  }, [spaceId, api]);
  useEffect(() => { void refreshBase().catch(report); }, [baseId, spaceId]);
  const running = jobs.some(job => !['succeeded', 'failed', 'cancelled'].includes(job.status));
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(async () => {
      try {
        const updated = await Promise.all(jobs.map(job => api.job(job.id, spaceId)));
        setJobs(updated);
        if (updated.some((job, index) => job.status !== jobs[index].status && job.status === 'succeeded')) await refreshBase();
      } catch (error) { report(error); }
    }, 700);
    return () => window.clearInterval(timer);
  }, [running, jobs, spaceId]);

  async function upload(file: File, okf = false) {
    await run(async () => {
      setProgress(0);
      try {
        const result = await api.upload(baseId, spaceId, file, setProgress, okf, modelId || undefined);
        if ('status' in result) setJobs(previous => [result as Job, ...previous]);
        await refreshBase();
      } finally { setProgress(undefined); }
    });
  }
  function download(blob: Blob, name: string) {
    const url = URL.createObjectURL(blob);
    const anchor = window.document.createElement('a');
    anchor.href = url; anchor.download = name; anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function markdown(text: string) {
    return host.renderMarkdown?.(text) ?? <div className="markdown" dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(marked.parse(text, { async: false })) }} />;
  }
  async function version(operation: 'copy' | 'sync' | 'publish' | 'rollback', id?: string) {
    await run(async () => { await api.version(baseId, spaceId, operation, id); setDocument(undefined); setSelectedConcept(undefined); await refreshBase(); });
  }

  return <div className="knowledge-app">
    <header className="app-bar"><div className="brand"><FileText size={20} /><strong>Knowledge</strong></div>
      <select aria-label="空间" value={spaceId} onChange={event => { setBaseId(''); setBases([]); setBase(undefined); setDocument(undefined); setSelectedConcept(undefined); setSpaceId(event.target.value); }}>{spaces.map(space => <option key={space.id} value={space.id}>{space.name}</option>)}</select>
      {canWrite && host.manageSpaces !== false && <IconButton title="新建空间" onClick={() => { setDialog('space'); setNewName(''); }}><FolderPlus size={18} /></IconButton>}
      <span className="spacer" />
      {modelOptions.length > 0 && <select aria-label="模型" value={modelId} onChange={event => { setModelId(event.target.value); sessionStorage.setItem('km:model:' + spaceId, event.target.value); }}><option value="">默认模型</option>{modelOptions.map(model => <option key={model.id} value={model.id}>{model.name}</option>)}</select>}
      <IconButton title="刷新" disabled={busy} onClick={() => void run(async () => { await refreshSpaces(); await refreshBases(); await refreshBase(); })}><RefreshCw size={18} /></IconButton>
    </header>
    {message && <div role="alert" className="error">{message}<IconButton title="关闭提示" onClick={() => setMessage('')}><X size={16} /></IconButton></div>}
    <div className="workspace">
      <aside className="base-sidebar"><div className="section-title"><h2>知识库</h2>{canWrite && <div className="actions">{hiddenBindings.length > 0 && <IconButton title="恢复隐藏绑定" onClick={() => setDialog('restore')}><History size={18} /></IconButton>}<IconButton title="新建知识库" onClick={() => { setDialog('base'); setNewName(''); }}><Plus size={18} /></IconButton></div>}</div>
        <nav>{bases.map(row => <button key={row.id} className={'base-row ' + (row.id === baseId ? 'selected' : '')} onClick={() => setBaseId(row.id)}><FileText size={17} /><span>{row.name}</span><small>{row.status === 'archived' ? '归档' : ''}</small></button>)}</nav>
      </aside>
      <main>
        {!base ? <div className="empty">暂无知识库</div> : <>
          <div className="base-heading"><div><h1>{base.name}</h1><div className="metadata">{documents.length} 文档 · {concepts.length} Wiki · {base.branch?.sync_state}</div></div>
            <div className="actions">
              <IconButton title="导出 OKF" onClick={() => void run(async () => download(await api.exportOkf(baseId, spaceId), base.name + '.okf.zip'))}><Download size={18} /></IconButton>
              {canWrite && <>
                <IconButton title="重命名知识库" disabled={busy} onClick={() => { setDialog('rename'); setNewName(base.name); }}><Pencil size={18} /></IconButton>
                <IconButton title="绑定其他空间" onClick={() => { setTargetSpace(''); setDialog('binding'); }}><Copy size={18} /></IconButton>
                <IconButton title="归档知识库" onClick={() => void run(async () => { await api.editBase(base, spaceId, { status: base.status === 'archived' ? 'active' : 'archived' }); await refreshBases(); await refreshBase(); })}><Archive size={18} /></IconButton>
                <IconButton title="删除知识库" onClick={() => setDialog('delete')}><Trash2 size={18} /></IconButton>
                <button className="command" disabled={busy} onClick={() => uploadRef.current?.click()}><Upload size={16} />上传</button>
              </>}
            </div>
          </div>
          <input ref={uploadRef} hidden type="file" accept=".txt,.md,.markdown,.html,.htm,.pdf,.docx" onChange={event => { const file = event.target.files?.[0]; if (file) void upload(file); event.target.value = ''; }} />
          <input ref={okfRef} hidden type="file" accept=".zip,.md,.markdown" onChange={event => { const file = event.target.files?.[0]; if (file) void upload(file, true); event.target.value = ''; }} />
          {progress !== undefined && <div className="upload-progress"><progress max={100} value={progress} /><span>{progress}%</span></div>}
          {jobs.map(job => <div key={job.id} className="job"><span>{job.filename}</span><progress max={1} value={job.progress} /><span>{job.status === 'succeeded' ? '完成' : job.status === 'failed' ? job.error : job.status === 'cancelled' ? '已取消' : job.stage}</span>{!['succeeded', 'failed', 'cancelled'].includes(job.status) && <IconButton title="取消入库" onClick={() => void run(async () => { const cancelled = await api.cancel(job.id, spaceId); setJobs(rows => rows.map(row => row.id === cancelled.id ? cancelled : row)); })}><X size={16} /></IconButton>}</div>)}
          <div className="tabs" role="tablist">{[['source', '文档'], ['wiki', 'Wiki'], ['graph', '关系图'], ['search', '检索'], ['versions', '版本'], ['discovery', '发现建议']].map(([id, label]) => <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)}>{label}</button>)}</div>
          {tab === 'source' && <section className="document-layout">
            <div className="document-list">{documents.map(row => <button key={row.id} className={document?.id === row.id ? 'selected' : ''} onClick={() => void run(async () => { const next = await api.document(row.id, spaceId); setDocument(next); setSourceText(next.metadata.raw_text ?? ''); })}><FileText size={16} /><span>{row.title || row.filename}<small>{row.chunk_count} 引用 · {row.status}</small></span></button>)}</div>
            <div className="source-editor">{document ? <><div className="section-title"><h2>{document.title || document.filename}</h2><div className="actions">{canWrite && <><IconButton title="归档文档" onClick={() => void run(async () => { await api.archiveDocument(document.id, spaceId); await refreshBase(); })}><Archive size={17} /></IconButton><IconButton title="保存正文" disabled={busy} onClick={() => void run(async () => { const next = await api.editDocument(document, spaceId, sourceText); setDocument(next); await refreshBase(); })}><Save size={17} /></IconButton></>}</div></div><textarea aria-label="文档正文" value={sourceText} readOnly={!canWrite} onChange={event => setSourceText(event.target.value)} /><details><summary>预览</summary>{markdown(sourceText)}</details>
              <details className="document-structure"><summary>主题与片段</summary>{buckets.map(bucket => <section className="bucket-section" key={bucket.id}><div className="section-title"><h2>{bucket.title}</h2>{canWrite && <IconButton title="编辑主题" disabled={busy} onClick={() => setLayerEdit({ layer: 'buckets', entity: bucket, text: bucket.metadata.content ?? '' })}><Pencil size={17} /></IconButton>}</div>{markdown(bucket.metadata.content || bucket.summary)}{chunks.filter(chunk => chunk.bucket_id === bucket.id).map(chunk => <article className="evidence" key={chunk.id}><div className="section-title"><small>{chunk.source_ref || chunk.id}</small>{canWrite && <IconButton title="编辑片段" disabled={busy} onClick={() => setLayerEdit({ layer: 'chunks', entity: chunk, text: chunk.content })}><Pencil size={17} /></IconButton>}</div>{markdown(chunk.content)}</article>)}</section>)}</details>
              {layerEdit && <section className="layer-editor"><h2>{layerEdit.layer === 'buckets' ? '编辑主题' : '编辑片段'}</h2><textarea aria-label="结构正文" value={layerEdit.text} onChange={event => setLayerEdit({ ...layerEdit, text: event.target.value })} /><div className="actions"><button className="command" disabled={busy} onClick={() => void run(async () => { await api.editLayer(layerEdit.layer as 'buckets', layerEdit.entity, spaceId, { content_md: layerEdit.text }); const [nextBuckets, nextChunks] = await Promise.all([api.buckets(document.id, spaceId), api.chunks(document.id, spaceId)]); setBuckets(nextBuckets); setChunks(nextChunks); setLayerEdit(undefined); await refreshBase(); })}><Save size={16} />保存结构</button><button disabled={busy} onClick={() => setLayerEdit(undefined)}>取消</button></div></section>}
            </> : <div className="empty">暂无选中文档</div>}</div>
          </section>}
          {tab === 'wiki' && <section><div className="section-title"><h2>Wiki</h2><div className="actions"><button onClick={() => void run(async () => { const lint = await api.lint(baseId, spaceId); setMessage(lint.issue_count ? `发现 ${lint.issue_count} 项：${lint.issues.slice(0, 3).map(issue => issue.message).join('；')}` : '检查通过'); })}>检查</button>{canWrite && <button onClick={() => okfRef.current?.click()}><Upload size={16} />导入 OKF</button>}</div></div><div className="wiki-layout"><div>{concepts.map(concept => <button className="wiki-row" key={concept.id} onClick={() => { setSelectedConcept(concept); setEditingConcept(false); }}><span>{concept.title}</span><small>{concept.concept_type}</small></button>)}</div><div className="source-editor">{selectedConcept ? <><div className="section-title"><h2>{selectedConcept.title}</h2>{canWrite && <button onClick={() => { setConceptText(selectedConcept.content_md); setEditingConcept(true); }}>编辑</button>}</div>{editingConcept ? <><textarea aria-label="Wiki 正文" value={conceptText} onChange={event => setConceptText(event.target.value)} /><div className="actions"><button className="command" disabled={busy} onClick={() => void run(async () => { const next = await api.editLayer('concepts', selectedConcept, spaceId, { content_md: conceptText }) as Concept; setSelectedConcept(next); setEditingConcept(false); await refreshBase(); })}><Save size={16} />保存</button><button onClick={() => setEditingConcept(false)}>取消</button></div></> : markdown(selectedConcept.content_md)}</> : <div className="empty">暂无选中页面</div>}</div></div></section>}
          {tab === 'graph' && <section className="graph"><KnowledgeGraphCanvas concepts={concepts as any} onSelectConcept={(concept: any) => { setSelectedConcept(concept); setTab('wiki'); }} /></section>}
          {tab === 'search' && <section><form className="search-bar" onSubmit={event => { event.preventDefault(); void run(async () => setResults(await api.query(spaceId, query, [baseId], host.useModel ?? true, modelId || undefined))); }}><Search size={18} /><input aria-label="检索问题" value={query} onChange={event => setQuery(event.target.value)} /><button className="command" disabled={busy}>检索</button></form>{results?.chunks.map(chunk => <article className="evidence" key={chunk.id}><button className="citation" onClick={() => void run(async () => setCitation(await api.citation(chunk.id, spaceId)))}>{chunk.source_ref || chunk.id}</button>{markdown(chunk.content)}</article>)}{results && !results.chunks.length && <div className="empty">暂无匹配引用</div>}{results && <details><summary>检索过程</summary><pre>{JSON.stringify(results.route_trace, null, 2)}</pre></details>}</section>}
{tab === 'versions' && <section><div className="section-title"><h2><History size={18} />版本</h2>{canWrite && <div className="actions"><button disabled={busy} onClick={() => void version('copy')}><GitBranch size={16} />复制</button><button disabled={busy} onClick={() => void version('sync')}><RefreshCw size={16} />同步</button><button className="command" disabled={busy} onClick={() => void version('publish')}><Check size={16} />发布</button></div>}</div><table><thead><tr><th>版本</th><th>创建时间</th><th>状态</th><th /></tr></thead><tbody>{versions.map(row => <tr key={row.id}><td>{row.version}</td><td>{new Date(row.created_at).toLocaleString()}</td><td>{row.id === base.branch?.head_version_id ? '当前' : row.id === base.metadata.published_version_id ? '已发布' : '历史'}</td><td>{canWrite && row.id !== base.branch?.head_version_id && <button disabled={busy} onClick={() => void version('rollback', row.id)}>回滚</button>}</td></tr>)}</tbody></table></section>}
          {tab === 'discovery' && <section>{suggestions.map(row => <article className="suggestion" key={row.id}><div className="section-title"><h2>{row.title}</h2><span>{row.status}</span></div><p>{row.reason}</p><pre>{JSON.stringify(row.payload, null, 2)}</pre><div className="actions">{canWrite && row.status === 'pending' && <><button onClick={() => void run(async () => { await api.suggestion(row.id, spaceId, 'review'); await refreshBase(); })}><Check size={16} />审核</button><button onClick={() => void run(async () => { await api.suggestion(row.id, spaceId, 'reject'); await refreshBase(); })}><X size={16} />拒绝</button></>}<button onClick={() => download(new Blob([JSON.stringify(row, null, 2)], { type: 'application/json' }), row.id + '.json')}><Download size={16} />导出</button>{canWrite && host.publishSuggestions && row.status === 'reviewed' && <button onClick={() => void run(async () => { await api.suggestion(row.id, spaceId, 'publish'); await refreshBase(); })}>发布</button>}</div></article>)}{!suggestions.length && <div className="empty">暂无建议</div>}</section>}
        </>}
      </main>
    </div>
    {(dialog || citation) && <div className="modal-backdrop" onClick={event => { if (event.target === event.currentTarget) { setDialog(undefined); setCitation(undefined); } }}><section role="dialog" aria-modal="true" className="modal"><div className="section-title"><h2>{citation ? '引用来源' : dialog === 'base' ? '新建知识库' : dialog === 'space' ? '新建空间' : dialog === 'binding' ? '绑定空间' : dialog === 'rename' ? '重命名知识库' : dialog === 'restore' ? '恢复隐藏绑定' : '删除知识库'}</h2><IconButton title="关闭" onClick={() => { setDialog(undefined); setCitation(undefined); }}><X size={18} /></IconButton></div>
      {citation ? <>{markdown(citation.content)}<small>{citation.source_ref}</small></> : dialog === 'restore' ? <div>{hiddenBindings.map(binding => <div className="section-title" key={binding.id}><span>{String(binding.metadata.name || binding.knowledge_base_id)}</span><button disabled={busy} onClick={() => void run(async () => { await api.editBinding(binding.knowledge_base_id, spaceId, { hidden: false, status: 'active' }); setDialog(undefined); await refreshBases(); setBaseId(binding.knowledge_base_id); })}>恢复</button></div>)}</div> : dialog === 'delete' ? <div className="actions"><button onClick={() => void run(async () => { await api.deleteBase(baseId, spaceId); setDialog(undefined); await refreshBases(); })}>隐藏当前绑定</button><button className="danger" onClick={() => void run(async () => { await api.deleteBase(baseId, spaceId, true); setDialog(undefined); await refreshBases(); })}>彻底删除</button></div> : dialog === 'binding' ? <><select aria-label="目标空间" value={targetSpace} onChange={event => setTargetSpace(event.target.value)}><option value="">选择空间</option>{spaces.filter(space => space.id !== spaceId).map(space => <option key={space.id} value={space.id}>{space.name}</option>)}</select><button className="command" disabled={!targetSpace || busy} onClick={() => void run(async () => { await api.bind(baseId, spaceId, targetSpace); setDialog(undefined); })}>绑定</button></> : <form onSubmit={event => { event.preventDefault(); void run(async () => { if (dialog === 'space') { const next = await api.createSpace(newName); await refreshSpaces(); setSpaceId(next.id); } else if (dialog === 'rename' && base) { await api.editBase(base, spaceId, { name: newName }); await refreshBases(); await refreshBase(); } else { const next = await api.createBase(spaceId, newName); await refreshBases(); setBaseId(next.id); } setDialog(undefined); }); }}><input autoFocus aria-label="名称" required value={newName} onChange={event => setNewName(event.target.value)} /><button className="command" disabled={busy}>{dialog === 'rename' ? '保存' : '创建'}</button></form>}
    </section></div>}
    {busy && <span className="busy-indicator" aria-label="处理中"><LoaderCircle size={16} /></span>}
  </div>;
}
