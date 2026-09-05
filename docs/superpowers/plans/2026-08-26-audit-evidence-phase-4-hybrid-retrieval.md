# 审核证据 Phase 4 混合检索与模型预算 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在统一检索接口后增加 BM25、OpenAI-compatible embedding 向量召回和专门 reranker，并让模型输入预算由“连通性验证成功且管理员确认”的上下文配置控制。

**Architecture:** 旧词法检索保留为功能开关关闭时的兼容路径。新路径先并行取得 BM25 与 SQLite 向量候选，用 Reciprocal Rank Fusion 合并，再对有限候选执行独立 reranker；审核要素编排消费统一结果，不直接耦合具体检索算法。

**Tech Stack:** Python 3.11、SQLModel/SQLite、httpx、OpenAI-compatible embeddings API、现有 LLMClient、pytest、React/TypeScript/Vitest

**Spec:** `docs/superpowers/specs/2026-08-26-audit-material-evidence-pipeline-design.md`

## Global Constraints

- 第一版不引入外部向量数据库；向量记录保存在新增 SQLite 表，接口允许后续替换实现。
- embedding 和 reranker API Key 必须加密存储，任何日志和 API 响应不得返回明文。
- BM25、vector 和 reranker 都必须保留候选数、采用数、耗时和失败回退 trace。
- embedding 失败时回退 BM25；reranker 失败时回退融合排序，不得让普通知识问答整体失败。
- 普通 `knowledge_search` 的调用次数预算保持不变；只替换每次调用内部排序方式。
- 审核编排仍按每个必需要素执行固定查询组并写入台账。
- OpenAI-compatible 接口没有统一的上下文窗口发现端点；仅通过文本/流式/JSON 探针只能验证连通性和协议能力，不能证明上下文上限。
- 未完成连通性验证或未经管理员确认上下文窗口的模型继续使用 `32_000` token 安全输入预算。
- `safe_input_tokens` 必须小于 `context_window_tokens - max_output_tokens - 4_096`。
- 切换模型不能绕过 Phase 3 的文件、块、要素覆盖 Gate。
- 固定检索评测集必须同时记录旧排序和新排序，不允许只保留成功示例。

---

## File Structure

- `backend/app/knowledge/retrieval/contracts.py`: 候选、trace、retriever 和 reranker 协议。
- `backend/app/knowledge/retrieval/tokenizer.py`: 中英文稳定分词和中文 bigram。
- `backend/app/knowledge/retrieval/bm25.py`: 纯 Python BM25 召回。
- `backend/app/knowledge/retrieval/vector.py`: embedding 客户端、SQLite 向量写入和余弦召回。
- `backend/app/knowledge/retrieval/reranker.py`: 独立 reranker 输入、严格输出和回退。
- `backend/app/knowledge/retrieval/hybrid.py`: RRF 融合和整体编排。
- `backend/app/api/knowledge_retrieval.py`: 检索配置和向量索引状态 API。
- `frontend-enterprise/src/pages/KnowledgePage.tsx`: 检索配置和索引状态界面。

### Task 1: 检索配置、向量记录和模型预算数据模型

**Files:**
- Modify: `backend/app/db/models.py`
- Modify: `backend/app/db/database.py`
- Modify: `backend/app/llm/schemas.py`
- Modify: `backend/app/api/model_configs.py`
- Create: `backend/tests/test_hybrid_retrieval.py`
- Modify: `backend/tests/test_model_configs_api.py`

**Interfaces:**
- Consumes: 现有 `ModelConfig` 和密钥加密函数。
- Produces: `KnowledgeRetrievalConfig`、`KnowledgeChunkEmbedding`；`ModelConfig.context_window_tokens`、`context_window_source` 和 `safe_input_tokens`。

- [ ] **Step 1: 编写预算约束测试**

```python
def test_safe_input_budget_must_leave_output_and_system_reserve() -> None:
    with pytest.raises(HTTPException) as exc:
        validate_model_token_budget(
            context_window_tokens=64_000,
            safe_input_tokens=60_000,
            max_output_tokens=8_192,
        )
    assert exc.value.detail == "MODEL_TOKEN_BUDGET_INVALID"


def test_non_default_budget_requires_verified_and_attested_context() -> None:
    with pytest.raises(HTTPException) as exc:
        validate_model_token_budget(
            context_window_tokens=128_000,
            context_window_source="unverified",
            trust_status="verified",
            safe_input_tokens=96_000,
            max_output_tokens=8_192,
        )
    assert exc.value.detail == "MODEL_CONTEXT_NOT_ATTESTED"
```

