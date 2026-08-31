import { describe, expect, it } from 'vitest';

import { apiErrorMessage } from './apiErrorMessages';

describe('apiErrorMessage', () => {
  it('localizes a known stable error code passed as a raw string', () => {
    expect(apiErrorMessage('MODEL_PROTOCOL_OPTIONS_INVALID', '保存失败')).toBe(
      '模型协议选项无效，请检查 API 协议与协议参数',
    );
  });

  it('wraps an unknown stable error code without rewriting normal diagnostics', () => {
    expect(apiErrorMessage('SOME_NEW_BACKEND_FAILURE', '操作失败')).toBe(
      '操作失败（错误码：SOME_NEW_BACKEND_FAILURE）',
    );
    expect(apiErrorMessage('upstream request failed: timeout', '操作失败')).toBe(
      'upstream request failed: timeout',
    );
  });

  it('replaces an oversized raw message and fallback with the generic notice', () => {
    const rawHtml = `<html><body>${'blocked by intercepting proxy '.repeat(10)}</body></html>`;
    expect(rawHtml.length).toBeGreaterThan(160);

    expect(apiErrorMessage(rawHtml, rawHtml)).toBe('操作失败，请稍后重试。');
  });

  it('keeps a normal short message intact even when it is not a stable error code', () => {
    expect(apiErrorMessage('invalid_model', '操作失败')).toBe('invalid_model');
  });
});
