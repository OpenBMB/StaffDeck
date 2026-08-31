import { api } from '@/api/client';
import type { ModelAuthMode, ModelConfigRead } from '@/types';

export type ApiKeyProtocol = Exclude<ModelConfigRead['api_protocol'], 'codex_app_server'>;

// Keeps a model config's display name short enough to stay readable in the
// stat card and table — long vendor/model combos would otherwise overflow both.
// Measured against the "默认模型" stat card at a 1440px desktop viewport: it
// only has ~147px for the value text, so even "OpenAI · gpt-4o" (15 chars)
// sits right at the edge — 30 keeps most real names close to that boundary
// instead of running 2-3x past it, while the card's own truncate+tooltip
// still catches whatever a longer vendor/model combo can't avoid.
export const CONFIG_NAME_MAX_LENGTH = 30;

export type ChannelCategory = 'vendor' | 'subscription' | 'custom';

export type ChannelPreset = {
  id: string;
  category: ChannelCategory;
  name: string;
  description: string;
  badgeLabel: string;
  badgeColor: { background: string; text: string };
  apiProtocol: ApiKeyProtocol | null;
  baseUrl: string | null;
};

export const CHANNEL_PRESETS: ChannelPreset[] = [
  {
    id: 'openai',
    category: 'vendor',
    name: 'OpenAI',
    description: 'GPT-4o、GPT-4o mini 等',
    badgeLabel: 'O',
    badgeColor: { background: '#e6f4ef', text: '#0f6b52' },
    apiProtocol: 'openai_chat_completions',
    baseUrl: 'https://api.openai.com/v1',
  },
  {
    id: 'anthropic',
    category: 'vendor',
    name: 'Anthropic',
    description: 'Claude 全系列模型',
    badgeLabel: 'A',
    badgeColor: { background: '#fbeee7', text: '#b1502f' },
    apiProtocol: 'anthropic_messages',
    baseUrl: 'https://api.anthropic.com',
  },
  {
    id: 'gemini',
    category: 'vendor',
    name: 'Google Gemini',
    description: 'Gemini 2.5 Pro / Flash',
    badgeLabel: 'G',
    badgeColor: { background: '#eaf1fe', text: '#2f5fd6' },
    apiProtocol: 'gemini_generate_content',
    baseUrl: 'https://generativelanguage.googleapis.com',
  },
  {
    id: 'deepseek',
    category: 'vendor',
    name: 'DeepSeek',
    description: 'DeepSeek-V3 / R1',
    badgeLabel: 'D',
    badgeColor: { background: '#eef0fb', text: '#4a3fb0' },
    apiProtocol: 'openai_chat_completions',
    baseUrl: 'https://api.deepseek.com/v1',
  },
  {
    id: 'moonshot',
    category: 'vendor',
    name: 'Moonshot（Kimi）',
    description: 'Kimi K2 等',
    badgeLabel: 'K',
    badgeColor: { background: '#fdeef4', text: '#c23b78' },
    apiProtocol: 'openai_chat_completions',
    baseUrl: 'https://api.moonshot.cn/v1',
  },
  {
    id: 'chatgpt_subscription',
    category: 'subscription',
    name: 'ChatGPT 订阅（Codex）',
    description: '复用本机 Codex 登录，无需 API Key',
    badgeLabel: '',
    badgeColor: { background: '#f4f4f6', text: '#55596b' },
    apiProtocol: null,
    baseUrl: null,
  },
  {
    id: 'custom',
    category: 'custom',
    name: '自定义渠道',
    description: '自建代理 / 私有部署，手动配置',
    badgeLabel: '',
    badgeColor: { background: '#f4f4f6', text: '#55596b' },
    apiProtocol: null,
    baseUrl: null,
  },
];

export type VendorFormValues = {
  apiKey: string;
  model: string;
};

export type CustomFormValues = {
  apiProtocol: ApiKeyProtocol;
  baseUrl: string;
  apiKey: string;
  model: string;
  temperature: string;
  maxOutputTokens: string;
  extraBody: string;
};