- [ ] **Step 2: 编写向量记录内容版本测试**

```python
def test_chunk_embedding_identity_includes_content_hash_and_model() -> None:
    row = KnowledgeChunkEmbedding(
        tenant_id="tenant_demo",
        knowledge_base_id="kb-1",
        knowledge_base_version_id="kbver-1",
        chunk_id="chunk-1",
        retrieval_config_id="retrieval-1",
        embedding_model="text-embedding-model",
        content_sha256="a" * 64,
        dimensions=3,
        vector_json=[0.1, 0.2, 0.3],
    )
    assert row.status == "ready"
```

- [ ] **Step 3: 运行测试并确认失败**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_hybrid_retrieval.py tests/test_model_configs_api.py -k "embedding_identity or safe_input_budget" -v`

Expected: FAIL，新字段和模型尚不存在。

- [ ] **Step 4: 增加检索配置和向量模型**

```python
class KnowledgeRetrievalConfig(SQLModel, table=True):
    __tablename__ = "knowledge_retrieval_configs"

    id: str = Field(default_factory=lambda: new_id("retrieval"), primary_key=True)
    tenant_id: str = Field(index=True)
    name: str
    embedding_base_url: str
    embedding_api_key_encrypted: str
    embedding_model: str
    embedding_dimensions: int
    reranker_mode: str = Field(default="llm", index=True)
    reranker_model_config_id: Optional[str] = Field(default=None, index=True)
    candidate_limit: int = 40
    rerank_limit: int = 12
    enabled: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeChunkEmbedding(SQLModel, table=True):
    __tablename__ = "knowledge_chunk_embeddings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "chunk_id", "retrieval_config_id", "content_sha256",
            name="uq_knowledge_chunk_embedding_version"
        ),
    )

    id: str = Field(default_factory=lambda: new_id("kembed"), primary_key=True)
    tenant_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    knowledge_base_version_id: str = Field(index=True)
    chunk_id: str = Field(index=True)
    retrieval_config_id: str = Field(index=True)
    embedding_model: str
    content_sha256: str = Field(index=True)
    dimensions: int
    vector_json: list[float] = Field(default_factory=list, sa_column=Column(JSON))
    status: str = Field(default="ready", index=True)
    error_code: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
```

- [ ] **Step 5: 增加模型预算字段和验证**

`ModelConfig` 增加：

```python
context_window_tokens: Optional[int] = None
context_window_source: str = "default"
safe_input_tokens: int = 32_000
```

```python
def validate_model_token_budget(
    *,
    context_window_tokens: int | None,
    context_window_source: str,
    trust_status: str,
    safe_input_tokens: int,
    max_output_tokens: int,
) -> None:
    if safe_input_tokens < 1:
        raise HTTPException(status_code=422, detail="MODEL_TOKEN_BUDGET_INVALID")
    if (
        context_window_tokens is None
        or context_window_source != "admin_attested"
        or trust_status != "verified"
    ):
        if safe_input_tokens != 32_000:
            detail = (
                "MODEL_CONTEXT_NOT_ATTESTED"
                if context_window_tokens is not None
                else "MODEL_CONTEXT_NOT_VERIFIED"
            )
            raise HTTPException(status_code=422, detail=detail)
        return
    if safe_input_tokens > context_window_tokens - max_output_tokens - 4_096:
        raise HTTPException(status_code=422, detail="MODEL_TOKEN_BUDGET_INVALID")
```

创建和更新 schema/API 响应同步字段。`context_window_source` 只接受 `default | admin_attested`；API 仅在现有 `ModelConfig.trust_status == "verified"`（由 `/api/model-configs/{config_id}/test` 的文本、流式和 JSON 探针设置）时，允许租户管理员提交 `context_window_source=admin_attested` 和非默认预算。预算元数据不属于 provider security fingerprint，不能自行把 `trust_status` 从未验证改为已验证；修改 base URL、API Key、协议或模型 ID 后，现有逻辑会把信任状态复位，预算立即回退到 `32_000`。该状态表示“管理员依据提供方文档确认，且当前配置连通性已验证”，不宣称应用从提供方 API 自动探测了窗口上限。模型 ID 与窗口值都必须由管理员按提供方文档填写。

- [ ] **Step 6: 增加 SQLite 可空列迁移并运行测试**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_hybrid_retrieval.py tests/test_model_configs_api.py -k "model or budget or embedding" -v`

