from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlmodel import Session, select

from app.agents.schema import (
    AgentModelBindingInput,
    AgentModelsUpdateRequest,
    AgentProfileCreateRequest,
    AgentProfileUpdateRequest,
    AgentResourceBindingInput,
    AgentResourcesUpdateRequest,
)
from app.api import agents as internal_agents
from app.agents.branching import visible_skill
from app.core.capability_manifest import CapabilityManifestBuilder
from app.db import get_session
from app.db.models import AgentModelBinding, AgentProfile, ModelConfig
from app.public_api.auth import (
    PublicPrincipal,
    enforce_agent_access,
    require_scopes,
)
from app.public_api.errors import PublicAPIError
from app.public_api.idempotency import replay_idempotent_response, store_idempotent_response
from app.public_api.schemas import (
    AgentCreate,
    AgentUpdate,
    ModelBindingsUpdate,
    ResourceBindingsUpdate,
)
from app.public_api.utils import etag_for


router = APIRouter(prefix="/agents", tags=["agents"])


def _agent_payload(value: object) -> dict:
    if hasattr(value, "model_dump"):
        payload = value.model_dump(mode="json")
    elif isinstance(value, dict):
        payload = dict(value)
    else:
        raise TypeError("Unsupported agent payload")
    payload.pop("tenant_id", None)
    return payload


@router.get("", response_model=dict)
def list_agents(
    limit: int = Query(50, ge=1, le=100),
    principal: PublicPrincipal = Depends(require_scopes("agents:read")),
    db: Session = Depends(get_session),
) -> dict:
    rows = internal_agents.list_agents(principal.tenant_id, db, principal.actor_user)
    if principal.agent_id:
        rows = [row for row in rows if row.id == principal.agent_id]
    data = [_agent_payload(row) for row in rows[:limit]]
    return {"data": data, "next_cursor": None}


@router.post("", response_model=dict, status_code=201)
def create_agent(
    body: AgentCreate,
    request: Request,
    response: Response,
    principal: PublicPrincipal = Depends(require_scopes("agents:write")),
    db: Session = Depends(get_session),
) -> dict:
    if principal.agent_id:
        raise PublicAPIError(403, "TENANT_KEY_REQUIRED", "A tenant key is required.")
    replay = replay_idempotent_response(db, principal, request, body.model_dump(mode="json"))
    if replay:
        response.status_code = replay[0]
        return replay[1]
    created = internal_agents.create_agent(
        AgentProfileCreateRequest(
            tenant_id=principal.tenant_id,
            name=body.name,
            description=body.description,
            persona_prompt=body.persona_prompt,
            source_mode=body.source_mode,
            copy_from_agent_id=body.copy_from_agent_id,
            metadata=body.metadata,
        ),
        db,
        principal.actor_user,
    )
    payload = _agent_payload(created)
    response.headers["ETag"] = etag_for(payload)
    store_idempotent_response(
        db,
        principal,
        request,
        body.model_dump(mode="json"),
        payload,
        status_code=201,
        resource_id=created.id,
    )
    return payload


@router.get("/{agent_id}", response_model=dict)
def get_agent(
    agent_id: str,
    response: Response,
    principal: PublicPrincipal = Depends(require_scopes("agents:read")),
    db: Session = Depends(get_session),
) -> dict:
    enforce_agent_access(principal, agent_id)
    payload = _agent_payload(
        internal_agents.get_agent(agent_id, principal.tenant_id, db, principal.actor_user)
    )
    response.headers["ETag"] = etag_for(payload)
    return payload


@router.patch("/{agent_id}", response_model=dict)
def update_agent(
    agent_id: str,
    body: AgentUpdate,
    response: Response,
    if_match: str | None = Header(default=None, alias="If-Match"),
    principal: PublicPrincipal = Depends(require_scopes("agents:write")),
    db: Session = Depends(get_session),
) -> dict:
    enforce_agent_access(principal, agent_id, write=True)
    current = _agent_payload(
        internal_agents.get_agent(agent_id, principal.tenant_id, db, principal.actor_user)
    )
    current_etag = etag_for(current)
    if not if_match:
        raise PublicAPIError(428, "IF_MATCH_REQUIRED", "If-Match is required for this update.")
    if if_match != current_etag:
        raise PublicAPIError(412, "ETAG_MISMATCH", "The agent changed since it was read.")
    updated = internal_agents.update_agent(
        agent_id,
        AgentProfileUpdateRequest(
            tenant_id=principal.tenant_id,
            **body.model_dump(exclude_unset=True),
        ),
        db,
        principal.actor_user,
    )
    payload = _agent_payload(updated)
    response.headers["ETag"] = etag_for(payload)
    return payload


