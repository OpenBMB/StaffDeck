# Advanced Knowledge Retrieval Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement schema-driven Embedding, BM25, weighted-fusion, and independent Reranker configuration windows with hot apply, safe versioned vector rebuilds, and no `.env` restart requirement.

**Architecture:** Keep `knowledge_retrieval_configs` as the tenant configuration store. Add typed option groups and an adapter capability registry; expose adapter-specific fields only when the backend declares them. Runtime-only changes update the active configuration immediately, while Embedding identity changes create a pending configuration keyed by a new config ID, rebuild in the background, and atomically activate only after validation.

**Tech Stack:** FastAPI, SQLModel, SQLite migrations, Pydantic v2, httpx, pytest, React, TypeScript, Vitest, existing StaffDeck UI components, browser smoke verification.

**Spec:** `docs/superpowers/specs/2026-08-29-knowledge-retrieval-configuration-design.md`

## Global Constraints

- Keep the existing BM25 and lexical fallback available whenever Embedding or Reranker fails.
- Use 1024 as the new Zhipu `embedding-3` preset dimension; do not treat 1536 as a universal default.
- Keep candidate count 40 and rerank count 12 as defaults, and enforce `rerank_limit <= candidate_limit`.
- Never store or return plaintext Embedding, dedicated Reranker, or LLM Reranker API keys.
- Do not require `HYBRID_KNOWLEDGE_RETRIEVAL_ENABLED` or a service restart for database-backed retrieval configuration.
- Do not introduce a vector database or expose HNSW/IVF/PQ settings that the current SQLite JSON storage does not implement.
- Preserve all unrelated dirty worktree changes; stage only files belonging to the current task in each commit.
- Every behavior-changing edit starts with a failing test and ends with a focused test run.

## File Map

Create the following focused modules and tests:

- `backend/app/knowledge/retrieval/options.py`: typed option models, defaults, cross-field validation, and Embedding identity fingerprinting.
- `backend/app/knowledge/retrieval/capabilities.py`: adapter registry and serializable parameter capabilities.
- `backend/app/knowledge/retrieval/providers.py`: provider-neutral test/retry helpers and dedicated Rerank HTTP adapter.
- `backend/tests/test_knowledge_retrieval_options.py`: option and capability contract tests.
- `backend/tests/test_knowledge_retrieval_providers.py`: HTTP payload, retry, timeout, and response mapping tests.
- `frontend-enterprise/src/pages/knowledge-retrieval/RetrievalSettingsDialog.tsx`: shared dialog shell and dynamic field rendering.
- `frontend-enterprise/src/pages/knowledge-retrieval/EmbeddingSettingsDialog.tsx`: Embedding form.
- `frontend-enterprise/src/pages/knowledge-retrieval/Bm25SettingsDialog.tsx`: BM25 form.
- `frontend-enterprise/src/pages/knowledge-retrieval/FusionSettingsDialog.tsx`: fusion form.
- `frontend-enterprise/src/pages/knowledge-retrieval/RerankerSettingsDialog.tsx`: dedicated API and LLM Reranker form.
- `frontend-enterprise/src/pages/knowledge-retrieval/retrievalSettings.ts`: frontend draft defaults, diff classification, and capability-to-field helpers.
- `frontend-enterprise/src/pages/knowledge-retrieval/retrievalSettings.test.ts`: frontend pure-function tests.

Modify the existing integration points:

- `backend/app/db/models.py`: extend `KnowledgeRetrievalConfig` and keep vector rows isolated by config ID.
- `backend/app/db/database.py`: additive SQLite migration for retrieval configuration fields.
- `backend/app/knowledge/retrieval/vector.py`: send dimensions, use provider options, and validate actual response dimensions.
- `backend/app/knowledge/retrieval/indexer.py`: use configured batch size and preserve per-batch failure isolation.
- `backend/app/knowledge/retrieval/tokenizer.py`: expose configured tokenizer behavior without changing current default behavior.
- `backend/app/knowledge/retrieval/bm25.py`: consume BM25 options and field weights.
- `backend/app/knowledge/retrieval/hybrid.py`: consume fusion weights, RRF `k`, branch limits, and thresholds.
- `backend/app/knowledge/retrieval/reranker.py`: support independent LLM options and provider contracts.
- `backend/app/knowledge/service.py`: remove the runtime environment gate and select active/pending configs safely.
- `backend/app/api/knowledge_retrieval.py`: capabilities, validate/test, atomic save, versioning, and status endpoints.
- `frontend-enterprise/src/types/index.ts`: generated API-facing retrieval types.
- `frontend-enterprise/src/pages/KnowledgePage.tsx`: summary cards, dialog orchestration, draft/apply flow, and rebuild status.
- `frontend-enterprise/src/pages/KnowledgePage.retrieval.test.tsx`: panel and dialog integration assertions.
- `README.md`, `README.zh.md`, `backend/.env.example`: document hot apply, model-matched dimensions, and deprecated env behavior.

