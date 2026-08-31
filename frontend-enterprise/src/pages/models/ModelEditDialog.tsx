import { useEffect, useState, type ReactNode } from 'react';
import { LoaderCircle, LogIn, LogOut } from 'lucide-react';

import { api, ApiError } from '@/api/client';
import { TENANT_ID } from '@/api/client';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  Input,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Switch,
  Textarea,
} from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import IconModels from '@/assets/icons/sys-models.svg?react';
import type { CodexSubscriptionAccountRead, ModelAuthMode, ModelConfigRead } from '@/types';
import { modelActionError, providerErrorFromApiError, toastContentForProviderError } from '../ModelsPage';
import { CONFIG_NAME_MAX_LENGTH, type ApiKeyProtocol } from './channelPresets';

export type ModelEditDialogProps = {
  open: boolean;
  selected: ModelConfigRead | null;
  availableProtocols: ApiKeyProtocol[];
  subscriptionAccount: CodexSubscriptionAccountRead | null;
  subscriptionLoading: boolean;
  onStartSubscriptionLogin: () => void;
  onCancelSubscriptionLogin: () => void;
  onRequestSubscriptionLogout: () => void;
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
};

type ModelForm = {
  name: string;
  auth_mode: ModelAuthMode;
  api_protocol: ApiKeyProtocol;
  base_url: string;
  model: string;
  api_key: string;
  temperature: string;
  max_output_tokens: string;
  extra_body: string;
  is_default: boolean;
  enabled: boolean;
};

const BLANK_FORM: ModelForm = {
  name: '',
  auth_mode: 'api_key',
  api_protocol: 'openai_chat_completions',
  base_url: '',
  model: '',
  api_key: '',
  temperature: '0.2',
  max_output_tokens: '8192',
  extra_body: '{}',
  is_default: false,
  enabled: true,
};

