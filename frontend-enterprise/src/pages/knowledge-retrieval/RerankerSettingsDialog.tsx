import { useEffect, useState } from 'react';

import type { RerankerDraft } from './retrievalSettings';
import { FormField, fieldClassName, RetrievalSettingsDialog } from './RetrievalSettingsDialog';

export function RerankerSettingsDialog({ open, value, onOpenChange, onChange, onTest }: { open: boolean; value: RerankerDraft; onOpenChange: (open: boolean) => void; onChange: (value: RerankerDraft) => void; onTest?: (value: RerankerDraft) => void }) {
  const [local, setLocal] = useState(value);
  useEffect(() => { if (open) setLocal(value); }, [open, value]);
  const update = (patch: Partial<RerankerDraft['options']>) => setLocal((current) => ({ ...current, options: { ...current.options, ...patch } }));
  return (
    <RetrievalSettingsDialog title="Reranker 重排设置" description="Reranker 使用独立的连接、模型和 API Key；切换 BM25/向量/Reranker 参数属于热应用，不会重建 Embedding 索引。" open={open} onOpenChange={onOpenChange} onTest={onTest ? () => onTest(local) : undefined} onApply={() => onChange(local)}>
      <div className="grid gap-[14px] md:grid-cols-2">
        <FormField label="重排模式"><select className={fieldClassName} value={local.options.mode} onChange={(e) => { const mode = e.target.value as RerankerDraft['options']['mode']; setLocal((current) => ({ ...current, adapter: mode === 'dedicated_api' ? 'zhipu_rerank' : 'llm_rerank', options: { ...current.options, mode } })); }}><option value="none">不重排</option><option value="dedicated_api">独立 Rerank API</option><option value="llm">对话/生成模型重排</option></select></FormField>
        <FormField label="适配器"><select className={fieldClassName} value={local.adapter} onChange={(e) => setLocal({ ...local, adapter: e.target.value })}><option value="zhipu_rerank">智谱 Rerank</option><option value="generic_rerank">通用 Rerank API</option><option value="llm_rerank">对话模型 JSON 重排</option></select></FormField>
        <FormField label="Reranker 地址"><input className={fieldClassName} value={local.baseUrl} disabled={local.options.mode === 'none'} onChange={(e) => setLocal({ ...local, baseUrl: e.target.value })} placeholder="https://open.bigmodel.cn/api/paas/v4" /></FormField>
        <FormField label="Reranker 模型"><input className={fieldClassName} value={local.model} disabled={local.options.mode === 'none'} onChange={(e) => setLocal({ ...local, model: e.target.value })} placeholder="rerank-model 或对话模型名" /></FormField>
        <FormField label="API Key（只写入）" hint={local.apiKeyMasked ? `已配置 ${local.apiKeyMasked}，留空保持不变` : undefined}><input type="password" autoComplete="new-password" className={fieldClassName} value={local.apiKey} disabled={local.options.mode === 'none'} onChange={(e) => setLocal({ ...local, apiKey: e.target.value })} /></FormField>
        <FormField label="候选数量"><input type="number" className={fieldClassName} min="1" max="128" value={local.options.candidate_limit} onChange={(e) => update({ candidate_limit: Number(e.target.value) })} /></FormField>
        <FormField label="重排数量" hint="默认 12；必须不大于候选数量"><input type="number" className={fieldClassName} min="1" max="128" value={local.options.rerank_limit} onChange={(e) => update({ rerank_limit: Number(e.target.value) })} /></FormField>
        <FormField label="最低相关性分数"><input type="number" step="0.01" className={fieldClassName} min="0" max="1" value={local.options.minimum_score} onChange={(e) => update({ minimum_score: Number(e.target.value) })} /></FormField>
        <FormField label="查询最大字符数"><input type="number" className={fieldClassName} min="1" value={local.options.max_query_chars} onChange={(e) => update({ max_query_chars: Number(e.target.value) })} /></FormField>
        <FormField label="候选片段最大字符数"><input type="number" className={fieldClassName} min="200" value={local.options.max_document_chars} onChange={(e) => update({ max_document_chars: Number(e.target.value) })} /></FormField>
        <FormField label="超时（秒）"><input type="number" className={fieldClassName} min="5" max="600" value={local.options.timeout_seconds} onChange={(e) => update({ timeout_seconds: Number(e.target.value) })} /></FormField>
        <FormField label="最大重试次数"><input type="number" className={fieldClassName} min="0" max="5" value={local.options.max_retries} onChange={(e) => update({ max_retries: Number(e.target.value) })} /></FormField>
        <FormField label="重试退避（毫秒）"><input type="number" className={fieldClassName} min="100" max="10000" value={local.options.retry_backoff_ms} onChange={(e) => update({ retry_backoff_ms: Number(e.target.value) })} /></FormField>
        {local.options.mode === 'llm' ? <>
          <FormField label="Temperature"><input type="number" step="0.1" className={fieldClassName} min="0" max="2" value={local.options.temperature} onChange={(e) => update({ temperature: Number(e.target.value) })} /></FormField>
          <FormField label="最大输出 Token"><input type="number" className={fieldClassName} min="1" value={local.options.max_output_tokens} onChange={(e) => update({ max_output_tokens: Number(e.target.value) })} /></FormField>
          <FormField label="输入预算 Token"><input type="number" className={fieldClassName} min="1" value={local.options.input_budget_tokens} onChange={(e) => update({ input_budget_tokens: Number(e.target.value) })} /></FormField>
          <FormField label="重排提示词模板"><textarea className="min-h-[130px] rounded-[8px] border border-[#e3e7f1] p-[10px] text-[12px] text-[#18181a] outline-none" value={local.options.prompt_template} onChange={(e) => update({ prompt_template: e.target.value })} placeholder="留空使用安全内置模板" /></FormField>
        </> : null}
        {local.options.mode === 'dedicated_api' ? <>
          <label className="flex items-center gap-[8px] text-[12px] text-[#5b6273]"><input type="checkbox" checked={local.options.return_documents} onChange={(e) => update({ return_documents: e.target.checked })} />要求接口回传文档</label>
          <label className="flex items-center gap-[8px] text-[12px] text-[#5b6273]"><input type="checkbox" checked={local.options.return_raw_scores} onChange={(e) => update({ return_raw_scores: e.target.checked })} />要求接口回传原始分数</label>
        </> : null}
      </div>
    </RetrievalSettingsDialog>
  );
}
