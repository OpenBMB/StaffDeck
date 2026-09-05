from __future__ import annotations

import hashlib
from datetime import datetime
from time import perf_counter
from typing import Any, Literal, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.async_jobs import enqueue_async_job
from app.db import get_session
from app.db.models import (
    KnowledgeBaseVersion,
    KnowledgeChunk,
    KnowledgeChunkEmbedding,
    KnowledgeRetrievalConfig,
    ModelConfig,
    User,
    new_id,
    utc_now,
)
from app.knowledge.service import KnowledgeService, _retrieval_config_is_valid
from app.knowledge.retrieval.capabilities import adapter_capabilities
from app.knowledge.retrieval.options import (
    BM25Options,
    EmbeddingOptions,
    FusionOptions,
    RerankerOptions,
    RetrievalOptions,
    embedding_identity_fingerprint,
)
from app.knowledge.retrieval.providers import (
    DedicatedRerankProvider,
    OpenAICompatibleChatRerankClient,
    RerankProviderError,
)
from app.knowledge.retrieval.reranker import RerankResponse, RERANK_PROMPT
from app.knowledge.retrieval.vector import EmbeddingError, OpenAICompatibleEmbeddingProvider
from app.security.auth import get_current_user
from app.security.encryption import decrypt_secret, encrypt_secret, mask_secret
from app.security.permissions import ensure_tenant_admin, require_tenant_admin
from app.security.tenant import ensure_tenant

router = APIRouter(
    prefix="/api/enterprise/knowledge-retrieval",
    tags=["enterprise:knowledge-retrieval"],
    dependencies=[Depends(get_current_user)],
)


class KnowledgeRetrievalConfigRequest(BaseModel):
    tenant_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=120)
    embedding: Optional["EmbeddingConfigRequest"] = None
    bm25: BM25Options | None = None
    fusion: FusionOptions | None = None
    reranker: Optional["RerankerConfigRequest"] = None
    embedding_base_url: str | None = Field(default=None, max_length=1000)
    embedding_api_key: str | None = None
    embedding_model: str | None = Field(default=None, max_length=240)
    embedding_dimensions: int | None = Field(default=None, gt=0, le=100_000)
    embedding_adapter: str | None = None
    reranker_mode: Literal["llm", "none", "dedicated_api"] = "llm"
    reranker_model_config_id: str | None = None
    reranker_adapter: str | None = None
    reranker_base_url: str | None = Field(default=None, max_length=1000)
    reranker_api_key: str | None = None
    reranker_model: str | None = Field(default=None, max_length=240)
    candidate_limit: int | None = Field(default=None, ge=1, le=500)
    rerank_limit: int | None = Field(default=None, ge=1, le=128)
    enabled: bool = False
    expected_revision: int | None = Field(default=None, ge=1)


class EmbeddingConfigRequest(EmbeddingOptions):
    api_key: str | None = None


class RerankerConfigRequest(RerankerOptions):
    api_key: str | None = None


KnowledgeRetrievalConfigRequest.model_rebuild()


class KnowledgeRetrievalConfigRead(BaseModel):
    id: str
    tenant_id: str
    name: str
    embedding_base_url: str
    embedding_api_key_masked: str
    embedding_model: str
    embedding_dimensions: int
    reranker_mode: str
    reranker_model_config_id: str | None
    candidate_limit: int
    rerank_limit: int
    enabled: bool
    schema_version: int
    revision: int
    status: str
    embedding_adapter: str
    embedding: dict[str, Any]
    bm25: dict[str, Any]
    fusion: dict[str, Any]
    reranker: dict[str, Any]
    reranker_adapter: str
    reranker_base_url: str
    reranker_model: str
    reranker_api_key_masked: str
    requires_reindex: bool = False
    active_config_id: str | None = None
    pending_config_id: str | None = None
    last_tested_at: str | None = None
    tested_fingerprint: str | None = None
    created_at: str
    updated_at: str


class KnowledgeVectorIndexStatus(BaseModel):
    knowledge_base_version_id: str
    total_chunks: int
    ready_embeddings: int
    failed_embeddings: int
    missing_embeddings: int
    embedding_model: str
    updated_at: str | None


