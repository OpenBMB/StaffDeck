export type BasicEntity = {
  id: string; created_at: string; updated_at: string;
};
export type Entity = BasicEntity & { tenant_id: string };
export type BasicDocument = BasicEntity & { filename: string; title?: string; status: string; bucket_count: number; chunk_count: number; metadata: { raw_text?: string; [key: string]: unknown } };
export type BasicBucket = BasicEntity & { document_id: string; title: string; summary: string; metadata: { content?: string } };
export type BasicChunk = BasicEntity & { document_id: string; bucket_id: string; content: string; source_ref?: string; citation_id?: string };
export type BasicJob = BasicEntity & { status: string; stage: string; progress: number; error?: string; document_id?: string; filename: string };
export type BasicSearchResult = { chunks: BasicChunk[]; selected_concepts: Array<Record<string, unknown>>; evidence_pack: Array<Record<string, unknown>>; route_trace: Array<Record<string, unknown>> };
export type Space = Entity & { name: string; owner_id: string; owner_name: string; can_write?: boolean };
export type KnowledgeBase = Entity & { name: string; description?: string; status: string; metadata: Record<string, unknown>; branch?: Branch };
export type Branch = Entity & { space_id: string; knowledge_base_id: string; head_version_id: string; base_version_id: string; sync_state: string; status: string };
export type Binding = Entity & { space_id: string; knowledge_base_id: string; hidden: boolean; metadata: Record<string, unknown>; branch: Branch };
export type Version = Entity & { version: string; name: string; status: string };
export type Document = Entity & { knowledge_base_id: string; knowledge_base_version_id: string; filename: string; title?: string; status: string; bucket_count: number; chunk_count: number; metadata: { raw_text?: string; [key: string]: unknown } };
export type Bucket = Entity & { document_id: string; title: string; summary: string; metadata: { content?: string } };
export type Chunk = Entity & { document_id: string; bucket_id: string; content: string; source_ref?: string; citation_id?: string };
export type Concept = Entity & { document_id?: string; concept_id: string; concept_type: string; title: string; content_md: string; links: Array<{ target?: string; href?: string }>; citations: Array<{ target?: string }>; source_refs: Array<Record<string, unknown>> };
export type Job = Entity & { status: string; stage: string; progress: number; error?: string; document_id?: string; filename: string };
export type Suggestion = Entity & { title: string; status: string; suggestion_type: string; payload: Record<string, unknown>; source_refs: unknown[]; reason?: string };
export type SearchResult = { chunks: Chunk[]; selected_concepts: Array<Record<string, unknown>>; evidence_pack: Array<Record<string, unknown>>; route_trace: Array<Record<string, unknown>> };

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
export type UploadTransport = (url: string, body: string, headers: Record<string, string>, progress: (percent: number) => void, signal?: AbortSignal) => Promise<Response>;

