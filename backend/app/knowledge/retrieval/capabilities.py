from __future__ import annotations

from copy import deepcopy
from typing import Any


_CAPABILITIES: dict[str, dict[str, dict[str, Any]]] = {
    "embedding": {
        "zhipu_embedding": {
            "label": "智谱 Embedding",
            "protocol": "openai_embeddings",
            "limits": {"max_batch_size": 64, "max_input_tokens": 3072},
            "fields": {
                "dimensions": {
                    "type": "select",
                    "options": [256, 512, 1024, 2048],
                    "default": 1024,
                    "requires_reindex": True,
                },
                "batch_size": {"type": "integer", "min": 1, "max": 64, "default": 32},
                "max_input_tokens": {"type": "integer", "min": 1, "max": 3072, "default": 3072},
                "timeout_seconds": {"type": "number", "min": 5, "max": 600, "default": 120},
                "max_retries": {"type": "integer", "min": 0, "max": 5, "default": 2},
                "retry_backoff_ms": {"type": "integer", "min": 100, "max": 10000, "default": 500},
                "oversize_policy": {"type": "select", "options": ["fail", "safe_truncate"], "default": "fail"},
                "similarity_threshold": {"type": "number", "min": -1, "max": 1, "default": 0},
            },
        },
        "openai_compatible_embedding": {
            "label": "OpenAI-compatible Embedding",
            "protocol": "openai_embeddings",
            "limits": {"max_batch_size": 128},
            "fields": {
                "dimensions": {"type": "integer", "min": 1, "max": 100_000, "default": None},
                "batch_size": {"type": "integer", "min": 1, "max": 128, "default": 32},
                "timeout_seconds": {"type": "number", "min": 5, "max": 600, "default": 120},
                "max_retries": {"type": "integer", "min": 0, "max": 5, "default": 2},
                "retry_backoff_ms": {"type": "integer", "min": 100, "max": 10000, "default": 500},
                "max_input_tokens": {"type": "integer", "min": 1, "max": 1_000_000, "default": None},
                "oversize_policy": {"type": "select", "options": ["fail", "safe_truncate"], "default": "fail"},
                "similarity_threshold": {"type": "number", "min": -1, "max": 1, "default": 0},
            },
        },
    },
    "bm25": {
        "builtin_bm25": {
            "label": "内置 BM25",
            "protocol": "local",
            "fields": {
                "enabled": {"type": "boolean", "default": True},
                "k1": {"type": "number", "min": 0.1, "max": 3, "default": 1.5},
                "b": {"type": "number", "min": 0, "max": 1, "default": 0.75},
                "candidate_limit": {"type": "integer", "min": 1, "max": 500, "default": 40},
                "minimum_score": {"type": "number", "min": 0, "max": 1_000_000, "default": 0},
                "tokenizer": {"type": "select", "options": ["cjk_bigram_latin"], "default": "cjk_bigram_latin"},
                "cjk_ngram": {"type": "integer", "min": 1, "max": 3, "default": 2},
                "preserve_identifiers": {"type": "boolean", "default": True},
                "deduplicate_query_terms": {"type": "boolean", "default": True},
                "stopwords_enabled": {"type": "boolean", "default": False},
                "stopwords": {"type": "string_list", "default": []},
                "content_weight": {"type": "number", "min": 0, "max": 10, "default": 1},
                "summary_weight": {"type": "number", "min": 0, "max": 10, "default": 0},
                "source_ref_weight": {"type": "number", "min": 0, "max": 10, "default": 0},
            },
        }
    },
    "fusion": {
        "weighted_rrf": {
            "label": "加权 RRF",
            "protocol": "local",
            "fields": {
                "mode": {"type": "select", "options": ["hybrid", "bm25", "vector"], "default": "hybrid"},
                "bm25_limit": {"type": "integer", "min": 1, "max": 500, "default": 40},
                "vector_limit": {"type": "integer", "min": 1, "max": 500, "default": 40},
                "rrf_k": {"type": "integer", "min": 1, "max": 1000, "default": 60},
                "bm25_weight": {"type": "number", "min": 0, "max": 10, "default": 1},
                "vector_weight": {"type": "number", "min": 0, "max": 10, "default": 1},
                "final_limit": {"type": "integer", "min": 1, "max": 500, "default": 40},
            },
        }
    },
    "reranker": {
        "zhipu_rerank": {
            "label": "智谱 Rerank",
            "protocol": "rerank_results",
            "limits": {"max_documents": 128, "max_query_chars": 4096, "max_document_chars": 4096},
            "fields": {
                "candidate_limit": {"type": "integer", "min": 1, "max": 128, "default": 40},
                "rerank_limit": {"type": "integer", "min": 1, "max": 128, "default": 12},
                "minimum_score": {"type": "number", "min": 0, "max": 1, "default": 0},
                "max_query_chars": {"type": "integer", "min": 1, "max": 100_000, "default": 4096},
                "max_document_chars": {"type": "integer", "min": 200, "max": 100_000, "default": 2000},
                "timeout_seconds": {"type": "number", "min": 5, "max": 600, "default": 120},
                "max_retries": {"type": "integer", "min": 0, "max": 5, "default": 2},
                "retry_backoff_ms": {"type": "integer", "min": 100, "max": 10000, "default": 500},
                "return_documents": {"type": "boolean", "default": False},
                "return_raw_scores": {"type": "boolean", "default": False},
            },
        },
        "generic_rerank": {
            "label": "通用 Rerank API",
            "protocol": "rerank_results",
            "limits": {"max_documents": 128},
            "fields": {
                "candidate_limit": {"type": "integer", "min": 1, "max": 128, "default": 40},
                "rerank_limit": {"type": "integer", "min": 1, "max": 128, "default": 12},
            },
        },
        "llm_rerank": {
            "label": "对话模型重排",
            "protocol": "chat_json",
            "limits": {"max_documents": 40},
            "fields": {
                "temperature": {"type": "number", "min": 0, "max": 2, "default": 0},
                "max_output_tokens": {"type": "integer", "min": 1, "max": 32_000, "default": 2048},
                "input_budget_tokens": {"type": "integer", "min": 1, "max": 1_000_000, "default": 32_000},
                "candidate_limit": {"type": "integer", "min": 1, "max": 40, "default": 40},
                "rerank_limit": {"type": "integer", "min": 1, "max": 40, "default": 12},
                "minimum_score": {"type": "number", "min": 0, "max": 1, "default": 0},
                "max_query_chars": {"type": "integer", "min": 1, "max": 100_000, "default": 4096},
                "max_document_chars": {"type": "integer", "min": 200, "max": 100_000, "default": 2000},
                "timeout_seconds": {"type": "number", "min": 5, "max": 600, "default": 120},
                "max_retries": {"type": "integer", "min": 0, "max": 5, "default": 2},
                "retry_backoff_ms": {"type": "integer", "min": 100, "max": 10000, "default": 500},
            },
        },
    },
}


def adapter_capabilities() -> dict[str, dict[str, dict[str, Any]]]:
    return deepcopy(_CAPABILITIES)
