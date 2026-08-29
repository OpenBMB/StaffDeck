import { useEffect, useState } from 'react';

import type { EmbeddingDraft } from './retrievalSettings';
import { FormField, fieldClassName, RetrievalSettingsDialog } from './RetrievalSettingsDialog';

export function EmbeddingSettingsDialog({
  open,
  value,
  onOpenChange,
  onChange,
  onTest,
  capabilities,
}: {
  open: boolean;
  value: EmbeddingDraft;
  onOpenChange: (open: boolean) => void;
  onChange: (value: EmbeddingDraft) => void;
  onTest?: (value: EmbeddingDraft) => void;
  capabilities?: Record<string, unknown>;
}) {
  const [local, setLocal] = useState(value);
  useEffect(() => { if (open) setLocal(value); }, [open, value]);
  const updateOptions = (patch: Partial<EmbeddingDraft['options']>) => setLocal((current) => ({ ...current, options: { ...current.options, ...patch } }));
  const adapterRows = Array.isArray(capabilities?.embedding)
    ? capabilities.embedding.filter((item): item is Record<string, any> => Boolean(item) && typeof item === 'object')
    : [];
  const adapterOptions = adapterRows.length ? adapterRows : [{ id: local.adapter, label: local.adapter }];
  const selectedCapability = adapterRows.find((item) => item.id === local.adapter);
  const fields = selectedCapability?.fields && typeof selectedCapability.fields === 'object'
    ? selectedCapability.fields as Record<string, any>
    : {};
  const dimensionOptions = Array.isArray(fields.dimensions?.options) ? fields.dimensions.options : [];
  const maxBatchSize = fields.batch_size?.max ?? selectedCapability?.limits?.max_batch_size ?? 128;
  const maxInputTokens = fields.max_input_tokens?.max ?? selectedCapability?.limits?.max_input_tokens;
  return (
    <RetrievalSettingsDialog
      title="Embedding 向量化设置"
      description="Embedding 身份、维度或扩展参数变化会创建新的待重建索引，不会立即替换正在使用的索引。"
      open={open}
      onOpenChange={onOpenChange}
      onTest={onTest ? () => onTest(local) : undefined}
      onApply={() => onChange(local)}
    >
      <div className="grid gap-[14px] md:grid-cols-2">
        <FormField label="适配器"><select className={fieldClassName} value={local.adapter} onChange={(e) => setLocal({ ...local, adapter: e.target.value })}>{adapterOptions.map((item) => <option key={String(item.id)} value={String(item.id)}>{String(item.label ?? item.id)}</option>)}</select></FormField>
        <FormField label="Embedding 地址"><input className={fieldClassName} value={local.baseUrl} onChange={(e) => setLocal({ ...local, baseUrl: e.target.value })} placeholder="https://open.bigmodel.cn/api/paas/v4" /></FormField>
        <FormField label="Embedding 模型"><input className={fieldClassName} value={local.model} onChange={(e) => setLocal({ ...local, model: e.target.value })} /></FormField>
        <FormField label="API Key（只写入）" hint={local.apiKeyMasked ? `已配置 ${local.apiKeyMasked}，留空保持不变` : '不会回显已保存密钥'}><input type="password" autoComplete="new-password" className={fieldClassName} value={local.apiKey} onChange={(e) => setLocal({ ...local, apiKey: e.target.value })} /></FormField>
        <FormField label="维度模式"><select className={fieldClassName} value={local.options.dimension_mode} onChange={(e) => updateOptions({ dimension_mode: e.target.value as 'auto' | 'explicit' })}><option value="explicit">显式维度</option><option value="auto">首次测试自动识别</option></select></FormField>
        <FormField label="向量维度" hint={dimensionOptions.length ? `当前适配器支持：${dimensionOptions.join(' / ')}；新建智谱 Embedding-3 默认 1024。` : undefined}>{dimensionOptions.length ? <select className={fieldClassName} value={local.options.dimensions ?? ''} disabled={local.options.dimension_mode === 'auto'} onChange={(e) => updateOptions({ dimensions: e.target.value ? Number(e.target.value) : null })}>{dimensionOptions.map((item: unknown) => <option key={String(item)} value={String(item)}>{String(item)}</option>)}</select> : <input type="number" className={fieldClassName} min="1" value={local.options.dimensions ?? ''} disabled={local.options.dimension_mode === 'auto'} onChange={(e) => updateOptions({ dimensions: e.target.value ? Number(e.target.value) : null })} />}</FormField>
        <FormField label="批量大小"><input type="number" className={fieldClassName} min="1" max={maxBatchSize} value={local.options.batch_size} onChange={(e) => updateOptions({ batch_size: Number(e.target.value) })} /></FormField>
        <FormField label="超时（秒）"><input type="number" className={fieldClassName} min="5" max="600" value={local.options.timeout_seconds} onChange={(e) => updateOptions({ timeout_seconds: Number(e.target.value) })} /></FormField>
        <FormField label="最大重试次数"><input type="number" className={fieldClassName} min="0" max="5" value={local.options.max_retries} onChange={(e) => updateOptions({ max_retries: Number(e.target.value) })} /></FormField>
        <FormField label="重试退避（毫秒）"><input type="number" className={fieldClassName} min="100" max="10000" value={local.options.retry_backoff_ms} onChange={(e) => updateOptions({ retry_backoff_ms: Number(e.target.value) })} /></FormField>
        <FormField label="最大输入 Token"><input type="number" className={fieldClassName} min="1" max={maxInputTokens} value={local.options.max_input_tokens ?? ''} onChange={(e) => updateOptions({ max_input_tokens: e.target.value ? Number(e.target.value) : null })} /></FormField>
        <FormField label="超长文本策略"><select className={fieldClassName} value={local.options.oversize_policy} onChange={(e) => updateOptions({ oversize_policy: e.target.value as 'fail' | 'safe_truncate' })}><option value="fail">失败并保留原文</option><option value="safe_truncate">安全截断后继续</option></select></FormField>
        <FormField label="相似度下限"><input type="number" step="0.01" className={fieldClassName} min="-1" max="1" value={local.options.similarity_threshold} onChange={(e) => updateOptions({ similarity_threshold: Number(e.target.value) })} /></FormField>
      </div>
    </RetrievalSettingsDialog>
  );
}