---

### Task 1: Add typed retrieval options and adapter capability contracts

**Files:**
- Create: `backend/app/knowledge/retrieval/options.py`
- Create: `backend/app/knowledge/retrieval/capabilities.py`
- Create: `backend/tests/test_knowledge_retrieval_options.py`
- Modify: `backend/app/db/models.py:726-750`
- Modify: `backend/app/db/database.py` in `_migrate_sqlite_skill_schema`

**Interfaces:**
- Produces `EmbeddingOptions`, `BM25Options`, `FusionOptions`, `RerankerOptions`, `RetrievalOptions`, `embedding_identity_fingerprint(config_or_options)`, and `adapter_capabilities()`.
- `RetrievalOptions` must deserialize legacy rows with current defaults.
- `KnowledgeRetrievalConfig` gains `schema_version`, `revision`, `status`, adapter names, four JSON option columns, independent Reranker endpoint/model/key fields, test metadata, activation metadata, and `last_error_code`.

- [ ] **Step 1: Write failing option tests**

```python
def test_zhipu_embedding_defaults_to_supported_1024_dimension() -> None:
    options = EmbeddingOptions.for_adapter("zhipu_embedding", model="embedding-3")
    assert options.dimensions == 1024
    assert options.batch_size == 32
    zhipu = adapter_capabilities()["embedding"]["zhipu_embedding"]
    assert zhipu["limits"]["max_batch_size"] == 64


def test_generic_embedding_defaults_to_auto_dimension() -> None:
    options = EmbeddingOptions.for_adapter("openai_compatible_embedding", model="custom")
    assert options.dimension_mode == "auto"


def test_rerank_limit_cannot_exceed_candidate_limit() -> None:
    with pytest.raises(ValueError, match="RERANK_LIMIT_EXCEEDS_CANDIDATE_LIMIT"):
        RetrievalOptions(candidate_limit=10, rerank_limit=12)


def test_embedding_identity_fingerprint_excludes_retry_settings() -> None:
    first = EmbeddingOptions(model="embedding-3", dimensions=1024, max_retries=2)
    second = first.model_copy(update={"max_retries": 5})
    assert embedding_identity_fingerprint(first) == embedding_identity_fingerprint(second)


def test_embedding_identity_fingerprint_includes_dimensions() -> None:
    first = EmbeddingOptions(model="embedding-3", dimensions=1024)
    second = first.model_copy(update={"dimensions": 512})
    assert embedding_identity_fingerprint(first) != embedding_identity_fingerprint(second)
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `backend\.venv\Scripts\python.exe -m pytest backend/tests/test_knowledge_retrieval_options.py -q`

Expected: collection or assertion failures because the option models and fingerprint function do not exist.

- [ ] **Step 3: Implement the typed models and capability registry**

Use Pydantic models with strict numeric bounds:

```python
class EmbeddingOptions(BaseModel):
    adapter: str = "openai_compatible_embedding"
    model: str = Field(min_length=1, max_length=240)
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

    @model_validator(mode="after")
    def validate_dimensions(self) -> "EmbeddingOptions":
        if self.dimension_mode == "explicit" and self.dimensions is None:
            raise ValueError("EMBEDDING_DIMENSIONS_REQUIRED")
        return self
