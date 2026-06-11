from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.async_jobs import enqueue_async_job
from app.db import get_session
from app.db.models import (
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeDiscoverySuggestion,
    KnowledgeDocument,
    KnowledgeIngestJob,
    ModelConfig,
)
from app.knowledge.schema import (
    KnowledgeBucketRead,
    KnowledgeChunkRead,
    KnowledgeDiscoveryRead,
    KnowledgeDocumentRead,
    KnowledgeDocumentUploadRequest,
    KnowledgeIngestJobRead,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
)
from app.knowledge.service import IngestPayload, KnowledgeService, bucket_read, chunk_read
from app.security.tenant import ensure_tenant

router = APIRouter(prefix="/api/enterprise/knowledge", tags=["enterprise:knowledge"])


@router.post("/documents", response_model=KnowledgeIngestJobRead)
def upload_document(
    request: KnowledgeDocumentUploadRequest,
    db: Session = Depends(get_session),
) -> KnowledgeIngestJobRead:
    ensure_tenant(db, request.tenant_id)
    service = KnowledgeService(db)
    job = service.create_ingest_job(
        IngestPayload(
            tenant_id=request.tenant_id,
            filename=request.filename,
            content_base64=request.content_base64,
            title=request.title,
            metadata=request.metadata,
        )
    )
    enqueue_async_job(
        "knowledge_ingest",
        service.run_ingest_job,
        job.id,
        metadata={"tenant_id": request.tenant_id, "filename": request.filename},
    )
    return job_read(job)


@router.get("/jobs/{job_id}", response_model=KnowledgeIngestJobRead)
def get_job(job_id: str, tenant_id: str = Query(...), db: Session = Depends(get_session)) -> KnowledgeIngestJobRead:
    ensure_tenant(db, tenant_id)
    job = db.get(KnowledgeIngestJob, job_id)
    if not job or job.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Knowledge ingest job not found")
    return job_read(job)


@router.get("/documents", response_model=list[KnowledgeDocumentRead])
def list_documents(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
) -> list[KnowledgeDocumentRead]:
    ensure_tenant(db, tenant_id)
    rows = db.exec(
        select(KnowledgeDocument)
        .where(KnowledgeDocument.tenant_id == tenant_id)
        .order_by(KnowledgeDocument.created_at.desc())
    ).all()
    return [document_read(row) for row in rows]


@router.get("/documents/{document_id}", response_model=KnowledgeDocumentRead)
def get_document(
    document_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
) -> KnowledgeDocumentRead:
    row = _get_document(db, tenant_id, document_id)
    return document_read(row)


@router.get("/documents/{document_id}/buckets", response_model=list[KnowledgeBucketRead])
def get_document_buckets(
    document_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
) -> list[KnowledgeBucketRead]:
    _get_document(db, tenant_id, document_id)
    rows = db.exec(
        select(KnowledgeBucket)
        .where(KnowledgeBucket.tenant_id == tenant_id, KnowledgeBucket.document_id == document_id)
        .order_by(KnowledgeBucket.created_at.asc())
    ).all()
    return [bucket_read(row) for row in rows]


@router.get("/buckets/{bucket_id}/chunks", response_model=list[KnowledgeChunkRead])
def get_bucket_chunks(
    bucket_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
) -> list[KnowledgeChunkRead]:
    ensure_tenant(db, tenant_id)
    bucket = db.get(KnowledgeBucket, bucket_id)
    if not bucket or bucket.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Knowledge bucket not found")
    rows = db.exec(
        select(KnowledgeChunk)
        .where(KnowledgeChunk.tenant_id == tenant_id, KnowledgeChunk.bucket_id == bucket_id)
        .order_by(KnowledgeChunk.chunk_index.asc())
    ).all()
    return [chunk_read(row) for row in rows]


@router.post("/search", response_model=KnowledgeSearchResponse)
def search_knowledge(
    request: KnowledgeSearchRequest,
    db: Session = Depends(get_session),
) -> KnowledgeSearchResponse:
    ensure_tenant(db, request.tenant_id)
    model_config = db.exec(
        select(ModelConfig).where(
            ModelConfig.tenant_id == request.tenant_id,
            ModelConfig.is_default == True,  # noqa: E712
            ModelConfig.enabled == True,  # noqa: E712
        )
    ).first()
    return KnowledgeService(db).search(request, model_config)


@router.get("/discoveries", response_model=list[KnowledgeDiscoveryRead])
def list_discoveries(
    tenant_id: str = Query(...),
    status: str | None = Query(None),
    db: Session = Depends(get_session),
) -> list[KnowledgeDiscoveryRead]:
    ensure_tenant(db, tenant_id)
    stmt = select(KnowledgeDiscoverySuggestion).where(KnowledgeDiscoverySuggestion.tenant_id == tenant_id)
    if status:
        stmt = stmt.where(KnowledgeDiscoverySuggestion.status == status)
    rows = db.exec(stmt.order_by(KnowledgeDiscoverySuggestion.created_at.desc())).all()
    return [discovery_read(row) for row in rows]


@router.post("/discoveries/{suggestion_id}/confirm")
def confirm_discovery(
    suggestion_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
) -> dict[str, object]:
    row = _get_discovery(db, tenant_id, suggestion_id)
    result = KnowledgeService(db).confirm_discovery(row)
    return {"status": "confirmed", "result": result}


@router.post("/discoveries/{suggestion_id}/reject")
def reject_discovery(
    suggestion_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
) -> dict[str, str]:
    row = _get_discovery(db, tenant_id, suggestion_id)
    KnowledgeService(db).reject_discovery(row)
    return {"status": "rejected"}


def job_read(row: KnowledgeIngestJob) -> KnowledgeIngestJobRead:
    return KnowledgeIngestJobRead(
        id=row.id,
        tenant_id=row.tenant_id,
        document_id=row.document_id,
        filename=row.filename,
        status=row.status,
        stage=row.stage,
        progress=row.progress,
        error=row.error,
        metadata={key: value for key, value in (row.metadata_json or {}).items() if key != "content_base64"},
        created_at=row.created_at.isoformat(),
        started_at=row.started_at.isoformat() if row.started_at else None,
        finished_at=row.finished_at.isoformat() if row.finished_at else None,
        updated_at=row.updated_at.isoformat(),
    )


def document_read(row: KnowledgeDocument) -> KnowledgeDocumentRead:
    return KnowledgeDocumentRead(
        id=row.id,
        tenant_id=row.tenant_id,
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


def discovery_read(row: KnowledgeDiscoverySuggestion) -> KnowledgeDiscoveryRead:
    return KnowledgeDiscoveryRead(
        id=row.id,
        tenant_id=row.tenant_id,
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


def _get_discovery(db: Session, tenant_id: str, suggestion_id: str) -> KnowledgeDiscoverySuggestion:
    ensure_tenant(db, tenant_id)
    row = db.get(KnowledgeDiscoverySuggestion, suggestion_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Knowledge discovery not found")
    return row
