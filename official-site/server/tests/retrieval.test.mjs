import assert from 'node:assert/strict';
import test from 'node:test';

import { createRetriever, tokenize } from '../lib/retrieval.mjs';

test('tokenize produces semantic units for Chinese and English text', () => {
  const tokens = tokenize('数字员工 StaffDeck workflow');
  assert(tokens.includes('数字'));
  assert(tokens.includes('staffdeck'));
  assert(tokens.includes('workflow'));
});
test('retriever ranks related product material without routing keywords', () => {
  const search = createRetriever({ chunks: [
    { id: 'a', title: '定时任务', text: '数字员工可以按计划主动执行任务。' },
    { id: 'b', title: '知识库', text: '企业文档被整理为可引用的知识来源。' },
  ] });
  const [result] = search('如何让员工按照计划主动工作', 1);
  assert.equal(result.id, 'a');
});