Expected: PASS，旧模型迁移后 `safe_input_tokens=32000`。

- [ ] **Step 7: 提交模型和迁移**

```powershell
git add backend/app/db/models.py backend/app/db/database.py backend/app/llm/schemas.py backend/app/api/model_configs.py backend/tests/test_hybrid_retrieval.py backend/tests/test_model_configs_api.py
git commit -m "feat: add retrieval and model budget metadata"
```

### Task 2: 统一检索契约和 BM25

**Files:**
- Create: `backend/app/knowledge/retrieval/__init__.py`
- Create: `backend/app/knowledge/retrieval/contracts.py`
- Create: `backend/app/knowledge/retrieval/tokenizer.py`
- Create: `backend/app/knowledge/retrieval/bm25.py`
- Modify: `backend/tests/test_hybrid_retrieval.py`

**Interfaces:**
- Consumes: `KnowledgeChunk` 列表。
- Produces: `RetrievalCandidate`、`RetrievalResult`、`CandidateRetriever` Protocol、`BM25Retriever.retrieve(query, chunks, limit)`。

- [ ] **Step 1: 编写中文短语和英文术语召回测试**

```python
def test_bm25_retrieves_chinese_phrase_and_exact_standard_number() -> None:
    chunks = [
        _chunk("c1", "组织应识别主要能源使用并确定相关变量"),
        _chunk("c2", "GB/T 23331-2020 要求建立能源评审"),
        _chunk("c3", "员工请假流程与能源管理无关"),
    ]
    result = BM25Retriever().retrieve("GB/T 23331-2020 能源评审", chunks, limit=2)
    assert [item.chunk.id for item in result.candidates] == ["c2", "c1"]
    assert result.trace[0]["strategy"] == "bm25"
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_hybrid_retrieval.py::test_bm25_retrieves_chinese_phrase_and_exact_standard_number -v`

Expected: FAIL，BM25 模块尚不存在。

- [ ] **Step 3: 定义检索契约**

```python
@dataclass(frozen=True)
class RetrievalCandidate:
    chunk: KnowledgeChunk
    score: float
    source: str
    rank: int


@dataclass(frozen=True)
class RetrievalResult:
    candidates: list[RetrievalCandidate]
    trace: list[dict[str, Any]]


class CandidateRetriever(Protocol):
    def retrieve(
        self, query: str, chunks: list[KnowledgeChunk], limit: int
    ) -> RetrievalResult:
        raise NotImplementedError
```

- [ ] **Step 4: 实现稳定 tokenizer 和 BM25 公式**

```python
def retrieval_terms(text: str) -> list[str]:
    normalized = text.lower()
    latin = re.findall(r"[a-z0-9][a-z0-9_.\-/]*", normalized)
    chinese_runs = re.findall(r"[\u4e00-\u9fff]+", normalized)
    chinese = [run for run in chinese_runs]
    for run in chinese_runs:
        chinese.extend(run[index:index + 2] for index in range(max(0, len(run) - 1)))
    return latin + chinese
```

BM25 参数固定为 `k1=1.5`、`b=0.75`，分数相同时按 `chunk_index`、`id` 排序，保证测试稳定。

- [ ] **Step 5: 运行 BM25 测试并提交**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_hybrid_retrieval.py -k "bm25" -v`

Expected: PASS。

```powershell
git add backend/app/knowledge/retrieval backend/tests/test_hybrid_retrieval.py
git commit -m "feat: add bm25 knowledge retrieval"
```

### Task 3: OpenAI-compatible embedding 和 SQLite 向量索引

**Files:**
- Create: `backend/app/knowledge/retrieval/vector.py`
- Create: `backend/app/knowledge/retrieval/indexer.py`
- Modify: `backend/app/knowledge/service.py`
- Modify: `backend/tests/test_hybrid_retrieval.py`

**Interfaces:**
- Consumes: `KnowledgeRetrievalConfig`、`KnowledgeChunk`、`httpx.Client`。
- Produces: `EmbeddingProvider.embed(texts) -> list[list[float]]`、`KnowledgeVectorIndexer.index_version(...)`、`VectorRetriever.retrieve(...)`。

- [ ] **Step 1: 编写 embedding 请求和维度验证测试**

```python
def test_embedding_provider_uses_openai_compatible_contract() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    vectors = OpenAICompatibleEmbeddingProvider(config, client=client).embed(["能源评审"])
    assert vectors == [[0.1, 0.2, 0.3]]
    assert captured[0].url == "https://embedding.example/v1/embeddings"
    assert json.loads(captured[0].content) == {"model": config.embedding_model, "input": ["能源评审"]}
