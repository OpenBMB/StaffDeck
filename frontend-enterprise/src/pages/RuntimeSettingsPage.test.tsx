// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import type { EnterpriseAuthUser } from '@/auth';
import type { UIConfigRead } from '@/types';

import RuntimeSettingsPage from './RuntimeSettingsPage';
import { validateContextSettings } from './RuntimeSettingsPage';

const { toastMock } = vi.hoisted(() => ({
  toastMock: {
    custom: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
    loading: vi.fn(),
    dismiss: vi.fn(),
  },
}));

vi.mock('sonner', () => ({ toast: toastMock }));

const adminUser: EnterpriseAuthUser = {
  id: 'user-1',
  tenant_id: 'tenant_demo',
  username: 'admin',
  role: 'admin',
};

function makeUiConfig(overrides: Partial<UIConfigRead> = {}): UIConfigRead {
  return {
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
    context_allowed_roles: ['user', 'assistant'],
    context_long_summary_prefix: '历史的信息可以被总结为：',
    context_medium_summary_prefix: '近期的历史信息总结为：',
    sandbox_enabled: false,
    harness_storage_path: '',
    effective_harness_storage_path: '',
    sandbox_network_mode: 'all',
    sandbox_allowed_domains: [],
    context_compression_mode: 'legacy',
    acp_model_context_limit: 128000,
    acp_nudge_max_pct: 0.7,
    acp_nudge_emergency_pct: 0.85,
    acp_nudge_min_pct: 0.45,
    acp_enabled: true,
    updated_at: '2026-08-25T00:00:00Z',
    ...overrides,
  };
}

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

function makeFetchMock(config: UIConfigRead) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method || 'GET';
    if (method === 'GET' && url.includes('/api/enterprise/ui-config')) return jsonResponse(config);
    if (method === 'PUT' && url.includes('/api/enterprise/ui-config')) {
      const body = JSON.parse(String(init?.body || '{}')) as Record<string, unknown>;
      return jsonResponse({ ...config, ...body });
    }
    return jsonResponse({});
  });
}

function putBody(fetchMock: ReturnType<typeof makeFetchMock>): Record<string, unknown> {
  const call = fetchMock.mock.calls.find(
    ([input, init]) => init?.method === 'PUT' && String(input).includes('/api/enterprise/ui-config'),
  );
  expect(call).toBeTruthy();
  return JSON.parse(String(call?.[1]?.body)) as Record<string, unknown>;
}

function renderPage() {
  return render(
    <I18nProvider>
      <RuntimeSettingsPage currentUser={adminUser} />
    </I18nProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  toastMock.custom.mockClear();
});

