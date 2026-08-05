from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlmodel import Session

from app.api import agents as internal_agents
from app.agents.schema import AgentProfileRead
from app.db import get_session
from app.public_api.auth import PublicPrincipal, require_scopes
from app.public_api.errors import PublicAPIError
from app.public_api.idempotency import replay_idempotent_response, store_idempotent_response


router = APIRouter(prefix="/gallery/agents", tags=["gallery"])


def _payload(value: object) -> dict:
    if hasattr(value, "model_dump"):
        payload = value.model_dump(mode="json")
    elif isinstance(value, dict):
        payload = dict(value)
    else:
        raise TypeError("Unsupported gallery agent payload")
    payload.pop("tenant_id", None)
    metadata = dict(payload.get("metadata") or {})
    payload["added"] = bool(
        metadata.get("used_by_current_user")
        or metadata.get("chat_used_by_current_user")
    )
    return payload


def _gallery_agents(db: Session, principal: PublicPrincipal) -> list[AgentProfileRead]:
    rows = internal_agents.list_agents(
        principal.tenant_id,
        db,
        principal.actor_user,
    )
    return [
        row
        for row in rows
        if not row.is_overall
        and row.status == "active"
        and (row.metadata or {}).get("published_to_gallery") is True
    ]


@router.get("", response_model=dict)
def list_gallery_agents(
    principal: PublicPrincipal = Depends(require_scopes("gallery:read")),
    db: Session = Depends(get_session),
) -> dict:
    return {
        "data": [_payload(row) for row in _gallery_agents(db, principal)],
        "next_cursor": None,
    }


@router.get("/{agent_id}", response_model=dict)
def get_gallery_agent(
    agent_id: str,
    principal: PublicPrincipal = Depends(require_scopes("gallery:read")),
    db: Session = Depends(get_session),
) -> dict:
    row = next((item for item in _gallery_agents(db, principal) if item.id == agent_id), None)
    if not row:
        raise PublicAPIError(404, "GALLERY_AGENT_NOT_FOUND", "Gallery agent not found.")
    return _payload(row)


@router.post("/{agent_id}:add", response_model=dict)
def add_gallery_agent(
    agent_id: str,
    request: Request,
    response: Response,
    principal: PublicPrincipal = Depends(require_scopes("gallery:use")),
    db: Session = Depends(get_session),
) -> dict:
    payload = {"agent_id": agent_id}
    replay = replay_idempotent_response(db, principal, request, payload)
    if replay:
        response.status_code = replay[0]
        return replay[1]
    if not any(item.id == agent_id for item in _gallery_agents(db, principal)):
        raise PublicAPIError(404, "GALLERY_AGENT_NOT_FOUND", "Gallery agent not found.")
    selected = internal_agents.use_chat_agent(
        agent_id,
        principal.tenant_id,
        principal.actor_user,
        db,
    )
    result = _payload(selected)
    result["added"] = True
    store_idempotent_response(
        db,
        principal,
        request,
        payload,
        result,
        status_code=200,
        resource_id=agent_id,
    )
    return result
