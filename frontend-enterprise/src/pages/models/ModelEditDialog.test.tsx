// @vitest-environment jsdom

import { createElement } from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '@/api/client';
import { I18nProvider } from '@/i18n';
import type { ModelConfigRead } from '@/types';
import ModelEditDialog, { type ModelEditDialogProps } from './ModelEditDialog';

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>();
  return {
    ...actual,
    api: { ...actual.api, put: vi.fn() },
  };
});

vi.mock('@/components/ui/app-toast', () => ({
  notify: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn(), loading: vi.fn(), dismiss: vi.fn() },
}));

const mockedPut = vi.mocked(api.put);

function stubSelectPointerCapture() {
  if (!Element.prototype.hasPointerCapture) {
    Element.prototype.hasPointerCapture = () => false;
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = () => {};
  }
}

const SELECTED_MODEL: ModelConfigRead = {
  id: 'model-1',
  tenant_id: 'tenant_demo',
  name: 'OpenAI · gpt-4o',
  provider: 'openai_compatible',
  auth_mode: 'api_key',
  api_protocol: 'openai_chat_completions',
  base_url: 'https://api.openai.com/v1',
  api_key_masked: 'sk-****1234',
  model: 'gpt-4o',
  temperature: 0.2,
  max_output_tokens: 8192,
  extra_body: {},
  protocol_options: {},
  legacy_unmapped_options: {},
  trust_status: 'verified',
  verification_attempt_status: 'succeeded',
  config_revision: 1,
  security_revision: 1,
  is_default: false,
  enabled: true,
  updated_at: '2026-08-30T00:00:00Z',
};

function renderDialog(overrides: Partial<ModelEditDialogProps> = {}) {
  const props: ModelEditDialogProps = {
    open: true,
    selected: SELECTED_MODEL,
    availableProtocols: ['openai_chat_completions', 'anthropic_messages', 'gemini_generate_content'],
    subscriptionAccount: null,
    subscriptionLoading: false,
    onStartSubscriptionLogin: vi.fn(),
    onCancelSubscriptionLogin: vi.fn(),
    onRequestSubscriptionLogout: vi.fn(),
    onOpenChange: vi.fn(),
    onSaved: vi.fn(),
    ...overrides,
  };
  return { props, ...render(createElement(I18nProvider, null, createElement(ModelEditDialog, props))) };
}

beforeEach(async () => {
  stubSelectPointerCapture();
  mockedPut.mockReset();
  const { notify } = await import('@/components/ui/app-toast');
  vi.mocked(notify.error).mockReset();
});

afterEach(() => {
  cleanup();
});

describe('ModelEditDialog — numeric field validation', () => {
  it('rejects a blank Temperature instead of silently sending 0', async () => {
    const { notify } = await import('@/components/ui/app-toast');
    const user = userEvent.setup();
    renderDialog();

    await user.clear(screen.getByDisplayValue('0.2'));
    await user.click(screen.getByRole('button', { name: '保存' }));

    expect(notify.error).toHaveBeenCalledWith('Temperature 与 Max Tokens 必须是数字');
    expect(mockedPut).not.toHaveBeenCalled();
  });

  it('rejects a blank Max Tokens instead of silently sending 0', async () => {
    const { notify } = await import('@/components/ui/app-toast');
    const user = userEvent.setup();
    renderDialog();

    await user.clear(screen.getByDisplayValue('8192'));
    await user.click(screen.getByRole('button', { name: '保存' }));

    expect(notify.error).toHaveBeenCalledWith('Temperature 与 Max Tokens 必须是数字');
    expect(mockedPut).not.toHaveBeenCalled();
  });

  it('saves successfully when both numeric fields are filled', async () => {
    mockedPut.mockResolvedValue(SELECTED_MODEL);
    const { notify } = await import('@/components/ui/app-toast');
    const user = userEvent.setup();
    renderDialog();

    await user.click(screen.getByRole('button', { name: '保存' }));

    expect(mockedPut).toHaveBeenCalled();
    expect(notify.error).not.toHaveBeenCalled();
  });
});
