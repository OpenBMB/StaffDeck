from __future__ import annotations

from uuid import UUID
from dataclasses import replace
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.api.agents import _metadata_with_creator, agent_read
from app.db import get_session
from app.db.models import AgentProfile, PilotDeckProjectBinding
from app.public_api.auth import PublicPrincipal, enforce_agent_access, require_scopes, _agent_access_sets
from app.public_api.errors import PublicAPIError

router = APIRouter(tags=["agents"])


class EnsureProjectBinding(BaseModel):
    installation_id: UUID
    project_binding_id: UUID
    name: str = Field(min_length=1, max_length=120)
    source_mode: Literal['blank'] = 'blank'


def ensure_project_binding(body: EnsureProjectBinding, principal: PublicPrincipal,
                           db: Session) -> dict:
    if principal.agent_id:
        raise PublicAPIError(403, "TENANT_KEY_REQUIRED", "A tenant key is required.")
    key = (principal.tenant_id, str(body.installation_id), str(body.project_binding_id))
    mapping = db.get(PilotDeckProjectBinding, key)
    if mapping is None:
        name = body.name.strip()
        if not name:
            raise PublicAPIError(422, "AGENT_NAME_REQUIRED", "An agent name is required.")
        agent = AgentProfile(
            tenant_id=principal.tenant_id, name=name, is_overall=False, status="active",
            metadata_json=_metadata_with_creator({"pilotdeck_project": {
                "installation_id": key[1], "project_binding_id": key[2],
            }}, principal.actor_user),
        )
        mapping = PilotDeckProjectBinding(tenant_id=key[0], installation_id=key[1],
            project_binding_id=key[2], agent_id=agent.id)
        db.add(agent)
        db.add(mapping)
        try:
            db.commit()
        except IntegrityError:
            # The losing transaction rolls back both rows before reading the winner.
            db.rollback()
            mapping = db.get(PilotDeckProjectBinding, key)
            if mapping is None:
                raise PublicAPIError(409, "PROJECT_BINDING_CONFLICT", "Project binding conflicts.")
        # Visibility was sampled before this request created its owned agent.
        visible, manageable = _agent_access_sets(db, principal.tenant_id, principal.actor_user)
        principal = replace(principal, allowed_agent_ids=visible, manageable_agent_ids=manageable)
    enforce_agent_access(principal, mapping.agent_id, write=True)
    agent = db.get(AgentProfile, mapping.agent_id)
    if agent is None or agent.tenant_id != principal.tenant_id or agent.is_overall:
        raise PublicAPIError(409, "PROJECT_BINDING_TARGET_INVALID", "The bound agent is unavailable.")
    if agent.status != "active":
        raise PublicAPIError(409, "PROJECT_BINDING_TARGET_ARCHIVED", "The bound agent is archived.")
    # Also apply the native ownership check on replays and after credential rotation.
    from app.api.agents import _ensure_can_manage_agent
    _ensure_can_manage_agent(agent, principal.actor_user)
    return {"installation_id": key[1], "project_binding_id": key[2],
            "agent": agent_read(agent, []).model_dump(mode="json", exclude={"tenant_id"})}


@router.post("/agents:ensure-project-binding", response_model=dict)
def ensure_project_binding_route(body: EnsureProjectBinding,
    principal: PublicPrincipal = Depends(require_scopes("agents:write")),
    db: Session = Depends(get_session)) -> dict:
    return ensure_project_binding(body, principal, db)