```

- [ ] **Step 2: 编写内容变化重建测试**

```python
def test_vector_index_reembeds_only_changed_content() -> None:
    unchanged = _chunk("c1", "unchanged")
    changed = _chunk("c2", "changed")
    indexer, provider = _indexer_with_existing_hash(
        hashlib.sha256(unchanged.content.encode("utf-8")).hexdigest()
    )
    summary = indexer.index_version("tenant_demo", "kbver-1", [
        unchanged,
        changed,
    ])
    assert provider.inputs == ["changed"]
    assert summary.skipped == 1
    assert summary.indexed == 1
```

- [ ] **Step 3: 运行测试并确认失败**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_hybrid_retrieval.py -k "embedding_provider or reembeds" -v`

Expected: FAIL，向量模块尚不存在。

- [ ] **Step 4: 实现 provider 和安全错误**

```python
class OpenAICompatibleEmbeddingProvider:
    def __init__(
        self, config: KnowledgeRetrievalConfig, *, client: httpx.Client | None = None
    ):
        self.base_url = config.embedding_base_url.rstrip("/")
        self.api_key = decrypt_secret(config.embedding_api_key_encrypted)
        self.model = config.embedding_model
        self.dimensions = config.embedding_dimensions
        self.client = client or httpx.Client(timeout=120.0)

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = self.client.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": texts},
        )
        response.raise_for_status()
        rows = sorted(response.json()["data"], key=lambda item: int(item["index"]))
        vectors = [[float(value) for value in row["embedding"]] for row in rows]
        if len(vectors) != len(texts) or any(len(vector) != self.dimensions for vector in vectors):
            raise EmbeddingError("EMBEDDING_DIMENSION_MISMATCH")
        return vectors
```

- [ ] **Step 5: 实现向量索引和余弦召回**

```python
def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise VectorIndexError("VECTOR_DIMENSION_MISMATCH")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)
```

索引器每批最多 32 块；单批失败只标记该批 `status=failed`，已成功向量保留。知识文档入库成功后仅在混合检索开关启用且配置有效时排队索引。

- [ ] **Step 6: 运行向量测试并提交**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_hybrid_retrieval.py -k "embedding or vector" -v`

Expected: PASS。

```powershell
git add backend/app/knowledge/retrieval/vector.py backend/app/knowledge/retrieval/indexer.py backend/app/knowledge/service.py backend/tests/test_hybrid_retrieval.py
git commit -m "feat: index knowledge chunk embeddings"
```

### Task 4: 独立 reranker 和严格回退

**Files:**
- Create: `backend/app/knowledge/retrieval/reranker.py`
- Modify: `backend/tests/test_hybrid_retrieval.py`

**Interfaces:**
- Consumes: 融合候选和 `ModelConfig`。
- Produces: `CandidateReranker.rerank(query, candidates, limit) -> RetrievalResult`、`LLMReranker`。

- [ ] **Step 1: 编写重排只接受候选 ID 测试**

```python
def test_reranker_rejects_unknown_candidate_ids() -> None:
    client = FakeLLMClient({"ranked": [{"chunk_id": "outside", "score": 0.99}]})
    result = LLMReranker(client).rerank("能源绩效", [_candidate("c1"), _candidate("c2")], limit=2)
    assert [item.chunk.id for item in result.candidates] == ["c1", "c2"]
    assert result.trace[-1]["fallback_reason"] == "RERANK_UNKNOWN_CANDIDATE"
```

- [ ] **Step 2: 编写稳定重排测试**

```python
def test_reranker_uses_model_order_and_keeps_source_scores() -> None:
    client = FakeLLMClient({"ranked": [{"chunk_id": "c2", "score": 0.9}, {"chunk_id": "c1", "score": 0.7}]})
    result = LLMReranker(client).rerank("能源绩效", [_candidate("c1"), _candidate("c2")], limit=2)
    assert [item.chunk.id for item in result.candidates] == ["c2", "c1"]
    assert all(item.source == "reranker" for item in result.candidates)
