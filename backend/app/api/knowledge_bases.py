from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from sqlmodel import Session, select

from app.db import get_session

from app.db.models import User, ModelConfig

from app.knowledge.http_adapter import km_http_operation

from app.knowledge.schema import KnowledgeBaseCreateRequest, KnowledgeConceptRead, KnowledgeConceptUpdateRequest, KnowledgeBaseRead, KnowledgeBaseRollbackRequest, KnowledgeBaseUpdateRequest

from app.security.auth import get_current_user

from app.security.permissions import require_agent_scope_viewer

from app.llm.model_config_resolver import resolve_model_config_for_runtime

router = APIRouter(prefix='/api/enterprise/knowledge-bases', tags=['enterprise:knowledge-bases'], dependencies=[Depends(get_current_user)])

@router.get('', response_model=list[KnowledgeBaseRead], dependencies=[Depends(require_agent_scope_viewer)])
@km_http_operation
def list_knowledge_bases(tenant_id: str=Query(...), agent_id: str | None=Query(None), db: Session=Depends(get_session)) -> list[KnowledgeBaseRead]:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.post('', response_model=KnowledgeBaseRead)
@km_http_operation
def create_knowledge_base(request: KnowledgeBaseCreateRequest, agent_id: str | None=Query(None), db: Session=Depends(get_session), current_user: User=Depends(get_current_user)) -> KnowledgeBaseRead:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.get('/{knowledge_base_id}', response_model=KnowledgeBaseRead, dependencies=[Depends(require_agent_scope_viewer)])
@km_http_operation
def get_knowledge_base(knowledge_base_id: str, tenant_id: str=Query(...), agent_id: str | None=Query(None), db: Session=Depends(get_session)) -> KnowledgeBaseRead:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.put('/{knowledge_base_id}', response_model=KnowledgeBaseRead)
@km_http_operation
def update_knowledge_base(knowledge_base_id: str, request: KnowledgeBaseUpdateRequest, agent_id: str | None=Query(None), db: Session=Depends(get_session), current_user: User=Depends(get_current_user)) -> KnowledgeBaseRead:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.get('/{knowledge_base_id}/versions', dependencies=[Depends(require_agent_scope_viewer)])
@km_http_operation
def list_knowledge_base_versions(knowledge_base_id: str, tenant_id: str=Query(...), agent_id: str | None=Query(None), db: Session=Depends(get_session)) -> list[dict[str, object]]:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.get('/{knowledge_base_id}/okf/concepts', response_model=list[KnowledgeConceptRead], dependencies=[Depends(require_agent_scope_viewer)])
@km_http_operation
def list_okf_concepts(knowledge_base_id: str, tenant_id: str=Query(...), agent_id: str | None=Query(None), concept_type: str | None=Query(None), db: Session=Depends(get_session)) -> list[KnowledgeConceptRead]:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.get('/{knowledge_base_id}/okf/concepts/{concept_id:path}', response_model=KnowledgeConceptRead, dependencies=[Depends(require_agent_scope_viewer)])
@km_http_operation
def get_okf_concept(knowledge_base_id: str, concept_id: str, tenant_id: str=Query(...), agent_id: str | None=Query(None), db: Session=Depends(get_session)) -> KnowledgeConceptRead:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.put('/{knowledge_base_id}/okf/concepts/{concept_id:path}', response_model=KnowledgeConceptRead)
@km_http_operation
def upsert_okf_concept(knowledge_base_id: str, concept_id: str, request: KnowledgeConceptUpdateRequest, agent_id: str | None=Query(None), db: Session=Depends(get_session), current_user: User=Depends(get_current_user)) -> KnowledgeConceptRead:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.get('/{knowledge_base_id}/okf/export', dependencies=[Depends(require_agent_scope_viewer)])
@km_http_operation
def export_okf(knowledge_base_id: str, tenant_id: str=Query(...), agent_id: str | None=Query(None), db: Session=Depends(get_session)) -> Response:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.post('/{knowledge_base_id}/okf/lint', dependencies=[Depends(require_agent_scope_viewer)])
@km_http_operation
def lint_okf(knowledge_base_id: str, tenant_id: str=Query(...), agent_id: str | None=Query(None), db: Session=Depends(get_session)) -> dict[str, object]:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.delete('/{knowledge_base_id}')
@km_http_operation
def delete_knowledge_base(knowledge_base_id: str, tenant_id: str=Query(...), agent_id: str | None=Query(None), db: Session=Depends(get_session), current_user: User=Depends(get_current_user)) -> dict[str, str]:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.post('/{knowledge_base_id}/sync-from-overall')
@km_http_operation
def sync_knowledge_base_from_overall(knowledge_base_id: str, tenant_id: str=Query(...), agent_id: str=Query(...), db: Session=Depends(get_session), current_user: User=Depends(get_current_user)) -> dict[str, object]:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.post('/{knowledge_base_id}/promote-to-overall')
@km_http_operation
def promote_knowledge_base_to_overall(knowledge_base_id: str, tenant_id: str=Query(...), agent_id: str=Query(...), db: Session=Depends(get_session), current_user: User=Depends(get_current_user)) -> dict[str, object]:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')

@router.post('/{knowledge_base_id}/rollback')
@km_http_operation
def rollback_knowledge_base(knowledge_base_id: str, request: KnowledgeBaseRollbackRequest, db: Session=Depends(get_session), current_user: User=Depends(get_current_user)) -> dict[str, object]:
    raise RuntimeError('HTTP compatibility adapter did not dispatch')