describe('RuntimeSettingsPage 上下文压缩机制', () => {
  it('loads and shows the persisted compression mode with ACP thresholds', async () => {
    const fetchMock = makeFetchMock(makeUiConfig({ context_compression_mode: 'acp' }));
    vi.stubGlobal('fetch', fetchMock);

    renderPage();

    const select = (await screen.findByLabelText(/上下文压缩机制/)) as HTMLSelectElement;
    expect(select.value).toBe('acp');
    expect(await screen.findByLabelText(/上下文上限/)).toBeTruthy();
    expect(screen.getByLabelText(/常规压缩触发阈值/)).toBeTruthy();
    expect(screen.getByLabelText(/紧急压缩触发阈值/)).toBeTruthy();
    expect(screen.getByLabelText(/最低压缩触发阈值/)).toBeTruthy();
  });

  it('sends the compression mode and ACP thresholds in the PUT body', async () => {
    const user = userEvent.setup();
    const fetchMock = makeFetchMock(makeUiConfig());
    vi.stubGlobal('fetch', fetchMock);

    renderPage();

    const select = (await screen.findByLabelText(/上下文压缩机制/)) as HTMLSelectElement;
    await user.selectOptions(select, 'acp');
    await user.click(screen.getByRole('button', { name: '保存设置' }));
    await user.click(await screen.findByRole('button', { name: '确认保存' }));

    await waitFor(() => {
      const body = putBody(fetchMock);
      expect(body.context_compression_mode).toBe('acp');
      expect(body.acp_model_context_limit).toBe(128000);
      expect(body.acp_nudge_max_pct).toBe(0.7);
      expect(body.acp_nudge_emergency_pct).toBe(0.85);
      expect(body.acp_nudge_min_pct).toBe(0.45);
    });
  });

  it('reloads the saved acp mode after a save round trip', async () => {
    const user = userEvent.setup();
    let current = makeUiConfig();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method || 'GET';
      if (method === 'PUT' && url.includes('/api/enterprise/ui-config')) {
        current = { ...current, ...(JSON.parse(String(init?.body || '{}')) as Record<string, unknown>) };
        return jsonResponse(current);
      }
      if (method === 'GET' && url.includes('/api/enterprise/ui-config')) return jsonResponse(current);
      return jsonResponse({});
    });
    vi.stubGlobal('fetch', fetchMock);

    const { unmount } = renderPage();
    const select = (await screen.findByLabelText(/上下文压缩机制/)) as HTMLSelectElement;
    await user.selectOptions(select, 'acp');
    await user.click(screen.getByRole('button', { name: '保存设置' }));
    await user.click(await screen.findByRole('button', { name: '确认保存' }));
    await waitFor(() => expect(putBody(fetchMock).context_compression_mode).toBe('acp'));

    // 重新加载页面：GET 回读已保存的 acp 与阈值
    unmount();
    renderPage();
    const reloaded = (await screen.findByLabelText(/上下文压缩机制/)) as HTMLSelectElement;
    expect(reloaded.value).toBe('acp');
    expect(await screen.findByLabelText(/上下文上限/)).toBeTruthy();
  });

  it('falls back to legacy when the backend returns an unknown mode', async () => {
    const fetchMock = makeFetchMock(
      makeUiConfig({ context_compression_mode: 'auto' as UIConfigRead['context_compression_mode'] }),
    );
    vi.stubGlobal('fetch', fetchMock);

    renderPage();

    const select = (await screen.findByLabelText(/上下文压缩机制/)) as HTMLSelectElement;
    expect(select.value).toBe('legacy');
    expect(screen.queryByLabelText(/上下文上限/)).toBeNull();
  });

  it('shows an error toast and keeps the previous selection when save fails', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method || 'GET';
      if (method === 'GET' && url.includes('/api/enterprise/ui-config')) return jsonResponse(makeUiConfig());
      if (method === 'PUT' && url.includes('/api/enterprise/ui-config')) {
        return {
          ok: false,
          status: 500,
          statusText: 'Internal Server Error',
          text: async () => JSON.stringify({ detail: '保存失败' }),
        } as Response;
      }
      return jsonResponse({});
    });
    vi.stubGlobal('fetch', fetchMock);

    renderPage();

    const select = (await screen.findByLabelText(/上下文压缩机制/)) as HTMLSelectElement;
    await user.selectOptions(select, 'acp');
    await user.click(screen.getByRole('button', { name: '保存设置' }));
    await user.click(await screen.findByRole('button', { name: '确认保存' }));

    await waitFor(() => expect(toastMock.custom).toHaveBeenCalled());
    // 失败后保留用户选择，不回退
    expect((screen.getByLabelText(/上下文压缩机制/) as HTMLSelectElement).value).toBe('acp');
  });

  it('shows the confirm dialog only when the compression mode changed', async () => {
    const user = userEvent.setup();
    const fetchMock = makeFetchMock(makeUiConfig());
    vi.stubGlobal('fetch', fetchMock);

    renderPage();

    // 未改变 mode：直接保存，无确认对话框
    await user.click(screen.getByRole('button', { name: '保存设置' }));
    await waitFor(() => expect(putBody(fetchMock).context_compression_mode).toBe('legacy'));
    expect(screen.queryByText('切换上下文压缩机制？')).toBeNull();

    // 改变 mode：保存前出现确认对话框，说明影响范围
    const select = (await screen.findByLabelText(/上下文压缩机制/)) as HTMLSelectElement;
    await user.selectOptions(select, 'acp');
    await user.click(screen.getByRole('button', { name: '保存设置' }));
    expect(await screen.findByText('切换上下文压缩机制？')).toBeTruthy();
    expect(screen.getByText(/当前租户的全部会话/)).toBeTruthy();

    // 取消：不发 PUT
    await user.click(screen.getByRole('button', { name: '取消' }));
    await waitFor(() => {
      const puts = fetchMock.mock.calls.filter(([, init]) => init?.method === 'PUT');
      expect(puts).toHaveLength(1);
    });
  });

  it('disables ACP controls when acp_enabled is false', async () => {
    const fetchMock = makeFetchMock(makeUiConfig({ acp_enabled: false, context_compression_mode: 'acp' }));
    vi.stubGlobal('fetch', fetchMock);

    renderPage();

    const select = (await screen.findByLabelText(/上下文压缩机制/)) as HTMLSelectElement;
    const acpOption = select.querySelector('option[value="acp"]') as HTMLOptionElement;
    expect(acpOption.disabled).toBe(true);
    expect(acpOption.textContent).toContain('未启用');

    const limitInput = (await screen.findByLabelText(/上下文上限/)) as HTMLInputElement;
    expect(limitInput.disabled).toBe(true);
    expect((screen.getByLabelText(/常规压缩触发阈值/) as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText(/紧急压缩触发阈值/) as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText(/最低压缩触发阈值/) as HTMLInputElement).disabled).toBe(true);
  });

  it('rejects an empty ACP threshold input with an error toast and no PUT', async () => {
    const user = userEvent.setup();
    const fetchMock = makeFetchMock(makeUiConfig({ context_compression_mode: 'acp' }));
    vi.stubGlobal('fetch', fetchMock);

    renderPage();

    const limitInput = (await screen.findByLabelText(/上下文上限/)) as HTMLInputElement;
    await user.clear(limitInput);
    await user.click(screen.getByRole('button', { name: '保存设置' }));

    await waitFor(() => expect(toastMock.custom).toHaveBeenCalled());
    const puts = fetchMock.mock.calls.filter(([, init]) => init?.method === 'PUT');
    expect(puts).toHaveLength(0);
  });

  it('rejects an out-of-range ratio with an error toast and no PUT', async () => {
    const user = userEvent.setup();
    const fetchMock = makeFetchMock(makeUiConfig({ context_compression_mode: 'acp' }));
    vi.stubGlobal('fetch', fetchMock);

    renderPage();

    const maxPctInput = (await screen.findByLabelText(/常规压缩触发阈值/)) as HTMLInputElement;
    await user.clear(maxPctInput);
    await user.type(maxPctInput, '1.5');
    await user.click(screen.getByRole('button', { name: '保存设置' }));

    await waitFor(() => expect(toastMock.custom).toHaveBeenCalled());
    const puts = fetchMock.mock.calls.filter(([, init]) => init?.method === 'PUT');
    expect(puts).toHaveLength(0);
  });

  it('rejects a context limit below 1 with an error toast and no PUT', async () => {
    const user = userEvent.setup();
    const fetchMock = makeFetchMock(makeUiConfig({ context_compression_mode: 'acp' }));
    vi.stubGlobal('fetch', fetchMock);

    renderPage();

    const limitInput = (await screen.findByLabelText(/上下文上限/)) as HTMLInputElement;
    await user.clear(limitInput);
    await user.type(limitInput, '0');
    await user.click(screen.getByRole('button', { name: '保存设置' }));

    await waitFor(() => expect(toastMock.custom).toHaveBeenCalled());
    const puts = fetchMock.mock.calls.filter(([, init]) => init?.method === 'PUT');
    expect(puts).toHaveLength(0);
  });

  it('rejects threshold ratios out of order with an error toast and no PUT', async () => {
    const user = userEvent.setup();
    const fetchMock = makeFetchMock(makeUiConfig({ context_compression_mode: 'acp' }));
    vi.stubGlobal('fetch', fetchMock);

    renderPage();

    // 最低阈值 0.9 > 常规阈值 0.7，违反 最低 <= 常规 <= 紧急
    const minPctInput = (await screen.findByLabelText(/最低压缩触发阈值/)) as HTMLInputElement;
    await user.clear(minPctInput);
    await user.type(minPctInput, '0.9');
    await user.click(screen.getByRole('button', { name: '保存设置' }));

    await waitFor(() => expect(toastMock.custom).toHaveBeenCalled());
    const puts = fetchMock.mock.calls.filter(([, init]) => init?.method === 'PUT');
    expect(puts).toHaveLength(0);
  });
});

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
  context_compression_mode: 'legacy' as const,
  acp_model_context_limit: '128000',
  acp_nudge_max_pct: '0.70',
  acp_nudge_emergency_pct: '0.85',
  acp_nudge_min_pct: '0.45',
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
