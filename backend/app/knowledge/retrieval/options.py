from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _OptionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmbeddingOptions(_OptionModel):
    adapter: str = "openai_compatible_embedding"
    model: str = Field(min_length=1, max_length=240)
    base_url: str = ""
    dimension_mode: Literal["auto", "explicit"] = "auto"
    dimensions: int | None = Field(default=None, ge=1, le=100_000)
    batch_size: int = Field(default=32, ge=1, le=128)
    timeout_seconds: float = Field(default=120.0, ge=5.0, le=600.0)
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_backoff_ms: int = Field(default=500, ge=100, le=10_000)
    max_input_tokens: int | None = Field(default=None, ge=1)
    oversize_policy: Literal["fail", "safe_truncate"] = "fail"
    similarity_threshold: float = Field(default=0.0, ge=-1.0, le=1.0)
    extra_params: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def for_adapter(cls, adapter: str, *, model: str) -> "EmbeddingOptions":
        if adapter == "zhipu_embedding":
            if model.strip().lower() == "embedding-3":
                return cls(
                    adapter=adapter,
                    model=model,
                    dimension_mode="explicit",
                    dimensions=1024,
                    max_input_tokens=3072,
                )
            if model.strip().lower() == "embedding-2":
                return cls(
                    adapter=adapter,
                    model=model,
                    dimension_mode="explicit",
                    dimensions=1024,
                    max_input_tokens=512,
                )
        return cls(adapter=adapter, model=model)

    @model_validator(mode="after")
    def validate_dimensions(self) -> "EmbeddingOptions":
        if self.dimension_mode == "explicit" and self.dimensions is None:
            raise ValueError("EMBEDDING_DIMENSIONS_REQUIRED")
        if self.adapter == "zhipu_embedding" and self.model.strip().lower() == "embedding-3":
            if self.dimensions not in {256, 512, 1024, 2048}:
                raise ValueError("EMBEDDING_DIMENSION_UNSUPPORTED")
        return self


class BM25Options(_OptionModel):
    enabled: bool = True
    k1: float = Field(default=1.5, ge=0.1, le=3.0)
    b: float = Field(default=0.75, ge=0.0, le=1.0)
    candidate_limit: int = Field(default=40, ge=1, le=500)
    minimum_score: float = Field(default=0.0, ge=0.0)
    tokenizer: Literal["cjk_bigram_latin"] = "cjk_bigram_latin"
    cjk_ngram: int = Field(default=2, ge=1, le=3)
    preserve_identifiers: bool = True
    deduplicate_query_terms: bool = True
    stopwords_enabled: bool = False
    stopwords: list[str] = Field(default_factory=list)
    content_weight: float = Field(default=1.0, ge=0.0, le=10.0)
    summary_weight: float = Field(default=0.0, ge=0.0, le=10.0)
    source_ref_weight: float = Field(default=0.0, ge=0.0, le=10.0)


class FusionOptions(_OptionModel):
    mode: Literal["hybrid", "bm25", "vector"] = "hybrid"
    bm25_limit: int = Field(default=40, ge=1, le=500)
    vector_limit: int = Field(default=40, ge=1, le=500)
    algorithm: Literal["weighted_rrf"] = "weighted_rrf"
    rrf_k: int = Field(default=60, ge=1, le=1000)
    bm25_weight: float = Field(default=1.0, ge=0.0, le=10.0)
    vector_weight: float = Field(default=1.0, ge=0.0, le=10.0)
    final_limit: int = Field(default=40, ge=1, le=500)


class RerankerOptions(_OptionModel):
    mode: Literal["none", "dedicated_api", "llm"] = "llm"
    adapter: str = ""
    base_url: str = ""
    model: str = ""
    candidate_limit: int = Field(default=40, ge=1, le=128)
    rerank_limit: int = Field(default=12, ge=1, le=128)
    minimum_score: float = Field(default=0.0, ge=0.0, le=1.0)
    max_query_chars: int = Field(default=4096, ge=1, le=100_000)
    max_document_chars: int = Field(default=2000, ge=200, le=100_000)
    timeout_seconds: float = Field(default=120.0, ge=5.0, le=600.0)
    max_retries: int = Field(default=2, ge=0, le=5)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_output_tokens: int = Field(default=2048, ge=1, le=32_000)
    input_budget_tokens: int = Field(default=32_000, ge=1, le=1_000_000)
    prompt_template: str = ""
    return_documents: bool = False
    return_raw_scores: bool = False
    extra_params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_limits(self) -> "RerankerOptions":
        if self.rerank_limit > self.candidate_limit:
            raise ValueError("RERANK_LIMIT_EXCEEDS_CANDIDATE_LIMIT")
        return self


class RetrievalOptions(_OptionModel):
    embedding: EmbeddingOptions | None = None
    bm25: BM25Options = Field(default_factory=BM25Options)
    fusion: FusionOptions = Field(default_factory=FusionOptions)
    reranker: RerankerOptions = Field(default_factory=RerankerOptions)
    candidate_limit: int = Field(default=40, ge=1, le=500)
    rerank_limit: int = Field(default=12, ge=1, le=128)

    @model_validator(mode="after")
    def validate_limits(self) -> "RetrievalOptions":
        if self.rerank_limit > self.candidate_limit:
            raise ValueError("RERANK_LIMIT_EXCEEDS_CANDIDATE_LIMIT")
        return self

    @classmethod
    def for_new_zhipu_embedding(cls) -> "RetrievalOptions":
        return cls(
            embedding=EmbeddingOptions.for_adapter(
                "zhipu_embedding", model="embedding-3"
            )
        )


def embedding_identity_fingerprint(value: EmbeddingOptions | Any) -> str:
    if not isinstance(value, EmbeddingOptions):
        value = EmbeddingOptions(
            adapter=str(getattr(value, "embedding_adapter", "openai_compatible_embedding")),
            model=str(getattr(value, "embedding_model", "")),
            base_url=str(getattr(value, "embedding_base_url", "")),
            dimension_mode=(
                "explicit"
                if getattr(value, "embedding_dimensions", None) is not None
                else "auto"
            ),
            dimensions=getattr(value, "embedding_dimensions", None),
            extra_params=dict(getattr(value, "embedding_options_json", {}).get("extra_params", {})),
        )
    payload = {
        "adapter": value.adapter,
        "base_url": value.base_url.rstrip("/"),
        "model": value.model,
        "dimension_mode": value.dimension_mode,
        "dimensions": value.dimensions,
        "extra_params": value.extra_params,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
