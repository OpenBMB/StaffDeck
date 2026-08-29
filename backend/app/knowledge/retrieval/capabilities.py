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
            },
        },
        "openai_compatible_embedding": {
            "label": "OpenAI-compatible Embedding",
            "protocol": "openai_embeddings",
            "limits": {"max_batch_size": 128},
            "fields": {
                "dimensions": {"type": "integer", "min": 1, "max": 100_000, "default": None},
                "batch_size": {"type": "integer", "min": 1, "max": 128, "default": 32},
            },
        },
    },
    "reranker": {
        "zhipu_rerank": {
            "label": "智谱 Rerank",
            "protocol": "rerank_results",
            "limits": {"max_documents": 128, "max_query_chars": 4096, "max_document_chars": 4096},
            "fields": {
                "candidate_limit": {"type": "integer", "min": 1, "max": 128, "default": 40},
                "rerank_limit": {"type": "integer", "min": 1, "max": 128, "default": 12},
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
            },
        },
    },
}


def adapter_capabilities() -> dict[str, dict[str, dict[str, Any]]]:
    return deepcopy(_CAPABILITIES)