class KnowledgeReindexRequest(BaseModel):
    tenant_id: str = Field(min_length=1)
    knowledge_base_version_id: str | None = None
    config_id: str | None = None


class KnowledgeRetrievalConfigActionRequest(BaseModel):
    tenant_id: str = Field(min_length=1)
    config_id: str | None = None


def _http_error_from_validation(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "RERANK_LIMIT_EXCEEDS_CANDIDATE_LIMIT" in message:
        detail = "RERANK_LIMIT_EXCEEDS_CANDIDATE_LIMIT"
    elif "EMBEDDING_DIMENSIONS_REQUIRED" in message:
        detail = "EMBEDDING_DIMENSIONS_REQUIRED"
    elif "EMBEDDING_DIMENSION_UNSUPPORTED" in message:
        detail = "EMBEDDING_DIMENSION_UNSUPPORTED"
    else:
        detail = "RETRIEVAL_OPTIONS_INVALID"
    return HTTPException(status_code=422, detail=detail)


def _normalized_draft(
    request: KnowledgeRetrievalConfigRequest,
) -> tuple[RetrievalOptions, str | None, str | None]:
    if request.embedding is not None:
        embedding_values = request.embedding.model_dump(exclude={"api_key"})
        api_key = request.embedding.api_key
    else:
        if not request.embedding_base_url or not request.embedding_model:
            raise HTTPException(status_code=422, detail="EMBEDDING_CONFIG_REQUIRED")
        embedding_values = {
            "adapter": request.embedding_adapter or "openai_compatible_embedding",
            "model": request.embedding_model.strip(),
            "base_url": request.embedding_base_url,
            "dimension_mode": "explicit",
            "dimensions": request.embedding_dimensions,
        }
        api_key = request.embedding_api_key
    embedding_values["base_url"] = _validate_embedding_url(str(embedding_values.get("base_url") or ""))
    try:
        embedding = EmbeddingOptions.model_validate(embedding_values)
    except ValueError as exc:
        raise _http_error_from_validation(exc) from exc

    bm25 = request.bm25 or BM25Options()
    fusion = request.fusion or FusionOptions()
    if request.reranker is not None:
        reranker_values = request.reranker.model_dump(exclude={"api_key"})
        reranker_api_key = request.reranker.api_key
    else:
        reranker_values = {
            "mode": request.reranker_mode,
            "adapter": request.reranker_adapter or (
                "zhipu_rerank" if request.reranker_mode == "dedicated_api" else ""
            ),
            "base_url": request.reranker_base_url or "",
            "model": request.reranker_model or "",
            "candidate_limit": request.candidate_limit or 40,
            "rerank_limit": request.rerank_limit or 12,
        }
        reranker_api_key = request.reranker_api_key
    candidate_limit = request.candidate_limit or int(
        reranker_values.get("candidate_limit") or fusion.final_limit
    )
    rerank_limit = request.rerank_limit or int(reranker_values.get("rerank_limit") or 12)
    reranker_values["candidate_limit"] = candidate_limit
    reranker_values["rerank_limit"] = rerank_limit
    try:
        reranker = RerankerOptions.model_validate(reranker_values)
        options = RetrievalOptions(
            embedding=embedding,
            bm25=bm25,
            fusion=fusion,
            reranker=reranker,
            candidate_limit=candidate_limit,
            rerank_limit=rerank_limit,
        )
    except ValueError as exc:
        raise _http_error_from_validation(exc) from exc
    if reranker.mode == "dedicated_api":
        if not reranker.base_url or not reranker.model:
            raise HTTPException(status_code=422, detail="RERANKER_CONFIG_REQUIRED")
        _validate_embedding_url(reranker.base_url)
    return options, api_key, reranker_api_key


def _options_for_config(row: KnowledgeRetrievalConfig) -> RetrievalOptions:
    stored_embedding = row.embedding_options_json or {}
    if not isinstance(stored_embedding, dict):
        stored_embedding = {}
    embedding_values: dict[str, Any] = {
        "adapter": row.embedding_adapter or "openai_compatible_embedding",
        "model": row.embedding_model,
        "base_url": row.embedding_base_url,
        "dimension_mode": "explicit",
        "dimensions": row.embedding_dimensions,
    }
    embedding_values.update(stored_embedding)
    embedding = EmbeddingOptions.model_validate(embedding_values)
    bm25 = BM25Options.model_validate(row.bm25_options_json or {})
    fusion = FusionOptions.model_validate(row.fusion_options_json or {})
    stored_reranker = row.reranker_options_json or {}
    if not isinstance(stored_reranker, dict):
        stored_reranker = {}
    reranker_values: dict[str, Any] = {
        "mode": row.reranker_mode,
        "adapter": row.reranker_adapter,
        "base_url": row.reranker_base_url,
        "model": row.reranker_model,
        "candidate_limit": row.candidate_limit,
        "rerank_limit": row.rerank_limit,
    }
    reranker_values.update(stored_reranker)
    reranker = RerankerOptions.model_validate(reranker_values)
    return RetrievalOptions(
        embedding=embedding,
        bm25=bm25,
        fusion=fusion,
        reranker=reranker,
        candidate_limit=row.candidate_limit,
        rerank_limit=row.rerank_limit,
    )


def _masked_encrypted_secret(value: str) -> str:
    try:
        return mask_secret(decrypt_secret(value))
    except ValueError:
        return "****" if value else ""


def retrieval_config_read(
    row: KnowledgeRetrievalConfig,
    *,
    requires_reindex: bool = False,
    active_config_id: str | None = None,
    pending_config_id: str | None = None,
) -> KnowledgeRetrievalConfigRead:
    try:
        api_key = decrypt_secret(row.embedding_api_key_encrypted)
        masked_key = mask_secret(api_key)
    except ValueError:
        masked_key = "****" if row.embedding_api_key_encrypted else ""
    options = _options_for_config(row)
    return KnowledgeRetrievalConfigRead(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        embedding_base_url=row.embedding_base_url,
        embedding_api_key_masked=masked_key,
        embedding_model=row.embedding_model,
        embedding_dimensions=row.embedding_dimensions,
        reranker_mode=row.reranker_mode,
        reranker_model_config_id=row.reranker_model_config_id,
        candidate_limit=row.candidate_limit,
        rerank_limit=row.rerank_limit,
        enabled=row.enabled,
        schema_version=row.schema_version,
        revision=row.revision,
        status=row.status,
        embedding_adapter=row.embedding_adapter,
        embedding=options.embedding.model_dump() if options.embedding else {},
        bm25=options.bm25.model_dump(),
        fusion=options.fusion.model_dump(),
        reranker=options.reranker.model_dump(),
        reranker_adapter=row.reranker_adapter,
        reranker_base_url=row.reranker_base_url,
        reranker_model=row.reranker_model,
        reranker_api_key_masked=_masked_encrypted_secret(row.reranker_api_key_encrypted),
        requires_reindex=requires_reindex,
        active_config_id=active_config_id,
        pending_config_id=pending_config_id,
        last_tested_at=row.last_tested_at.isoformat() if row.last_tested_at else None,
        tested_fingerprint=row.tested_fingerprint,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _validate_embedding_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=422, detail="EMBEDDING_BASE_URL_INVALID")
    return normalized


def _draft_config(
    request: KnowledgeRetrievalConfigRequest,
    options: RetrievalOptions,
    embedding_api_key: str | None,
    reranker_api_key: str | None,
) -> KnowledgeRetrievalConfig:
    row = KnowledgeRetrievalConfig(
        id="retrieval-draft",
        tenant_id=request.tenant_id,
        embedding_api_key_encrypted="",
        reranker_api_key_encrypted="",
        status="active",
        enabled=request.enabled,
    )
    _apply_options_to_config(
        row,
        request,
        options,
        embedding_api_key,
        reranker_api_key,
        preserve_existing_keys=False,
    )
    return row


def _provider_error_code(exc: Exception, fallback: str) -> str:
    if exc.args and isinstance(exc.args[0], str):
        return str(exc.args[0])
    return fallback


@router.get("/capabilities")
def get_retrieval_capabilities(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> dict[str, list[dict[str, Any]]]:
    ensure_tenant_admin(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    capabilities = adapter_capabilities()
    return {
        family: [{"id": adapter_id, **metadata} for adapter_id, metadata in adapters.items()]
        for family, adapters in capabilities.items()
    }


@router.post("/validate")
def validate_retrieval_draft(
    request: KnowledgeRetrievalConfigRequest,
    db: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> dict[str, Any]:
    ensure_tenant_admin(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    options, _embedding_api_key, _reranker_api_key = _normalized_draft(request)
    return {
        "ok": True,
        "embedding": options.embedding.model_dump() if options.embedding else {},
        "bm25": options.bm25.model_dump(),
        "fusion": options.fusion.model_dump(),
        "reranker": options.reranker.model_dump(),
        "candidate_limit": options.candidate_limit,
        "rerank_limit": options.rerank_limit,
    }


@router.post("/test/embedding")
def test_embedding_connection(
    request: KnowledgeRetrievalConfigRequest,
    db: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> dict[str, Any]:
    ensure_tenant_admin(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    options, embedding_api_key, reranker_api_key = _normalized_draft(request)
    draft = _draft_config(request, options, embedding_api_key, reranker_api_key)
    started = perf_counter()
    try:
        provider = OpenAICompatibleEmbeddingProvider(draft, options)
        vectors = provider.embed(["StaffDeck embedding connection test"])
        dimensions = len(vectors[0]) if vectors else 0
        return {
            "ok": True,
            "adapter": options.embedding.adapter if options.embedding else "",
            "latency_ms": max(0, int((perf_counter() - started) * 1000)),
            "dimensions": dimensions,
            "error_code": None,
        }
    except Exception as exc:  # noqa: BLE001 - diagnostics must not expose provider secrets.
        return {
            "ok": False,
            "adapter": options.embedding.adapter if options.embedding else "",
            "latency_ms": max(0, int((perf_counter() - started) * 1000)),
            "dimensions": None,
            "error_code": _provider_error_code(exc, "EMBEDDING_PROVIDER_UNAVAILABLE"),
        }


@router.post("/test/reranker")
def test_reranker_connection(
    request: KnowledgeRetrievalConfigRequest,
    db: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> dict[str, Any]:
    ensure_tenant_admin(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    options, embedding_api_key, reranker_api_key = _normalized_draft(request)
    started = perf_counter()
    if options.reranker.mode == "none":
        return {
            "ok": True,
            "adapter": "none",
            "latency_ms": 0,
            "error_code": None,
        }
    if options.reranker.mode == "llm" and options.reranker.base_url and options.reranker.model:
        draft = _draft_config(request, options, embedding_api_key, reranker_api_key)
        try:
            client = OpenAICompatibleChatRerankClient(draft, options.reranker)
            response = client.generate_json_with_options(
                RERANK_PROMPT,
                {"query": "connection test", "candidates": [{"chunk_id": "test", "content": "test document"}]},
                temperature=options.reranker.temperature,
                max_output_tokens=options.reranker.max_output_tokens,
                input_budget_tokens=options.reranker.input_budget_tokens,
            )
            RerankResponse.model_validate(response)
            return {
                "ok": True,
                "adapter": options.reranker.adapter or "llm_rerank",
                "latency_ms": max(0, int((perf_counter() - started) * 1000)),
                "error_code": None,
            }
        except Exception as exc:  # noqa: BLE001 - diagnostics must not expose provider secrets.
            return {
                "ok": False,
                "adapter": options.reranker.adapter or "llm_rerank",
                "latency_ms": max(0, int((perf_counter() - started) * 1000)),
                "error_code": _provider_error_code(exc, "RERANK_PROVIDER_UNAVAILABLE"),
            }
    if options.reranker.mode != "dedicated_api":
        return {
            "ok": False,
            "adapter": options.reranker.adapter or "llm",
            "latency_ms": 0,
            "error_code": "RERANKER_MODEL_CONFIG_REQUIRED",
        }
    draft = _draft_config(request, options, embedding_api_key, reranker_api_key)
    try:
        provider = DedicatedRerankProvider(draft, options.reranker)
        provider.rerank("StaffDeck reranker connection test", ["test document"], 1)
        return {
            "ok": True,
            "adapter": options.reranker.adapter,
            "latency_ms": max(0, int((perf_counter() - started) * 1000)),
            "error_code": None,
        }
    except Exception as exc:  # noqa: BLE001 - diagnostics must not expose provider secrets.
        return {
            "ok": False,
            "adapter": options.reranker.adapter,
            "latency_ms": max(0, int((perf_counter() - started) * 1000)),
            "error_code": _provider_error_code(exc, "RERANK_PROVIDER_UNAVAILABLE"),
        }


def _config_for_tenant(db: Session, tenant_id: str) -> KnowledgeRetrievalConfig | None:
    return db.exec(
        select(KnowledgeRetrievalConfig)
        .where(KnowledgeRetrievalConfig.tenant_id == tenant_id)
        .where(KnowledgeRetrievalConfig.status == "active")
        .order_by(KnowledgeRetrievalConfig.updated_at.desc())
    ).first()


def _config_by_id_for_tenant(
    db: Session, tenant_id: str, config_id: str
) -> KnowledgeRetrievalConfig:
    row = db.get(KnowledgeRetrievalConfig, config_id)
    if row is None or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="RETRIEVAL_CONFIG_NOT_FOUND")
    return row


def _config_is_index_ready(db: Session, config: KnowledgeRetrievalConfig) -> bool:
    chunks = db.exec(
        select(KnowledgeChunk).where(KnowledgeChunk.tenant_id == config.tenant_id)
    ).all()
    if not chunks:
        return False
    rows = db.exec(
        select(KnowledgeChunkEmbedding).where(
            KnowledgeChunkEmbedding.tenant_id == config.tenant_id,
            KnowledgeChunkEmbedding.retrieval_config_id == config.id,
            KnowledgeChunkEmbedding.embedding_model == config.embedding_model,
            KnowledgeChunkEmbedding.status == "ready",
        )
    ).all()
    ready = {
        (row.chunk_id, row.content_sha256)
        for row in rows
        if row.dimensions == config.embedding_dimensions
    }
    return all((chunk.id, _content_sha256(chunk)) in ready for chunk in chunks)


def _apply_options_to_config(
    row: KnowledgeRetrievalConfig,
    request: KnowledgeRetrievalConfigRequest,
    options: RetrievalOptions,
    embedding_api_key: str | None,
    reranker_api_key: str | None,
    *,
    preserve_existing_keys: bool,
) -> None:
    assert options.embedding is not None
    embedding = options.embedding
    reranker = options.reranker
    row.name = request.name.strip()
    row.embedding_adapter = embedding.adapter
    row.embedding_base_url = embedding.base_url.rstrip("/")
    row.embedding_model = embedding.model.strip()
    row.embedding_dimensions = embedding.dimensions or 0
    row.embedding_options_json = embedding.model_dump()
    row.bm25_options_json = options.bm25.model_dump()
    row.fusion_options_json = options.fusion.model_dump()
    row.reranker_mode = reranker.mode
    row.reranker_adapter = reranker.adapter
    row.reranker_base_url = reranker.base_url.rstrip("/")
    row.reranker_model = reranker.model.strip()
    row.reranker_options_json = reranker.model_dump()
    row.reranker_model_config_id = request.reranker_model_config_id
    row.candidate_limit = options.candidate_limit
    row.rerank_limit = options.rerank_limit
    row.enabled = request.enabled
    row.schema_version = 2
    if embedding_api_key is not None or not preserve_existing_keys:
        row.embedding_api_key_encrypted = encrypt_secret(embedding_api_key or "")
    if reranker_api_key is not None or not preserve_existing_keys:
        row.reranker_api_key_encrypted = encrypt_secret(reranker_api_key or "")
    row.updated_at = utc_now()


@router.put("/config", response_model=KnowledgeRetrievalConfigRead)
def upsert_retrieval_config(
    request: KnowledgeRetrievalConfigRequest,
    db: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> KnowledgeRetrievalConfigRead:
    ensure_tenant_admin(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    options, embedding_api_key, reranker_api_key = _normalized_draft(request)
    if request.reranker_model_config_id:
        model_config = db.get(ModelConfig, request.reranker_model_config_id)
        if not model_config or model_config.tenant_id != request.tenant_id:
            raise HTTPException(status_code=422, detail="RERANKER_MODEL_CONFIG_INVALID")
    row = _config_for_tenant(db, request.tenant_id)
    if row is None:
        row = KnowledgeRetrievalConfig(
            id=new_id("retrieval"),
            tenant_id=request.tenant_id,
            embedding_api_key_encrypted="",
            reranker_api_key_encrypted="",
            revision=1,
            status="active",
        )
        _apply_options_to_config(
            row,
            request,
            options,
            embedding_api_key,
            reranker_api_key,
            preserve_existing_keys=False,
        )
        row.activated_at = utc_now()
        if row.enabled and not _retrieval_config_is_valid(row):
            raise HTTPException(status_code=422, detail="EMBEDDING_CONFIG_INVALID")
        db.add(row)
        db.commit()
        db.refresh(row)
        return retrieval_config_read(row, active_config_id=row.id)

    if request.expected_revision is not None and request.expected_revision != row.revision:
        raise HTTPException(status_code=409, detail="RETRIEVAL_CONFIG_REVISION_CONFLICT")

    old_options = _options_for_config(row)
    assert old_options.embedding is not None and options.embedding is not None
    identity_changed = embedding_identity_fingerprint(old_options.embedding) != embedding_identity_fingerprint(options.embedding)
    if identity_changed:
        pending = KnowledgeRetrievalConfig(
            id=new_id("retrieval"),
            tenant_id=request.tenant_id,
            embedding_api_key_encrypted="",
            reranker_api_key_encrypted="",
            revision=1,
            status="pending_index",
            enabled=False,
        )
        _apply_options_to_config(
            pending,
            request,
            options,
            embedding_api_key,
            reranker_api_key,
            preserve_existing_keys=False,
        )
        pending.enabled = False
        db.add(pending)
        db.commit()
        db.refresh(pending)
        return retrieval_config_read(
            pending,
            requires_reindex=True,
            active_config_id=row.id,
            pending_config_id=pending.id,
        )

    _apply_options_to_config(
        row,
        request,
        options,
        embedding_api_key,
        reranker_api_key,
        preserve_existing_keys=True,
    )
    row.revision += 1
    row.status = "active"
    row.activated_at = row.activated_at or utc_now()
    if row.enabled and not _retrieval_config_is_valid(row):
        raise HTTPException(status_code=422, detail="EMBEDDING_CONFIG_INVALID")
    db.add(row)
    db.commit()
    db.refresh(row)
    return retrieval_config_read(row, active_config_id=row.id)


@router.get(
    "/config",
    response_model=KnowledgeRetrievalConfigRead,
    dependencies=[Depends(require_tenant_admin)],
)
def get_retrieval_config(
    tenant_id: str = Query(...), db: Session = Depends(get_session)  # noqa: B008
) -> KnowledgeRetrievalConfigRead:
    ensure_tenant(db, tenant_id)
    row = _config_for_tenant(db, tenant_id)
    if row is None:
        raise HTTPException(status_code=404, detail="RETRIEVAL_CONFIG_NOT_FOUND")
    return retrieval_config_read(row)


def _current_embedding_rows(
    db: Session,
    config: KnowledgeRetrievalConfig | None,
    chunks: list[KnowledgeChunk],
) -> dict[tuple[str, str], KnowledgeChunkEmbedding]:
    if config is None or not chunks:
        return {}
    rows = db.exec(
        select(KnowledgeChunkEmbedding).where(
            KnowledgeChunkEmbedding.tenant_id == config.tenant_id,
            KnowledgeChunkEmbedding.retrieval_config_id == config.id,
            KnowledgeChunkEmbedding.chunk_id.in_([chunk.id for chunk in chunks]),
        )
    ).all()
    return {
        (row.chunk_id, row.content_sha256): row
        for row in rows
        if row.embedding_model == config.embedding_model
        and row.dimensions == config.embedding_dimensions
    }


def _content_sha256(chunk: KnowledgeChunk) -> str:
    return hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()


def _status_for_version(
    version_id: str,
    chunks: list[KnowledgeChunk],
    config: KnowledgeRetrievalConfig | None,
    rows: dict[tuple[str, str], KnowledgeChunkEmbedding],
) -> KnowledgeVectorIndexStatus:
    ready = 0
    failed = 0
    updated_at: datetime | None = None
    for chunk in chunks:
        row = rows.get((chunk.id, _content_sha256(chunk)))
        if row is None:
            continue
        if row.status == "ready":
            ready += 1
        elif row.status == "failed":
            failed += 1
        if updated_at is None or row.created_at > updated_at:
            updated_at = row.created_at
    return KnowledgeVectorIndexStatus(
        knowledge_base_version_id=version_id,
        total_chunks=len(chunks),
        ready_embeddings=ready,
        failed_embeddings=failed,
        missing_embeddings=len(chunks) - ready - failed,
        embedding_model=config.embedding_model if config else "",
        updated_at=updated_at.isoformat() if updated_at else None,
    )


@router.get(
    "/index-status",
    response_model=list[KnowledgeVectorIndexStatus],
    dependencies=[Depends(require_tenant_admin)],
)
def get_index_status(
    tenant_id: str = Query(...), db: Session = Depends(get_session)  # noqa: B008
) -> list[KnowledgeVectorIndexStatus]:
    ensure_tenant(db, tenant_id)
    config = _config_for_tenant(db, tenant_id)
    versions = db.exec(
        select(KnowledgeBaseVersion)
        .where(KnowledgeBaseVersion.tenant_id == tenant_id)
        .order_by(KnowledgeBaseVersion.created_at)
    ).all()
    chunks = db.exec(
        select(KnowledgeChunk).where(KnowledgeChunk.tenant_id == tenant_id)
    ).all()
    rows = _current_embedding_rows(db, config, chunks)
    chunks_by_version: dict[str, list[KnowledgeChunk]] = {}
    for chunk in chunks:
        if chunk.knowledge_base_version_id:
            chunks_by_version.setdefault(chunk.knowledge_base_version_id, []).append(chunk)
    return [
        _status_for_version(version.id, chunks_by_version.get(version.id, []), config, rows)
        for version in versions
    ]


def _document_needs_reindex(
    document_chunks: list[KnowledgeChunk],
    rows: dict[tuple[str, str], KnowledgeChunkEmbedding],
) -> bool:
    for chunk in document_chunks:
        row = rows.get((chunk.id, _content_sha256(chunk)))
        if row is None or row.status != "ready":
            return True
    return False


@router.post("/activate")
def activate_retrieval_config(
    request: KnowledgeRetrievalConfigActionRequest,
    db: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> dict[str, Any]:
    ensure_tenant_admin(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    if not request.config_id:
        raise HTTPException(status_code=422, detail="RETRIEVAL_CONFIG_REQUIRED")
    target = _config_by_id_for_tenant(db, request.tenant_id, request.config_id)
    if target.status not in {"pending_index", "ready"}:
        raise HTTPException(status_code=409, detail="RETRIEVAL_CONFIG_NOT_PENDING")
    if not _config_is_index_ready(db, target):
        raise HTTPException(status_code=409, detail="RETRIEVAL_INDEX_NOT_READY")
    active = _config_for_tenant(db, request.tenant_id)
    if active is not None and active.id != target.id:
        active.status = "archived"
        active.enabled = False
        active.updated_at = utc_now()
        db.add(active)
    target.status = "active"
    target.enabled = True
    target.revision += 1
    target.activated_at = utc_now()
    target.updated_at = utc_now()
    target.last_error_code = None
    db.add(target)
    db.commit()
    return {
        "status": "active",
        "active_config_id": target.id,
        "archived_config_id": active.id if active and active.id != target.id else None,
    }


@router.post("/rollback")
def rollback_retrieval_config(
    request: KnowledgeRetrievalConfigActionRequest,
    db: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> dict[str, Any]:
    ensure_tenant_admin(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    active = _config_for_tenant(db, request.tenant_id)
    archived_query = (
        select(KnowledgeRetrievalConfig)
        .where(
            KnowledgeRetrievalConfig.tenant_id == request.tenant_id,
            KnowledgeRetrievalConfig.status == "archived",
        )
        .order_by(KnowledgeRetrievalConfig.updated_at.desc())
    )
    if request.config_id:
        archived_query = archived_query.where(
            KnowledgeRetrievalConfig.id == request.config_id
        )
    previous = db.exec(archived_query).first()
    if previous is None:
        raise HTTPException(status_code=409, detail="RETRIEVAL_ROLLBACK_NOT_AVAILABLE")
    if active is not None:
        active.status = "archived"
        active.enabled = False
        active.updated_at = utc_now()
        db.add(active)
    previous.status = "active"
    previous.enabled = True
    previous.revision += 1
    previous.activated_at = utc_now()
    previous.updated_at = utc_now()
    db.add(previous)
    db.commit()
    return {
        "status": "active",
        "active_config_id": previous.id,
        "archived_config_id": active.id if active else None,
    }


@router.post("/reindex")
def reindex_knowledge(
    request: KnowledgeReindexRequest,
    db: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> dict[str, object]:
    ensure_tenant_admin(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    config = (
        _config_by_id_for_tenant(db, request.tenant_id, request.config_id)
        if request.config_id
        else _config_for_tenant(db, request.tenant_id)
    )
    if config is None:
        raise HTTPException(status_code=404, detail="RETRIEVAL_CONFIG_NOT_FOUND")
    if (
        (not config.enabled and config.status != "pending_index")
        or not _retrieval_config_is_valid(config)
    ):
        raise HTTPException(status_code=409, detail="RETRIEVAL_CONFIG_DISABLED_OR_INVALID")

    version_filter = request.knowledge_base_version_id
    if version_filter:
        version = db.get(KnowledgeBaseVersion, version_filter)
        if not version or version.tenant_id != request.tenant_id:
            raise HTTPException(status_code=404, detail="KNOWLEDGE_VERSION_NOT_FOUND")

    chunks_query = select(KnowledgeChunk).where(KnowledgeChunk.tenant_id == request.tenant_id)
    if version_filter:
        chunks_query = chunks_query.where(
            KnowledgeChunk.knowledge_base_version_id == version_filter
        )
    chunks = db.exec(chunks_query.order_by(KnowledgeChunk.document_id, KnowledgeChunk.chunk_index)).all()
    rows = _current_embedding_rows(db, config, chunks)
    chunks_by_document: dict[tuple[str, str], list[KnowledgeChunk]] = {}
    for chunk in chunks:
        if chunk.knowledge_base_version_id:
            chunks_by_document.setdefault(
                (chunk.knowledge_base_version_id, chunk.document_id), []
            ).append(chunk)

    job_ids: list[str] = []
    queued_document_ids: list[str] = []
    skipped_document_ids: list[str] = []
    for (version_id, document_id), document_chunks in chunks_by_document.items():
        if not _document_needs_reindex(document_chunks, rows):
            skipped_document_ids.append(document_id)
            continue
        job = enqueue_async_job(
            "knowledge_vector_index",
            KnowledgeService.run_vector_index_job,
            request.tenant_id,
            version_id,
            document_id,
            config.id,
            metadata={
                "tenant_id": request.tenant_id,
                "knowledge_base_version_id": version_id,
                "document_id": document_id,
            },
        )
        job_id = getattr(job, "id", None)
        if job_id:
            job_ids.append(str(job_id))
        queued_document_ids.append(document_id)

    return {
        "status": "queued",
        "config_id": config.id,
        "job_ids": job_ids,
        "queued_document_ids": queued_document_ids,
        "skipped_document_ids": skipped_document_ids,
    }
