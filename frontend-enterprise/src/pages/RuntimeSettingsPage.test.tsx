// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';

import RuntimeSettingsPage, {
  validateContextSettings,
} from './RuntimeSettingsPage';

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  put: vi.fn(),
}));

vi.mock('../api/client', () => ({
  TENANT_ID: 'tenant_demo',
  api: {
    get: mocks.get,
    put: mocks.put,
  },
}));

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

const runtimeSettings = {
  tenant_id: 'tenant_demo',
  show_thinking_trace: true,
  show_skill_trace: true,
  show_tool_trace: true,
  reflection_max_rounds: 1,
  agent_loop_max_actions: 32,
  context_token_budget: 32000,
  context_compaction_trigger_ratio: 0.7,
  context_recent_round_limit: 6,
  context_long_summary_token_budget: 4000,
  context_medium_summary_token_budget: 4000,
  context_allowed_roles: ['user', 'assistant'] as Array<'user' | 'assistant'>,
  context_long_summary_prefix: '历史的信息可以被总结为：',
  context_medium_summary_prefix: '近期的历史信息总结为：',
  sandbox_enabled: false,
  harness_storage_path: '',
  effective_harness_storage_path: '/Users/demo/.staffdeck/workspaces',
  restart_scheduled: false,
  sandbox_network_mode: 'all' as const,
  sandbox_allowed_domains: [],
  sandbox_status: 'disabled' as const,
  sandbox_status_message: '沙盒已由管理员关闭。',
  updated_at: '2026-08-29T00:00:00Z',
};

function renderRuntimeSettings(): void {
  /** Renders the page with its locale observer and the tenant administrator identity. */

  render(
    <I18nProvider>
      <RuntimeSettingsPage
        currentUser={{
          id: 'admin_demo',
          tenant_id: 'tenant_demo',
          username: 'admin',
          role: 'admin',
        }}
      />
    </I18nProvider>,
  );
}

beforeEach(() => {
  mocks.get.mockReset();
  mocks.put.mockReset();
  mocks.get.mockResolvedValue(runtimeSettings);
  mocks.put.mockResolvedValue(runtimeSettings);
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

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
describe('Harness workspace settings', () => {
  it('labels the field as a Harness-only workspace, shows its effective root, and saves the stable API field', async () => {
    const user = userEvent.setup();
    renderRuntimeSettings();

    const input = await screen.findByPlaceholderText('/Users/demo/.staffdeck/workspaces');
    expect(screen.getByText('Harness 工作区目录')).toBeTruthy();
    expect(screen.getByText('/Users/demo/.staffdeck/workspaces')).toBeTruthy();
    expect(screen.getByText(/仅用于 Harness 的任务文件和生成产物/)).toBeTruthy();
    expect(screen.queryByText('文件存储目录')).toBeNull();

    await user.type(input, '/Volumes/work/harness');
    await user.click(screen.getByRole('button', { name: '保存设置' }));

    await waitFor(() => {
      expect(mocks.put).toHaveBeenCalledWith(
        '/api/enterprise/ui-config',
        expect.objectContaining({ harness_storage_path: '/Volumes/work/harness' }),
      );
    });
  });

  it('localizes the effective workspace and sandbox status for en-US', async () => {
    window.localStorage.setItem('staffdeck_locale', 'en-US');
    mocks.get.mockImplementation((path: string) => {
      if (path.includes('/ui-config')) {
        return Promise.resolve({
          ...runtimeSettings,
          sandbox_enabled: true,
          sandbox_status: 'unavailable' as const,
          sandbox_status_message: 'No sandbox runtime is available.',
        });
      }
      return Promise.resolve(runtimeSettings);
    });

    renderRuntimeSettings();

    expect(await screen.findByText('Current effective Harness workspace')).toBeTruthy();
    expect(screen.getByText('/Users/demo/.staffdeck/workspaces')).toBeTruthy();
    expect(
      screen.getByText((_, element) => element?.textContent === 'Sandbox status:Unavailable'),
    ).toBeTruthy();
  });
});
