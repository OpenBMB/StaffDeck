// @vitest-environment jsdom

import { createElement } from 'react';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/client';
import { I18nProvider } from '../i18n';
import { OPEN_MODEL_CREATE_EVENT } from '@/components/QuickStartGuide';
import ModelsPage, {
  modelActionError,
  modelAuthModeLabel,
  modelProviderDiagnosticText,
  modelProviderErrorMessage,
} from './ModelsPage';

const subscriptionStatusCopy = {
  '已连接本机 Codex 管理的 ChatGPT 订阅。': {
    status: 'connected',
    translation: 'Connected to the ChatGPT subscription managed by local Codex.',
  },
  '请在 Codex 打开的浏览器页面完成 ChatGPT 登录。': {
    status: 'pending',
    translation: 'Complete the ChatGPT login in the browser opened by Codex.',
  },
  '本机 Codex 尚未登录 ChatGPT 订阅。': {
    status: 'requires_login',
    translation: 'Local Codex is not signed in to the ChatGPT subscription.',
  },
} as const;

// 构造满足前端请求封装的成功响应，避免测试依赖真实网络。
function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body),
  } as Response;
}

// 模拟模型页初始化请求，并把订阅状态交给真实页面渲染。
function stubModelsPageFetch(subscriptionAccount: {
  status: 'connected' | 'pending' | 'requires_login' | 'unavailable';
  plan_type: null;
  message: string;
}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/codex-subscription/account')) return jsonResponse(subscriptionAccount);
    if (url.includes('/model-configs/protocols')) {
      return jsonResponse({ protocols: ['openai_chat_completions'] });
    }
    if (url.includes('/model-configs')) return jsonResponse([]);
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

// 补齐 jsdom 未实现的指针捕获 API，供生产 Select 组件处理用户点击。
function stubSelectPointerCapture() {
  if (!Element.prototype.hasPointerCapture) {
    Element.prototype.hasPointerCapture = () => false;
  }
  if (!Element.prototype.releasePointerCapture) {
    Element.prototype.releasePointerCapture = () => {};
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = () => {};
  }
}

beforeEach(() => {
  stubSelectPointerCapture();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe('model provider diagnostics', () => {
  it('shows a friendly message and keeps raw upstream fields out of it', () => {
    const providerError = {
      code: 'MODEL_UPSTREAM_ERROR',
      message: 'provider failed',
      upstream_status: 422,
      provider_code: 'invalid_model',
      provider_message: 'model does not exist',
      upstream_body: '<html><body>blocked by WAF from 203.0.113.7</body></html>',
      request_id: 'req_123',
    };

    const message = modelProviderErrorMessage(providerError, '测试失败');
    expect(message).toBe('模型服务返回了错误，请稍后重试或联系管理员。');
    expect(message).not.toContain('<');
    expect(message).not.toContain('上游');
    expect(message).not.toContain(providerError.upstream_body);

    const diagnostic = modelProviderDiagnosticText(providerError);
    expect(diagnostic).toContain('HTTP 状态码：422');
    expect(diagnostic).toContain('上游错误码：invalid_model');
    expect(diagnostic).toContain('上游消息：model does not exist');
    expect(diagnostic).toContain(providerError.upstream_body);
    expect(diagnostic).toContain('Request ID：req_123');
  });

  it('reads structured provider diagnostics from a failed save response without leaking them into the main message', () => {
    const error = new ApiError(502, JSON.stringify({
      detail: {
        code: 'MODEL_UPSTREAM_ERROR',
        message: 'provider failed',
        upstream_status: 400,
        provider_code: 'invalid_request',
        upstream_body: '<html><body>blocked by WAF from 203.0.113.7</body></html>',
      },
    }), 'Bad Gateway');

    const message = modelActionError(error, '保存失败');
    expect(message).toBe('模型服务返回了错误，请稍后重试或联系管理员。');
    expect(message).not.toContain('<');
    expect(message).not.toContain('上游');

    const payload = JSON.parse(error.body) as {
      detail: { code: string; message: string; upstream_status: number; provider_code: string; upstream_body: string };
    };
    const diagnostic = modelProviderDiagnosticText(payload.detail);
    expect(diagnostic).toContain('HTTP 状态码：400');
    expect(diagnostic).toContain('上游错误码：invalid_request');
    expect(diagnostic).toContain(payload.detail.upstream_body);
  });

  it('falls back to the generic provider message for an unmapped provider error code', () => {
    expect(modelProviderErrorMessage({
      code: 'MODEL_SOME_FUTURE_CODE',
      message: 'provider failed',
    }, '测试失败')).toBe('连接模型服务失败，请稍后重试或联系管理员。');
  });

  it('localizes a stable model configuration error code', () => {
    const error = new ApiError(422, JSON.stringify({
      detail: 'MODEL_PROTOCOL_OPTIONS_INVALID',
    }), 'Unprocessable Entity');

    expect(modelActionError(error, '保存失败')).toBe(
      '模型协议选项无效，请检查 API 协议与协议参数',
    );
  });

  it('wraps an unknown stable error code instead of exposing a bare token', () => {
    const error = new ApiError(422, JSON.stringify({ detail: 'MODEL_NEW_FAILURE' }), '');

    expect(modelActionError(error, '保存失败')).toBe('操作失败（错误码：MODEL_NEW_FAILURE）');
  });

  it('labels authentication modes and explains subscription-specific failures', () => {
    expect(modelAuthModeLabel('api_key')).toBe('API Key');
    expect(modelAuthModeLabel('chatgpt_subscription')).toBe('ChatGPT 订阅（Codex）');
    expect(modelProviderErrorMessage({
      code: 'MODEL_SUBSCRIPTION_AUTH_REQUIRED',
      message: 'login required',
    }, '测试失败')).toBe('请先在本机 Codex 中登录 ChatGPT 订阅，再测试或启用此模型。');
  });

  it('renders ChatGPT subscription account statuses from the API in English', async () => {
    window.localStorage.setItem('staffdeck_locale', 'en-US');

    for (const [message, account] of Object.entries(subscriptionStatusCopy)) {
      const fetchMock = stubModelsPageFetch({
        status: account.status,
        plan_type: null,
        message,
      });
      const user = userEvent.setup();
      render(createElement(I18nProvider, null, createElement(ModelsPage)));

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          expect.stringContaining('/codex-subscription/account'),
          expect.anything(),
        );
      });
      window.dispatchEvent(new CustomEvent(OPEN_MODEL_CREATE_EVENT));
      await user.click(await screen.findByRole('option', { name: 'ChatGPT Subscription (Codex)' }));
      await user.click(await screen.findByRole('button', { name: 'Next' }));
      expect(await screen.findByText(account.translation)).toBeTruthy();
      expect(screen.queryByText(/local Codex subscription runtime/i)).toBeNull();

      cleanup();
      vi.unstubAllGlobals();
      window.localStorage.setItem('staffdeck_locale', 'en-US');
    }
  });

  it('explains that Codex, rather than StaffDeck, owns ChatGPT login credentials', async () => {
    const fetchMock = stubModelsPageFetch({
      status: 'connected',
      plan_type: null,
      message: '已连接本机 Codex 管理的 ChatGPT 订阅。',
    });
    const user = userEvent.setup();
    render(createElement(I18nProvider, null, createElement(ModelsPage)));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/codex-subscription/account'),
        expect.anything(),
      );
    });
    window.dispatchEvent(new CustomEvent(OPEN_MODEL_CREATE_EVENT));
    await user.click(await screen.findByRole('option', { name: 'ChatGPT 订阅（Codex）' }));
    await user.click(await screen.findByRole('button', { name: '下一步' }));

    expect(await screen.findByText(
      '登录由本机 Codex runtime 管理。StaffDeck 不保存 ChatGPT OAuth code、access token 或 refresh token。',
    )).toBeTruthy();
    expect(screen.queryByText(/加密保存必要订阅凭据/)).toBeNull();
  });
});
