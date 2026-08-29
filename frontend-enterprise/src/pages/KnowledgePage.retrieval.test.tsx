import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { KnowledgeRetrievalPanel } from './KnowledgePage';

describe('KnowledgeRetrievalPanel', () => {
  it('shows hybrid retrieval controls and keeps the key write-only', () => {
    const rendered = renderToStaticMarkup(
      createElement(KnowledgeRetrievalPanel, { tenantId: 'tenant_demo' }),
    );

    expect(rendered).toContain('混合检索配置');
    expect(rendered).toContain('Embedding 向量化');
    expect(rendered).toContain('BM25 关键词检索');
    expect(rendered).toContain('融合与候选集');
    expect(rendered).toContain('Reranker 重排');
    expect(rendered).toContain('重建缺失向量');
    expect(rendered).toContain('保存并热应用');
    expect(rendered).not.toContain('embedding_api_key_masked');
  });
});
