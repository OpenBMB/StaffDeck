import { describe, expect, it } from 'vitest';

import { ApiError } from '../api/client';
import { modelActionError, modelProviderDiagnosticText, modelProviderErrorMessage } from './ModelsPage';

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
});
