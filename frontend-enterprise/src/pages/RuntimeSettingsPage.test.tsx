// @vitest-environment jsdom

import { describe, expect, it } from 'vitest';

import {
  buildApiEndpointLinks,
  validateContextSettings,
  validateNetworkSettings,
} from './RuntimeSettingsPage';

const validForm = {
  show_thinking_trace: true,
  show_skill_trace: true,
  show_tool_trace: true,
  reflection_max_rounds: '1',
  agent_loop_max_actions: '32',
  context_token_budget: '32000',
  context_compaction_trigger_ratio: '0.70',
  context_recent_round_limit: '6',
  context_long_summary_token_budget: '4000',
  context_medium_summary_token_budget: '4000',
  context_allowed_roles: ['user', 'assistant'] as Array<'user' | 'assistant'>,
  context_long_summary_prefix: '历史的信息可以被总结为：',
  context_medium_summary_prefix: '近期的历史信息总结为：',
  sandbox_enabled: false,
  harness_storage_path: '',
  sandbox_network_mode: 'all' as const,
  sandbox_allowed_domains: '',
};

describe('runtime context settings validation', () => {
  it('accepts the default runtime settings', () => {
    expect(validateContextSettings(validForm)).toBeNull();
  });

  it('rejects summary budgets larger than the context budget', () => {
    expect(validateContextSettings({
      ...validForm,
      context_token_budget: '7000',
    })).toBe('长期与近期摘要预算之和不能超过上下文预算');
  });

  it('requires at least one history role and both summary prefixes', () => {
    expect(validateContextSettings({
      ...validForm,
      context_allowed_roles: [],
    })).toBe('至少保留一种历史消息角色');
    expect(validateContextSettings({
      ...validForm,
      context_medium_summary_prefix: '   ',
    })).toBe('摘要前缀不能为空');
  });
});

describe('network and API endpoint helpers', () => {
  it('derives exactly one current same-machine Base URL for display', () => {
    expect(buildApiEndpointLinks('http://127.0.0.1:6204/api/v1/')).toEqual({
      baseUrl: 'http://127.0.0.1:6204/api/v1',
    });
  });

  it('validates local, LAN, and public next-launch input before save', () => {
    expect(validateNetworkSettings({ mode: 'local', port: '6204', public_url: '' })).toBeNull();
    expect(validateNetworkSettings({ mode: 'lan', port: '6205', public_url: '' })).toBeNull();
    expect(validateNetworkSettings({
      mode: 'public',
      port: '443',
      public_url: 'https://staff.example.com',
    })).toBeNull();
    expect(validateNetworkSettings({ mode: 'local', port: '0', public_url: '' })).toBe(
      '端口必须是 1 到 65535 之间的整数',
    );
    expect(validateNetworkSettings({ mode: 'public', port: '6204', public_url: '' })).toBe(
      '公网访问需要填写完整的 HTTP(S) URL',
    );
    expect(validateNetworkSettings({
      mode: 'public',
      port: '6204',
      public_url: 'https://user:secret@staff.example.com',
    })).toBe('公网 URL 不能包含用户名、密码、查询参数、片段或路径');
  });
});