```

- [ ] **Step 3: 运行测试并确认失败**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_hybrid_retrieval.py -k "reranker" -v`

Expected: FAIL，reranker 尚不存在。

- [ ] **Step 4: 实现专门重排契约**

```python
class RerankItem(BaseModel):
    chunk_id: str
    score: float = Field(ge=0.0, le=1.0)


class RerankResponse(BaseModel):
    ranked: list[RerankItem]


class LLMReranker:
    def rerank(
        self, query: str, candidates: list[RetrievalCandidate], limit: int
    ) -> RetrievalResult:
        original = {item.chunk.id: item for item in candidates}
        try:
            parsed = RerankResponse.model_validate(self.client.generate_json(
                RERANK_PROMPT,
                {
                    "query": query,
                    "candidates": [
                        {"chunk_id": item.chunk.id, "content": item.chunk.content[:2_000]}
                        for item in candidates
                    ],
                },
            ))
            if any(item.chunk_id not in original for item in parsed.ranked):
                raise RerankError("RERANK_UNKNOWN_CANDIDATE")
            ranked = [
                RetrievalCandidate(original[item.chunk_id].chunk, item.score, "reranker", index + 1)
                for index, item in enumerate(parsed.ranked[:limit])
            ]
            return RetrievalResult(ranked, [{"strategy": "reranker", "selected_count": len(ranked)}])
        except (LLMError, ValidationError, RerankError) as exc:
            return RetrievalResult(candidates[:limit], [{"strategy": "reranker", "fallback_reason": error_code(exc)}])
```

- [ ] **Step 5: 运行重排测试并提交**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_hybrid_retrieval.py -k "reranker" -v`

Expected: PASS。

```powershell
git add backend/app/knowledge/retrieval/reranker.py backend/tests/test_hybrid_retrieval.py
git commit -m "feat: add dedicated knowledge reranker"
```

### Task 5: RRF 混合编排并接入 KnowledgeService

**Files:**
- Create: `backend/app/knowledge/retrieval/hybrid.py`
- Modify: `backend/app/knowledge/service.py:815`
- Modify: `backend/tests/test_hybrid_retrieval.py`
- Modify: `backend/tests/test_knowledge_base.py`

**Interfaces:**
- Consumes: `BM25Retriever`、`VectorRetriever`、`LLMReranker`。
- Produces: `HybridKnowledgeRetriever.retrieve(query, chunks, candidate_limit, rerank_limit)`；在现有 `KnowledgeService._search(...)` 内按功能开关选择旧排序或混合排序。

- [ ] **Step 1: 编写融合和回退测试**

```python
def test_hybrid_rrf_combines_bm25_and_vector_then_reranks() -> None:
    retriever = HybridKnowledgeRetriever(
        bm25=FakeRetriever("bm25", ["c1", "c2"]),
        vector=FakeRetriever("vector", ["c3", "c2"]),
        reranker=FakeReranker(["c2", "c3", "c1"]),
    )
    result = retriever.retrieve("能源评审", chunks, candidate_limit=3, rerank_limit=3)
    assert [item.chunk.id for item in result.candidates] == ["c2", "c3", "c1"]
    assert [item["strategy"] for item in result.trace] == ["bm25", "vector", "rrf", "reranker"]
```

```python
def test_vector_failure_falls_back_to_bm25_without_losing_answer() -> None:
    retriever = HybridKnowledgeRetriever(
        bm25=FakeRetriever("bm25", ["c1"]),
        vector=FailingRetriever("EMBEDDING_PROVIDER_UNAVAILABLE"),
        reranker=PassthroughReranker(),
    )
    result = retriever.retrieve("能源评审", chunks, candidate_limit=3, rerank_limit=2)
    assert [item.chunk.id for item in result.candidates] == ["c1"]
    assert any(item.get("fallback_reason") == "EMBEDDING_PROVIDER_UNAVAILABLE" for item in result.trace)
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_hybrid_retrieval.py -k "hybrid_rrf or vector_failure" -v`

Expected: FAIL，混合编排器尚不存在。

- [ ] **Step 3: 实现 RRF**

```python
def reciprocal_rank_fusion(
    groups: list[list[RetrievalCandidate]], k: int = 60
) -> list[RetrievalCandidate]:
    scores: dict[str, float] = {}
    chunks: dict[str, KnowledgeChunk] = {}
    for group in groups:
        for rank, candidate in enumerate(group, start=1):
            chunks[candidate.chunk.id] = candidate.chunk
            scores[candidate.chunk.id] = scores.get(candidate.chunk.id, 0.0) + 1.0 / (k + rank)
    ordered = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))
    return [
        RetrievalCandidate(chunks[chunk_id], scores[chunk_id], "rrf", index + 1)
        for index, chunk_id in enumerate(ordered)
    ]
