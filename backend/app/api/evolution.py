from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session

from app.db import get_session
from app.db.models import EvolutionProposal, User
from app.evolution import EvolutionService
from app.evolution.schema import (
    EvolutionActionRequest,
    EvolutionAnalyzeRequest,
    EvolutionProposalRead,
    EvolutionRejectRequest,
)
from app.security.auth import get_current_user
from app.security.permissions import ensure_agent_scope_manager
from app.security.tenant import ensure_tenant


router = APIRouter(prefix="/api/enterprise", tags=["enterprise:evolution"])


@router.get(
    "/agents/{agent_id}/evolution/proposals",
    response_model=list[EvolutionProposalRead],
)
def list_evolution_proposals(
    agent_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[EvolutionProposalRead]:
    ensure_tenant(db, tenant_id)
    ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    return [_proposal_read(row) for row in EvolutionService(db).list(tenant_id, agent_id)]


@router.post(
    "/agents/{agent_id}/evolution:analyze",
    response_model=EvolutionProposalRead,
)
def analyze_evolution_candidate(
    agent_id: str,
    request: EvolutionAnalyzeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> EvolutionProposalRead:
    ensure_tenant(db, request.tenant_id)
    ensure_agent_scope_manager(db, request.tenant_id, agent_id, current_user)
    return _proposal_read(EvolutionService(db).analyze(agent_id, request, current_user))


@router.get(
    "/evolution/proposals/{proposal_id}",
    response_model=EvolutionProposalRead,
)
def get_evolution_proposal(
    proposal_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> EvolutionProposalRead:
    row = _proposal(db, tenant_id, proposal_id)
    ensure_agent_scope_manager(db, tenant_id, row.agent_id, current_user)
    return _proposal_read(row)


@router.post(
    "/evolution/proposals/{proposal_id}:evaluate",
    response_model=EvolutionProposalRead,
)
def evaluate_evolution_proposal(
    proposal_id: str,
    request: EvolutionActionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> EvolutionProposalRead:
    row = _proposal(db, request.tenant_id, proposal_id)
    ensure_agent_scope_manager(db, request.tenant_id, row.agent_id, current_user)
    return _proposal_read(EvolutionService(db).evaluate(row))


@router.post(
    "/evolution/proposals/{proposal_id}:approve",
    response_model=EvolutionProposalRead,
)
def approve_evolution_proposal(
    proposal_id: str,
    request: EvolutionActionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> EvolutionProposalRead:
    row = _proposal(db, request.tenant_id, proposal_id)
    ensure_agent_scope_manager(db, request.tenant_id, row.agent_id, current_user)
    return _proposal_read(EvolutionService(db).approve(row, current_user))


@router.post(
    "/evolution/proposals/{proposal_id}:reject",
    response_model=EvolutionProposalRead,
)
def reject_evolution_proposal(
    proposal_id: str,
    request: EvolutionRejectRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> EvolutionProposalRead:
    row = _proposal(db, request.tenant_id, proposal_id)
    ensure_agent_scope_manager(db, request.tenant_id, row.agent_id, current_user)
    return _proposal_read(EvolutionService(db).reject(row, current_user, request.reason))


@router.post(
    "/evolution/proposals/{proposal_id}:rollback",
    response_model=EvolutionProposalRead,
)
def rollback_evolution_proposal(
    proposal_id: str,
    request: EvolutionActionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> EvolutionProposalRead:
    row = _proposal(db, request.tenant_id, proposal_id)
    ensure_agent_scope_manager(db, request.tenant_id, row.agent_id, current_user)
    return _proposal_read(EvolutionService(db).rollback(row, current_user))


def _proposal(db: Session, tenant_id: str, proposal_id: str) -> EvolutionProposal:
    ensure_tenant(db, tenant_id)
    row = db.get(EvolutionProposal, proposal_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "EVOLUTION_PROPOSAL_NOT_FOUND",
                "message": "未找到自进化候选",
            },
        )
    return row


def _proposal_read(row: EvolutionProposal) -> EvolutionProposalRead:
    return EvolutionProposalRead(
        id=row.id,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        resource_type=row.resource_type,  # type: ignore[arg-type]
        resource_id=row.resource_id,
        resource_key=row.resource_key,
        resource_name=row.resource_name,
        base_version=row.base_version,
        status=row.status,
        trigger_type=row.trigger_type,
        risk_level=row.risk_level,
        hypothesis=row.hypothesis,
        rationale=row.rationale,
        expected_outcome=row.expected_outcome,
        source_feedback_ids=list(row.source_feedback_ids_json or []),
        evidence=list(row.evidence_json or []),
        candidate=dict(row.candidate_json or {}),
        diff=list(row.diff_json or []),
        evaluation=dict(row.evaluation_json or {}),
        error=row.error,
        created_by_user_id=row.created_by_user_id,
        reviewed_by_user_id=row.reviewed_by_user_id,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
        reviewed_at=row.reviewed_at.isoformat() if row.reviewed_at else None,
        published_at=row.published_at.isoformat() if row.published_at else None,
        rolled_back_at=row.rolled_back_at.isoformat() if row.rolled_back_at else None,
    )
