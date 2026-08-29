// @vitest-environment jsdom

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { Bm25SettingsDialog } from './knowledge-retrieval/Bm25SettingsDialog';
import { EmbeddingSettingsDialog } from './knowledge-retrieval/EmbeddingSettingsDialog';
import { FusionSettingsDialog } from './knowledge-retrieval/FusionSettingsDialog';
import { RerankerSettingsDialog } from './knowledge-retrieval/RerankerSettingsDialog';
import {
  defaultRetrievalDraft,
  type RetrievalDraft,
} from './knowledge-retrieval/retrievalSettings';

describe('retrieval settings dialogs', () => {
  it('exposes the four independently editable windows', () => {
    const draft: RetrievalDraft = defaultRetrievalDraft();
    const onChange = () => undefined;
    render(
      <>
        <EmbeddingSettingsDialog open value={draft.embedding} onOpenChange={onChange} onChange={onChange} capabilities={{ embedding: [{ id: 'zhipu_embedding', label: '智谱 Embedding', fields: { dimensions: { options: [256, 512, 1024, 2048] }, batch_size: { max: 64 } } }], bm25: [], fusion: [], reranker: [] }} />
        <Bm25SettingsDialog open value={draft.bm25} onOpenChange={onChange} onChange={onChange} />
        <FusionSettingsDialog open value={draft.fusion} onOpenChange={onChange} onChange={onChange} />
        <RerankerSettingsDialog open value={draft.reranker} onOpenChange={onChange} onChange={onChange} />
      </>,
    );

    expect(screen.getByText('Embedding 向量化设置')).toBeTruthy();
    expect(screen.getByText('BM25 关键词检索设置')).toBeTruthy();
    expect(screen.getByText('融合与候选集设置')).toBeTruthy();
    expect(screen.getByText('Reranker 重排设置')).toBeTruthy();
    expect(document.querySelector('option[value="2048"]')).toBeTruthy();
  });
});
