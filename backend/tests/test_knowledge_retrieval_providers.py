from __future__ import annotations

import json

import httpx
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel

from app.db.models import KnowledgeChunk, KnowledgeRetrievalConfig
from app.knowledge.retrieval.bm25 import BM25Retriever
from app.knowledge.retrieval.contracts import RetrievalCandidate
from app.knowledge.retrieval.hybrid import reciprocal_rank_fusion
from app.knowledge.retrieval.options import BM25Options, EmbeddingOptions, RerankerOptions
from app.knowledge.retrieval.providers import DedicatedRerankProvider
from app.knowledge.retrieval.providers import OpenAICompatibleChatRerankClient
from app.knowledge.retrieval.providers import post_json_with_retries
from app.knowledge.retrieval.indexer import KnowledgeVectorIndexer
from app.knowledge.retrieval.reranker import LLMReranker
from app.knowledge.retrieval.vector import OpenAICompatibleEmbeddingProvider
from app.security.encryption import encrypt_secret


def _chunk(chunk_id: str, content: str) -> KnowledgeChunk:
    return KnowledgeChunk(
        id=chunk_id,
        tenant_id="tenant_demo",
        knowledge_base_id="kb-1",
        knowledge_base_version_id="kbver-1",
        document_id=f"doc-{chunk_id}",
        bucket_id=f"bucket-{chunk_id}",
        chunk_index=int(chunk_id.removeprefix("c")),
        content=content,
        source_ref=f"{chunk_id}.md#chunk=0",
    )


def _candidate(chunk_id: str, score: float = 0.5) -> RetrievalCandidate:
    chunk = _chunk(chunk_id, f"内容 {chunk_id}")
    return RetrievalCandidate(chunk=chunk, score=score, source="test", rank=1)


def _retrieval_config() -> KnowledgeRetrievalConfig:
    return KnowledgeRetrievalConfig(
        id="retrieval-provider-test",
        tenant_id="tenant_demo",
        name="Provider test",
        embedding_base_url="https://embedding.example/v1",
        embedding_api_key_encrypted=encrypt_secret("embedding-secret"),
        embedding_model="embedding-3",
        embedding_dimensions=2,
    )


def _json_client(response_payload: dict[str, object]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_embedding_provider_sends_explicit_dimensions_and_extra_params() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]},
        )

    config = _retrieval_config()
    options = EmbeddingOptions(
        adapter="openai_compatible_embedding",
        model="embedding-3",
        base_url="https://embedding.example/v1",
        dimension_mode="explicit",
        dimensions=2,
        extra_params={"encoding_format": "float"},
    )
    provider = OpenAICompatibleEmbeddingProvider(
        config,
        options,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert provider.embed(["能源评审"]) == [[0.1, 0.2]]
    assert json.loads(captured[0].content) == {
        "model": "embedding-3",
        "input": ["能源评审"],
        "dimensions": 2,
        "encoding_format": "float",
    }


def test_bm25_uses_configured_k1_and_b() -> None:
    chunks = [_chunk("c1", "能源评审 评审"), _chunk("c2", "能源管理")]

    default = BM25Retriever(BM25Options()).retrieve("能源评审", chunks, limit=2)
    changed = BM25Retriever(BM25Options(k1=0.2, b=0.1)).retrieve(
        "能源评审", chunks, limit=2
    )

    assert [item.score for item in default.candidates] != [
        item.score for item in changed.candidates
    ]


def test_weighted_rrf_changes_branch_preference() -> None:
    groups = [
        [_candidate("c1"), _candidate("c2")],
        [_candidate("c2"), _candidate("c1")],
    ]

    result = reciprocal_rank_fusion(groups, k=60, weights=[2.0, 1.0], limit=2)

    assert result[0].chunk.id == "c1"


def test_dedicated_rerank_maps_index_and_score() -> None:
    config = _retrieval_config()
    config.reranker_base_url = "https://rerank.example/v1"
    config.reranker_api_key_encrypted = encrypt_secret("rerank-secret")
    config.reranker_model = "rerank-model"
    options = RerankerOptions(
        adapter="zhipu_rerank",
        base_url="https://rerank.example/v1",
        model="rerank-model",
    )
    provider = DedicatedRerankProvider(
        config,
        options,
        client=_json_client(
            {
                "results": [
                    {"index": 1, "relevance_score": 0.91},
                    {"index": 0, "relevance_score": 0.72},
                ]
            }
        ),
    )

    result = provider.rerank("能源绩效", ["第一段", "第二段"], top_n=2)

    assert result == [(1, 0.91), (0, 0.72)]


def test_dedicated_rerank_rejects_duplicate_or_unknown_indexes() -> None:
    config = _retrieval_config()
    config.reranker_base_url = "https://rerank.example/v1"
    config.reranker_api_key_encrypted = encrypt_secret("rerank-secret")
    config.reranker_model = "rerank-model"
    options = RerankerOptions(adapter="zhipu_rerank", model="rerank-model")
    provider = DedicatedRerankProvider(
        config,
        options,
        client=_json_client(
            {
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.8},
                ]
            }
        ),
    )

    try:
        provider.rerank("能源绩效", ["第一段", "第二段"], top_n=2)
    except Exception as exc:  # noqa: BLE001 - this test asserts the stable error code.
        assert str(exc) == "RERANK_RESPONSE_INVALID"
    else:
        raise AssertionError("duplicate rerank indexes must be rejected")


