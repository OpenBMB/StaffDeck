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
    expect(rendered).toContain('Embedding 地址');
    expect(rendered).toContain('Reranker 模式');
    expect(rendered).toContain('Reranker 模型');
    expect(rendered).toContain('重建缺失向量');
    expect(rendered).toContain('API Key（只写入）');
    expect(rendered).toContain('请输入 API Key');
    expect(rendered).not.toContain('embedding_api_key_masked');
  });
});
