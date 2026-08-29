import type { KnowledgeRetrievalConfigRead } from '../../types';

export type EmbeddingDraft = {
  adapter: string;
  baseUrl: string;
  model: string;
  apiKey: string;
  apiKeyMasked: string;
  options: {
    dimension_mode: 'auto' | 'explicit';
    dimensions: number | null;
    batch_size: number;
    timeout_seconds: number;
    max_retries: number;
    retry_backoff_ms: number;
    max_input_tokens: number | null;
    oversize_policy: 'fail' | 'safe_truncate';
    similarity_threshold: number;
    extra_params: Record<string, unknown>;
  };
};

export type Bm25Draft = {
  adapter: 'builtin_bm25';
  options: {
    enabled: boolean;
    k1: number;
    b: number;
    candidate_limit: number;
    minimum_score: number;
    tokenizer: 'cjk_bigram_latin';
    cjk_ngram: number;
    preserve_identifiers: boolean;
    deduplicate_query_terms: boolean;
    stopwords_enabled: boolean;
    stopwords: string[];
    content_weight: number;
    summary_weight: number;
    source_ref_weight: number;
  };
};

export type FusionDraft = {
  adapter: 'weighted_rrf';
  options: {
    mode: 'hybrid' | 'bm25' | 'vector';
    bm25_limit: number;
    vector_limit: number;
    rrf_k: number;
    bm25_weight: number;
    vector_weight: number;
    final_limit: number;
  };
};

export type RerankerDraft = {
  adapter: string;
  baseUrl: string;
  model: string;
  apiKey: string;
  apiKeyMasked: string;
  options: {
    mode: 'none' | 'dedicated_api' | 'llm';
    candidate_limit: number;
    rerank_limit: number;
    minimum_score: number;
    max_query_chars: number;
    max_document_chars: number;
    timeout_seconds: number;
    max_retries: number;
    retry_backoff_ms: number;
    temperature: number;
    max_output_tokens: number;
    input_budget_tokens: number;
    prompt_template: string;
    return_documents: boolean;
    return_raw_scores: boolean;
    extra_params: Record<string, unknown>;
  };
};

export type RetrievalDraft = {
  name: string;
  embedding: EmbeddingDraft;
  bm25: Bm25Draft;
  fusion: FusionDraft;
  reranker: RerankerDraft;
  candidateLimit: number;
  rerankLimit: number;
  enabled: boolean;
  revision?: number;
};

export type RetrievalChange = {
  requiresReindex: boolean;
  changedFields: string[];
};

export function defaultRetrievalDraft(): RetrievalDraft {
  return {
    name: '知识库混合检索',
    embedding: {
      adapter: 'zhipu_embedding',
      baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
      model: 'embedding-3',
      apiKey: '',
      apiKeyMasked: '',
      options: {
        dimension_mode: 'explicit',
        dimensions: 1024,
        batch_size: 32,
        timeout_seconds: 120,
        max_retries: 2,
        retry_backoff_ms: 500,
        max_input_tokens: 3072,
        oversize_policy: 'fail',
        similarity_threshold: 0,
        extra_params: {},
      },
    },
    bm25: {
      adapter: 'builtin_bm25',
      options: {
        enabled: true,
        k1: 1.5,
        b: 0.75,
        candidate_limit: 40,
        minimum_score: 0,
        tokenizer: 'cjk_bigram_latin',
        cjk_ngram: 2,
        preserve_identifiers: true,
        deduplicate_query_terms: true,
        stopwords_enabled: false,
        stopwords: [],
        content_weight: 1,
        summary_weight: 0,
        source_ref_weight: 0,
      },
    },
    fusion: {
      adapter: 'weighted_rrf',
      options: {
        mode: 'hybrid',
        bm25_limit: 40,
        vector_limit: 40,
        rrf_k: 60,
        bm25_weight: 1,
        vector_weight: 1,
        final_limit: 40,
      },
    },
    reranker: {
      adapter: 'llm_rerank',
      baseUrl: '',
      model: '',
      apiKey: '',
      apiKeyMasked: '',
      options: {
        mode: 'none',
        candidate_limit: 40,
        rerank_limit: 12,
        minimum_score: 0,
        max_query_chars: 4096,
        max_document_chars: 2000,
        timeout_seconds: 120,
        max_retries: 2,
        retry_backoff_ms: 500,
        temperature: 0,
        max_output_tokens: 2048,
        input_budget_tokens: 32000,
        prompt_template: '',
        return_documents: false,
        return_raw_scores: false,
        extra_params: {},
      },
    },
    candidateLimit: 40,
    rerankLimit: 12,
    enabled: false,
  };
}

export function classifyRetrievalChange(
  before: RetrievalDraft,
  after: RetrievalDraft,
): RetrievalChange {
  const changedFields: string[] = [];
  collectChanges(before, after, '', changedFields);
  const identityPaths = new Set([
    'embedding.adapter',
    'embedding.baseUrl',
    'embedding.model',
    'embedding.options.dimension_mode',
    'embedding.options.dimensions',
    'embedding.options.extra_params',
  ]);
  return {
    requiresReindex: changedFields.some((field) => identityPaths.has(field)),
    changedFields,
  };
}

