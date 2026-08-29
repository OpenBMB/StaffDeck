import { useEffect, useState } from 'react';

import type { FusionDraft } from './retrievalSettings';
import { FormField, fieldClassName, RetrievalSettingsDialog } from './RetrievalSettingsDialog';

export function FusionSettingsDialog({ open, value, onOpenChange, onChange }: { open: boolean; value: FusionDraft; onOpenChange: (open: boolean) => void; onChange: (value: FusionDraft) => void }) {
  const [local, setLocal] = useState(value);
  useEffect(() => { if (open) setLocal(value); }, [open, value]);
  const update = (patch: Partial<FusionDraft['options']>) => setLocal((current) => ({ ...current, options: { ...current.options, ...patch } }));
  return (
    <RetrievalSettingsDialog title="融合与候选集设置" description="BM25 和向量分支先独立召回，再按加权 RRF 合并并去重；这里的修改不需要重建向量。" open={open} onOpenChange={onOpenChange} onApply={() => onChange(local)}>
      <div className="grid gap-[14px] md:grid-cols-2">
        <FormField label="检索模式"><select className={fieldClassName} value={local.options.mode} onChange={(e) => update({ mode: e.target.value as FusionDraft['options']['mode'] })}><option value="hybrid">混合（BM25 + 向量）</option><option value="bm25">仅 BM25</option><option value="vector">仅向量</option></select></FormField>
        <FormField label="RRF k"><input type="number" className={fieldClassName} min="1" max="1000" value={local.options.rrf_k} onChange={(e) => update({ rrf_k: Number(e.target.value) })} /></FormField>
        <FormField label="BM25 分支召回数"><input type="number" className={fieldClassName} min="1" max="500" value={local.options.bm25_limit} onChange={(e) => update({ bm25_limit: Number(e.target.value) })} /></FormField>
        <FormField label="向量分支召回数"><input type="number" className={fieldClassName} min="1" max="500" value={local.options.vector_limit} onChange={(e) => update({ vector_limit: Number(e.target.value) })} /></FormField>
        <FormField label="BM25 权重"><input type="number" step="0.1" className={fieldClassName} min="0" max="10" value={local.options.bm25_weight} onChange={(e) => update({ bm25_weight: Number(e.target.value) })} /></FormField>
        <FormField label="向量权重"><input type="number" step="0.1" className={fieldClassName} min="0" max="10" value={local.options.vector_weight} onChange={(e) => update({ vector_weight: Number(e.target.value) })} /></FormField>
        <FormField label="最终候选数"><input type="number" className={fieldClassName} min="1" max="500" value={local.options.final_limit} onChange={(e) => update({ final_limit: Number(e.target.value) })} /></FormField>
        <p className="m-0 rounded-[8px] bg-[#f7f9fc] p-[10px] text-[12px] leading-[1.6] text-[#858b9c] md:col-span-2">固定规则：相同 chunk_id 只保留一个候选；RRF 权重只影响排序，不会复制文本或改变原始知识库内容。</p>
      </div>
    </RetrievalSettingsDialog>
  );
}
