from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from sqlmodel import Session, select

from app.db import get_session

from app.db.models import User, ModelConfig

from app.knowledge.http_adapter import km_http_operation

from app.knowledge.schema import KnowledgeBucketRead, KnowledgeChunkRead, KnowledgeChunkUpdateRequest, KnowledgeDiscoveryRead, KnowledgeDocumentRead, KnowledgeDocumentUpdateRequest, KnowledgeDocumentUploadRequest, KnowledgeBucketUpdateRequest, KnowledgeOkfImportRequest, KnowledgeIngestJobRead, KnowledgeSearchRequest, KnowledgeSearchResponse

from app.security.auth import get_current_user

from app.security.permissions import require_agent_scope_viewer

from app.llm.model_config_resolver import resolve_model_config_for_runtime

router = APIRouter(prefix='/api/enterprise/knowledge', tags=['enterprise:knowledge'], dependencies=[Depends(get_current_user)])

@router.post('/documents', response_model=KnowledgeIngestJobRead)
@km_http_operation
def upload_document(request: KnowledgeDocumentUploadRequest, agent_id: str | None=Query(None), db: Session=Depends(get_session), current_user: User=Depends(get_current_user)) -> KnowledgeIngestJobRead:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@km_http_operation
def upload_document_for_public_host(request: KnowledgeDocumentUploadRequest, agent_id: str, db: Session, current_user: User, *, host_origin: dict[str, str]) -> KnowledgeIngestJobRead:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.post('/okf/import')
@km_http_operation
def import_okf_bundle(request: KnowledgeOkfImportRequest, db: Session=Depends(get_session), current_user: User=Depends(get_current_user)) -> dict[str, object]:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.get('/jobs', response_model=list[KnowledgeIngestJobRead], dependencies=[Depends(require_agent_scope_viewer)])
@km_http_operation
def list_jobs(tenant_id: str=Query(...), agent_id: str | None=Query(None), status: str | None=Query(None), limit: int=Query(8, ge=1, le=50), db: Session=Depends(get_session)) -> list[KnowledgeIngestJobRead]:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.get('/jobs/{job_id}', response_model=KnowledgeIngestJobRead, dependencies=[Depends(require_agent_scope_viewer)])
@km_http_operation
def get_job(job_id: str, tenant_id: str=Query(...), agent_id: str | None=Query(None), db: Session=Depends(get_session)) -> KnowledgeIngestJobRead:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.post('/jobs/{job_id}/cancel', response_model=KnowledgeIngestJobRead)
@km_http_operation
def cancel_job(job_id: str, tenant_id: str=Query(...), db: Session=Depends(get_session), current_user: User=Depends(get_current_user), agent_id: str | None=Query(None)) -> KnowledgeIngestJobRead:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.get('/documents', response_model=list[KnowledgeDocumentRead], dependencies=[Depends(require_agent_scope_viewer)])
@km_http_operation
def list_documents(tenant_id: str=Query(...), knowledge_base_id: str | None=Query(None), agent_id: str | None=Query(None), include_all_versions: bool=Query(False), db: Session=Depends(get_session)) -> list[KnowledgeDocumentRead]:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.get('/documents/{document_id}', response_model=KnowledgeDocumentRead, dependencies=[Depends(require_agent_scope_viewer)])
@km_http_operation
def get_document(document_id: str, tenant_id: str=Query(...), agent_id: str | None=Query(None), db: Session=Depends(get_session)) -> KnowledgeDocumentRead:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.put('/documents/{document_id}', response_model=KnowledgeDocumentRead)
@km_http_operation
def update_document(document_id: str, request: KnowledgeDocumentUpdateRequest, db: Session=Depends(get_session), current_user: User=Depends(get_current_user), agent_id: str | None=Query(None)) -> KnowledgeDocumentRead:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.get('/documents/{document_id}/buckets', response_model=list[KnowledgeBucketRead], dependencies=[Depends(require_agent_scope_viewer)])
@km_http_operation
def get_document_buckets(document_id: str, tenant_id: str=Query(...), agent_id: str | None=Query(None), db: Session=Depends(get_session)) -> list[KnowledgeBucketRead]:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.put('/buckets/{bucket_id}', response_model=KnowledgeBucketRead)
@km_http_operation
def update_bucket(bucket_id: str, request: KnowledgeBucketUpdateRequest, db: Session=Depends(get_session), current_user: User=Depends(get_current_user), agent_id: str | None=Query(None)) -> KnowledgeBucketRead:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.get('/buckets/{bucket_id}/chunks', response_model=list[KnowledgeChunkRead], dependencies=[Depends(require_agent_scope_viewer)])
@km_http_operation
def get_bucket_chunks(bucket_id: str, tenant_id: str=Query(...), agent_id: str | None=Query(None), db: Session=Depends(get_session)) -> list[KnowledgeChunkRead]:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.put('/chunks/{chunk_id}', response_model=KnowledgeChunkRead)
@km_http_operation
def update_chunk(chunk_id: str, request: KnowledgeChunkUpdateRequest, db: Session=Depends(get_session), current_user: User=Depends(get_current_user), agent_id: str | None=Query(None)) -> KnowledgeChunkRead:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.post('/search', response_model=KnowledgeSearchResponse)
@km_http_operation
def search_knowledge(request: KnowledgeSearchRequest, db: Session=Depends(get_session), current_user: User=Depends(get_current_user)) -> KnowledgeSearchResponse:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

def _get_default_model(db: Session, tenant_id: str) -> ModelConfig | None:
    row = db.exec(select(ModelConfig).where(ModelConfig.tenant_id == tenant_id, ModelConfig.is_default == True, ModelConfig.enabled == True)).first()
    return _runtime_model(db, tenant_id, row) if row else None

def _get_request_model(db: Session, tenant_id: str, model_config_id: str | None=None) -> ModelConfig | None:
    from app.knowledge.public_host_selection import public_host_retrieval_selected
    if public_host_retrieval_selected():
        if model_config_id:
            raise HTTPException(status_code=400, detail='Public host retrieval cannot select an SD model')
        return None
    if not model_config_id:
        return _get_default_model(db, tenant_id)
    model_config = db.get(ModelConfig, model_config_id)
    if not model_config or model_config.tenant_id != tenant_id or (not model_config.enabled):
        raise HTTPException(status_code=404, detail='Model config not found')
    return _runtime_model(db, tenant_id, model_config)

def _runtime_model(db: Session, tenant_id: str, row: ModelConfig):
    return resolve_model_config_for_runtime(db, tenant_id, row.id)

@router.get('/discoveries', response_model=list[KnowledgeDiscoveryRead], dependencies=[Depends(require_agent_scope_viewer)])
@km_http_operation
def list_discoveries(tenant_id: str=Query(...), knowledge_base_id: str | None=Query(None), status: str | None=Query(None), agent_id: str | None=Query(None), db: Session=Depends(get_session)) -> list[KnowledgeDiscoveryRead]:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.post('/discoveries/{suggestion_id}/confirm')
@km_http_operation
def confirm_discovery(suggestion_id: str, tenant_id: str=Query(...), db: Session=Depends(get_session), current_user: User=Depends(get_current_user), agent_id: str | None=Query(None)) -> dict[str, object]:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.post('/discoveries/{suggestion_id}/reject')
@km_http_operation
def reject_discovery(suggestion_id: str, tenant_id: str=Query(...), db: Session=Depends(get_session), current_user: User=Depends(get_current_user), agent_id: str | None=Query(None)) -> dict[str, str]:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')
