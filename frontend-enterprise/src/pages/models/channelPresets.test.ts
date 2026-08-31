import { afterEach, describe, expect, it, vi } from 'vitest';

import { api } from '@/api/client';
import {
  CHANNEL_PRESETS,
  buildModelConfigPayload,
  fetchProviderModels,
  type ChannelPreset,
} from './channelPresets';

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>();
  return { ...actual, api: { ...actual.api, post: vi.fn() } };
});

const mockedPost = vi.mocked(api.post);

afterEach(() => {
  mockedPost.mockReset();
});

function findPreset(id: string): ChannelPreset {
  const preset = CHANNEL_PRESETS.find((item) => item.id === id);
  if (!preset) throw new Error(`missing preset ${id}`);
  return preset;
}

describe('CHANNEL_PRESETS', () => {
  it('includes the initial five vendor presets plus custom and subscription entries', () => {
    const ids = CHANNEL_PRESETS.map((preset) => preset.id);
    expect(ids).toEqual(
      expect.arrayContaining(['openai', 'anthropic', 'gemini', 'deepseek', 'moonshot', 'custom', 'chatgpt_subscription']),
    );
  });

  it('gives every vendor-category preset a fixed protocol and base URL', () => {
    const vendors = CHANNEL_PRESETS.filter((preset) => preset.category === 'vendor');
    expect(vendors.length).toBeGreaterThanOrEqual(5);
    for (const vendor of vendors) {
      expect(vendor.apiProtocol).not.toBeNull();
      expect(vendor.baseUrl).not.toBeNull();
    }
  });


  it('leaves protocol and base URL unset for the custom and subscription categories', () => {
    expect(findPreset('custom').apiProtocol).toBeNull();
    expect(findPreset('custom').baseUrl).toBeNull();
    expect(findPreset('chatgpt_subscription').apiProtocol).toBeNull();
    expect(findPreset('chatgpt_subscription').baseUrl).toBeNull();
  });
});

describe('buildModelConfigPayload', () => {
  const common = { name: 'OpenAI · GPT-4o', isDefault: true, enabled: true };

  it('derives protocol and base URL from the preset for a vendor-category channel', () => {
    const openai = findPreset('openai');
    const payload = buildModelConfigPayload(openai, { apiKey: 'sk-test-123', model: 'gpt-4o' }, common);

    expect(payload.auth_mode).toBe('api_key');
    expect(payload.api_protocol).toBe(openai.apiProtocol);
    expect(payload.base_url).toBe(openai.baseUrl);
    expect(payload.api_key).toBe('sk-test-123');
    expect(payload.model).toBe('gpt-4o');
    expect(payload.name).toBe('OpenAI · GPT-4o');
    expect(payload.is_default).toBe(true);
    expect(payload.enabled).toBe(true);
  });

  it('passes user-supplied protocol, base URL and advanced params through verbatim for a custom channel', () => {
    const custom = findPreset('custom');
    const payload = buildModelConfigPayload(
      custom,
      {
        apiProtocol: 'anthropic_messages',
        baseUrl: 'https://my-proxy.example.com/v1',
        apiKey: 'sk-custom-456',
        model: 'claude-3-7-sonnet',
        temperature: '0.5',
        maxOutputTokens: '4096',
        extraBody: '{"thinking":{"type":"disabled"}}',
      },
      common,
    );

    expect(payload.auth_mode).toBe('api_key');
    expect(payload.api_protocol).toBe('anthropic_messages');
    expect(payload.base_url).toBe('https://my-proxy.example.com/v1');
    expect(payload.api_key).toBe('sk-custom-456');
    expect(payload.model).toBe('claude-3-7-sonnet');
    expect(payload.temperature).toBe(0.5);
    expect(payload.max_output_tokens).toBe(4096);
    expect(payload.extra_body).toEqual({ thinking: { type: 'disabled' } });
  });

  it('throws on invalid extra_body JSON for a custom channel — callers must validate before calling this', () => {
    const custom = findPreset('custom');
    expect(() =>
      buildModelConfigPayload(
        custom,
        {
          apiProtocol: 'openai_chat_completions',
          baseUrl: 'https://my-proxy.example.com/v1',
          apiKey: 'sk-custom-456',
          model: 'gpt-4o',
          temperature: '0.5',
          maxOutputTokens: '4096',
          extraBody: '{not valid json',
        },
        common,
      ),
    ).toThrow();
  });

  it('omits API key, protocol, base URL and extra_body for a ChatGPT subscription channel', () => {
    const subscription = findPreset('chatgpt_subscription');
    const payload = buildModelConfigPayload(subscription, { model: 'gpt-5.1-codex' }, common);

    expect(payload.auth_mode).toBe('chatgpt_subscription');
    expect(payload.model).toBe('gpt-5.1-codex');
    expect(payload).not.toHaveProperty('api_key');
    expect(payload).not.toHaveProperty('api_protocol');
    expect(payload).not.toHaveProperty('base_url');
    expect(payload).not.toHaveProperty('extra_body');
  });
});