export class KnowledgeClient {
  constructor(
    public baseUrl = '', private bearer?: string,
    private transport: typeof fetch = fetch,
    public apiPath = '/api/km/v1',
    private requestHeaders: () => Record<string, string> = () => ({}),
    private uploadTransport?: UploadTransport,
  ) {}
  private xhrUpload: UploadTransport = (url, body, headers, progress, signal) => new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    for (const [name, value] of Object.entries(headers)) xhr.setRequestHeader(name, value);
    xhr.upload.onprogress = event => { if (event.lengthComputable) progress(Math.round(event.loaded / event.total * 100)); };
    xhr.onload = () => resolve(new Response(xhr.responseText, { status: xhr.status, headers: { 'Content-Type': xhr.getResponseHeader('Content-Type') || 'application/json' } }));
    xhr.onerror = () => reject(new Error('Upload connection failed'));
    xhr.onabort = () => reject(new DOMException('Upload cancelled', 'AbortError'));
    const abort = () => xhr.abort();
    if (signal?.aborted) { reject(signal.reason); return; }
    signal?.addEventListener('abort', abort, { once: true });
    xhr.onloadend = () => signal?.removeEventListener('abort', abort);
    xhr.send(body);
  });
  async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const transport = this.transport;
    const response = await transport(this.baseUrl + this.apiPath + path, {
      method, headers: { ...this.requestHeaders(), ...(body ? { 'Content-Type': 'application/json' } : {}), ...(this.bearer ? { Authorization: 'Bearer ' + this.bearer } : {}) },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: response.statusText }));
      throw new ApiError(response.status, typeof error.detail === 'string' ? error.detail : JSON.stringify(error.detail));
    }
    return response.json();
  }
  async download(path: string): Promise<Blob> {
    const transport = this.transport;
    const response = await transport(this.baseUrl + this.apiPath + path, { headers: { ...this.requestHeaders(), ...(this.bearer ? { Authorization: 'Bearer ' + this.bearer } : {}) } });
    if (!response.ok) throw new ApiError(response.status, 'Download failed');
    return response.blob();
  }
  scoped(path: string, space: string) { return path + (path.includes('?') ? '&' : '?') + 'space_id=' + encodeURIComponent(space); }
  spaces() { return this.request<Space[]>('GET', '/spaces'); }
  async uploadBasic(file: File, scope: Record<string, string>, progress: (percent: number) => void, signal?: AbortSignal, model_id?: string): Promise<BasicJob> {
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = '';
    for (let start = 0; start < bytes.length; start += 16384) binary += String.fromCharCode(...bytes.subarray(start, start + 16384));
    const url = this.baseUrl + this.apiPath + '/documents';
    const body = JSON.stringify({ ...scope, filename: file.name, content_base64: btoa(binary), ...(model_id ? { model_id } : {}) });
    const headers = { ...this.requestHeaders(), 'Content-Type': 'application/json', ...(this.bearer ? { Authorization: 'Bearer ' + this.bearer } : {}) };
    const response = await (this.uploadTransport || this.xhrUpload)(url, body, headers, progress, signal);
    const result = await response.json();
    if (!response.ok) throw new ApiError(response.status, result.detail || response.statusText);
    progress(100); return result;
  }
  createSpace(name: string) { return this.request<Space>('POST', '/spaces', { name }); }
  bases(space: string) { return this.request<KnowledgeBase[]>('GET', this.scoped('/knowledge-bases', space)); }
  base(id: string, space: string) { return this.request<KnowledgeBase>('GET', this.scoped('/knowledge-bases/' + id, space)); }
  createBase(space_id: string, name: string) { return this.request<KnowledgeBase>('POST', '/knowledge-bases', { space_id, name }); }
  editBase(base: KnowledgeBase, space: string, changes: Partial<Pick<KnowledgeBase, 'name' | 'description' | 'status'>>) { return this.request<KnowledgeBase>('PUT', this.scoped('/knowledge-bases/' + base.id, space), { ...changes, expected_updated_at: base.updated_at }); }
  deleteBase(id: string, space: string, permanent = false) { return this.request<{ status: string }>('DELETE', this.scoped('/knowledge-bases/' + id + '?permanent=' + permanent, space)); }
  bind(id: string, source: string, space_id: string, hidden = false) { return this.request<Binding>('POST', '/knowledge-bases/' + id + '/bindings?source_space_id=' + encodeURIComponent(source), { space_id, hidden }); }
  bindings(space: string) { return this.request<Binding[]>('GET', this.scoped('/bindings', space)); }
  editBinding(id: string, space: string, changes: { hidden?: boolean; status?: 'active' | 'inactive' | 'deleted'; metadata?: Record<string, unknown> }) { return this.request<Binding>('PUT', this.scoped('/knowledge-bases/' + id + '/binding', space), changes); }
  documents(id: string, space: string) { return this.request<Document[]>('GET', this.scoped('/knowledge-bases/' + id + '/documents', space)); }
  document(id: string, space: string) { return this.request<Document>('GET', this.scoped('/documents/' + id, space)); }
  editDocument(document: Document, space: string, content_md: string, title?: string) { return this.request<Document>('PUT', this.scoped('/documents/' + document.id, space), { expected_updated_at: document.updated_at, content_md, title }); }
  archiveDocument(id: string, space: string) { return this.request<{ status: string }>('DELETE', this.scoped('/documents/' + id, space)); }
  buckets(id: string, space: string) { return this.request<Bucket[]>('GET', this.scoped('/documents/' + id + '/buckets', space)); }
  chunks(id: string, space: string) { return this.request<Chunk[]>('GET', this.scoped('/documents/' + id + '/chunks', space)); }
  concepts(id: string, space: string) { return this.request<Concept[]>('GET', this.scoped('/knowledge-bases/' + id + '/concepts', space)); }
  editLayer(layer: 'buckets', entity: Entity, space: string, changes: { content_md?: string; title?: string; summary?: string }): Promise<Bucket>;
  editLayer(layer: 'chunks', entity: Entity, space: string, changes: { content_md?: string; title?: string; summary?: string }): Promise<Chunk>;
  editLayer(layer: 'concepts', entity: Entity, space: string, changes: { content_md?: string; title?: string; summary?: string }): Promise<Concept>;
  editLayer(layer: 'buckets' | 'chunks' | 'concepts', entity: Entity, space: string, changes: { content_md?: string; title?: string; summary?: string }): Promise<Bucket | Chunk | Concept> { return this.request('PUT', this.scoped('/' + layer + '/' + entity.id, space), { ...changes, expected_updated_at: entity.updated_at }); }
  editBranch(id: string, version: Version, space: string, changes: { name?: string; description?: string; status?: string; metadata?: Record<string, unknown> }) { return this.request<Version>('PUT', this.scoped('/knowledge-bases/' + id + '/branch', space), { ...changes, expected_updated_at: version.updated_at }); }
  upsertConcept(id: string, conceptId: string, space: string, content_md: string, document_id?: string, expected_updated_at?: string) { return this.request<Concept>('PUT', this.scoped('/knowledge-bases/' + id + '/concepts/' + conceptId.split('/').map(encodeURIComponent).join('/'), space), { content_md, document_id, expected_updated_at }); }
  upload(id: string, space_id: string, file: File, progress: (percent: number) => void, okf = false, model_id?: string, signal?: AbortSignal): Promise<Job | { document_id: string }> {
    return file.arrayBuffer().then(buffer => new Promise((resolve, reject) => {
      const bytes = new Uint8Array(buffer);
      let binary = '';
      for (let start = 0; start < bytes.length; start += 16384) binary += String.fromCharCode(...bytes.subarray(start, start + 16384));
      const url = this.baseUrl + this.apiPath + '/knowledge-bases/' + id + (okf ? '/okf' : '/documents');
      const payload = JSON.stringify({ space_id, filename: file.name, content_base64: btoa(binary), ...(!okf && model_id ? { model_id } : {}) });
      if (this.uploadTransport) {
        this.uploadTransport(url, payload, { ...this.requestHeaders(), 'Content-Type': 'application/json', ...(this.bearer ? { Authorization: 'Bearer ' + this.bearer } : {}) }, progress, signal)
          .then(async response => { const value = await response.json(); if (!response.ok) throw new ApiError(response.status, value.detail || response.statusText); resolve(value); }).catch(reject);
        return;
      }
      const xhr = new XMLHttpRequest();
      xhr.open('POST', url);
      xhr.setRequestHeader('Content-Type', 'application/json');
      for (const [name, value] of Object.entries(this.requestHeaders())) xhr.setRequestHeader(name, value);
      if (this.bearer) xhr.setRequestHeader('Authorization', 'Bearer ' + this.bearer);
      xhr.upload.onprogress = event => { if (event.lengthComputable) progress(Math.round(event.loaded / event.total * 100)); };
      xhr.onload = () => { const body = JSON.parse(xhr.responseText); if (xhr.status >= 200 && xhr.status < 300) resolve(body); else reject(new ApiError(xhr.status, body.detail || xhr.statusText)); };
      xhr.onerror = () => reject(new Error('Upload connection failed'));
      xhr.onabort = () => reject(new DOMException('Upload cancelled', 'AbortError'));
      const abort = () => xhr.abort();
      if (signal?.aborted) { reject(signal.reason); return; }
      signal?.addEventListener('abort', abort, { once: true });
      xhr.onloadend = () => signal?.removeEventListener('abort', abort);
      xhr.send(payload);
    }));
  }
  job(id: string, space: string) { return this.request<Job>('GET', this.scoped('/jobs/' + id, space)); }
  jobs(space: string) { return this.request<Job[]>('GET', this.scoped('/jobs', space)); }
  cancel(id: string, space: string) { return this.request<Job>('POST', this.scoped('/jobs/' + id + '/cancel', space)); }
  query(space_id: string, query: string, knowledge_base_ids: string[] = [], use_model = true, model_id?: string) { return this.request<SearchResult>('POST', '/query', { space_id, query, knowledge_base_ids, use_model, model_id }); }
  citation(id: string, space: string) { return this.request<Chunk>('GET', this.scoped('/citations/' + id, space)); }
  versions(id: string, space: string) { return this.request<Version[]>('GET', this.scoped('/knowledge-bases/' + id + '/versions', space)); }
  version(id: string, space: string, operation: 'copy' | 'sync' | 'publish' | 'rollback', version_id?: string) { return this.request<Branch>('POST', this.scoped('/knowledge-bases/' + id + '/versions/' + operation, space), { version_id }); }
  suggestions(id: string, space: string) { return this.request<Suggestion[]>('GET', this.scoped('/knowledge-bases/' + id + '/suggestions', space)); }
  suggestion(id: string, space: string, operation: 'review'): Promise<{ status: 'reviewed'; published: false }>;
  suggestion(id: string, space: string, operation: 'reject' | 'export'): Promise<Suggestion>;
  suggestion(id: string, space: string, operation: 'publish'): Promise<Record<string, unknown>>;
  suggestion(id: string, space: string, operation: 'review' | 'reject' | 'export' | 'publish') { return this.request('POST', this.scoped('/suggestions/' + id + '/' + operation, space)); }
  lint(id: string, space: string) { return this.request<{ issue_count: number; issues: Array<Record<string, unknown>> }>('POST', this.scoped('/knowledge-bases/' + id + '/okf/lint', space)); }
  async exportOkf(id: string, space: string) {
    const transport = this.transport;
    const response = await transport(this.baseUrl + this.apiPath + this.scoped('/knowledge-bases/' + id + '/okf', space), { headers: { ...this.requestHeaders(), ...(this.bearer ? { Authorization: 'Bearer ' + this.bearer } : {}) } });
    if (!response.ok) throw new ApiError(response.status, 'Export failed');
    return response.blob();
  }
}
