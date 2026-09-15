import { describe, expect, it, vi } from 'vitest';

import { ApiError, api } from './client';
import { ReadBackoff } from './read-backoff';

describe('ApiError', () => {
  it('never renders a gateway HTML page as the error message', () => {
    const html = '<html><head><title>502 Bad Gateway</title></head><body>nginx</body></html>';
    for (const body of [html, JSON.stringify({ detail: html })]) {
      const error = new ApiError(502, body, 'Bad Gateway');
      expect(error.message).toContain('HTTP 502');
      expect(error.message).not.toContain('<html>');
      expect(error.code).toBe('UPSTREAM_INVALID_RESPONSE');
    }
  });

  it('backs off one failing read endpoint without blocking other endpoints', () => {
    const backoff = new ReadBackoff();
    const error = new Error('down');
    backoff.failed('/sessions', error, 0);
    expect(backoff.blocked('/sessions', 999)).toBe(error);
    expect(backoff.blocked('/models', 999)).toBeNull();
    expect(backoff.blocked('/sessions', 1000)).toBeNull();
    backoff.failed('/sessions', error, 1000);
    expect(backoff.blocked('/sessions', 2999)).toBe(error);
    backoff.succeeded('/sessions');
    expect(backoff.blocked('/sessions', 1001)).toBeNull();
  });

  it('does not replay or block a write when the read endpoint is unavailable', async () => {
    const fetcher = vi.fn(async () => new Response('<html>502 Bad Gateway</html>', { status: 502 }));
    vi.stubGlobal('fetch', fetcher);
    vi.stubGlobal('window', { localStorage: { getItem: () => null } });
    try {
      await expect(api.get('/api/chat/sessions?tenant_id=backoff-test')).rejects.toMatchObject({ status: 502 });
      await expect(api.get('/api/chat/sessions?tenant_id=backoff-test')).rejects.toMatchObject({ status: 502 });
      expect(fetcher).toHaveBeenCalledTimes(1);
      await expect(api.post('/api/chat/sessions?tenant_id=backoff-test', {})).rejects.toMatchObject({ status: 502 });
      expect(fetcher).toHaveBeenCalledTimes(2);
    } finally { vi.unstubAllGlobals(); }
  });
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
});
