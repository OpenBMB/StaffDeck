import { describe, expect, it } from 'vitest';

import {
  classifyRetrievalChange,
  defaultRetrievalDraft,
  mergeRetrievalConfig,
} from './retrievalSettings';

describe('retrieval settings', () => {
  it('uses 1024 as the Zhipu Embedding-3 preset', () => {
    expect(defaultRetrievalDraft().embedding.options.dimensions).toBe(1024);
  });

  it('classifies BM25 and reranker changes as hot changes', () => {
    const base = defaultRetrievalDraft();
    const result = classifyRetrievalChange(base, {
      ...base,
      bm25: { ...base.bm25, options: { ...base.bm25.options, k1: 0.8 } },
      reranker: {
        ...base.reranker,
        options: { ...base.reranker.options, rerank_limit: 8 },
      },
    });

    expect(result.requiresReindex).toBe(false);
    expect(result.changedFields).toEqual(['bm25.options.k1', 'reranker.options.rerank_limit']);
  });

  it('classifies an embedding dimension change as requiring reindex', () => {
    const base = defaultRetrievalDraft();
    const result = classifyRetrievalChange(base, {
      ...base,
      embedding: {
        ...base.embedding,
        options: { ...base.embedding.options, dimensions: 512 },
      },
    });

    expect(result.requiresReindex).toBe(true);
  });

  it('keeps the write-only API key blank when loading a saved config', () => {
    const draft = mergeRetrievalConfig(defaultRetrievalDraft(), {
      name: '已保存',
      embedding_api_key_masked: 'sk-****1234',
      embedding: {
        adapter: 'zhipu_embedding',
        model: 'embedding-3',
        base_url: 'https://example.test/v1',
        dimension_mode: 'explicit',
        dimensions: 1024,
      },
    });

    expect(draft.name).toBe('已保存');
    expect(draft.embedding.apiKey).toBe('');
    expect(draft.embedding.apiKeyMasked).toBe('sk-****1234');
  });
});