```

- [ ] **Step 4: 在 KnowledgeService 保留兼容路径**

```python
if not settings.hybrid_knowledge_retrieval_enabled or retrieval_config is None:
    ranked_candidates = _rank_chunks(query, chunks, selected_buckets, expanded_sections)
else:
    hybrid = self.hybrid_retriever(retrieval_config, model_config)
    hybrid_result = hybrid.retrieve(
        query, chunks, retrieval_config.candidate_limit, retrieval_config.rerank_limit
    )
    ranked_candidates = [item.chunk for item in hybrid_result.candidates]
    route_trace.extend(hybrid_result.trace)
```

随后继续调用现有 `_select_diverse_chunk_hits` 和 `_expand_related_chunks`，保持响应 schema 和引用行为不变。

- [ ] **Step 5: 运行混合检索与旧检索回归**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_hybrid_retrieval.py tests/test_knowledge_base.py -k "search or hybrid or rank" -v`

Expected: PASS；开关关闭时顺序与旧测试一致。

- [ ] **Step 6: 提交混合检索**

```powershell
git add backend/app/knowledge/retrieval/hybrid.py backend/app/knowledge/service.py backend/tests/test_hybrid_retrieval.py backend/tests/test_knowledge_base.py
git commit -m "feat: integrate hybrid knowledge retrieval"
```

### Task 6: 使用已验证的模型输入预算

**Files:**
- Modify: `backend/app/llm/client.py:85`
- Modify: `backend/tests/test_llm_client.py`
- Modify: `backend/tests/test_model_configs_api.py`

**Interfaces:**
- Consumes: `ModelConfig.safe_input_tokens`、`context_window_source` 和现有 `trust_status`。
- Produces: `LLMClient.input_token_budget`，传给 `_fit_request_messages(messages, token_budget=...)`。

- [ ] **Step 1: 编写默认和已验证预算测试**

```python
def test_llm_client_uses_verified_safe_input_budget() -> None:
    client = LLMClient(_model_config(
        trust_status="verified",
        context_window_tokens=128_000,
        context_window_source="admin_attested",
        safe_input_tokens=96_000,
    ))
    assert client.input_token_budget == 96_000


def test_llm_client_defaults_unknown_context_to_32000() -> None:
    client = LLMClient(_model_config(context_window_tokens=None, safe_input_tokens=32_000))
    assert client.input_token_budget == 32_000


def test_llm_client_ignores_stale_large_budget_after_security_change() -> None:
    client = LLMClient(_model_config(
        trust_status="unverified",
        context_window_tokens=128_000,
        context_window_source="admin_attested",
        safe_input_tokens=96_000,
    ))
    assert client.input_token_budget == 32_000
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_llm_client.py -k "safe_input_budget or unknown_context" -v`

Expected: FAIL，客户端仍使用模块常量。

- [ ] **Step 3: 接入实例预算**

在 `LLMClient.__init__`：

```python
budget_is_trusted = (
    getattr(model_config, "trust_status", "unverified") == "verified"
    and getattr(model_config, "context_window_source", "default") == "admin_attested"
    and getattr(model_config, "context_window_tokens", None) is not None
)
self.input_token_budget = (
    int(getattr(model_config, "safe_input_tokens", DEFAULT_INPUT_TOKEN_BUDGET))
    if budget_is_trusted
    else DEFAULT_INPUT_TOKEN_BUDGET
)
```

所有 `_fit_request_messages(request_messages)` 调用改为：

```python
request_messages = _fit_request_messages(
    request_messages,
    token_budget=self.input_token_budget,
)
```