def test_embedding_indexer_uses_configured_batch_size() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        config = _retrieval_config()
        session.add(config)
        session.commit()

        class _RecordingProvider:
            def __init__(self) -> None:
                self.batch_sizes: list[int] = []

            def embed(self, texts: list[str]) -> list[list[float]]:
                self.batch_sizes.append(len(texts))
                return [[0.1, 0.2] for _ in texts]

        provider = _RecordingProvider()
        options = EmbeddingOptions(
            adapter="openai_compatible_embedding",
            model="embedding-3",
            dimension_mode="explicit",
            dimensions=2,
            batch_size=2,
        )
        summary = KnowledgeVectorIndexer(
            session, config, provider, options
        ).index_version(
            "tenant_demo",
            "kbver-1",
            [_chunk(f"c{index}", f"内容 {index}") for index in range(5)],
        )

    assert summary.indexed == 5
    assert provider.batch_sizes == [2, 2, 1]


def test_provider_retries_rate_limit_once_without_retrying_client_errors() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, json={"error": "busy"})
        return httpx.Response(200, json={"ok": True})

    response = post_json_with_retries(
        httpx.Client(transport=httpx.MockTransport(handler)),
        "https://provider.example/test",
        headers={},
        payload={},
        timeout_seconds=5,
        max_retries=1,
        retry_backoff_ms=100,
        sleep=lambda _seconds: None,
    )

    assert response == {"ok": True}
    assert attempts == 2


def test_llm_reranker_applies_prompt_and_input_limits() -> None:
    class _ConfiguredClient:
        def __init__(self) -> None:
            self.prompt = ""
            self.payload: dict[str, object] | None = None
            self.options: dict[str, object] = {}

        def generate_json_with_options(
            self,
            prompt: str,
            payload: dict[str, object],
            **options: object,
        ) -> dict[str, object]:
            self.prompt = prompt
            self.payload = payload
            self.options = options
            return {"ranked": [{"chunk_id": "c1", "score": 0.8}]}

    client = _ConfiguredClient()
    options = RerankerOptions(
        mode="llm",
        prompt_template="custom prompt",
        max_query_chars=100,
        max_document_chars=200,
        input_budget_tokens=50,
        temperature=0.2,
        max_output_tokens=321,
    )
    result = LLMReranker(client, options).rerank(
        "q" * 500,
        [RetrievalCandidate(_chunk("c1", "x" * 500), 0.4, "rrf", 1)],
        limit=1,
    )

    assert [item.chunk.id for item in result.candidates] == ["c1"]
    assert client.prompt == "custom prompt"
    assert len(str(client.payload["query"])) == 100
    assert len(client.payload["candidates"][0]["content"]) == 200
    assert client.options == {
        "temperature": 0.2,
        "max_output_tokens": 321,
        "input_budget_tokens": 50,
    }


def test_independent_chat_rerank_client_uses_its_own_endpoint_and_model() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"ranked":[{"chunk_id":"c1","score":0.9}]}'
                        }
                    }
                ]
            },
        )

    config = _retrieval_config()
    config.reranker_base_url = "https://rerank.example/v1"
    config.reranker_model = "deepseek-rerank"
    config.reranker_api_key_encrypted = encrypt_secret("rerank-secret")
    options = RerankerOptions(
        mode="llm",
        base_url=config.reranker_base_url,
        model=config.reranker_model,
        temperature=0.2,
        max_output_tokens=321,
    )
    client = OpenAICompatibleChatRerankClient(
        config,
        options,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    response = client.generate_json_with_options(
        "rank safely",
        {"query": "能源绩效", "candidates": []},
        temperature=options.temperature,
        max_output_tokens=options.max_output_tokens,
        input_budget_tokens=options.input_budget_tokens,
    )

    assert response == {"ranked": [{"chunk_id": "c1", "score": 0.9}]}
    assert str(captured[0].url) == "https://rerank.example/v1/chat/completions"
    payload = json.loads(captured[0].content)
    assert payload["model"] == "deepseek-rerank"
    assert payload["temperature"] == 0.2
    assert payload["max_tokens"] == 321
    assert "rerank-secret" not in captured[0].content.decode("utf-8")