export type SubscriptionFormValues = {
  model: string;
};

export type ModelConfigCreatePayload = {
  name: string;
  auth_mode: ModelAuthMode;
  model: string;
  temperature: number;
  max_output_tokens: number;
  is_default: boolean;
  enabled: boolean;
  api_protocol?: ApiKeyProtocol;
  base_url?: string;
  api_key?: string;
  extra_body?: Record<string, unknown>;
};

const DEFAULT_TEMPERATURE = 0.2;
const DEFAULT_MAX_OUTPUT_TOKENS = 8192;

export function buildModelConfigPayload(
  channel: ChannelPreset,
  formValues: VendorFormValues | CustomFormValues | SubscriptionFormValues,
  common: { name: string; isDefault: boolean; enabled: boolean },
): ModelConfigCreatePayload {
  const base = {
    name: common.name,
    is_default: common.isDefault,
    enabled: common.enabled,
  };

  if (channel.category === 'vendor') {
    const values = formValues as VendorFormValues;
    return {
      ...base,
      auth_mode: 'api_key',
      model: values.model,
      temperature: DEFAULT_TEMPERATURE,
      max_output_tokens: DEFAULT_MAX_OUTPUT_TOKENS,
      api_protocol: channel.apiProtocol ?? undefined,
      base_url: channel.baseUrl ?? undefined,
      api_key: values.apiKey || undefined,
    };
  }

  if (channel.category === 'custom') {
    const values = formValues as CustomFormValues;
    let extraBody: Record<string, unknown> = {};
    const trimmed = values.extraBody.trim();
    if (trimmed) {
      const parsed = JSON.parse(trimmed) as unknown;
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new SyntaxError('额外参数必须是合法的 JSON 对象');
      }
      extraBody = parsed as Record<string, unknown>;
    }
    // A blank field must not silently become 0 — Number('') is 0, which the
    // backend accepts as a real (nonsensical) value instead of rejecting it.
    const temperature = Number(values.temperature);
    const maxOutputTokens = Number(values.maxOutputTokens);
    if (!values.temperature.trim() || !values.maxOutputTokens.trim() || Number.isNaN(temperature) || Number.isNaN(maxOutputTokens)) {
      throw new SyntaxError('Temperature 与 Max Tokens 必须是数字');
    }
    return {
      ...base,
      auth_mode: 'api_key',
      model: values.model,
      temperature,
      max_output_tokens: maxOutputTokens,
      api_protocol: values.apiProtocol,
      base_url: values.baseUrl.trim() || undefined,
      api_key: values.apiKey || undefined,
      extra_body: extraBody,
    };
  }

  const values = formValues as SubscriptionFormValues;
  return {
    ...base,
    auth_mode: 'chatgpt_subscription',
    model: values.model,
    temperature: DEFAULT_TEMPERATURE,
    max_output_tokens: DEFAULT_MAX_OUTPUT_TOKENS,
  };
}

export type ProviderModelOption = { id: string; label: string };

export type ListModelsResult = {
  success: boolean;
  models: ProviderModelOption[];
};

/**
 * 按协议向渠道自身的接口拉取可用模型；网络/鉴权失败一律降级为
 * { success: false, models: [] }，调用方据此回退到手动输入，不抛出异常。
 */
export async function fetchProviderModels(params: {
  tenantId: string;
  apiProtocol: ModelConfigRead['api_protocol'];
  baseUrl?: string;
  apiKey?: string;
}): Promise<ListModelsResult> {
  try {
    return await api.post<ListModelsResult>(
      `/api/enterprise/model-configs/list-models?tenant_id=${encodeURIComponent(params.tenantId)}`,
      {
        tenant_id: params.tenantId,
        api_protocol: params.apiProtocol,
        base_url: params.baseUrl,
        api_key: params.apiKey,
      },
    );
  } catch {
    return { success: false, models: [] };
  }
}
