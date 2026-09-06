import { useEffect, useMemo, useRef, useState } from 'react';

import { ApiError } from '@/api/client';
import { Button, Card, CardContent, Input, Textarea, notify } from '@/components/ui';
import type { AuditCaseDocumentDetailRead, AuditCaseDocumentRead, AuditCaseDocumentVersionRead } from '@/types';

import {
  archiveAuditCaseDocument,
  createAuditCaseDocument,
  createAuditCaseDocumentVersion,
  loadAuditCaseDocument,
} from '../auditCaseApi';
import type { AuditCaseDocumentCreateRequest } from '../auditCaseTypes';
import { auditCaseErrorMessage } from '../auditCaseErrors';

function latestVersion(detail: AuditCaseDocumentDetailRead | null): AuditCaseDocumentVersionRead | null {
  if (!detail?.versions.length) return detail?.document.active_version || null;
  return detail.versions[detail.versions.length - 1];
}

function documentErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.code === 'DOCUMENT_VERSION_CONFLICT') {
    return '文档版本已变化，请重新加载后再保存';
  }
  return auditCaseErrorMessage(error, fallback);
}

function replaceDocument(documents: AuditCaseDocumentRead[], next: AuditCaseDocumentRead): AuditCaseDocumentRead[] {
  const found = documents.some((document) => document.id === next.id);
  return found
    ? documents.map((document) => (document.id === next.id ? next : document))
    : [...documents, next];
}

