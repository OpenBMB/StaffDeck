from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import AgentProfile, ChatSession, MemoryRecord, User
from app.memory.service import (
    ALLOWED_MEMORY_KINDS,
    memory_agent_id,
    memory_agent_scope_predicate,
    memory_matches_agent,
    memory_read,
    memory_rows_for_read,
)
from app.security.auth import get_current_user, require_current_tenant
from app.security.permissions import agent_owned_by_user, is_admin_user
from app.security.tenant import ensure_tenant

router = APIRouter(prefix="/api/enterprise/memories", tags=["enterprise:memories"])


class ManualMemoryCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=1200)
    kind: str = Field(default="fact")
    importance: float = Field(default=0.9, ge=0.0, le=1.0)
    agent_id: str | None = None
    user_id: str | None = None


@router.get("")
def list_memories(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    username: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(require_current_tenant),
    db: Session = Depends(get_session),
) -> list[dict]:
    ensure_tenant(db, tenant_id)
    can_view_all = _can_view_all_memories(db, tenant_id, agent_id, current_user)
    if not can_view_all and user_id and user_id != current_user.id:
        return []
    if not can_view_all and username and username != current_user.username:
        return []
    statement = select(MemoryRecord).where(
        MemoryRecord.tenant_id == tenant_id,
        MemoryRecord.kind != "conversation",
    )
    if can_view_all and user_id:
        statement = statement.where(MemoryRecord.user_id == user_id)
    elif not can_view_all:
        statement = statement.where(MemoryRecord.user_id == current_user.id)
    if can_view_all and username:
        statement = statement.where(MemoryRecord.username == username)
    agent_scope = memory_agent_scope_predicate(tenant_id, agent_id)
    if agent_scope is not None:
        statement = statement.where(agent_scope)
    rows = list(db.exec(statement.order_by(MemoryRecord.updated_at.desc()).limit(limit)).all())
    session_agents = _session_agent_map(db, rows) if agent_id else {}
    if agent_id:
        rows = [row for row in rows if _memory_matches_agent(row, agent_id, session_agents)]
    rows = memory_rows_for_read(rows[:limit])
    if q:
        needle = q.strip().lower()
        rows = [row for row in rows if needle in row.content.lower() or needle in (row.username or "").lower()]
    return [_memory_read_with_inferred_agent(row, session_agents) for row in rows]


@router.post("")
def create_manual_memory(
    request: ManualMemoryCreateRequest,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    if tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")
    ensure_tenant(db, tenant_id)
    content = request.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Memory content is required")
    if request.kind not in ALLOWED_MEMORY_KINDS:
        raise HTTPException(status_code=400, detail="Unsupported memory kind")

    agent_id = request.agent_id.strip() if request.agent_id else None
    if agent_id:
        agent = db.get(AgentProfile, agent_id)
        if not agent or agent.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="Agent not found")

    target_user_id = request.user_id.strip() if request.user_id else current_user.id
    can_write_for_others = _can_view_all_memories(db, tenant_id, agent_id, current_user)
    if target_user_id != current_user.id and not can_write_for_others:
        raise HTTPException(status_code=403, detail="Cannot write memory for another user")
    target_user = db.get(User, target_user_id)
    if not target_user or target_user.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="User not found")

    metadata = {"source": "manual_memory"}
    if agent_id:
        metadata["agent_id"] = agent_id
    record = MemoryRecord(
        tenant_id=tenant_id,
        user_id=target_user.id,
        username=target_user.username,
        session_id=None,
        kind=request.kind,
        content=content,
        importance=request.importance,
        metadata_json=metadata,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return memory_read(record)


@router.delete("/me")
def clear_my_memories(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    if tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")
    ensure_tenant(db, tenant_id)
    rows = list(
        db.exec(
            select(MemoryRecord)
            .where(
                MemoryRecord.tenant_id == tenant_id,
                MemoryRecord.user_id == current_user.id,
                MemoryRecord.kind != "conversation",
            )
            .order_by(MemoryRecord.updated_at.desc())
        ).all()
    )
    session_agents = _session_agent_map(db, rows) if agent_id else {}
    if agent_id:
        rows = [row for row in rows if _memory_matches_agent(row, agent_id, session_agents)]
    deleted = len(rows)
    for row in rows:
        db.delete(row)
    db.commit()
    return {"deleted": deleted}


def _session_agent_map(db: Session, rows: list[MemoryRecord]) -> dict[str, str | None]:
    session_ids = sorted({row.session_id for row in rows if row.session_id and not memory_agent_id(row)})
    if not session_ids:
        return {}
    sessions = db.exec(select(ChatSession).where(ChatSession.id.in_(session_ids))).all()
    return {session.id: session.agent_id for session in sessions}


def _can_view_all_memories(
    db: Session,
    tenant_id: str,
    agent_id: str | None,
    current_user: User,
) -> bool:
    if is_admin_user(current_user):
        return True
    if not agent_id:
        return False
    agent = db.get(AgentProfile, agent_id)
    if not agent or agent.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent_owned_by_user(agent, current_user)


def _memory_matches_agent(row: MemoryRecord, agent_id: str, session_agents: dict[str, str | None]) -> bool:
    if memory_matches_agent(row, agent_id):
        return True
    if memory_agent_id(row):
        return False
    return bool(row.session_id) and session_agents.get(row.session_id) == agent_id


def _memory_read_with_inferred_agent(row: MemoryRecord, session_agents: dict[str, str | None]) -> dict:
    payload = memory_read(row)
    if memory_agent_id(row) or not row.session_id:
        return payload
    inferred_agent_id = session_agents.get(row.session_id)
    if inferred_agent_id:
        metadata = dict(payload.get("metadata") or {})
        metadata["agent_id"] = inferred_agent_id
        metadata["agent_id_source"] = "session"
        payload["metadata"] = metadata
    return payload
