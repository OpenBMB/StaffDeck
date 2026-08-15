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
});