```

Register Zhipu limits and defaults: dimensions `[256, 512, 1024, 2048]`, default 1024 for StaffDeck, maximum batch 64, maximum input 3072. Register generic OpenAI-compatible mode with auto dimension and no false provider-specific limit.

Implement `RetrievalOptions` cross-validation so `candidate_limit >= 1`, `rerank_limit >= 1`, and `rerank_limit <= candidate_limit`. Implement the fingerprint over adapter, normalized base URL, model, dimension mode/value, and provider output-affecting `extra_params`; exclude API key, timeout, retries, and batch size.

- [ ] **Step 4: Add additive model columns and migration**

Use SQLModel JSON columns for option groups and ordinary columns for adapter identity, endpoints, encrypted secrets, status, revision, and timestamps. Add a migration block that checks `PRAGMA table_info(knowledge_retrieval_configs)` before each `ALTER TABLE`, backfills legacy rows with schema version 2, status `active`, revision 1, current BM25/fusion defaults, and the existing Reranker fields.

Do not silently change a legacy Zhipu `1536` value. Mark it as a validation warning that requires a test and new index; only new Zhipu presets default to 1024.

- [ ] **Step 5: Run option, model, and migration tests**

Run: `backend\.venv\Scripts\python.exe -m pytest backend/tests/test_knowledge_retrieval_options.py backend/tests/test_knowledge_retrieval_api.py -q`

Expected: PASS, with existing API tests updated only for additive response fields.

- [ ] **Step 6: Commit the task**

```powershell
git add backend/app/knowledge/retrieval/options.py backend/app/knowledge/retrieval/capabilities.py backend/app/db/models.py backend/app/db/database.py backend/tests/test_knowledge_retrieval_options.py backend/tests/test_knowledge_retrieval_api.py
git commit -m "feat: add retrieval option contracts"
```

### Task 2: Make Embedding, BM25, fusion, and Reranker consume real options

**Files:**
- Create: `backend/app/knowledge/retrieval/providers.py`
- Create: `backend/tests/test_knowledge_retrieval_providers.py`
- Modify: `backend/app/knowledge/retrieval/vector.py`
- Modify: `backend/app/knowledge/retrieval/indexer.py`
- Modify: `backend/app/knowledge/retrieval/tokenizer.py`
- Modify: `backend/app/knowledge/retrieval/bm25.py`
- Modify: `backend/app/knowledge/retrieval/hybrid.py`
- Modify: `backend/app/knowledge/retrieval/reranker.py`
- Modify: `backend/tests/test_hybrid_retrieval.py`

**Interfaces:**
- `OpenAICompatibleEmbeddingProvider(config, options, client=None)` sends `model`, `input`, and explicit `dimensions` when dimension mode is explicit.
- `BM25Retriever(options: BM25Options)` consumes every exposed BM25 parameter.
- `reciprocal_rank_fusion(groups, k, weights, limit)` applies weighted RRF.
- `DedicatedRerankProvider.rerank(query, documents, top_n)` calls the configured adapter endpoint and returns `(index, score)` pairs.
- `LLMReranker(client, options)` uses independent prompt and input-budget options.

- [ ] **Step 1: Write failing provider and retrieval tests**

```python
def test_embedding_provider_sends_zhipu_dimensions() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    config = _retrieval_config()
    options = EmbeddingOptions(
        adapter="zhipu_embedding", model="embedding-3", dimension_mode="explicit", dimensions=2
    )
    provider = OpenAICompatibleEmbeddingProvider(config, options, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert provider.embed(["能源评审"]) == [[0.1, 0.2]]
    assert json.loads(captured[0].content)["dimensions"] == 2


def test_bm25_uses_configured_k1_and_b() -> None:
    chunks = [_chunk("c1", "能源评审 评审"), _chunk("c2", "能源管理")]
    default = BM25Retriever(BM25Options()).retrieve("能源评审", chunks, limit=2)
    changed = BM25Retriever(BM25Options(k1=0.2, b=0.1)).retrieve("能源评审", chunks, limit=2)
    assert [item.score for item in default.candidates] != [item.score for item in changed.candidates]


def test_weighted_rrf_changes_branch_preference() -> None:
    groups = [[_candidate("c1"), _candidate("c2")], [_candidate("c2"), _candidate("c1")]]
    assert reciprocal_rank_fusion(groups, k=60, weights=[2.0, 1.0], limit=2)[0].chunk.id == "c1"


def test_dedicated_rerank_maps_index_and_score() -> None:
    response = {"results": [{"index": 1, "relevance_score": 0.91}, {"index": 0, "relevance_score": 0.72}]}
    provider = DedicatedRerankProvider(_rerank_config(), RerankerOptions(), client=_json_client(response))
    result = provider.rerank("能源绩效", ["第一段", "第二段"], top_n=2)
    assert result == [(1, 0.91), (0, 0.72)]
```

- [ ] **Step 2: Run focused tests and verify the old implementation fails**

Run: `backend\.venv\Scripts\python.exe -m pytest backend/tests/test_knowledge_retrieval_providers.py backend/tests/test_hybrid_retrieval.py -q`

Expected: failures for missing options arguments, missing `dimensions`, missing weighted RRF, and missing dedicated provider.

- [ ] **Step 3: Implement Embedding request options and bounded retries**

Refactor HTTP handling into `providers.py` with a bounded retry helper that retries only timeout, connection, and 429/5xx responses. Keep `httpx.Client` injection for tests. Send `dimensions` only when explicit; for auto mode, accept the first actual response dimension and let the caller persist it after connection testing. Keep strict response ordering and dimension validation.

Never truncate by default. If `safe_truncate` is selected, truncate only at the adapter-declared input budget and include a trace warning; indexing errors must retain the original content hash so later retry is deterministic.

- [ ] **Step 4: Implement BM25 and tokenizer options**

Pass `k1`, `b`, candidate limit, minimum score, N-gram length, identifier preservation, query-term deduplication, stopwords, and `content/summary/source_ref` weights. Preserve current defaults: content only, Chinese bigrams, identifier preservation, no stopword filtering, `k1=1.5`, `b=0.75`.

For each chunk, score configured fields independently and add weighted scores. Keep zero-score documents out of the result and keep deterministic `chunk_index/id` tie-breaking.

- [ ] **Step 5: Implement weighted RRF and independent Rerank providers**

Change `reciprocal_rank_fusion` to accept a list of branch weights and calculate `weight / (k + rank)`. Add branch-specific limits and the configured vector threshold. Keep de-duplication by chunk ID.

Implement the Zhipu-compatible dedicated Rerank adapter using `/rerank`, `query`, `documents`, `top_n`, and optional `return_documents`/`return_raw_scores`. Validate candidate indexes, score range, duplicate indexes, and unknown indexes. Keep the existing LLM JSON whitelist validation and add configurable temperature, output limit, input budget, candidate text length, and prompt template.

- [ ] **Step 6: Run focused and regression tests**

Run: `backend\.venv\Scripts\python.exe -m pytest backend/tests/test_knowledge_retrieval_providers.py backend/tests/test_hybrid_retrieval.py backend/tests/test_knowledge_retrieval_eval.py -q`

Expected: PASS, including old default ranking and vector fallback tests.

- [ ] **Step 7: Commit the task**

```powershell
git add backend/app/knowledge/retrieval/providers.py backend/app/knowledge/retrieval/vector.py backend/app/knowledge/retrieval/indexer.py backend/app/knowledge/retrieval/tokenizer.py backend/app/knowledge/retrieval/bm25.py backend/app/knowledge/retrieval/hybrid.py backend/app/knowledge/retrieval/reranker.py backend/tests/test_knowledge_retrieval_providers.py backend/tests/test_hybrid_retrieval.py
git commit -m "feat: apply configurable hybrid retrieval parameters"
```

### Task 3: Add admin capabilities, validation, connection tests, and atomic save APIs

**Files:**
- Create: `backend/tests/test_knowledge_retrieval_admin_api.py`
- Modify: `backend/app/api/knowledge_retrieval.py`
- Modify: `backend/app/knowledge/retrieval/capabilities.py`
- Modify: `backend/app/db/models.py`
- Modify: `frontend-enterprise/src/types/index.ts`

**Interfaces:**
- `GET /api/enterprise/knowledge-retrieval/capabilities?tenant_id=...` returns adapter schemas and defaults without secrets.
- `POST /api/enterprise/knowledge-retrieval/validate` validates a draft without saving.
- `POST /api/enterprise/knowledge-retrieval/test/embedding` tests a draft Embedding connection and returns actual dimensions.
- `POST /api/enterprise/knowledge-retrieval/test/reranker` tests a draft dedicated or LLM Reranker connection.
- `PUT /api/enterprise/knowledge-retrieval/config` accepts grouped options and `expected_revision`, returns `requires_reindex`, `active_config_id`, and `pending_config_id`.
- All management endpoints remain tenant-admin-only and return masked key fields.

- [ ] **Step 1: Write failing API tests**

```python
def test_capabilities_expose_zhipu_dimensions(client, admin_headers):
    response = client.get("/api/enterprise/knowledge-retrieval/capabilities?tenant_id=tenant_demo", headers=admin_headers)
    assert response.status_code == 200
    zhipu = next(item for item in response.json()["embedding"] if item["id"] == "zhipu_embedding")
    assert zhipu["fields"]["dimensions"]["options"] == [256, 512, 1024, 2048]


def test_embedding_connection_test_does_not_persist_key(client, admin_headers, mock_embedding):
    payload = _draft_payload(embedding_api_key="secret")
    response = client.post("/api/enterprise/knowledge-retrieval/test/embedding", json=payload, headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["dimensions"] == 1024
    assert client.get("/api/enterprise/knowledge-retrieval/config?tenant_id=tenant_demo", headers=admin_headers).status_code == 404


def test_config_save_rejects_rerank_limit_above_candidate_limit(client, admin_headers):
    response = client.put(
        "/api/enterprise/knowledge-retrieval/config",
        json=_draft_payload(candidate_limit=10, rerank_limit=12),
        headers=admin_headers,
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "RERANK_LIMIT_EXCEEDS_CANDIDATE_LIMIT"


def test_config_save_returns_reindex_requirement_for_embedding_identity_change(client, admin_headers):
    first = client.put("/api/enterprise/knowledge-retrieval/config", json=_draft_payload(dimensions=1024), headers=admin_headers)
    second = client.put("/api/enterprise/knowledge-retrieval/config", json=_draft_payload(dimensions=512), headers=admin_headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["requires_reindex"] is True
    assert second.json()["pending_config_id"]
```

- [ ] **Step 2: Run API tests and verify failure**

Run: `backend\.venv\Scripts\python.exe -m pytest backend/tests/test_knowledge_retrieval_admin_api.py -q`

Expected: 404/422 mismatches because the new endpoints and grouped request models do not exist.

- [ ] **Step 3: Implement grouped request/read schemas and capabilities endpoint**

Replace the flat request model with a backward-compatible request that accepts legacy fields and new `embedding`, `bm25`, `fusion`, and `reranker` groups. Normalize into `RetrievalOptions`. Return adapter field metadata, numeric bounds, enum options, defaults, and `requires_reindex` flags. Keep legacy read fields in the response so existing clients do not break.

- [ ] **Step 4: Implement draft validation and connection tests**

Reuse the adapter registry for validation. Build providers from the draft only in memory. Require tenant admin, cap request body size through existing FastAPI patterns, use request timeouts from the draft, and map upstream failures to stable codes. Return `{ok, adapter, latency_ms, dimensions, error_code}` without persisting draft secrets.

- [ ] **Step 5: Implement atomic save and optimistic revision checks**

Require `expected_revision` when updating an existing active row. On mismatch return HTTP 409 with `RETRIEVAL_CONFIG_REVISION_CONFLICT`. Encrypt only non-empty new keys. Store normalized options. Set `requires_reindex` when the Embedding fingerprint changes. Runtime-only changes increment the active revision and apply immediately; identity changes create `pending_index` while preserving the current active row.

- [ ] **Step 6: Run API and regression tests**

Run: `backend\.venv\Scripts\python.exe -m pytest backend/tests/test_knowledge_retrieval_admin_api.py backend/tests/test_knowledge_retrieval_api.py -q`

Expected: PASS with secret masking, admin authorization, grouped options, compatibility fields, and HTTP 409 revision behavior.

- [ ] **Step 7: Commit the task**

```powershell
git add backend/app/api/knowledge_retrieval.py backend/app/knowledge/retrieval/capabilities.py backend/app/db/models.py backend/tests/test_knowledge_retrieval_admin_api.py frontend-enterprise/src/types/index.ts
git commit -m "feat: add retrieval configuration admin APIs"
```

### Task 4: Remove the environment gate and implement safe versioned rebuild/switch

**Files:**
- Create: `backend/tests/test_knowledge_retrieval_versioning.py`
- Modify: `backend/app/knowledge/service.py:681-700,1687-1735`
- Modify: `backend/app/knowledge/retrieval/indexer.py`
- Modify: `backend/app/api/knowledge_retrieval.py`
- Modify: `backend/app/db/models.py`
- Modify: `backend/tests/test_knowledge_retrieval_api.py`
- Modify: `backend/tests/test_hybrid_retrieval.py`

**Interfaces:**
- `_active_retrieval_config(tenant_id)` selects only `status == "active"` and `enabled == True`; it does not read `get_settings().hybrid_knowledge_retrieval_enabled`.
- `_pending_retrieval_config(tenant_id)` returns at most one `pending_index` row.
- `POST /api/enterprise/knowledge-retrieval/reindex` accepts an optional `config_id`, reports the target config ID, and never changes active state by itself.
- `POST /api/enterprise/knowledge-retrieval/activate` atomically switches a ready pending config.
- `POST /api/enterprise/knowledge-retrieval/rollback` restores the previous archived config.

- [ ] **Step 1: Write failing versioning tests**

```python
def test_database_config_enables_hybrid_without_environment_flag(monkeypatch, session):
    monkeypatch.setattr("app.knowledge.service.get_settings", lambda: SimpleNamespace(hybrid_knowledge_retrieval_enabled=False))
    config = _retrieval_config(status="active", enabled=True)
    session.add(config)
    session.commit()
    service = KnowledgeService(session)
    assert service._active_retrieval_config(config.tenant_id).id == config.id


def test_embedding_identity_change_keeps_old_active_config(session):
    active = _retrieval_config(id="old", status="active", enabled=True, dimensions=1024)
    pending = _retrieval_config(id="new", status="pending_index", enabled=False, dimensions=512)
    session.add_all([active, pending])
    session.commit()
    assert KnowledgeService(session)._active_retrieval_config("tenant_demo").id == "old"


def test_failed_pending_rebuild_does_not_switch_active(session):
    active = _retrieval_config(id="old", status="active", enabled=True)
    pending = _retrieval_config(id="new", status="pending_index", enabled=False)
    session.add_all([active, pending])
    session.commit()
    mark_pending_failed(session, pending.id, "EMBEDDING_PROVIDER_UNAVAILABLE")
    assert KnowledgeService(session)._active_retrieval_config("tenant_demo").id == "old"


def test_ready_pending_config_switches_atomically(session):
    old = _retrieval_config(id="old", status="active", enabled=True)
    new = _retrieval_config(id="new", status="pending_index", enabled=False)
    session.add_all([old, new])
    session.commit()
    mark_pending_ready(session, new.id)
    activate_config(session, new.id)
    assert session.get(KnowledgeRetrievalConfig, "old").status == "archived"
    assert session.get(KnowledgeRetrievalConfig, "new").status == "active"
```

- [ ] **Step 2: Run versioning tests and verify failure**

Run: `backend\.venv\Scripts\python.exe -m pytest backend/tests/test_knowledge_retrieval_versioning.py -q`

Expected: failures because the service still requires the environment flag and has no pending/activate lifecycle.

- [ ] **Step 3: Remove the runtime environment gate**

Change both `_queue_vector_index_if_enabled` and `_active_retrieval_config` to use active database configuration only. Keep the setting in `Settings` for backward-compatible `.env` parsing but do not use it to disable an administrator-enabled tenant config. Update trace metadata to include config ID, revision, and active model.

- [ ] **Step 4: Use configured indexer batch settings**

Pass `EmbeddingOptions.batch_size` into `KnowledgeVectorIndexer`. Keep successful earlier batches committed when a later batch fails. Store the target retrieval config ID on every embedding row and retain content hash and actual vector dimension checks.

- [ ] **Step 5: Implement pending rebuild status and activation endpoints**

Reindex a pending target only. Compute ready/failed/missing counts per target config. Activation requires all chunks ready and matching the target dimension. In a single transaction, set old active to archived, target pending to active and enabled, and refresh activation timestamps. Rollback restores the most recent archived config without deleting vector rows.

- [ ] **Step 6: Add compatibility behavior for existing deployments**

If a legacy row has no status, treat it as active only when enabled. If multiple enabled rows exist, select the newest and record a migration normalization. Do not delete rows or vectors. Update `.env.example` comments to label the environment switch deprecated.

- [ ] **Step 7: Run backend regression tests**

Run: `backend\.venv\Scripts\python.exe -m pytest backend/tests/test_knowledge_retrieval_versioning.py backend/tests/test_knowledge_retrieval_api.py backend/tests/test_hybrid_retrieval.py backend/tests/test_knowledge_base.py -q`

Expected: PASS, including lexical fallback and existing vector index behavior.

- [ ] **Step 8: Commit the task**

```powershell
git add backend/app/knowledge/service.py backend/app/knowledge/retrieval/indexer.py backend/app/api/knowledge_retrieval.py backend/app/db/models.py backend/tests/test_knowledge_retrieval_versioning.py backend/tests/test_knowledge_retrieval_api.py backend/tests/test_hybrid_retrieval.py backend/tests/test_knowledge_base.py backend/.env.example
git commit -m "feat: hot apply and version retrieval indexes"
```

### Task 5: Build the four independent settings windows and draft/apply UX

**Files:**
- Create: `frontend-enterprise/src/pages/knowledge-retrieval/RetrievalSettingsDialog.tsx`
- Create: `frontend-enterprise/src/pages/knowledge-retrieval/EmbeddingSettingsDialog.tsx`
- Create: `frontend-enterprise/src/pages/knowledge-retrieval/Bm25SettingsDialog.tsx`
- Create: `frontend-enterprise/src/pages/knowledge-retrieval/FusionSettingsDialog.tsx`
- Create: `frontend-enterprise/src/pages/knowledge-retrieval/RerankerSettingsDialog.tsx`
- Create: `frontend-enterprise/src/pages/knowledge-retrieval/retrievalSettings.ts`
- Create: `frontend-enterprise/src/pages/knowledge-retrieval/retrievalSettings.test.ts`
- Create: `frontend-enterprise/src/pages/KnowledgePage.retrieval-settings.test.tsx`
- Modify: `frontend-enterprise/src/pages/KnowledgePage.tsx:170-380`
- Modify: `frontend-enterprise/src/pages/KnowledgePage.retrieval.test.tsx`
- Modify: `frontend-enterprise/src/types/index.ts:182-210`

**Interfaces:**
- `RetrievalDraft` mirrors the backend grouped options and keeps secret input fields write-only.
- `classifyRetrievalChange(before, after): { requiresReindex: boolean; changedFields: string[] }` uses the same identity rules as the backend.
- `RetrievalSettingsDialog` receives `title`, `open`, `capabilities`, `value`, `onChange`, `onTest`, `onApply`, and `onCancel`.
- The four specific dialogs use the shared shell and render capability-declared advanced fields.

- [ ] **Step 1: Write failing frontend pure-function tests**

```typescript
it('uses 1024 as the Zhipu embedding preset', () => {
  expect(defaultRetrievalDraft().embedding.options.dimensions).toBe(1024);
});

it('classifies BM25 and reranker changes as hot changes', () => {
  const result = classifyRetrievalChange(baseDraft, {
    ...baseDraft,
    bm25: { ...baseDraft.bm25, k1: 0.8 },
    reranker: { ...baseDraft.reranker, rerank_limit: 8 },
  });
  expect(result.requiresReindex).toBe(false);
});

it('classifies an embedding dimension change as requiring reindex', () => {
  const result = classifyRetrievalChange(baseDraft, {
    ...baseDraft,
    embedding: { ...baseDraft.embedding, dimensions: 512 },
  });
  expect(result.requiresReindex).toBe(true);
});
```

- [ ] **Step 2: Run the frontend tests and verify failure**

Run: `Set-Location frontend-enterprise; npm run test -- src/pages/knowledge-retrieval/retrievalSettings.test.ts`

Expected: failure because the grouped draft, defaults, and classifier do not exist.

- [ ] **Step 3: Implement grouped draft types, defaults, and change classification**

Define frontend types matching the API response. Use an adapter capability map to render numeric inputs, selects, toggles, and bounded JSON fields. Keep the UI default for a fresh Zhipu Embedding-3 draft at 1024, candidate 40, rerank 12, BM25 `1.5/0.75`, RRF `60`, and both weights `1.0`.

- [ ] **Step 4: Implement the shared dialog shell**

Use existing `Dialog`, `DialogContent`, `DialogTitle`, `Input`, `Textarea`, `UISelect`, and `UIButton` components. The shell provides Basic/Advanced tabs, inline field errors, a test result panel, restore-defaults, cancel, and apply-to-draft controls. It must not call the save API when applying a dialog draft.

- [ ] **Step 5: Implement Embedding and BM25 dialogs**

Embedding basic fields: adapter, URL, model, write-only API Key, dimension mode, dimensions. Advanced fields: batch, timeout, retries, backoff, input limit, oversize policy, similarity threshold, and adapter extension fields.

BM25 fields: enabled, `k1`, `b`, candidate count, minimum score, tokenizer, N-gram length, identifier preservation, query deduplication, stopwords, custom stopwords, and content/summary/source weights. Display the current-default note for fields whose default preserves existing behavior.

- [ ] **Step 6: Implement fusion and Reranker dialogs**

Fusion fields: mode, BM25/vector branch limits, weighted RRF, RRF `k`, branch weights, final candidate count, and fixed chunk-ID deduplication note.

Reranker mode tabs: none, dedicated Rerank API, and dialogue model. Dedicated mode gets independent adapter, URL, API Key, model, top_n, score threshold, candidate/document limits, timeout, retry, and provider extension fields. LLM mode gets independent protocol, URL, API Key, model, temperature, output budget, input budget, candidate text length, and editable prompt with restore-default button.

- [ ] **Step 7: Integrate cards, tests, apply flow, and reindex warnings into KnowledgePage**

Replace the large inline form with four summary cards. Load capabilities and config in parallel. Keep API keys blank in drafts after loading. Implement:

```typescript
async function saveAndApply() {
  const validation = await api.post('/api/enterprise/knowledge-retrieval/validate', draft);
  if (!validation.ok) return showFieldErrors(validation.errors);
  const result = await api.put('/api/enterprise/knowledge-retrieval/config', {
    ...draft,
    expected_revision: config?.revision ?? null,
  });
  setConfig(result);
  if (result.requires_reindex) openReindexConfirmation(result);
}
```

Add “测试全部连接”, “查看变更”, “保存并应用”, “保存草稿”, “恢复推荐值”, “重建向量”, “切换新索引”, and “回滚配置”. Show the old/new model and dimension before rebuild. Do not mention `.env` restart in the UI.

- [ ] **Step 8: Run frontend tests and typecheck**

Run: `Set-Location frontend-enterprise; npm run test -- src/pages/knowledge-retrieval/retrievalSettings.test.ts src/pages/KnowledgePage.retrieval-settings.test.tsx src/pages/KnowledgePage.retrieval.test.tsx`

Run: `Set-Location frontend-enterprise; npm run build`

Expected: PASS and a successful production build.

- [ ] **Step 9: Commit the task**

```powershell
git add frontend-enterprise/src/pages/knowledge-retrieval frontend-enterprise/src/pages/KnowledgePage.tsx frontend-enterprise/src/pages/KnowledgePage.retrieval.test.tsx frontend-enterprise/src/types/index.ts
git commit -m "feat: add advanced retrieval settings windows"
```

### Task 6: Update documentation, run complete verification, and perform browser acceptance

**Files:**
- Modify: `README.md`
- Modify: `README.zh.md`
- Modify: `backend/.env.example`
- Modify: `frontend-enterprise/src/pages/KnowledgePage.retrieval.test.tsx` if final labels change
- Create: `backend/tests/test_knowledge_retrieval_acceptance.py`

- [ ] **Step 1: Write the acceptance test matrix**

```python
def test_retrieval_defaults_and_hot_apply_contract() -> None:
    options = RetrievalOptions.for_new_zhipu_embedding()
    assert options.embedding.dimensions == 1024
    assert options.candidate_limit == 40
    assert options.rerank_limit == 12
    assert options.bm25.k1 == 1.5
    assert options.bm25.b == 0.75
    assert options.fusion.rrf_k == 60
```

- [ ] **Step 2: Update the documentation**

Document that administrators configure retrieval under 管理员 → 知识库, use four settings windows, test connections, save and apply without editing `.env`, and start a versioned rebuild when the Embedding identity changes. State that Zhipu Embedding-3 uses a 1024 StaffDeck preset while the provider supports 256/512/1024/2048 and has a 2048 provider default. Mark `HYBRID_KNOWLEDGE_RETRIEVAL_ENABLED` deprecated.

- [ ] **Step 3: Run backend full focused verification**

Run: `backend\.venv\Scripts\python.exe -m pytest backend/tests/test_knowledge_retrieval_options.py backend/tests/test_knowledge_retrieval_providers.py backend/tests/test_knowledge_retrieval_admin_api.py backend/tests/test_knowledge_retrieval_versioning.py backend/tests/test_hybrid_retrieval.py backend/tests/test_knowledge_retrieval_api.py backend/tests/test_knowledge_retrieval_eval.py -q`

Expected: PASS with no secret leakage assertion failures.

- [ ] **Step 4: Run frontend verification**

Run: `Set-Location frontend-enterprise; npm run test -- src/pages/knowledge-retrieval src/pages/KnowledgePage.retrieval.test.tsx`

Run: `Set-Location frontend-enterprise; npm run build`

Expected: PASS and successful build.

- [ ] **Step 5: Start or reload the local app using the documented platform launcher**

On Windows PowerShell run the repository’s documented `./scripts/dev_up.ps1 --detach` path only if the current supervisor is not already serving the branch. Confirm the active port through the project’s normal runtime files rather than guessing a process or port.

- [ ] **Step 6: Browser acceptance**

Open the Knowledge page as an administrator and verify:

1. Four summary cards are visible.
2. The Embedding dialog shows Zhipu 1024 and no 1536 default.
3. The BM25 dialog changes `k1` and `b` and the values survive reload.
4. The Fusion dialog changes RRF weights and `k`.
5. The Reranker dialog switches between dedicated API and LLM fields.
6. Blank API keys preserve existing masked keys.
7. Test buttons show latency, dimension, and stable error messages.
8. Save-and-apply does not require `.env` or a page-wide service restart.
9. Embedding dimension changes show a rebuild diff and preserve the old active configuration until activation.
10. `/api/health` and `/workspace/gallery` remain successful.

- [ ] **Step 7: Commit documentation and acceptance tests**

```powershell
git add README.md README.zh.md backend/.env.example backend/tests/test_knowledge_retrieval_acceptance.py
git commit -m "docs: document advanced retrieval configuration"
```

- [ ] **Step 8: Final status and changed-file review**

Run: `git status --short`

Run: `git log -6 --oneline`

Confirm every commit in this plan contains only its listed files and report any pre-existing dirty files separately. Do not claim completion until the verification-before-completion checklist passes.