- [ ] **Step 4: 运行 LLM 和协议回归测试**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_llm_client.py tests/test_model_protocols.py tests/test_model_configs_api.py -v`

Expected: PASS。

- [ ] **Step 5: 提交模型预算接入**

```powershell
git add backend/app/llm/client.py backend/tests/test_llm_client.py backend/tests/test_model_configs_api.py
git commit -m "feat: honor verified model input budgets"
```

### Task 7: 检索配置 API、前端索引状态和固定评测集

**Files:**
- Create: `backend/app/api/knowledge_retrieval.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/test_knowledge_retrieval_api.py`
- Create: `backend/tests/fixtures/knowledge_retrieval_eval.json`
- Create: `backend/tests/test_knowledge_retrieval_eval.py`
- Modify: `frontend-enterprise/src/types/index.ts`
- Modify: `frontend-enterprise/src/pages/KnowledgePage.tsx`
- Create: `frontend-enterprise/src/pages/KnowledgePage.retrieval.test.tsx`

**Interfaces:**
- Consumes: Tasks 1-6。
- Produces: `/api/enterprise/knowledge-retrieval/config`、`/index-status`、`/reindex`；管理界面和固定评测报告。

- [ ] **Step 1: 编写 API 密钥屏蔽测试**

```python
def test_retrieval_config_api_never_returns_embedding_key(client, admin_headers) -> None:
    response = client.put(
        "/api/enterprise/knowledge-retrieval/config",
        headers=admin_headers,
        json={
            "tenant_id": "tenant_demo",
            "name": "本地混合检索",
            "embedding_base_url": "https://embedding.example/v1",
            "embedding_api_key": "secret-key",
            "embedding_model": "text-embedding-model",
            "embedding_dimensions": 1024,
            "reranker_mode": "llm",
            "reranker_model_config_id": "model-1",
            "candidate_limit": 40,
            "rerank_limit": 12,
            "enabled": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["embedding_api_key_masked"].endswith("-key")
    assert "secret-key" not in response.text
```

- [ ] **Step 2: 编写固定评测指标测试**

```python
def test_hybrid_retrieval_eval_does_not_regress_recall() -> None:
    report = run_retrieval_eval(load_eval_fixture())
    assert report.hybrid_recall_at_8 >= report.legacy_recall_at_8
    assert report.invalid_citation_count == 0
    assert report.reranker_query_count == report.query_count
```

- [ ] **Step 3: 运行测试并确认失败**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_retrieval_api.py tests/test_knowledge_retrieval_eval.py -v`

Expected: FAIL，API 和评测器尚不存在。

- [ ] **Step 4: 实现管理 API**

路由依赖 `require_tenant_admin`。`PUT /config` 加密保存 key；`GET /index-status` 返回每知识库版本总块数、ready/failed/missing 向量数；`POST /reindex` 只排队缺失或内容哈希变化的块。

响应类型固定包含：

```python
class KnowledgeVectorIndexStatus(BaseModel):
    knowledge_base_version_id: str
    total_chunks: int
    ready_embeddings: int
    failed_embeddings: int
    missing_embeddings: int
    embedding_model: str
    updated_at: str | None
```

- [ ] **Step 5: 接入前端状态和验证**

前端显示 embedding 地址、模型、维度、reranker 模型、候选数、重排数和每知识库版本索引进度；API Key 输入只允许写入，不回填明文。

Run: `cd frontend-enterprise; npm test -- KnowledgePage.retrieval.test.tsx`

Expected: PASS。

Run: `cd frontend-enterprise; npm run build`

Expected: PASS。

- [ ] **Step 6: 运行后端评测、完整测试和 Ruff**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_retrieval_api.py tests/test_knowledge_retrieval_eval.py tests/test_audit_knowledge_orchestrator.py -v`

Expected: PASS。

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest -q`

Expected: PASS。

Run: `cd backend; .\.venv\Scripts\python.exe -m ruff check app tests`

Expected: PASS。

- [ ] **Step 7: 提交 API、UI 和评测集**

```powershell
git add backend/app/api/knowledge_retrieval.py backend/app/main.py backend/tests/test_knowledge_retrieval_api.py backend/tests/fixtures/knowledge_retrieval_eval.json backend/tests/test_knowledge_retrieval_eval.py frontend-enterprise/src/types/index.ts frontend-enterprise/src/pages/KnowledgePage.tsx frontend-enterprise/src/pages/KnowledgePage.retrieval.test.tsx
git commit -m "feat: manage and evaluate hybrid retrieval"
```
