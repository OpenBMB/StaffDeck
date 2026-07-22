from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, func, text
from sqlmodel import Session, select

from app.agents.branching import (
    ensure_agent_private_knowledge_branch,
    ensure_open_gallery_binding,
    is_open_gallery_resource,
    knowledge_version_for_upload,
    mark_resource_open_gallery,
    mark_resource_private_for_agent,
    metadata_preserving_creator,
    user_creator_metadata,
    visible_knowledge_base_versions,
    visible_knowledge_base_version_ids,
)
from app.async_jobs import enqueue_async_job
from app.db import get_session
from app.db.models import (
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeConcept,
    KnowledgeDiscoverySuggestion,
    KnowledgeDocument,
    KnowledgeIngestJob,
    KnowledgeBase,
    KnowledgeBaseVersion,
    ModelConfig,
    User,
    utc_now,
)
from app.llm.model_config_resolver import resolve_model_config_for_runtime
from app.knowledge.schema import (
    KnowledgeBucketRead,
    KnowledgeChunkRead,
    KnowledgeChunkUpdateRequest,
    KnowledgeDiscoveryRead,
    KnowledgeDocumentRead,
    KnowledgeDocumentUpdateRequest,
    KnowledgeDocumentUploadRequest,
    KnowledgeBucketUpdateRequest,
    KnowledgeOkfImportRequest,
    KnowledgeIngestJobRead,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
)
from app.knowledge.okf import (
    build_okf_for_document,
    create_concept_evidence_rows,
    parse_okf_bundle,
    upsert_concepts,
)
from app.knowledge.service import (
    IngestPayload,
    KnowledgeDiscoveryConflictError,
    KnowledgeDiscoveryValidationError,
    KnowledgeService,
    bucket_read,
    chunk_read,
    validate_discovered_skill,
)
from app.security.auth import ensure_current_user_tenant, get_current_user
from app.security.permissions import (
    ensure_agent_scope_manager,
    ensure_open_gallery_admin,
    require_agent_scope_viewer,
)
from app.security.tenant import ensure_tenant

router = APIRouter(
    prefix="/api/enterprise/knowledge",
    tags=["enterprise:knowledge"],
    dependencies=[Depends(get_current_user)],
)