@router.post("/{agent_id}:archive", response_model=dict)
def archive_agent(
    agent_id: str,
    principal: PublicPrincipal = Depends(require_scopes("agents:write")),
    db: Session = Depends(get_session),
) -> dict:
    enforce_agent_access(principal, agent_id, write=True)
    updated = internal_agents.update_agent(
        agent_id,
        AgentProfileUpdateRequest(tenant_id=principal.tenant_id, status="archived"),
        db,
        principal.actor_user,
    )
    return _agent_payload(updated)


@router.get("/{agent_id}/resources", response_model=dict)
def get_agent_resources(
    agent_id: str,
    principal: PublicPrincipal = Depends(require_scopes("agents:read")),
    db: Session = Depends(get_session),
) -> dict:
    enforce_agent_access(principal, agent_id)
    rows = internal_agents.get_agent_resources(
        agent_id, principal.tenant_id, db, principal.actor_user
    )
    return {"data": [row.model_dump(mode="json", exclude={"tenant_id"}) for row in rows]}


@router.put("/{agent_id}/resources", response_model=dict)
def update_agent_resources(
    agent_id: str,
    body: ResourceBindingsUpdate,
    principal: PublicPrincipal = Depends(require_scopes("agents:write")),
    db: Session = Depends(get_session),
) -> dict:
    enforce_agent_access(principal, agent_id, write=True)
    rows = internal_agents.update_agent_resources(
        agent_id,
        AgentResourcesUpdateRequest(
            tenant_id=principal.tenant_id,
            resources=[AgentResourceBindingInput(**item.model_dump()) for item in body.resources],
        ),
        db,
        principal.actor_user,
    )
    return {"data": [row.model_dump(mode="json", exclude={"tenant_id"}) for row in rows]}


@router.get("/{agent_id}/models", response_model=dict)
def get_agent_models(
    agent_id: str,
    principal: PublicPrincipal = Depends(require_scopes("agents:read")),
    db: Session = Depends(get_session),
) -> dict:
    enforce_agent_access(principal, agent_id)
    agent = db.get(AgentProfile, agent_id)
    if not agent or agent.tenant_id != principal.tenant_id:
        raise PublicAPIError(404, "AGENT_NOT_FOUND", "Agent not found.")
    rows = db.exec(
        select(AgentModelBinding)
        .where(
            AgentModelBinding.tenant_id == principal.tenant_id,
            AgentModelBinding.agent_id == agent_id,
        )
        .order_by(AgentModelBinding.role)
    ).all()
    return {
        "data": [
            {"role": row.role, "model_config_id": row.model_config_id}
            for row in rows
        ]
    }


@router.put("/{agent_id}/models", response_model=dict)
def update_agent_models(
    agent_id: str,
    body: ModelBindingsUpdate,
    principal: PublicPrincipal = Depends(require_scopes("agents:write")),
    db: Session = Depends(get_session),
) -> dict:
    enforce_agent_access(principal, agent_id, write=True)
    for binding in body.bindings:
        model = db.get(ModelConfig, binding.model_config_id)
        if (
            not model
            or model.tenant_id != principal.tenant_id
            or not model.enabled
            or model.trust_status != "verified"
        ):
            raise PublicAPIError(
                400,
                "MODEL_NOT_VERIFIED",
                f"Model {binding.model_config_id} is unavailable or unverified.",
            )
    internal_agents.update_agent_models(
        agent_id,
        AgentModelsUpdateRequest(
            tenant_id=principal.tenant_id,
            bindings=[AgentModelBindingInput(**item.model_dump()) for item in body.bindings],
        ),
        db,
        principal.actor_user,
    )
    return get_agent_models(agent_id, principal, db)


@router.get("/{agent_id}/capabilities", response_model=dict)
def get_agent_capabilities(
    agent_id: str,
    sop_id: str | None = Query(default=None),
    step_id: str | None = Query(default=None),
    principal: PublicPrincipal = Depends(require_scopes("capabilities:read")),
    db: Session = Depends(get_session),
) -> dict:
    enforce_agent_access(principal, agent_id)
    agent = db.get(AgentProfile, agent_id)
    if not agent or agent.tenant_id != principal.tenant_id or agent.status != "active":
        raise PublicAPIError(404, "AGENT_NOT_FOUND", "Agent not found.")
    skill = visible_skill(db, principal.tenant_id, sop_id, agent_id) if sop_id else None
    if sop_id and skill is None:
        raise PublicAPIError(404, "SOP_NOT_FOUND", "SOP not found in this agent scope.")
    manifest = CapabilityManifestBuilder(db).build(
        principal.tenant_id, agent_id, skill, step_id
    )
    return manifest.model_dump(mode="json")
