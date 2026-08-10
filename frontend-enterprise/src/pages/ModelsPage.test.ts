import { describe, expect, it } from 'vitest';

import { ApiError } from '../api/client';
import { modelActionError, modelProviderErrorMessage } from './ModelsPage';

describe('model provider diagnostics', () => {
  it('renders upstream status, provider code, body and request id', () => {
    expect(modelProviderErrorMessage({
      code: 'MODEL_UPSTREAM_ERROR',
      message: 'provider failed',
      upstream_status: 422,
      provider_code: 'invalid_model',
      provider_message: 'model does not exist',
      upstream_body: '{"error":{"code":"invalid_model"}}',
      request_id: 'req_123',
    }, '测试失败')).toBe(
      'MODEL_UPSTREAM_ERROR；HTTP 422；上游错误码：invalid_model；'
      + '上游消息：model does not exist；上游响应：{"error":{"code":"invalid_model"}}；'
      + 'Request ID：req_123',
    );
  });

  it('reads structured provider diagnostics from a failed save response', () => {
    const error = new ApiError(502, JSON.stringify({
      detail: {
        code: 'MODEL_UPSTREAM_ERROR',
        message: 'provider failed',
        upstream_status: 400,
        provider_code: 'invalid_request',
        upstream_body: '{"error":"bad request"}',
      },
    }), 'Bad Gateway');

    expect(modelActionError(error, '保存失败')).toContain(
      'MODEL_UPSTREAM_ERROR；HTTP 400；上游错误码：invalid_request',
    );
    expect(modelActionError(error, '保存失败')).toContain('上游响应：{"error":"bad request"}');
  });
});