@router.post("/documents", response_model=KnowledgeIngestJobRead)
def upload_document(
    request: KnowledgeDocumentUploadRequest,
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> KnowledgeIngestJobRead:
    ensure_tenant(db, request.tenant_id)
    creator_metadata = user_creator_metadata(current_user, request.metadata or {})
    knowledge_base = _resolve_upload_knowledge_base(
        db,
        request,
        agent_id,
        current_user,
        creator_metadata=creator_metadata,
    )
    version = knowledge_version_for_upload(
        db,
        request.tenant_id,
        knowledge_base.id,
        agent_id,
        metadata_json=creator_metadata,
    )
    db.commit()
    service = KnowledgeService(db)
    job = service.create_ingest_job(
        IngestPayload(
            tenant_id=request.tenant_id,
            knowledge_base_id=knowledge_base.id,
            knowledge_base_version_id=version.id,
            filename=request.filename,
            content_base64=request.content_base64,
            title=request.title,
            metadata=creator_metadata,
        )
    )
    enqueue_async_job(
        "knowledge_ingest",
        service.run_ingest_job,
        job.id,
        metadata={"tenant_id": request.tenant_id, "filename": request.filename},
    )
    return job_read(job)


@router.post("/okf/import")
def import_okf_bundle(
    request: KnowledgeOkfImportRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    ensure_tenant(db, request.tenant_id)
    try:
        content = base64.b64decode(request.content_base64)
        parsed_docs = parse_okf_bundle(request.filename, content)
    except Exception as exc:  # noqa: BLE001 - surface stable import failures.
        raise HTTPException(status_code=400, detail=f"OKF import failed: {exc}") from exc
    if not parsed_docs:
        raise HTTPException(
            status_code=400, detail="OKF bundle does not contain concept markdown files"
        )

    upload_request = KnowledgeDocumentUploadRequest(
        tenant_id=request.tenant_id,
        knowledge_base_id=request.knowledge_base_id,
        filename=request.filename,
        title=Path(request.filename).stem or "OKF Bundle",
        content_base64="",
        metadata={"okf_import": True, "source_filename": request.filename},
    )
    creator_metadata = user_creator_metadata(current_user, upload_request.metadata or {})
    knowledge_base = _resolve_upload_knowledge_base(
        db,
        upload_request,
        request.agent_id,
        current_user,
        creator_metadata=creator_metadata,
    )
    version = knowledge_version_for_upload(
        db,
        request.tenant_id,
        knowledge_base.id,
        request.agent_id,
        metadata_json=creator_metadata,
    )
    document = KnowledgeDocument(
        tenant_id=request.tenant_id,
        knowledge_base_id=knowledge_base.id,
        knowledge_base_version_id=version.id,
        filename=request.filename,
        file_type="okf",
        title=Path(request.filename).stem or request.filename,
        status="processing",
        metadata_json={
            **creator_metadata,
            "okf_import": True,
            "document_card": {
                "title": Path(request.filename).stem or request.filename,
                "filename": request.filename,
                "file_type": "okf",
                "summary": f"从 OKF bundle 导入 {len(parsed_docs)} 个概念页。",
                "outline": [
                    {
                        "section_id": item.concept_id,
                        "title": item.frontmatter.get("title") or item.concept_id,
                        "path": item.concept_id,
                        "level": 1,
                        "summary": item.frontmatter.get("description") or "",
                    }
                    for item in parsed_docs[:80]
                ],
                "applicable_scenarios": ["OKF Wiki", "业务知识检索"],
                "key_entities": sorted(
                    {str(item.frontmatter.get("type") or "Topic") for item in parsed_docs}
                ),
                "section_count": len(parsed_docs),
            },
            "okf": {"version": "0.1", "concept_count": len(parsed_docs)},
        },
    )
    db.add(document)
    db.flush()
    concept_rows = upsert_concepts(
        db,
        request.tenant_id,
        knowledge_base.id,
        version.id,
        [
            {
                "concept_id": item.concept_id,
                "content_md": item.content_md,
                "document_id": document.id,
                "source_refs": [{"document_id": document.id, "okf_file": f"{item.concept_id}.md"}],
            }
            for item in parsed_docs
        ],
    )
    create_concept_evidence_rows(
        db, request.tenant_id, knowledge_base.id, version.id, document, concept_rows
    )
    return {
        "status": "imported",
        "knowledge_base_id": knowledge_base.id,
        "knowledge_base_version_id": version.id,
        "version": version.version,
        "document_id": document.id,
        "concept_count": len(concept_rows),
    }


def _resolve_upload_knowledge_base(
    db: Session,
    request: KnowledgeDocumentUploadRequest,
    agent_id: str | None,
    current_user: object | None = None,
    creator_metadata: dict[str, Any] | None = None,
) -> KnowledgeBase:
    agent = ensure_agent_scope_manager(db, request.tenant_id, agent_id, current_user)
    if request.knowledge_base_id:
        knowledge_base = db.get(KnowledgeBase, request.knowledge_base_id)
        if (
            not knowledge_base
            or knowledge_base.tenant_id != request.tenant_id
            or knowledge_base.status == "archived"
        ):
            raise HTTPException(status_code=404, detail="Knowledge base not found")
        if not (agent and not agent.is_overall):
            _ensure_open_gallery_knowledge_admin(
                db, request.tenant_id, knowledge_base.id, current_user
            )
        return knowledge_base

    if not (agent and not agent.is_overall):
        ensure_open_gallery_admin(request.tenant_id, current_user)
    base_name = _knowledge_base_name_from_upload(request)
    name = _unique_knowledge_base_name(db, request.tenant_id, base_name)
    knowledge_base = KnowledgeBase(
        tenant_id=request.tenant_id,
        name=name,
        description=f"由文档 {request.filename} 创建",
        status="active",
        metadata_json={
            **(creator_metadata or user_creator_metadata(current_user, request.metadata or {})),
            "created_from_document_upload": True,
            "source_filename": request.filename,
        },
    )
    db.add(knowledge_base)
    db.flush()

    if agent and not agent.is_overall:
        mark_resource_private_for_agent(knowledge_base, agent.id, creator_metadata)
        ensure_agent_private_knowledge_branch(
            db,
            request.tenant_id,
            agent.id,
            knowledge_base,
            metadata_json=creator_metadata,
        )
    else:
        mark_resource_open_gallery(knowledge_base, creator_metadata)
        ensure_open_gallery_binding(
            db,
            request.tenant_id,
            "knowledge_base",
            knowledge_base.id,
            "active",
            metadata_json=creator_metadata,
        )
    return knowledge_base


def _knowledge_base_name_from_upload(request: KnowledgeDocumentUploadRequest) -> str:
    title = (request.title or "").strip()
    if title:
        return title
    stem = Path(request.filename).stem.strip()
    return stem or request.filename.strip() or "未命名知识库"


def _unique_knowledge_base_name(db: Session, tenant_id: str, base_name: str) -> str:
    normalized_base = base_name.strip() or "未命名知识库"
    existing_names = set(
        db.exec(select(KnowledgeBase.name).where(KnowledgeBase.tenant_id == tenant_id)).all()
    )
    if normalized_base not in existing_names:
        return normalized_base
    index = 2
    while True:
        candidate = f"{normalized_base} {index}"
        if candidate not in existing_names:
            return candidate
        index += 1


@router.get(
    "/jobs",
    response_model=list[KnowledgeIngestJobRead],
    dependencies=[Depends(require_agent_scope_viewer)],
)
def list_jobs(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(8, ge=1, le=50),
    db: Session = Depends(get_session),
) -> list[KnowledgeIngestJobRead]:
    ensure_tenant(db, tenant_id)
    KnowledgeService(db).finalize_stale_cancel_requested_jobs(tenant_id)
    visible_version_ids = visible_knowledge_base_version_ids(
        db, tenant_id, agent_id, include_inactive=True
    )
    if not visible_version_ids:
        return []
    statement = select(KnowledgeIngestJob).where(
        KnowledgeIngestJob.tenant_id == tenant_id,
        KnowledgeIngestJob.knowledge_base_version_id.in_(visible_version_ids),
    )
    if status:
        statuses = [item.strip() for item in status.split(",") if item.strip()]
        if statuses:
            statement = statement.where(KnowledgeIngestJob.status.in_(statuses))
    rows = db.exec(
        statement.order_by(
            KnowledgeIngestJob.created_at.desc(), KnowledgeIngestJob.id.desc()
        ).limit(limit)
    ).all()
    return [job_read(row) for row in rows]


@router.get(
    "/jobs/{job_id}",
    response_model=KnowledgeIngestJobRead,
    dependencies=[Depends(require_agent_scope_viewer)],
)
def get_job(
    job_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> KnowledgeIngestJobRead:
    ensure_tenant(db, tenant_id)
    job = db.get(KnowledgeIngestJob, job_id)
    if not job or job.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Knowledge ingest job not found")
    _ensure_knowledge_version_visible(db, tenant_id, job.knowledge_base_version_id, agent_id)
    KnowledgeService(db).finalize_stale_cancel_requested_job(job)
    return job_read(job)


@router.post("/jobs/{job_id}/cancel", response_model=KnowledgeIngestJobRead)
def cancel_job(
    job_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> KnowledgeIngestJobRead:
    ensure_current_user_tenant(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    existing = db.get(KnowledgeIngestJob, job_id)
    if existing and existing.tenant_id == tenant_id:
        _ensure_open_gallery_knowledge_admin(
            db, tenant_id, existing.knowledge_base_id, current_user
        )
    job = KnowledgeService(db).cancel_ingest_job(job_id, tenant_id)
    if not job:
        raise HTTPException(status_code=404, detail="Knowledge ingest job not found")
    return job_read(job)


@router.get(
    "/documents",
    response_model=list[KnowledgeDocumentRead],
    dependencies=[Depends(require_agent_scope_viewer)],
)
def list_documents(
    tenant_id: str = Query(...),
    knowledge_base_id: str | None = Query(None),
    agent_id: str | None = Query(None),
    include_all_versions: bool = Query(False),
    db: Session = Depends(get_session),
) -> list[KnowledgeDocumentRead]:
    ensure_tenant(db, tenant_id)
    visible_versions = visible_knowledge_base_versions(
        db, tenant_id, agent_id, include_inactive=True
    )
    if not visible_versions:
        return []
    if knowledge_base_id and knowledge_base_id not in visible_versions:
        return []
    stmt = select(KnowledgeDocument).where(KnowledgeDocument.tenant_id == tenant_id)
    if include_all_versions:
        visible_knowledge_base_ids = (
            [knowledge_base_id] if knowledge_base_id else list(visible_versions)
        )
        stmt = stmt.where(KnowledgeDocument.knowledge_base_id.in_(visible_knowledge_base_ids))
    else:
        visible_version_ids = [
            version.id
            for base_id, version in visible_versions.items()
            if not knowledge_base_id or base_id == knowledge_base_id
        ]
        stmt = stmt.where(KnowledgeDocument.knowledge_base_version_id.in_(visible_version_ids))
    rows = db.exec(stmt.order_by(KnowledgeDocument.created_at.desc())).all()
    return [document_read(row) for row in rows]


@router.get(
    "/documents/{document_id}",
    response_model=KnowledgeDocumentRead,
    dependencies=[Depends(require_agent_scope_viewer)],
)
def get_document(
    document_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> KnowledgeDocumentRead:
    row = _get_document(db, tenant_id, document_id)
    _ensure_knowledge_version_visible(db, tenant_id, row.knowledge_base_version_id, agent_id)
    return document_read(row)


@router.put("/documents/{document_id}", response_model=KnowledgeDocumentRead)
def update_document(
    document_id: str,
    request: KnowledgeDocumentUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> KnowledgeDocumentRead:
    row = _get_document(db, request.tenant_id, document_id)
    _ensure_open_gallery_knowledge_admin(db, request.tenant_id, row.knowledge_base_id, current_user)
    metadata = dict(row.metadata_json or {})
    if request.metadata is not None:
        metadata = metadata_preserving_creator(row.metadata_json, request.metadata)
    if request.title is not None:
        row.title = request.title.strip() or row.filename
        document_card = (
            metadata.get("document_card") if isinstance(metadata.get("document_card"), dict) else {}
        )
        metadata["document_card"] = {**document_card, "title": row.title}
    if request.status is not None:
        row.status = request.status
    if request.metadata is not None or request.title is not None:
        row.metadata_json = metadata
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    _refresh_document_okf_concepts(db, row)
    return document_read(row)


@router.get(
    "/documents/{document_id}/buckets",
    response_model=list[KnowledgeBucketRead],
    dependencies=[Depends(require_agent_scope_viewer)],
)
def get_document_buckets(
    document_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> list[KnowledgeBucketRead]:
    document = _get_document(db, tenant_id, document_id)
    _ensure_knowledge_version_visible(db, tenant_id, document.knowledge_base_version_id, agent_id)
    rows = _safe_document_bucket_rows(db, tenant_id, document_id)
    chunk_counts = dict(
        db.exec(
            select(KnowledgeChunk.bucket_id, func.count(KnowledgeChunk.id))
            .where(KnowledgeChunk.tenant_id == tenant_id, KnowledgeChunk.document_id == document_id)
            .group_by(KnowledgeChunk.bucket_id)
        ).all()
    )
    return [
        _bucket_read_mapping_with_stats(row, int(chunk_counts.get(str(row.get("id")), 0)))
        for row in rows
    ]


@router.put("/buckets/{bucket_id}", response_model=KnowledgeBucketRead)
def update_bucket(
    bucket_id: str,
    request: KnowledgeBucketUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> KnowledgeBucketRead:
    ensure_tenant(db, request.tenant_id)
    row = db.get(KnowledgeBucket, bucket_id)
    if not row or row.tenant_id != request.tenant_id:
        raise HTTPException(status_code=404, detail="Knowledge bucket not found")
    _ensure_open_gallery_knowledge_admin(db, request.tenant_id, row.knowledge_base_id, current_user)
    if request.title is not None:
        row.title = request.title.strip() or row.title
    if request.summary is not None:
        row.summary = request.summary
    if request.metadata is not None:
        row.metadata_json = metadata_preserving_creator(
            row.metadata_json,
            request.metadata,
        )
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    document = db.get(KnowledgeDocument, row.document_id)
    if document:
        _refresh_document_okf_concepts(db, document)
    chunk_count = db.exec(
        select(func.count(KnowledgeChunk.id)).where(
            KnowledgeChunk.tenant_id == request.tenant_id,
            KnowledgeChunk.bucket_id == bucket_id,
        )
    ).one()
    return bucket_read_with_stats(row, int(chunk_count or 0))


@router.get(
    "/buckets/{bucket_id}/chunks",
    response_model=list[KnowledgeChunkRead],
    dependencies=[Depends(require_agent_scope_viewer)],
)
def get_bucket_chunks(
    bucket_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> list[KnowledgeChunkRead]:
    ensure_tenant(db, tenant_id)
    bucket = db.get(KnowledgeBucket, bucket_id)
    if not bucket or bucket.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Knowledge bucket not found")
    _ensure_knowledge_version_visible(db, tenant_id, bucket.knowledge_base_version_id, agent_id)
    rows = _safe_bucket_chunk_rows(db, tenant_id, bucket_id)
    return [_chunk_read_mapping(row) for row in rows]


@router.put("/chunks/{chunk_id}", response_model=KnowledgeChunkRead)
def update_chunk(
    chunk_id: str,
    request: KnowledgeChunkUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> KnowledgeChunkRead:
    ensure_tenant(db, request.tenant_id)
    row = db.get(KnowledgeChunk, chunk_id)
    if not row or row.tenant_id != request.tenant_id:
        raise HTTPException(status_code=404, detail="Knowledge chunk not found")
    _ensure_open_gallery_knowledge_admin(db, request.tenant_id, row.knowledge_base_id, current_user)
    if request.content is not None:
        row.content = request.content
    if request.summary is not None:
        row.summary = request.summary
    if request.metadata is not None:
        row.metadata_json = metadata_preserving_creator(
            row.metadata_json,
            request.metadata,
        )
    row.updated_at = utc_now()
    db.add(row)
    bucket = _sync_bucket_content_from_chunks(db, request.tenant_id, row.bucket_id)
    db.commit()
    db.refresh(row)
    if bucket:
        db.refresh(bucket)
        document = db.get(KnowledgeDocument, bucket.document_id)
        if document:
            _refresh_document_okf_concepts(db, document)
    return chunk_read(row)


@router.post("/search", response_model=KnowledgeSearchResponse)
def search_knowledge(
    request: KnowledgeSearchRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> KnowledgeSearchResponse:
    require_agent_scope_viewer(request.tenant_id, request.agent_id, current_user, db)
    ensure_tenant(db, request.tenant_id)
    model_config = _get_request_model(db, request.tenant_id, request.model_config_id)
    visible_version_ids = visible_knowledge_base_version_ids(
        db,
        request.tenant_id,
        request.agent_id,
    )
    if not visible_version_ids:
        trace = [{"phase": "no_visible_knowledge", "message": "当前范围没有可见知识"}]
        return KnowledgeSearchResponse(trace=trace, route_trace=trace)
    if request.knowledge_base_version_ids:
        allowed_ids = set(visible_version_ids)
        request.knowledge_base_version_ids = [
            version_id
            for version_id in request.knowledge_base_version_ids
            if version_id in allowed_ids
        ]
    else:
        request.knowledge_base_version_ids = visible_version_ids
    return KnowledgeService(db).search(request, model_config)


def _get_default_model(db: Session, tenant_id: str) -> ModelConfig | None:
    row = db.exec(
        select(ModelConfig).where(
            ModelConfig.tenant_id == tenant_id,
            ModelConfig.is_default == True,  # noqa: E712
            ModelConfig.enabled == True,  # noqa: E712
        )
    ).first()
    return _runtime_model(db, tenant_id, row) if row else None


def _get_request_model(
    db: Session, tenant_id: str, model_config_id: str | None = None
) -> ModelConfig | None:
    if not model_config_id:
        return _get_default_model(db, tenant_id)
    model_config = db.get(ModelConfig, model_config_id)
    if not model_config or model_config.tenant_id != tenant_id or not model_config.enabled:
        raise HTTPException(status_code=404, detail="Model config not found")
    return _runtime_model(db, tenant_id, model_config)


def _runtime_model(db: Session, tenant_id: str, row: ModelConfig):
    return resolve_model_config_for_runtime(db, tenant_id, row.id)


@router.get(
    "/discoveries",
    response_model=list[KnowledgeDiscoveryRead],
    dependencies=[Depends(require_agent_scope_viewer)],
)
def list_discoveries(
    tenant_id: str = Query(...),
    knowledge_base_id: str | None = Query(None),
    status: str | None = Query(None),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> list[KnowledgeDiscoveryRead]:
    ensure_tenant(db, tenant_id)
    visible_versions = visible_knowledge_base_versions(
        db, tenant_id, agent_id, include_inactive=True
    )
    if knowledge_base_id and knowledge_base_id not in visible_versions:
        return []
    visible_version_ids = [
        version.id
        for base_id, version in visible_versions.items()
        if not knowledge_base_id or base_id == knowledge_base_id
    ]
    if not visible_version_ids:
        return []
    stmt = select(KnowledgeDiscoverySuggestion).where(
        KnowledgeDiscoverySuggestion.tenant_id == tenant_id,
        KnowledgeDiscoverySuggestion.knowledge_base_version_id.in_(visible_version_ids),
    )
    if status:
        stmt = stmt.where(KnowledgeDiscoverySuggestion.status == status)
    rows = db.exec(stmt.order_by(KnowledgeDiscoverySuggestion.created_at.desc())).all()
    visible_rows: list[KnowledgeDiscoverySuggestion] = []
    for row in rows:
        if row.status == "pending" and row.suggestion_type == "skill":
            payload = row.payload_json or {}
            skill_payload = payload.get("draft_skill") if isinstance(payload.get("draft_skill"), dict) else payload
            try:
                validate_discovered_skill(skill_payload)
            except KnowledgeDiscoveryValidationError:
                continue
        visible_rows.append(row)
    return [discovery_read(row) for row in visible_rows]


@router.post("/discoveries/{suggestion_id}/confirm")
def confirm_discovery(
    suggestion_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    row = _get_discovery(db, tenant_id, suggestion_id)
    _ensure_open_gallery_knowledge_admin(db, tenant_id, row.knowledge_base_id, current_user)
    try:
        result = KnowledgeService(db).confirm_discovery(row)
    except KnowledgeDiscoveryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KnowledgeDiscoveryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "confirmed", "result": result}


@router.post("/discoveries/{suggestion_id}/reject")
def reject_discovery(
    suggestion_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    row = _get_discovery(db, tenant_id, suggestion_id)
    _ensure_open_gallery_knowledge_admin(db, tenant_id, row.knowledge_base_id, current_user)
    try:
        KnowledgeService(db).reject_discovery(row)
    except KnowledgeDiscoveryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "rejected"}


def _refresh_document_okf_concepts(db: Session, document: KnowledgeDocument) -> None:
    metadata = document.metadata_json or {}
    section_nodes = (
        metadata.get("section_tree") if isinstance(metadata.get("section_tree"), list) else []
    )
    buckets = db.exec(
        select(KnowledgeBucket)
        .where(
            KnowledgeBucket.tenant_id == document.tenant_id,
            KnowledgeBucket.knowledge_base_id == document.knowledge_base_id,
            KnowledgeBucket.knowledge_base_version_id == document.knowledge_base_version_id,
            KnowledgeBucket.document_id == document.id,
        )
        .order_by(KnowledgeBucket.created_at.asc())
    ).all()
    if not section_nodes and not buckets:
        return
    db.exec(
        delete(KnowledgeConcept).where(
            KnowledgeConcept.tenant_id == document.tenant_id,
            KnowledgeConcept.knowledge_base_id == document.knowledge_base_id,
            KnowledgeConcept.knowledge_base_version_id == document.knowledge_base_version_id,
            KnowledgeConcept.document_id == document.id,
            KnowledgeConcept.concept_type.in_(["Source Document", "Source Section"]),
        )
    )
    db.flush()
    upsert_concepts(
        db,
        document.tenant_id,
        document.knowledge_base_id,
        document.knowledge_base_version_id,
        build_okf_for_document(document, section_nodes, buckets),
    )


def _sync_bucket_content_from_chunks(
    db: Session,
    tenant_id: str,
    bucket_id: str,
) -> KnowledgeBucket | None:
    bucket = db.get(KnowledgeBucket, bucket_id)
    if not bucket or bucket.tenant_id != tenant_id:
        return None
    chunks = db.exec(
        select(KnowledgeChunk)
        .where(KnowledgeChunk.tenant_id == tenant_id, KnowledgeChunk.bucket_id == bucket_id)
        .order_by(KnowledgeChunk.chunk_index.asc())
    ).all()
    content = "\n\n".join(chunk.content for chunk in chunks if chunk.content.strip()).strip()
    metadata = dict(bucket.metadata_json or {})
    metadata["content"] = content[:6000]
    metadata["chunk_count"] = len(chunks)
    metadata["representative_chunk_ids"] = [chunk.id for chunk in chunks[:3]]
    bucket.metadata_json = metadata
    bucket.token_estimate = max(1, len(content) // 2) if content else bucket.token_estimate
    bucket.updated_at = utc_now()
    db.add(bucket)
    return bucket


def job_read(row: KnowledgeIngestJob) -> KnowledgeIngestJobRead:
    return KnowledgeIngestJobRead(
        id=row.id,
        tenant_id=row.tenant_id,
        knowledge_base_id=row.knowledge_base_id,
        document_id=row.document_id,
        filename=row.filename,
        status=row.status,
        stage=row.stage,
        progress=row.progress,
        error=row.error,
        metadata={
            key: value
            for key, value in (row.metadata_json or {}).items()
            if key != "content_base64"
        },
        created_at=row.created_at.isoformat(),
        started_at=row.started_at.isoformat() if row.started_at else None,
        finished_at=row.finished_at.isoformat() if row.finished_at else None,
        updated_at=row.updated_at.isoformat(),
    )


def document_read(row: KnowledgeDocument) -> KnowledgeDocumentRead:
    return KnowledgeDocumentRead(
        id=row.id,
        tenant_id=row.tenant_id,
        knowledge_base_id=row.knowledge_base_id,
        knowledge_base_version_id=row.knowledge_base_version_id,
        filename=row.filename,
        file_type=row.file_type,
        title=row.title,
        status=row.status,
        bucket_count=row.bucket_count,
        chunk_count=row.chunk_count,
        metadata=row.metadata_json or {},
        error=row.error,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def bucket_read_with_stats(row: KnowledgeBucket, chunk_count: int) -> KnowledgeBucketRead:
    item = bucket_read(row)
    item.chunk_count = chunk_count
    item.status = "ready" if chunk_count > 0 and row.summary.strip() else "incomplete"
    return item


def _safe_document_bucket_rows(
    db: Session, tenant_id: str, document_id: str
) -> list[Mapping[str, Any]]:
    return list(
        db.execute(
            text(
                """
                SELECT
                    id,
                    tenant_id,
                    knowledge_base_id,
                    knowledge_base_version_id,
                    document_id,
                    CAST(bucket_key AS BLOB) AS bucket_key,
                    CAST(title AS BLOB) AS title,
                    CAST(summary AS BLOB) AS summary,
                    token_estimate,
                    CAST(metadata_json AS BLOB) AS metadata_json,
                    created_at,
                    updated_at
                FROM knowledge_buckets
                WHERE tenant_id = :tenant_id AND document_id = :document_id
                ORDER BY created_at ASC
                """
            ),
            {"tenant_id": tenant_id, "document_id": document_id},
        )
        .mappings()
        .all()
    )


def _safe_bucket_chunk_rows(db: Session, tenant_id: str, bucket_id: str) -> list[Mapping[str, Any]]:
    return list(
        db.execute(
            text(
                """
                SELECT
                    id,
                    tenant_id,
                    knowledge_base_id,
                    knowledge_base_version_id,
                    document_id,
                    bucket_id,
                    chunk_index,
                    CAST(content AS BLOB) AS content,
                    CAST(summary AS BLOB) AS summary,
                    CAST(source_ref AS BLOB) AS source_ref,
                    CAST(metadata_json AS BLOB) AS metadata_json,
                    created_at,
                    updated_at
                FROM knowledge_chunks
                WHERE tenant_id = :tenant_id AND bucket_id = :bucket_id
                ORDER BY chunk_index ASC
                """
            ),
            {"tenant_id": tenant_id, "bucket_id": bucket_id},
        )
        .mappings()
        .all()
    )


def _bucket_read_mapping_with_stats(
    row: Mapping[str, Any], chunk_count: int
) -> KnowledgeBucketRead:
    summary = _safe_text(row.get("summary"))
    metadata = _safe_json_object(row.get("metadata_json"))
    return KnowledgeBucketRead(
        id=_safe_text(row.get("id")),
        tenant_id=_safe_text(row.get("tenant_id")),
        knowledge_base_id=_safe_text(row.get("knowledge_base_id")),
        document_id=_safe_text(row.get("document_id")),
        bucket_key=_safe_text(row.get("bucket_key")),
        title=_safe_text(row.get("title"), "未命名片段"),
        summary=summary,
        token_estimate=_safe_int(row.get("token_estimate")),
        chunk_count=chunk_count
        or int(
            metadata.get("chunk_count") or len(metadata.get("representative_chunk_ids") or []) or 0
        ),
        status="ready" if chunk_count > 0 and summary.strip() else "incomplete",
        metadata=metadata,
        created_at=_safe_datetime_text(row.get("created_at")),
        updated_at=_safe_datetime_text(row.get("updated_at")),
    )


def _chunk_read_mapping(row: Mapping[str, Any]) -> KnowledgeChunkRead:
    return KnowledgeChunkRead(
        id=_safe_text(row.get("id")),
        tenant_id=_safe_text(row.get("tenant_id")),
        knowledge_base_id=_safe_text(row.get("knowledge_base_id")),
        document_id=_safe_text(row.get("document_id")),
        bucket_id=_safe_text(row.get("bucket_id")),
        chunk_index=_safe_int(row.get("chunk_index")),
        content=_safe_text(row.get("content")),
        summary=_safe_optional_text(row.get("summary")),
        source_ref=_safe_optional_text(row.get("source_ref")),
        metadata=_safe_json_object(row.get("metadata_json")),
        created_at=_safe_datetime_text(row.get("created_at")),
        updated_at=_safe_datetime_text(row.get("updated_at")),
    )


def _safe_text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        for encoding in ("utf-8", "gb18030"):
            try:
                return value.decode(encoding)
            except UnicodeDecodeError:
                continue
        return value.decode("utf-8", errors="replace")
    return str(value)


def _safe_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text_value = _safe_text(value)
    return text_value if text_value else None


def _safe_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    try:
        parsed = json.loads(_safe_text(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_datetime_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return _safe_text(value)


def discovery_read(row: KnowledgeDiscoverySuggestion) -> KnowledgeDiscoveryRead:
    return KnowledgeDiscoveryRead(
        id=row.id,
        tenant_id=row.tenant_id,
        knowledge_base_id=row.knowledge_base_id,
        document_id=row.document_id,
        bucket_id=row.bucket_id,
        suggestion_type=row.suggestion_type,  # type: ignore[arg-type]
        title=row.title,
        status=row.status,
        payload=row.payload_json or {},
        source_refs=row.source_refs_json or [],
        reason=row.reason,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _get_document(db: Session, tenant_id: str, document_id: str) -> KnowledgeDocument:
    ensure_tenant(db, tenant_id)
    row = db.get(KnowledgeDocument, document_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Knowledge document not found")
    return row


def _ensure_knowledge_version_visible(
    db: Session,
    tenant_id: str,
    knowledge_base_version_id: str | None,
    agent_id: str | None,
) -> None:
    if not knowledge_base_version_id:
        raise HTTPException(
            status_code=404,
            detail="Knowledge resource has no version binding; re-ingest the document or restore its knowledge-base version",
        )
    version = db.get(KnowledgeBaseVersion, knowledge_base_version_id)
    if not version or version.tenant_id != tenant_id:
        raise HTTPException(
            status_code=404,
            detail=f"Knowledge-base version {knowledge_base_version_id} does not exist in tenant {tenant_id}",
        )
    visible_base_ids = set(
        visible_knowledge_base_versions(db, tenant_id, agent_id, include_inactive=True)
    )
    if version.knowledge_base_id not in visible_base_ids:
        scope = agent_id or "open-gallery"
        raise HTTPException(
            status_code=404,
            detail=(
                f"Knowledge-base version {knowledge_base_version_id} belongs to resource "
                f"{version.knowledge_base_id}, which is not visible in scope {scope}"
            ),
        )


def _get_discovery(db: Session, tenant_id: str, suggestion_id: str) -> KnowledgeDiscoverySuggestion:
    ensure_tenant(db, tenant_id)
    row = db.get(KnowledgeDiscoverySuggestion, suggestion_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Knowledge discovery not found")
    return row


def _ensure_open_gallery_knowledge_admin(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    current_user: object | None,
) -> None:
    knowledge_base = db.get(KnowledgeBase, knowledge_base_id)
    metadata = (
        knowledge_base.metadata_json
        if knowledge_base and isinstance(knowledge_base.metadata_json, dict)
        else {}
    )
    owner_agent_id = metadata.get("owner_agent_id")
    if isinstance(owner_agent_id, str) and owner_agent_id:
        ensure_agent_scope_manager(db, tenant_id, owner_agent_id, current_user)
        return
    if (
        knowledge_base
        and knowledge_base.tenant_id == tenant_id
        and is_open_gallery_resource(db, tenant_id, "knowledge_base", knowledge_base)
    ):
        ensure_open_gallery_admin(tenant_id, current_user)
