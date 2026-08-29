from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from app.db.models import KnowledgeRetrievalConfig
from app.security.encryption import decrypt_secret

from .options import EmbeddingOptions, RerankerOptions


class ProviderRequestError(RuntimeError):
    """A provider request failed after the configured bounded retry policy."""


class RerankProviderError(RuntimeError):
    """A dedicated reranker returned an unusable response."""


def post_json_with_retries(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
    max_retries: int,
    retry_backoff_ms: int,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """POST JSON, retrying only transient transport and upstream failures."""

    if sleep is None:
        from time import sleep as default_sleep

        sleep = default_sleep
    attempts = max(1, max_retries + 1)
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = client.post(
                url,
                headers=headers,
                json=payload,
                timeout=timeout_seconds,
            )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < attempts:
                    retry_after = response.headers.get("retry-after")
                    try:
                        delay = float(retry_after) if retry_after else retry_backoff_ms / 1000
                    except ValueError:
                        delay = retry_backoff_ms / 1000
                    sleep(max(0.0, min(delay, 30.0)))
                    continue
                response.raise_for_status()
            response.raise_for_status()
            payload_value = response.json()
            if not isinstance(payload_value, dict):
                raise ProviderRequestError("PROVIDER_RESPONSE_INVALID")
            return payload_value
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                sleep(max(0.0, min(retry_backoff_ms / 1000, 30.0)))
                continue
            break
        except httpx.HTTPError as exc:
            raise ProviderRequestError("PROVIDER_REQUEST_FAILED") from exc
        except ProviderRequestError:
            raise
        except ValueError as exc:
            raise ProviderRequestError("PROVIDER_RESPONSE_INVALID") from exc
    raise ProviderRequestError("PROVIDER_REQUEST_FAILED") from last_error


def embedding_options_for_config(
    config: KnowledgeRetrievalConfig,
    options: EmbeddingOptions | None = None,
) -> EmbeddingOptions:
    if options is not None:
        return options
    stored = config.embedding_options_json or {}
    if not isinstance(stored, dict):
        stored = {}
    values: dict[str, Any] = {
        "adapter": config.embedding_adapter or "openai_compatible_embedding",
        "model": config.embedding_model,
        "base_url": config.embedding_base_url,
        "dimension_mode": "explicit" if config.embedding_dimensions > 0 else "auto",
        "dimensions": config.embedding_dimensions if config.embedding_dimensions > 0 else None,
    }
    values.update(stored)
    return EmbeddingOptions(
        **values,
    )


def reranker_options_for_config(
    config: KnowledgeRetrievalConfig,
    options: RerankerOptions | None = None,
) -> RerankerOptions:
    if options is not None:
        return options
    stored = config.reranker_options_json or {}
    if not isinstance(stored, dict):
        stored = {}
    values: dict[str, Any] = {
        "mode": config.reranker_mode,
        "adapter": config.reranker_adapter or "zhipu_rerank",
        "base_url": config.reranker_base_url,
        "model": config.reranker_model,
        "candidate_limit": config.candidate_limit,
        "rerank_limit": config.rerank_limit,
    }
    values.update(stored)
    return RerankerOptions(
        **values,
    )


class DedicatedRerankProvider:
    """Call a Zhipu/OpenAI-compatible rerank endpoint and return index/score pairs."""

    def __init__(
        self,
        config: KnowledgeRetrievalConfig,
        options: RerankerOptions | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self.options = reranker_options_for_config(config, options)
        self.base_url = (
            self.options.base_url or config.reranker_base_url or config.embedding_base_url
        ).rstrip("/")
        try:
            self.api_key = decrypt_secret(config.reranker_api_key_encrypted)
        except ValueError as exc:
            raise RerankProviderError("RERANK_SECRET_INVALID") from exc
        self.model = self.options.model or config.reranker_model
        self.client = client or httpx.Client(timeout=self.options.timeout_seconds)

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> list[tuple[int, float]]:
        if top_n <= 0 or not documents:
            return []
        if len(documents) > self.options.candidate_limit:
            raise RerankProviderError("RERANK_DOCUMENT_LIMIT_EXCEEDED")
        top_n = min(top_n, len(documents))
        payload: dict[str, Any] = {
            "model": self.model,
            "query": query,
            "documents": documents,
            "top_n": top_n,
        }
        if self.options.return_documents:
            payload["return_documents"] = True
        if self.options.return_raw_scores:
            payload["return_raw_scores"] = True
        for key, value in self.options.extra_params.items():
            if key not in {"model", "query", "documents", "top_n"}:
                payload[key] = value
        response = post_json_with_retries(
            self.client,
            f"{self.base_url}/rerank",
            headers={"Authorization": f"Bearer {self.api_key}"},
            payload=payload,
            timeout_seconds=self.options.timeout_seconds,
            max_retries=self.options.max_retries,
            retry_backoff_ms=self.options.retry_backoff_ms,
        )
        raw_results = response.get("results")
        if not isinstance(raw_results, list):
            raise RerankProviderError("RERANK_RESPONSE_INVALID")
        result: list[tuple[int, float]] = []
        seen: set[int] = set()
        for item in raw_results:
            if not isinstance(item, dict):
                raise RerankProviderError("RERANK_RESPONSE_INVALID")
            try:
                index = int(item["index"])
                score = float(item["relevance_score"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RerankProviderError("RERANK_RESPONSE_INVALID") from exc
            if index < 0 or index >= len(documents) or index in seen:
                raise RerankProviderError("RERANK_RESPONSE_INVALID")
            if not 0.0 <= score <= 1.0:
                raise RerankProviderError("RERANK_RESPONSE_INVALID")
            seen.add(index)
            result.append((index, score))
        return result[:top_n]