export function ProjectDocumentWorkspace({
  caseId,
  documents,
  disabled = false,
  onChanged,
  selectedDocumentId,
  onSelectedDocumentChange,
  onDirtyChange,
  compact = false,
}: {
  caseId: string;
  documents: AuditCaseDocumentRead[];
  disabled?: boolean;
  onChanged?: () => Promise<void> | void;
  selectedDocumentId?: string;
  onSelectedDocumentChange?: (id: string) => void;
  onDirtyChange?: (dirty: boolean) => void;
  compact?: boolean;
}) {
  const [localDocuments, setLocalDocuments] = useState(documents);
  const [internalSelectedId, setInternalSelectedId] = useState(documents[0]?.id || '');
  const selectedId = selectedDocumentId ?? internalSelectedId;
  const setSelectedId = (id: string) => {
    if (dirtyRef.current && !window.confirm('有未保存修改，确定放弃并切换文件吗？')) return;
    if (selectedDocumentId === undefined) setInternalSelectedId(id);
    onSelectedDocumentChange?.(id);
  };
  const [detail, setDetail] = useState<AuditCaseDocumentDetailRead | null>(null);
  const [content, setContent] = useState('');
  const [dirty, setDirty] = useState(false);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<'create' | 'save' | 'archive' | ''>('');
  const [error, setError] = useState('');
  const [newDocument, setNewDocument] = useState<AuditCaseDocumentCreateRequest>({
    document_key: '',
    title: '',
    document_type: 'work_document',
    zone: 'workspace',
    content_format: 'markdown',
    content: '',
  });
  const [archiveReason, setArchiveReason] = useState('文档内容已确认');
  const dirtyRef = useRef(false);
  const detailRequestRef = useRef(0);

  useEffect(() => { onDirtyChange?.(dirty || Boolean(newDocument.title || newDocument.document_key || newDocument.content)); }, [dirty, newDocument.title, newDocument.document_key, newDocument.content, onDirtyChange]);

  useEffect(() => {
    setLocalDocuments(documents);
    setInternalSelectedId((current) => documents.some((document) => document.id === current) ? current : documents[0]?.id || '');
  }, [documents]);

  const selectedDocument = useMemo(
    () => localDocuments.find((document) => document.id === selectedId) || null,
    [localDocuments, selectedId],
  );

  useEffect(() => {
    if (!selectedDocument) {
      setDetail(null);
      setContent('');
      return;
    }
    let active = true;
    const requestId = ++detailRequestRef.current;
    dirtyRef.current = false;
    setDirty(false);
    setError('');
    const seed: AuditCaseDocumentDetailRead = {
      document: selectedDocument,
      versions: selectedDocument.active_version ? [selectedDocument.active_version] : [],
    };
    setDetail(seed);
    setContent(selectedDocument.active_version?.content || '');
    setLoading(true);
    void loadAuditCaseDocument(caseId, selectedDocument.id)
      .then((loaded) => {
        if (!active || requestId !== detailRequestRef.current) return;
        setDetail(loaded);
        if (!dirtyRef.current) {
          const version = latestVersion(loaded);
          setContent(version?.content || '');
        }
      })
      .catch((loadError) => {
        if (active) setError(documentErrorMessage(loadError, '加载文档失败'));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [caseId, selectedId]);

  async function handleCreate() {
    if (disabled || busy) return;
    if (dirtyRef.current && !window.confirm('有未保存修改，确定放弃并切换文件吗？')) return;
    if (!newDocument.document_key.trim() || !newDocument.title.trim()) {
      setError('请填写文档标识和文档标题');
      return;
    }
    setBusy('create');
    setError('');
    try {
      const created = await createAuditCaseDocument(caseId, {
        ...newDocument,
        document_key: newDocument.document_key.trim(),
        title: newDocument.title.trim(),
      });
      setLocalDocuments((current) => replaceDocument(current, created));
      dirtyRef.current = false;
      setDirty(false);
      setSelectedId(created.id);
      setNewDocument((current) => ({ ...current, document_key: '', title: '', content: '' }));
      await onChanged?.();
      notify.success('工作文档已创建');
    } catch (createError) {
      setError(documentErrorMessage(createError, '创建工作文档失败'));
    } finally {
      setBusy('');
    }
  }

  async function handleSave() {
    if (disabled || busy || !detail || detail.document.status === 'archived') return;
    const current = latestVersion(detail);
    if (!current) {
      setError('当前文档没有可用版本');
      return;
    }
    detailRequestRef.current += 1;
    setBusy('save');
    setError('');
    try {
      const version = await createAuditCaseDocumentVersion(caseId, detail.document.id, {
        expected_version: current.version,
        content_format: current.content_format === 'text' ? 'text' : 'markdown',
        content,
      });
      const nextDocument: AuditCaseDocumentRead = {
        ...detail.document,
        active_version_id: version.id,
        updated_at: version.created_at,
        active_version: version,
      };
      setDetail({ document: nextDocument, versions: [...detail.versions, version] });
      setLocalDocuments((currentDocuments) => replaceDocument(currentDocuments, nextDocument));
      dirtyRef.current = false;
      setDirty(false);
      await onChanged?.();
      notify.success('文档新版本已保存');
    } catch (saveError) {
      setError(documentErrorMessage(saveError, '保存文档版本失败'));
    } finally {
      setBusy('');
    }
  }

  async function handleArchive() {
    if (disabled || busy || !detail || detail.document.status === 'archived') return;
    if (dirtyRef.current) { setError('请先保存内容，再冻结文档'); return; }
    detailRequestRef.current += 1;
    setBusy('archive');
    setError('');
    try {
      const archived = await archiveAuditCaseDocument(caseId, detail.document.id, archiveReason.trim() || '文档内容已确认');
      const nextDocument = { ...archived, active_version: detail.document.active_version };
      setDetail((current) => current ? { ...current, document: nextDocument } : current);
      setLocalDocuments((current) => replaceDocument(current, nextDocument));
      await onChanged?.();
      notify.success('文档已冻结');
    } catch (archiveError) {
      setError(documentErrorMessage(archiveError, '冻结文档失败'));
    } finally {
      setBusy('');
    }
  }

  const groupedDocuments = useMemo(() => {
    const groups = new Map<string, AuditCaseDocumentRead[]>();
    localDocuments.forEach((document) => groups.set(document.zone, [...(groups.get(document.zone) || []), document]));
    return [...groups.entries()];
  }, [localDocuments]);
  const currentVersion = latestVersion(detail);
  const readOnly = disabled || selectedDocument?.status === 'archived';

  return (
    <div className="grid gap-[16px]">
      {error && <p role="alert" className="rounded-[9px] bg-[#fff1f1] px-[12px] py-[10px] text-[12px] text-[#c20d0d]">{error}</p>}
      {disabled && <p className="rounded-[9px] bg-[#f5f6f8] px-[12px] py-[10px] text-[12px] text-[#858b9c]">当前文件只读：请检查项目角色、归档或提交状态</p>}

      {!compact && <Card>
        <CardContent className="grid gap-[10px] p-[14px]">
          <div>
            <h2 className="text-[14px] font-medium text-[#464c5e]">新建工作文档</h2>
            <p className="mt-[3px] text-[11px] text-[#a0a6b5]">工作文档与原始审核材料分离，保存时只新增版本。</p>
          </div>
          <div className="grid gap-[8px] md:grid-cols-2">
            <label className="grid gap-[4px] text-[12px] text-[#464c5e]">文档标识<Input aria-label="文档标识" value={newDocument.document_key} disabled={disabled || Boolean(busy)} onChange={(event) => { const { value } = event.currentTarget; setNewDocument((current) => ({ ...current, document_key: value })); }} /></label>
            <label className="grid gap-[4px] text-[12px] text-[#464c5e]">文档标题<Input aria-label="文档标题" value={newDocument.title} disabled={disabled || Boolean(busy)} onChange={(event) => { const { value } = event.currentTarget; setNewDocument((current) => ({ ...current, title: value })); }} /></label>
            <label className="grid gap-[4px] text-[12px] text-[#464c5e]">文档类型<Input aria-label="文档类型" value={newDocument.document_type} disabled={disabled || Boolean(busy)} onChange={(event) => { const { value } = event.currentTarget; setNewDocument((current) => ({ ...current, document_type: value })); }} /></label>
            <label className="grid gap-[4px] text-[12px] text-[#464c5e]">所属分区<Input aria-label="所属分区" value={newDocument.zone} disabled={disabled || Boolean(busy)} onChange={(event) => { const { value } = event.currentTarget; setNewDocument((current) => ({ ...current, zone: value })); }} /></label>
          </div>
          <label className="grid gap-[4px] text-[12px] text-[#464c5e]">初始内容<Textarea aria-label="初始内容" value={newDocument.content} disabled={disabled || Boolean(busy)} onChange={(event) => { const { value } = event.currentTarget; setNewDocument((current) => ({ ...current, content: value })); }} /></label>
          <Button type="button" disabled={disabled || Boolean(busy)} onClick={() => void handleCreate()}>{busy === 'create' ? '创建中…' : '创建文档'}</Button>
        </CardContent>
      </Card>}

      <div className={compact ? 'grid gap-[12px]' : 'grid gap-[12px] lg:grid-cols-[minmax(180px,0.7fr)_minmax(0,2fr)]'}>
        {!compact && <Card>
          <CardContent className="grid gap-[10px] p-[14px]">
            <div><h2 className="text-[14px] font-medium text-[#464c5e]">文档分区</h2><p className="mt-[3px] text-[11px] text-[#a0a6b5]">原始材料不会被工作文档覆盖。</p></div>
            {!groupedDocuments.length && <p className="rounded-[9px] border border-dashed border-[#e1e5ed] px-[10px] py-[14px] text-center text-[12px] text-[#858b9c]">暂无工作文档</p>}
            {groupedDocuments.map(([zone, items]) => (
              <section key={zone} className="grid gap-[6px]"><h3 className="text-[11px] font-medium text-[#858b9c]">{zone}</h3>{items.map((document) => <button key={document.id} type="button" className={`rounded-[8px] border px-[10px] py-[9px] text-left ${document.id === selectedId ? 'border-[#18181a] bg-[#f7f8fb]' : 'border-[#edf0f5]'}`} onClick={() => setSelectedId(document.id)}><span className="block truncate text-[12px] text-[#464c5e]">{document.title}</span><span className="mt-[3px] block text-[10px] text-[#858b9c]">{document.status === 'archived' ? '已冻结' : '可编辑'}</span></button>)}</section>
            ))}
          </CardContent>
        </Card>}

        <Card>
          <CardContent className="grid gap-[10px] p-[14px]">
            {!selectedDocument || !detail ? <p className="rounded-[9px] border border-dashed border-[#e1e5ed] px-[10px] py-[24px] text-center text-[12px] text-[#858b9c]">请选择一个工作文档</p> : (
              <>
                <div className="flex flex-wrap items-start justify-between gap-[10px]"><div><h2 className="text-[14px] font-medium text-[#464c5e]">{detail.document.title}</h2><p className="mt-[3px] text-[11px] text-[#858b9c]">{detail.document.document_type} · {detail.document.zone} · {currentVersion ? `版本 ${currentVersion.version}` : '暂无版本'}{loading ? ' · 加载中…' : ''}</p></div><div className="flex gap-[7px]"><Button type="button" variant="outline" disabled={Boolean(readOnly || busy)} onClick={() => void handleSave()}>{busy === 'save' ? '保存中…' : '保存新版本'}</Button><Button type="button" variant="outline" disabled={Boolean(readOnly || busy)} onClick={() => void handleArchive()}>{busy === 'archive' ? '冻结中…' : '冻结文档'}</Button></div></div>
                {detail.document.status === 'archived' && <p className="rounded-[9px] bg-[#f5f6f8] px-[10px] py-[8px] text-[11px] text-[#858b9c]">文档已冻结，历史版本仍可查看。</p>}
                <label className="grid gap-[4px] text-[12px] text-[#464c5e]">文档内容<Textarea aria-label="文档内容" value={content} disabled={Boolean(readOnly || busy)} onChange={(event) => { dirtyRef.current = true; setDirty(true); setContent(event.currentTarget.value); }} className="min-h-[320px] font-mono text-[12px]" /></label>
                <label className="grid gap-[4px] text-[12px] text-[#464c5e]">冻结原因<Input aria-label="冻结原因" value={archiveReason} disabled={Boolean(readOnly || busy)} onChange={(event) => setArchiveReason(event.currentTarget.value)} /></label>
                <div className="flex flex-wrap gap-[6px] text-[11px] text-[#858b9c]"><span>版本数量：{detail.versions.length}</span><span>当前字符：{content.length.toLocaleString()}</span><span>{dirty ? '有未保存修改' : '已保存'}</span></div>
              </>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