function collectChanges(before: unknown, after: unknown, prefix: string, output: string[]) {
  if (Object.is(before, after)) return;
  if (Array.isArray(before) && Array.isArray(after)) {
    if (JSON.stringify(before) !== JSON.stringify(after)) output.push(prefix);
    return;
  }
  if (isRecord(before) && isRecord(after)) {
    const keys = new Set([...Object.keys(before), ...Object.keys(after)]);
    [...keys].sort().forEach((key) => {
      if (key === 'apiKey' || key === 'apiKeyMasked' || key === 'revision') return;
      collectChanges(before[key], after[key], prefix ? `${prefix}.${key}` : key, output);
    });
    return;
  }
  output.push(prefix);
}

function isRecord(value: unknown): value is Record<string, any> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

export function mergeRetrievalConfig(
  current: RetrievalDraft,
  config: Partial<KnowledgeRetrievalConfigRead> & Record<string, any>,
): RetrievalDraft {
  const embedding = isRecord(config.embedding) ? config.embedding : {};
  const bm25 = isRecord(config.bm25) ? config.bm25 : {};
  const fusion = isRecord(config.fusion) ? config.fusion : {};
  const reranker = isRecord(config.reranker) ? config.reranker : {};
  return {
    ...current,
    name: typeof config.name === 'string' ? config.name : current.name,
    revision: typeof config.revision === 'number' ? config.revision : current.revision,
    enabled: typeof config.enabled === 'boolean' ? config.enabled : current.enabled,
    candidateLimit: typeof config.candidate_limit === 'number' ? config.candidate_limit : current.candidateLimit,
    rerankLimit: typeof config.rerank_limit === 'number' ? config.rerank_limit : current.rerankLimit,
    embedding: {
      ...current.embedding,
      adapter: typeof config.embedding_adapter === 'string' && config.embedding_adapter ? config.embedding_adapter : current.embedding.adapter,
      baseUrl: typeof embedding.base_url === 'string' ? embedding.base_url : (config.embedding_base_url || current.embedding.baseUrl),
      model: typeof embedding.model === 'string' ? embedding.model : (config.embedding_model || current.embedding.model),
      apiKey: '',
      apiKeyMasked: typeof config.embedding_api_key_masked === 'string' ? config.embedding_api_key_masked : current.embedding.apiKeyMasked,
      options: { ...current.embedding.options, ...withoutSecrets(embedding) },
    },
    bm25: { ...current.bm25, options: { ...current.bm25.options, ...withoutSecrets(bm25) } },
    fusion: { ...current.fusion, options: { ...current.fusion.options, ...withoutSecrets(fusion) } },
    reranker: {
      ...current.reranker,
      adapter: typeof config.reranker_adapter === 'string' && config.reranker_adapter ? config.reranker_adapter : (typeof reranker.adapter === 'string' && reranker.adapter ? reranker.adapter : current.reranker.adapter),
      baseUrl: typeof config.reranker_base_url === 'string' ? config.reranker_base_url : (typeof reranker.base_url === 'string' ? reranker.base_url : current.reranker.baseUrl),
      model: typeof config.reranker_model === 'string' ? config.reranker_model : (typeof reranker.model === 'string' ? reranker.model : current.reranker.model),
      apiKey: '',
      apiKeyMasked: typeof config.reranker_api_key_masked === 'string' ? config.reranker_api_key_masked : current.reranker.apiKeyMasked,
      options: { ...current.reranker.options, ...withoutSecrets(reranker) },
    },
  };
}

function withoutSecrets(value: Record<string, any>): Record<string, any> {
  const copy = { ...value };
  delete copy.api_key;
  delete copy.apiKey;
  delete copy.base_url;
  delete copy.model;
  delete copy.adapter;
  return copy;
}

export function retrievalDraftPayload(draft: RetrievalDraft, tenantId: string) {
  return {
    tenant_id: tenantId,
    name: draft.name.trim(),
    embedding: {
      adapter: draft.embedding.adapter,
      base_url: draft.embedding.baseUrl.trim(),
      ...(draft.embedding.apiKey ? { api_key: draft.embedding.apiKey } : {}),
      model: draft.embedding.model.trim(),
      ...draft.embedding.options,
    },
    bm25: draft.bm25.options,
    fusion: draft.fusion.options,
    reranker: {
      ...draft.reranker.options,
      adapter: draft.reranker.adapter,
      base_url: draft.reranker.baseUrl.trim(),
      ...(draft.reranker.apiKey ? { api_key: draft.reranker.apiKey } : {}),
      model: draft.reranker.model.trim(),
    },
    candidate_limit: draft.candidateLimit,
    rerank_limit: draft.rerankLimit,
    enabled: draft.enabled,
    ...(draft.revision ? { expected_revision: draft.revision } : {}),
  };
}
