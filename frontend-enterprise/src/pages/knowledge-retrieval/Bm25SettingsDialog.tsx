import { useEffect, useState } from 'react';

import type { Bm25Draft } from './retrievalSettings';
import { FormField, fieldClassName, RetrievalSettingsDialog } from './RetrievalSettingsDialog';

export function Bm25SettingsDialog({ open, value, onOpenChange, onChange }: { open: boolean; value: Bm25Draft; onOpenChange: (open: boolean) => void; onChange: (value: Bm25Draft) => void }) {
  const [local, setLocal] = useState(value);
  useEffect(() => { if (open) setLocal(value); }, [open, value]);
  const update = (patch: Partial<Bm25Draft['options']>) => setLocal((current) => ({ ...current, options: { ...current.options, ...patch } }));
  return (
    <RetrievalSettingsDialog title="BM25 关键词检索设置" description="默认值保持现有 StaffDeck 的中文双字组、标识符保留和正文检索行为。" open={open} onOpenChange={onOpenChange} onApply={() => onChange(local)}>
      <div className="grid gap-[14px] md:grid-cols-2">
        <label className="flex items-center gap-[8px] text-[12px] text-[#5b6273] md:col-span-2"><input type="checkbox" checked={local.options.enabled} onChange={(e) => update({ enabled: e.target.checked })} />启用 BM25</label>
        <FormField label="k1（词频饱和）"><input type="number" step="0.05" className={fieldClassName} min="0.1" max="3" value={local.options.k1} onChange={(e) => update({ k1: Number(e.target.value) })} /></FormField>
        <FormField label="b（长度归一化）"><input type="number" step="0.05" className={fieldClassName} min="0" max="1" value={local.options.b} onChange={(e) => update({ b: Number(e.target.value) })} /></FormField>
        <FormField label="候选数量"><input type="number" className={fieldClassName} min="1" max="500" value={local.options.candidate_limit} onChange={(e) => update({ candidate_limit: Number(e.target.value) })} /></FormField>
        <FormField label="最低分数"><input type="number" step="0.01" className={fieldClassName} min="0" value={local.options.minimum_score} onChange={(e) => update({ minimum_score: Number(e.target.value) })} /></FormField>
        <FormField label="分词器"><select className={fieldClassName} value={local.options.tokenizer} onChange={(e) => update({ tokenizer: e.target.value as 'cjk_bigram_latin' })}><option value="cjk_bigram_latin">中文 N-gram + 拉丁标识符</option></select></FormField>
        <FormField label="中文 N-gram 长度"><input type="number" className={fieldClassName} min="1" max="3" value={local.options.cjk_ngram} onChange={(e) => update({ cjk_ngram: Number(e.target.value) })} /></FormField>
        <label className="flex items-center gap-[8px] text-[12px] text-[#5b6273]"><input type="checkbox" checked={local.options.preserve_identifiers} onChange={(e) => update({ preserve_identifiers: e.target.checked })} />保留 GB/T、ISO、版本号等标识符</label>
        <label className="flex items-center gap-[8px] text-[12px] text-[#5b6273]"><input type="checkbox" checked={local.options.deduplicate_query_terms} onChange={(e) => update({ deduplicate_query_terms: e.target.checked })} />去重查询词</label>
        <label className="flex items-center gap-[8px] text-[12px] text-[#5b6273]"><input type="checkbox" checked={local.options.stopwords_enabled} onChange={(e) => update({ stopwords_enabled: e.target.checked })} />启用停用词</label>
        <FormField label="自定义停用词" hint="使用逗号、空格或换行分隔"><textarea className="min-h-[70px] rounded-[8px] border border-[#e3e7f1] p-[10px] text-[12px] text-[#18181a] outline-none" value={local.options.stopwords.join(', ')} onChange={(e) => update({ stopwords: e.target.value.split(/[\s,，]+/).filter(Boolean) })} /></FormField>
        <FormField label="正文权重"><input type="number" step="0.1" className={fieldClassName} min="0" max="10" value={local.options.content_weight} onChange={(e) => update({ content_weight: Number(e.target.value) })} /></FormField>
        <FormField label="摘要权重"><input type="number" step="0.1" className={fieldClassName} min="0" max="10" value={local.options.summary_weight} onChange={(e) => update({ summary_weight: Number(e.target.value) })} /></FormField>
        <FormField label="来源引用权重"><input type="number" step="0.1" className={fieldClassName} min="0" max="10" value={local.options.source_ref_weight} onChange={(e) => update({ source_ref_weight: Number(e.target.value) })} /></FormField>
      </div>
    </RetrievalSettingsDialog>
  );
}