describe('fetchProviderModels', () => {
  it('uses the caller tenant for both the list-models query and request body', async () => {
    mockedPost.mockResolvedValueOnce({ success: true, models: [] });

    await fetchProviderModels({
      tenantId: 'tenant-isolated',
      apiProtocol: 'openai_chat_completions',
      baseUrl: 'https://api.openai.com/v1',
      apiKey: 'sk-test',
    });

    const [url, body] = mockedPost.mock.calls[0] as [string, Record<string, unknown>];
    expect(url).toContain('tenant_id=tenant-isolated');
    expect(body.tenant_id).toBe('tenant-isolated');
  });

  it('posts protocol/base_url/api_key and returns the normalized result on success', async () => {
    mockedPost.mockResolvedValueOnce({
      success: true,
      models: [{ id: 'gpt-4o', label: 'gpt-4o' }],
    });

    const result = await fetchProviderModels({
      tenantId: 'tenant_demo',
      apiProtocol: 'openai_chat_completions',
      baseUrl: 'https://api.openai.com/v1',
      apiKey: 'sk-test',
    });

    expect(mockedPost).toHaveBeenCalledTimes(1);
    const [url, body] = mockedPost.mock.calls[0] as [string, Record<string, unknown>];
    expect(url).toContain('/api/enterprise/model-configs/list-models');
    expect(body.api_protocol).toBe('openai_chat_completions');
    expect(body.base_url).toBe('https://api.openai.com/v1');
    expect(body.api_key).toBe('sk-test');
    expect(result).toEqual({ success: true, models: [{ id: 'gpt-4o', label: 'gpt-4o' }] });
  });

  it('posts a codex_app_server request with no base_url/api_key for the ChatGPT subscription channel', async () => {
    mockedPost.mockResolvedValueOnce({
      success: true,
      models: [{ id: 'gpt-5.6-terra', label: 'GPT-5.6-Terra' }],
    });

    const result = await fetchProviderModels({ tenantId: 'tenant_demo', apiProtocol: 'codex_app_server' });

    const [, body] = mockedPost.mock.calls[0] as [string, Record<string, unknown>];
    expect(body.api_protocol).toBe('codex_app_server');
    expect(body.base_url).toBeUndefined();
    expect(body.api_key).toBeUndefined();
    expect(result).toEqual({ success: true, models: [{ id: 'gpt-5.6-terra', label: 'GPT-5.6-Terra' }] });
  });

  it('degrades to an empty, unsuccessful result instead of throwing when the request fails', async () => {
    mockedPost.mockRejectedValueOnce(new Error('network down'));

    const result = await fetchProviderModels({
      tenantId: 'tenant_demo',
      apiProtocol: 'openai_chat_completions',
      baseUrl: 'https://api.openai.com/v1',
      apiKey: 'sk-test',
    });

    expect(result).toEqual({ success: false, models: [] });
  });
});