export default function ModelEditDialog({
  open,
  selected,
  availableProtocols,
  subscriptionAccount,
  subscriptionLoading,
  onStartSubscriptionLogin,
  onCancelSubscriptionLogin,
  onRequestSubscriptionLogout,
  onOpenChange,
  onSaved,
}: ModelEditDialogProps) {
  const [form, setForm] = useState<ModelForm>(BLANK_FORM);
  const [saving, setSaving] = useState(false);
  const [saveStage, setSaveStage] = useState<'saving' | 'testing' | null>(null);

  useEffect(() => {
    if (!open || !selected) return;
    setForm({
      name: selected.name,
      auth_mode: selected.auth_mode || 'api_key',
      api_protocol: selected.auth_mode === 'chatgpt_subscription'
        ? 'openai_chat_completions'
        : selected.api_protocol as ApiKeyProtocol,
      base_url: selected.base_url || '',
      model: selected.model,
      api_key: '',
      temperature: String(selected.temperature),
      max_output_tokens: String(selected.max_output_tokens),
      extra_body: JSON.stringify(selected.extra_body || {}, null, 2),
      is_default: selected.is_default,
      enabled: selected.enabled,
    });
  }, [open, selected]);

  const updateForm = <K extends keyof ModelForm>(key: K, value: ModelForm[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const isSubscriptionForm = form.auth_mode === 'chatgpt_subscription';

  function closeDialog() {
    if (saving) return;
    onOpenChange(false);
  }

  async function save() {
    if (!selected) return;
    const name = form.name.trim();
    const model = form.model.trim();
    if (!name || !model) {
      notify.error('请填写名称和 Model');
      return;
    }
    const temperature = Number(form.temperature);
    const maxOutputTokens = Number(form.max_output_tokens);
    // A blank field must not silently become 0 — Number('') is 0, which
    // passes Number.isNaN and gets sent as a real (nonsensical) value.
    if (
      !form.temperature.trim() ||
      !form.max_output_tokens.trim() ||
      Number.isNaN(temperature) ||
      Number.isNaN(maxOutputTokens)
    ) {
      notify.error('Temperature 与 Max Tokens 必须是数字');
      return;
    }
    let extraBody: Record<string, unknown> = {};
    if (!isSubscriptionForm) {
      try {
        const parsed = JSON.parse(form.extra_body.trim() || '{}') as unknown;
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
          throw new Error('not an object');
        }
        extraBody = parsed as Record<string, unknown>;
      } catch {
        notify.error('额外参数必须是合法的 JSON 对象');
        return;
      }
    }
    const payload = {
      tenant_id: TENANT_ID,
      name,
      auth_mode: form.auth_mode,
      model,
      temperature,
      max_output_tokens: maxOutputTokens,
      is_default: form.enabled && form.is_default,
      enabled: form.enabled,
      ...(isSubscriptionForm ? {} : {
        api_protocol: form.api_protocol,
        base_url: form.base_url.trim() || undefined,
        extra_body: extraBody,
        api_key: form.api_key || undefined,
      }),
    };
    setSaving(true);
    setSaveStage(form.enabled ? 'testing' : 'saving');
    try {
      const verifyQuery = form.enabled ? '?verify_before_save=true' : '';
      await api.put<ModelConfigRead>(`/api/enterprise/model-configs/${selected.id}${verifyQuery}`, payload);
      if (form.enabled) {
        notify.success(form.is_default ? '测试通过，已启用并设为默认模型' : '测试通过，已启用');
      } else {
        notify.success('已保存');
      }
      onOpenChange(false);
      onSaved();
    } catch (error) {
      const providerError = error instanceof ApiError ? providerErrorFromApiError(error) : null;
      notify.error(
        providerError
          ? toastContentForProviderError(providerError, '保存失败')
          : modelActionError(error, '保存失败'),
      );
    } finally {
      setSaving(false);
      setSaveStage(null);
    }
  }

  if (!selected) return null;

  return (
    <Dialog open={open} onOpenChange={(next) => !next && closeDialog()}>
      <DialogContent
        aria-describedby={undefined}
        className="flex max-h-[calc(100dvh-4rem)] w-[calc(100%-2rem)] flex-col gap-[16px] overflow-hidden rounded-[14px] px-[20px] py-[16px] sm:max-w-[640px]"
      >
        <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          <IconModels className="size-[14px] shrink-0" />
          <DialogTitle className="min-w-0 truncate text-[14px] font-normal leading-none text-[#757f9c]">
            编辑模型：{selected.name}
          </DialogTitle>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-[12px]">
          <div className="grid grid-cols-1 gap-[14px] sm:grid-cols-2">
            <LabeledField label="名称">
              <Input
                value={form.name}
                placeholder="例如 GPT-4o"
                maxLength={CONFIG_NAME_MAX_LENGTH}
                onChange={(event) => updateForm('name', event.target.value)}
              />
              {form.name.length >= CONFIG_NAME_MAX_LENGTH && (
                <p className="text-[11px] text-[#b42318]">
                  名称最长 {CONFIG_NAME_MAX_LENGTH} 个字符，已达到上限。
                </p>
              )}
            </LabeledField>
            <LabeledField label="认证方式">
              <Select
                value={form.auth_mode}
                onValueChange={(value) => updateForm('auth_mode', value as ModelAuthMode)}
              >
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="api_key">API Key</SelectItem>
                  <SelectItem value="chatgpt_subscription">ChatGPT 订阅（Codex）</SelectItem>
                </SelectContent>
              </Select>
            </LabeledField>
            <LabeledField label="Model">
              <Input value={form.model} placeholder="例如 gpt-4o" onChange={(event) => updateForm('model', event.target.value)} />
            </LabeledField>
            {isSubscriptionForm ? (
              <div className="flex flex-col gap-[10px] rounded-[10px] border border-[#dce7ff] bg-[#f6f9ff] p-[12px] sm:col-span-2">
                <div className="flex flex-wrap items-start justify-between gap-[10px]">
                  <div className="min-w-0">
                    <p className="text-[12px] font-medium text-[#29466f]">ChatGPT 订阅</p>
                    <p className="mt-[3px] text-[12px] leading-[18px] text-[#5d6f8c]">
                      {subscriptionAccount?.message || '正在读取 ChatGPT 订阅状态…'}
                      {subscriptionAccount?.status === 'connected' && subscriptionAccount.plan_type
                        ? `（${subscriptionAccount.plan_type}）`
                        : ''}
                    </p>
                  </div>
                  {subscriptionAccount?.status === 'connected' ? (
                    <UIButton
                      type="button"
                      variant="outline"
                      disabled={subscriptionLoading}
                      onClick={onRequestSubscriptionLogout}
                      className="h-[30px] gap-[4px] border-[#cbd8f2] bg-white px-[10px] text-[12px] text-[#464c5e]"
                    >
                      <LogOut className="size-[13px]" />
                      退出本机 Codex
                    </UIButton>
                  ) : subscriptionAccount?.status === 'pending' ? (
                    <UIButton
                      type="button"
                      variant="outline"
                      disabled={subscriptionLoading}
                      onClick={onCancelSubscriptionLogin}
                      className="h-[30px] border-[#cbd8f2] bg-white px-[10px] text-[12px] text-[#464c5e]"
                    >
                      取消登录
                    </UIButton>
                  ) : (
                    <UIButton
                      type="button"
                      disabled={subscriptionLoading}
                      onClick={onStartSubscriptionLogin}
                      className="h-[30px] gap-[4px] bg-[#1a71ff] px-[10px] text-[12px] text-white hover:bg-[#1463df]"
                    >
                      {subscriptionLoading ? <LoaderCircle className="size-[13px] animate-spin" /> : <LogIn className="size-[13px]" />}
                      连接 ChatGPT 订阅
                    </UIButton>
                  )}
                </div>
                <p className="text-[11px] leading-[16px] text-[#7483a0]">
                  登录由本机 Codex runtime 管理。StaffDeck 不保存 ChatGPT OAuth code、access token 或 refresh token。
                </p>
              </div>
            ) : (
              <>
                <LabeledField label="API 协议">
                  <Select
                    value={form.api_protocol}
                    onValueChange={(value) => updateForm('api_protocol', value as ApiKeyProtocol)}
                  >
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {availableProtocols.includes('openai_chat_completions') && (
                        <SelectItem value="openai_chat_completions">OpenAI Chat Completions</SelectItem>
                      )}
                      {availableProtocols.includes('openai_responses') && (
                        <SelectItem value="openai_responses">OpenAI Responses API</SelectItem>
                      )}
                      {availableProtocols.includes('anthropic_messages') && (
                        <SelectItem value="anthropic_messages">Anthropic Messages</SelectItem>
                      )}
                      {availableProtocols.includes('gemini_generate_content') && (
                        <SelectItem value="gemini_generate_content">Gemini Generate Content</SelectItem>
                      )}
                    </SelectContent>
                  </Select>
                </LabeledField>
                <LabeledField label="Base URL">
                  <Input
                    value={form.base_url}
                    placeholder={form.api_protocol === 'openai_chat_completions' || form.api_protocol === 'openai_responses'
                      ? 'https://llm-center.modelbest.cn/llm/v1'
                      : 'https://llm-center.modelbest.cn/llm'}
                    onChange={(event) => updateForm('base_url', event.target.value)}
                  />
                </LabeledField>
                <LabeledField label="API Key">
                  <Input
                    type="password"
                    value={form.api_key}
                    placeholder="不修改请留空"
                    onChange={(event) => updateForm('api_key', event.target.value)}
                  />
                </LabeledField>
              </>
            )}
            <div className="grid grid-cols-2 gap-[14px]">
              <LabeledField label="Temperature">
                <Input
                  type="number"
                  min={0}
                  max={form.api_protocol === 'anthropic_messages' ? 1 : 2}
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
                  value={form.max_output_tokens}
                  onChange={(event) => updateForm('max_output_tokens', event.target.value)}
                />
              </LabeledField>
            </div>
            {!isSubscriptionForm && form.api_protocol === 'openai_chat_completions' && <div className="sm:col-span-2">
              <LabeledField label="额外请求参数（extra_body JSON）">
                <Textarea
                  rows={5}
                  value={form.extra_body}
                  placeholder={'{\n  "thinking": {\n    "type": "disabled"\n  }\n}'}
                  className="min-h-[116px] resize-y font-mono text-[12px]"
                  onChange={(event) => updateForm('extra_body', event.target.value)}
                />
              </LabeledField>
            </div>}
          </div>
          <div className="mt-[16px] flex flex-wrap items-center gap-[24px]">
            <label className="flex cursor-pointer items-center gap-[8px]">
              <Switch checked={form.is_default} onCheckedChange={(next) => updateForm('is_default', next)} />
              <span className="text-[12px] font-medium text-[#464c5e]">设为默认</span>
            </label>
            <label className="flex cursor-pointer items-center gap-[8px]">
              <Switch checked={form.enabled} onCheckedChange={(next) => updateForm('enabled', next)} />
              <span className="text-[12px] font-medium text-[#464c5e]">启用</span>
            </label>
          </div>
        </div>

        <div className="flex items-center justify-end gap-[8px] px-[12px]">
          <UIButton
            variant="outline"
            disabled={saving}
            onClick={closeDialog}
            className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
          >
            取消
          </UIButton>
          <UIButton
            disabled={saving}
            onClick={() => void save()}
            className="h-[32px] w-[80px] rounded-[10px] bg-[#18181a] px-[12px] text-[14px] font-normal text-white hover:bg-[#303030]"
          >
            {saving && <LoaderCircle className="size-[14px] animate-spin" />}
            {saveStage === 'testing' ? '测试并保存中' : saving ? '保存中' : '保存'}
          </UIButton>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function LabeledField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-[6px]">
      <span className="text-[12px] font-medium text-[#464c5e]">{label}</span>
      {children}
    </label>
  );
}
