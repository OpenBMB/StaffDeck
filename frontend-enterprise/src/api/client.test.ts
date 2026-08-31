import { describe, expect, it } from 'vitest';

import { ApiError } from './client';

describe('ApiError', () => {
  it('preserves a structured backend error code and human-readable message', () => {
    const error = new ApiError(404, JSON.stringify({
      detail: {
        code: 'EVOLUTION_FEEDBACK_NOT_FOUND',
        message: '未找到可用于改进的 Skill 或 SOP 反馈',
      },
    }), 'Not Found');

    expect(error.code).toBe('EVOLUTION_FEEDBACK_NOT_FOUND');
    expect(error.message).toBe('未找到可用于改进的 Skill 或 SOP 反馈');
  });

  it('keeps validation detail formatting compatible', () => {
    const error = new ApiError(422, JSON.stringify({
      detail: [{ loc: ['body', 'name'], msg: 'Field required' }],
    }), 'Unprocessable Entity');

    expect(error.code).toBeUndefined();
    expect(error.message).toBe('body.name: Field required');
  });

  it('recognizes a stable error code returned as a string detail', () => {
    const error = new ApiError(422, JSON.stringify({
      detail: 'MODEL_PROTOCOL_OPTIONS_INVALID',
    }), 'Unprocessable Entity');

    expect(error.code).toBe('MODEL_PROTOCOL_OPTIONS_INVALID');
    expect(error.message).toBe('MODEL_PROTOCOL_OPTIONS_INVALID');
  });

  it('replaces an oversized non-JSON body (e.g. an intercepting proxy block page) with a generic message', () => {
    const interceptPage = `<!DOCTYPE html><html><head><title>Attention Required!</title></head>`
      + `<body>Your IP address 203.0.113.7 has been blocked by the network gateway. `
      + `Please contact your administrator for more information.</body></html>`;
    expect(interceptPage.length).toBeGreaterThan(160);

    const error = new ApiError(403, interceptPage, 'Forbidden');

    expect(error.message).toBe('操作失败，请稍后重试。');
    expect(error.message).not.toContain('<');
    expect(error.message).not.toContain('203.0.113.7');
    expect(error.code).toBeUndefined();
  });

  it('keeps a short non-JSON body as the message', () => {
    const error = new ApiError(503, 'Service Unavailable', 'Service Unavailable');

    expect(error.message).toBe('Service Unavailable');
  });

  it('keeps a normal short JSON error body intact', () => {
    const error = new ApiError(400, JSON.stringify({ detail: 'invalid request' }), 'Bad Request');

    expect(error.message).toBe('invalid request');
  });
});
