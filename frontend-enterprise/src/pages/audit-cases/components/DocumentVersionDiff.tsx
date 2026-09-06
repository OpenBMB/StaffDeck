import { useEffect, useState } from 'react';
import type { AuditCaseDocumentVersionRead } from '@/types';
import { loadAuditCaseDocument } from '../auditCaseApi';

export function lineDiff(before: string, after: string): { kind: 'same' | 'removed' | 'added'; text: string }[] {
  const left = before.split('\n'); const right = after.split('\n');
  // A bounded LCS keeps long documents responsive; the fallback still shows full before/after.
  if (left.length * right.length > 250000) return [...left.map((text) => ({ kind: 'removed' as const, text })), ...right.map((text) => ({ kind: 'added' as const, text }))];
  const table = Array.from({ length: left.length + 1 }, () => new Uint32Array(right.length + 1));
  for (let i = left.length - 1; i >= 0; i--) for (let j = right.length - 1; j >= 0; j--) table[i][j] = left[i] === right[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
  const result: ReturnType<typeof lineDiff> = []; let i = 0; let j = 0;
  while (i < left.length || j < right.length) {
    if (i < left.length && j < right.length && left[i] === right[j]) { result.push({ kind: 'same', text: left[i++] }); j++; }
    else if (j < right.length && (i === left.length || table[i][j + 1] >= table[i + 1][j])) result.push({ kind: 'added', text: right[j++] });
    else result.push({ kind: 'removed', text: left[i++] });
  }
  return result;
}

export function DocumentVersionDiff({ caseId, documentId, approvedVersionId }: { caseId: string; documentId: string; approvedVersionId?: string | null }) {
  const [versions, setVersions] = useState<AuditCaseDocumentVersionRead[]>([]);
  const [beforeId, setBeforeId] = useState(''); const [afterId, setAfterId] = useState(''); const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    setVersions([]); setError('');
    void loadAuditCaseDocument(caseId, documentId).then((detail) => {
      if (!active) return;
      const values = [...detail.versions].sort((a, b) => a.version - b.version);
      setVersions(values); setBeforeId(approvedVersionId || values[Math.max(0, values.length - 2)]?.id || ''); setAfterId(values[values.length - 1]?.id || '');
    }).catch((e) => { if (active) setError(e instanceof Error ? e.message : '加载版本失败'); });
    return () => { active = false; };
  }, [caseId, documentId, approvedVersionId]);
  const before = versions.find((v) => v.id === beforeId); const after = versions.find((v) => v.id === afterId);
  return <section className="wb-card"><h2>版本差异</h2>{error && <p role="alert">{error}</p>}<div className="wb-actions">
    <label>比较基线<select aria-label="比较基线" value={beforeId} onChange={(e) => setBeforeId(e.target.value)}>{versions.map((v) => <option key={v.id} value={v.id}>v{v.version}{v.id === approvedVersionId ? ' · 已审批快照' : ''}</option>)}</select></label>
    <label>比较目标<select aria-label="比较目标" value={afterId} onChange={(e) => setAfterId(e.target.value)}>{versions.map((v) => <option key={v.id} value={v.id}>v{v.version}</option>)}</select></label>
  </div><p className="wb-muted">红色 − 删除；绿色 + 新增。比较保存版本，不包含未保存内容。</p>
    {before && after && <pre className="wb-diff">{lineDiff(before.content, after.content).map((line, index) => <span key={index} className={`wb-diff-${line.kind}`}>{line.kind === 'added' ? '+ ' : line.kind === 'removed' ? '− ' : '  '}{line.text}{'\n'}</span>)}</pre>}
  </section>;
}
