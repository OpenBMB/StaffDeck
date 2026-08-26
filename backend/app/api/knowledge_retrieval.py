from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal
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
    User,
    new_id,
    utc_now,
)
from app.knowledge.service import KnowledgeService, _retrieval_config_is_valid
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
    embedding_base_url: str = Field(min_length=1, max_length=1000)
    embedding_api_key: str | None = None
    embedding_model: str = Field(min_length=1, max_length=240)
    embedding_dimensions: int = Field(gt=0, le=100_000)
    reranker_mode: Literal["llm", "none"] = "llm"
    reranker_model_config_id: str | None = None
    candidate_limit: int = Field(default=40, ge=1, le=500)
    rerank_limit: int = Field(default=12, ge=1, le=100)
    enabled: bool = False


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


def retrieval_config_read(row: KnowledgeRetrievalConfig) -> KnowledgeRetrievalConfigRead:
    try:
        api_key = decrypt_secret(row.embedding_api_key_encrypted)
        masked_key = mask_secret(api_key)
    except ValueError:
        # A bad APP_SECRET must not turn a secret-management error into a secret leak.
        masked_key = "****" if row.embedding_api_key_encrypted else ""
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
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _validate_embedding_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=422, detail="EMBEDDING_BASE_URL_INVALID")
    return normalized


def _config_for_tenant(db: Session, tenant_id: str) -> KnowledgeRetrievalConfig | None:
    return db.exec(
        select(KnowledgeRetrievalConfig)
        .where(KnowledgeRetrievalConfig.tenant_id == tenant_id)
        .order_by(KnowledgeRetrievalConfig.updated_at.desc())
    ).first()


@router.put("/config", response_model=KnowledgeRetrievalConfigRead)
def upsert_retrieval_config(
    request: KnowledgeRetrievalConfigRequest,
    db: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> KnowledgeRetrievalConfigRead:
    ensure_tenant_admin(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    base_url = _validate_embedding_url(request.embedding_base_url)
    row = _config_for_tenant(db, request.tenant_id)
    if row is None:
        row = KnowledgeRetrievalConfig(
            id=new_id("retrieval"),
            tenant_id=request.tenant_id,
            name=request.name.strip(),
            embedding_base_url=base_url,
            embedding_api_key_encrypted=encrypt_secret(request.embedding_api_key or ""),
            embedding_model=request.embedding_model.strip(),
            embedding_dimensions=request.embedding_dimensions,
            reranker_mode=request.reranker_mode,
            reranker_model_config_id=request.reranker_model_config_id,
            candidate_limit=request.candidate_limit,
            rerank_limit=request.rerank_limit,
            enabled=request.enabled,
        )
    else:
        row.name = request.name.strip()
        row.embedding_base_url = base_url
        if request.embedding_api_key is not None:
            row.embedding_api_key_encrypted = encrypt_secret(request.embedding_api_key)
        row.embedding_model = request.embedding_model.strip()
        row.embedding_dimensions = request.embedding_dimensions
        row.reranker_mode = request.reranker_mode
        row.reranker_model_config_id = request.reranker_model_config_id
        row.candidate_limit = request.candidate_limit
        row.rerank_limit = request.rerank_limit
        row.enabled = request.enabled
        row.updated_at = utc_now()

    if row.enabled and not _retrieval_config_is_valid(row):
        raise HTTPException(status_code=422, detail="EMBEDDING_CONFIG_INVALID")
    db.add(row)
    db.commit()
    db.refresh(row)
    return retrieval_config_read(row)


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


@router.post("/reindex")
def reindex_knowledge(
    request: KnowledgeReindexRequest,
    db: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> dict[str, object]:
    ensure_tenant_admin(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    config = _config_for_tenant(db, request.tenant_id)
    if config is None:
        raise HTTPException(status_code=404, detail="RETRIEVAL_CONFIG_NOT_FOUND")
    if not config.enabled or not _retrieval_config_is_valid(config):
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
        "job_ids": job_ids,
        "queued_document_ids": queued_document_ids,
        "skipped_document_ids": skipped_document_ids,
    }
