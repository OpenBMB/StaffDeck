import { useEffect, useState, type ReactNode } from 'react';
import { AlertCircle, CheckCircle2, ExternalLink, FlaskConical, Settings2 } from 'lucide-react';

import { api } from '@/api/client';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Input,
} from '@/components/ui';
import { Button } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import type { ModelConfigRead } from '@/types';

type ModelSetupDialogProps = {
  open: boolean;
  tenantId: string;
  canConfigure: boolean;
  onOpenChange: (open: boolean) => void;
  onConfigured: (model: ModelConfigRead) => void;
};

type ModelSetupForm = {
  name: string;
  provider: string;
  baseUrl: string;
  model: string;
  apiKey: string;
  temperature: string;
  maxOutputTokens: string;
};

type TestResult = {
  success: boolean;
  message: string;
} | null;

const INITIAL_FORM: ModelSetupForm = {
  name: '默认模型',
  provider: 'openai_compatible',
  baseUrl: '',
  model: '',
  apiKey: '',
  temperature: '0.2',
  maxOutputTokens: '8192',
};

export default function ModelSetupDialog({
  open,
  tenantId,
  canConfigure,
  onOpenChange,
  onConfigured,
}: ModelSetupDialogProps) {
  const [form, setForm] = useState<ModelSetupForm>(INITIAL_FORM);
  const [savedModelId, setSavedModelId] = useState('');
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<TestResult>(null);

  useEffect(() => {
    if (!open) return;
    setForm(INITIAL_FORM);
    setSavedModelId('');
    setTesting(false);
    setTestResult(null);
  }, [open]);

  const updateForm = <K extends keyof ModelSetupForm>(key: K, value: ModelSetupForm[K]) => {
    setForm((current) => ({ ...current, [key]: value }));
    setTestResult(null);
  };

  async function saveAndTest() {
    const name = form.name.trim();
    const provider = form.provider.trim();
    const model = form.model.trim();
    if (!name || !provider || !model) {
      notify.error('请填写配置名称、Provider 和 Model');
      return;
    }
    const temperature = Number(form.temperature);
    const maxOutputTokens = Number(form.maxOutputTokens);
    if (!Number.isFinite(temperature) || !Number.isFinite(maxOutputTokens)) {
      notify.error('Temperature 与 Max Tokens 必须是数字');
      return;
    }

    setTesting(true);
    setTestResult(null);
    try {
      const payload = {
        tenant_id: tenantId,
        name,
        provider,
        base_url: form.baseUrl.trim() || undefined,
        api_key: form.apiKey || undefined,
        model,
        temperature,
        max_output_tokens: maxOutputTokens,
        is_default: false,
        enabled: false,
      };
      const saved = savedModelId
        ? await api.put<ModelConfigRead>(`/api/enterprise/model-configs/${savedModelId}`, payload)
        : await api.post<ModelConfigRead>('/api/enterprise/model-configs', payload);
      setSavedModelId(saved.id);

      const result = await api.post<{ success: boolean; message: string; output?: string }>(
        `/api/enterprise/model-configs/${saved.id}/test?tenant_id=${encodeURIComponent(tenantId)}`,
      );
      if (!result.success) {
        setTestResult({ success: false, message: result.message || '模型连接失败，请检查配置后重试。' });
        return;
      }

      const activated = await api.put<ModelConfigRead>(`/api/enterprise/model-configs/${saved.id}`, {
        ...payload,
        api_key: undefined,
        is_default: true,
        enabled: true,
      });
      setTestResult({ success: true, message: result.output || result.message || '模型连接成功。' });
      onConfigured(activated);
    } catch (error) {
      setTestResult({
        success: false,
        message: error instanceof Error ? error.message : '模型保存或连接测试失败，请检查配置后重试。',
      });
    } finally {
      setTesting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[calc(100dvh-32px)] overflow-y-auto sm:max-w-[680px]">
        <DialogHeader>
          <div className="mb-[8px] grid size-[40px] place-items-center rounded-[8px] bg-[#f2f6ff] text-[#1a71ff]">
            <Settings2 className="size-[20px]" />
          </div>
          <DialogTitle>需要先配置模型</DialogTitle>
          <DialogDescription>
            当前没有可用模型。完成配置并通过连通性测试后，才能发送对话和执行任务。
          </DialogDescription>
        </DialogHeader>

        {canConfigure ? (
          <div className="grid gap-[14px] py-[4px] sm:grid-cols-2">
            <LabeledField label="配置名称">
              <Input value={form.name} onChange={(event) => updateForm('name', event.target.value)} />
            </LabeledField>
            <LabeledField label="Provider">
              <Input value={form.provider} onChange={(event) => updateForm('provider', event.target.value)} />
            </LabeledField>
            <LabeledField label="Base URL">
              <Input
                value={form.baseUrl}
                placeholder="例如 https://api.openai.com/v1"
                onChange={(event) => updateForm('baseUrl', event.target.value)}
              />
            </LabeledField>
            <LabeledField label="Model">
              <Input
                value={form.model}
                placeholder="例如 gpt-4o"
                onChange={(event) => updateForm('model', event.target.value)}
              />
            </LabeledField>
            <LabeledField label="API Key">
              <Input
                type="password"
                value={form.apiKey}
                placeholder={savedModelId ? '不修改请留空' : 'sk-...'}
                onChange={(event) => updateForm('apiKey', event.target.value)}
              />
            </LabeledField>
            <div className="grid grid-cols-2 gap-[12px]">
              <LabeledField label="Temperature">
                <Input
                  type="number"
                  min={0}
                  max={2}
                  step={0.1}
                  value={form.temperature}
                  onChange={(event) => updateForm('temperature', event.target.value)}
                />
              </LabeledField>
              <LabeledField label="Max Tokens">
                <Input
                  type="number"
                  min={128}
                  max={32000}
                  value={form.maxOutputTokens}
                  onChange={(event) => updateForm('maxOutputTokens', event.target.value)}
                />
              </LabeledField>
            </div>
          </div>
        ) : (
          <div className="flex items-start gap-[10px] rounded-[8px] border border-[#f0d9a8] bg-[#fffaf0] p-[12px] text-[13px] text-[#7b5c16]">
            <AlertCircle className="mt-[1px] size-[16px] shrink-0" />
            <span>当前账号没有模型管理权限，请联系管理员完成模型配置和连通性测试。</span>
          </div>
        )}

        {testResult && (
          <div
            className={testResult.success
              ? 'flex items-start gap-[10px] rounded-[8px] border border-[#b7e4c7] bg-[#f0fbf4] p-[12px] text-[13px] text-[#247447]'
              : 'flex items-start gap-[10px] rounded-[8px] border border-[#f2c4c4] bg-[#fff5f5] p-[12px] text-[13px] text-[#b42318]'}
          >
            {testResult.success
              ? <CheckCircle2 className="mt-[1px] size-[16px] shrink-0" />
              : <AlertCircle className="mt-[1px] size-[16px] shrink-0" />}
            <span className="min-w-0 wrap-break-word">{testResult.message}</span>
          </div>
        )}

        <DialogFooter className={`gap-[8px] ${canConfigure ? 'sm:justify-between' : 'sm:justify-end'}`}>
          {canConfigure && (
            <Button
              type="button"
              variant="outline"
              onClick={() => window.open('/enterprise/models', '_blank', 'noopener,noreferrer')}
            >
              <ExternalLink className="size-[15px]" />
              打开模型管理
            </Button>
          )}
          <div className="flex justify-end gap-[8px]">
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              {testResult?.success ? '返回对话' : '稍后配置'}
            </Button>
            {canConfigure && !testResult?.success && (
              <Button type="button" disabled={testing} onClick={() => void saveAndTest()}>
                <FlaskConical className="size-[15px]" />
                {testing ? '正在测试' : savedModelId ? '保存并重新测试' : '保存并测试'}
              </Button>
            )}
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function LabeledField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex min-w-0 flex-col gap-[6px]">
      <span className="text-[12px] font-medium text-[#464c5e]">{label}</span>
      {children}
    </label>
  );
}
