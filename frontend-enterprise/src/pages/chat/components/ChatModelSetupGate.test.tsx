// @vitest-environment jsdom

import { createElement } from 'react';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '@/api/client';
import { I18nProvider } from '@/i18n';
import type { CodexSubscriptionAccountRead, ModelConfigRead } from '@/types';
import ChatModelSetupGate, { type ChatModelSetupGateProps } from './ChatModelSetupGate';

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>();
  return {
    ...actual,
    api: { ...actual.api, get: vi.fn(), post: vi.fn(), put: vi.fn() },
  };
});

const mockedGet = vi.mocked(api.get);
const mockedPost = vi.mocked(api.post);

const requiresLogin = {
  status: 'requires_login',
  plan_type: null,
  message: '尚未连接',
} as CodexSubscriptionAccountRead;

/** 渲染带国际化上下文的聊天模型门禁。 */
function renderGate(overrides: Partial<ChatModelSetupGateProps> = {}) {
  const props: ChatModelSetupGateProps = {
    open: true,
    tenantId: 'tenant-isolated',
    canConfigure: true,
    onOpenChange: vi.fn(),
    onConfigured: vi.fn(),
    ...overrides,
  };
  return {
    props,
    ...render(createElement(I18nProvider, null, createElement(ChatModelSetupGate, props))),
  };
}

/** 补齐 Radix Select 在 jsdom 中依赖的指针捕获接口。 */
function stubSelectPointerCapture() {
  if (!Element.prototype.hasPointerCapture) Element.prototype.hasPointerCapture = () => false;
  if (!Element.prototype.releasePointerCapture) Element.prototype.releasePointerCapture = () => {};
  if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {};
}

beforeEach(() => {
  stubSelectPointerCapture();
  mockedGet.mockReset();
  mockedPost.mockReset();
  mockedGet.mockImplementation((url: unknown) => {
    const requestUrl = String(url);
    if (requestUrl.includes('/protocols')) {
      return Promise.resolve({ protocols: ['openai_chat_completions'] });
    }
    if (requestUrl.includes('/codex-subscription/account')) return Promise.resolve(requiresLogin);
    return Promise.reject(new Error(`unexpected api.get call: ${requestUrl}`));
  });
});

afterEach(() => cleanup());

describe('ChatModelSetupGate', () => {
  it('shows the shared channel wizard for an administrator and scopes all reads to the chat tenant', async () => {
    renderGate();

    expect(await screen.findByRole('option', { name: 'OpenAI' })).toBeTruthy();
    expect(screen.getByRole('option', { name: 'ChatGPT 订阅（Codex）' })).toBeTruthy();
    expect(screen.getByRole('option', { name: '自定义渠道' })).toBeTruthy();
    expect(screen.queryByText('需要先配置模型')).toBeNull();

    expect(mockedGet).toHaveBeenCalledWith(
      '/api/enterprise/model-configs/protocols?tenant_id=tenant-isolated',
    );
    expect(mockedGet).toHaveBeenCalledWith(
      '/api/enterprise/model-configs/codex-subscription/account?tenant_id=tenant-isolated',
    );
  });

  it('removes draft save from the chat gate and completes only after a passing test', async () => {
    const savedModel = { id: 'model-verified', name: 'OpenAI · gpt-4o' } as ModelConfigRead;
    mockedPost.mockImplementation((url: unknown) => {
      if (String(url).includes('/list-models')) return Promise.resolve({ success: false, models: [] });
      if (String(url).includes('verify_before_save=true')) return Promise.resolve(savedModel);
      return Promise.reject(new Error(`unexpected api.post call: ${String(url)}`));
    });
    const user = userEvent.setup();
    const { props } = renderGate();

    await user.click(await screen.findByRole('option', { name: 'OpenAI' }));
    await user.click(screen.getByRole('button', { name: '下一步' }));
    expect(screen.queryByRole('button', { name: '保存' })).toBeNull();

    await user.type(screen.getByPlaceholderText('sk-...'), 'sk-test-123');
    await user.type(screen.getByPlaceholderText('选择或输入模型'), 'gpt-4o');
    await user.click(screen.getByRole('button', { name: '测试' }));

    await waitFor(() => expect(props.onConfigured).toHaveBeenCalledWith(savedModel));
    const saveCall = mockedPost.mock.calls.find(([url]) => String(url).includes('verify_before_save=true'));
    expect(saveCall?.[1]).toMatchObject({ tenant_id: 'tenant-isolated', enabled: true });
  });

  it('shows only the administrator-contact notice to users without model-management permission', async () => {
    renderGate({ canConfigure: false });

    expect(screen.getByText('当前账号没有模型管理权限，请联系管理员完成模型配置和连通性测试。')).toBeTruthy();
    expect(screen.queryByRole('option', { name: 'OpenAI' })).toBeNull();
    expect(screen.queryByText('API Key')).toBeNull();
    await waitFor(() => expect(mockedGet).not.toHaveBeenCalled());
  });
});
