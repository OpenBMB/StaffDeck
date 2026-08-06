import { describe, expect, it } from 'vitest';

import { buildDistillFailure } from './distillFailure';

describe('distill failure details', () => {
  it('preserves the provider error and extracts its code', () => {
    const failure = buildDistillFailure(
      'LLM provider request failed (APIConnectionError); message=Connection error; provider_code=upstream_timeout',
      'distill',
    );

    expect(failure).toEqual({
      code: 'upstream_timeout',
      detail:
        'LLM provider request failed (APIConnectionError); message=Connection error; provider_code=upstream_timeout',
      stage: 'SOP 草稿生成',
      summary: '生成失败',
    });
  });

  it('provides an actionable fallback without inventing an error code', () => {
    expect(buildDistillFailure('改写失败，当前草稿未变更。', 'rewrite')).toEqual({
      code: undefined,
      detail: '上游没有返回详细错误信息，请检查所选模型配置及服务日志。',
      stage: 'SOP 局部改写',
      summary: '改写失败',
    });
  });
});
