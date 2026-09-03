"""Read-only projection of ORM rows into security ResourceRefs and Staff bindings.

This is the single place that knows how legacy tables encode ownership,
visibility and binding. Everything downstream (PEP, compiler, hosts) consumes
the attribute-carrying ``ResourceRef`` and never touches SQLAlchemy.

The rules reproduced here are those of ``app.agents.branching``:

- open gallery  == bound to the tenant's overall agent with a non-private binding
- private       == binding or resource metadata marks agent-private scope
- visible to an agent == active binding and (private or open gallery)
"""

from __future__ import annotations

from typing import Any, Iterable

from sqlmodel import Session, select

from app.agents.branching import (
    _binding_is_private,
    _metadata_is_private,
    _resource_metadata,
    get_agent,
    get_overall_agent,
    is_open_gallery_resource,
)
from app.db.models import (
    AgentModelBinding,
    AgentProfile,
    AgentResourceBinding,
    ChannelBinding,
    ChatSession,
    GeneralSkill,
    HumanHandoffRequest,
    KnowledgeBase,
    MCPServer,
    ModelConfig,
    Skill,
    Team,
    Tool,
)
from staffdeck_harness.contracts.security import ResourceRef

_RESOURCE_MODEL: dict[str, type] = {
    "skill": Skill,
    "sop": Skill,
    "general_skill": GeneralSkill,
    "tool": Tool,
    "mcp_server": MCPServer,
    "knowledge_base": KnowledgeBase,
}

# Legacy binding rows use these resource_type strings.
_BINDING_TYPE: dict[str, str] = {
    "skill": "skill",
    "sop": "skill",
    "general_skill": "general_skill",
    "tool": "tool",
    "mcp_server": "mcp_server",
    "knowledge_base": "knowledge_base",
}


def agent_ref(row: AgentProfile) -> ResourceRef:
    meta = dict(row.metadata_json or {})
    return ResourceRef(
        type="agent",
        id=row.id,
        tenant_id=row.tenant_id,
        attributes={
            "owner_user_id": meta.get("owner_user_id"),
            "is_overall": bool(row.is_overall),
            "published_to_gallery": meta.get("published_to_gallery") is True,
            "shared_user_ids": tuple(meta.get("shared_user_ids") or ()),
            "status": row.status,
        },
    )


def _owner_of(resource: Any) -> str | None:
    meta = _resource_metadata(resource)
    return meta.get("owner_user_id") or meta.get("created_by_user_id")


def bound_resource_ref(
    db: Session,
    tenant_id: str,
    resource_type: str,
    resource: Any,
    *,
    agent: AgentProfile | None,
    binding: AgentResourceBinding | None = None,
) -> ResourceRef:
    """Project one resource as seen from one agent (binding may be absent)."""

    meta = _resource_metadata(resource)
    binding_status = binding.status if binding is not None else None
    private = bool(
        (binding is not None and _binding_is_private(binding)) or _metadata_is_private(meta)
    )
    open_gallery = is_open_gallery_resource(db, tenant_id, _BINDING_TYPE.get(resource_type, resource_type), resource)
    return ResourceRef(
        type=resource_type,  # type: ignore[arg-type]
        id=str(resource.id),
        tenant_id=str(getattr(resource, "tenant_id", tenant_id)),
        attributes={
            "owner_user_id": _owner_of(resource),
            "status": getattr(resource, "status", "active"),
            "enabled": getattr(resource, "enabled", True),
            "binding_status": binding_status,
            "private_to_agent": private,
            "open_gallery": open_gallery,
            "agent_is_overall": bool(agent.is_overall) if agent is not None else False,
            "shared_user_ids": tuple(meta.get("shared_user_ids") or ()),
            "capability_scope": getattr(resource, "capability_scope", "general"),
        },
    )


def binding_for(db: Session, tenant_id: str, agent_id: str | None, resource_type: str, resource_id: str) -> AgentResourceBinding | None:
    if not agent_id:
        return None
    return db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == agent_id,
            AgentResourceBinding.resource_type == _BINDING_TYPE.get(resource_type, resource_type),
            AgentResourceBinding.resource_id == resource_id,
            AgentResourceBinding.status != "deleted",
        )
    ).first()


def live_resource_ref(db: Session, tenant_id: str, resource_type: str, resource: Any, *, agent: AgentProfile | None) -> ResourceRef:
    """Project a live row as seen from the acting agent, resolving its binding row."""

    binding = binding_for(db, tenant_id, agent.id if agent is not None else None, resource_type, str(resource.id))
    return bound_resource_ref(db, tenant_id, resource_type, resource, agent=agent, binding=binding)


def agent_bindings(db: Session, tenant_id: str, agent_id: str, resource_type: str | None = None) -> list[AgentResourceBinding]:
    stmt = select(AgentResourceBinding).where(
        AgentResourceBinding.tenant_id == tenant_id,
        AgentResourceBinding.agent_id == agent_id,
        AgentResourceBinding.status != "deleted",
    )
    if resource_type:
        stmt = stmt.where(AgentResourceBinding.resource_type == _BINDING_TYPE.get(resource_type, resource_type))
    return list(db.exec(stmt.order_by(AgentResourceBinding.updated_at.desc())).all())


def bound_resources(db: Session, tenant_id: str, agent_id: str | None, resource_type: str) -> list[tuple[Any, AgentResourceBinding | None, ResourceRef]]:
    """All resources of one type visible to an agent (overall agent sees the open gallery)."""

    model = _RESOURCE_MODEL[resource_type]
    agent = get_agent(db, tenant_id, agent_id) if agent_id else None
    out: list[tuple[Any, AgentResourceBinding | None, ResourceRef]] = []
    if agent is None or agent.is_overall:
        rows = db.exec(select(model).where(model.tenant_id == tenant_id)).all()  # type: ignore[attr-defined]
        for row in rows:
            ref = bound_resource_ref(db, tenant_id, resource_type, row, agent=agent)
            if ref.attributes["open_gallery"]:
                out.append((row, None, ref))
        return out
    for binding in agent_bindings(db, tenant_id, agent.id, resource_type):
        row = db.get(model, binding.resource_id)
        if row is None or getattr(row, "tenant_id", None) != tenant_id:
            continue
        ref = bound_resource_ref(db, tenant_id, resource_type, row, agent=agent, binding=binding)
        if ref.attributes["binding_status"] == "active" and (
            ref.attributes["private_to_agent"] or ref.attributes["open_gallery"]
        ):
            out.append((row, binding, ref))
    return out


def model_config_ref(row: ModelConfig) -> ResourceRef:
    return ResourceRef(type="model_config", id=row.id, tenant_id=row.tenant_id, attributes={"enabled": bool(row.enabled)})


def agent_model_route(db: Session, tenant_id: str, agent_id: str | None) -> dict[str, str]:
    """role -> model_config_id for one agent (``default`` always present when configured)."""

    route: dict[str, str] = {}
    if agent_id:
        for row in db.exec(
            select(AgentModelBinding).where(AgentModelBinding.tenant_id == tenant_id, AgentModelBinding.agent_id == agent_id)
        ).all():
            route[row.role] = row.model_config_id
    if "default" not in route:
        default = db.exec(
            select(ModelConfig).where(ModelConfig.tenant_id == tenant_id, ModelConfig.is_default == True, ModelConfig.enabled == True)  # noqa: E712
        ).first()
        if default is not None:
            route["default"] = default.id
    return route


def channel_ref(row: ChannelBinding) -> ResourceRef:
    return ResourceRef(
        type="channel",
        id=row.id,
        tenant_id=row.tenant_id,
        attributes={"binding_status": row.status, "channel": row.channel, "agent_id": row.agent_id, "team_id": row.team_id},
    )


def agent_channels(db: Session, tenant_id: str, agent_id: str) -> list[ChannelBinding]:
    return list(
        db.exec(
            select(ChannelBinding).where(
                ChannelBinding.tenant_id == tenant_id, ChannelBinding.agent_id == agent_id, ChannelBinding.status != "disabled"
            )
        ).all()
    )


def session_ref(row: ChatSession) -> ResourceRef:
    return ResourceRef(type="session", id=row.id, tenant_id=row.tenant_id, attributes={"user_id": row.user_id, "agent_id": row.agent_id, "status": row.status})


def handoff_ref(row: HumanHandoffRequest, *, agent_owner_user_id: str | None = None) -> ResourceRef:
    return ResourceRef(
        type="handoff",
        id=row.id,
        tenant_id=row.tenant_id,
        attributes={
            "assignee_user_id": getattr(row, "assignee_user_id", None),
            "requester_user_id": getattr(row, "requester_user_id", None) or getattr(row, "user_id", None),
            "agent_owner_user_id": agent_owner_user_id,
            "status": row.status,
        },
    )


def team_ref(row: Team) -> ResourceRef:
    return ResourceRef(type="team", id=row.id, tenant_id=row.tenant_id, attributes={"owner_user_id": row.owner_user_id, "status": row.status})


def overall_agent_id(db: Session, tenant_id: str) -> str | None:
    row = get_overall_agent(db, tenant_id)
    return row.id if row else None


def refs_for(rows: Iterable[Any], make: Any) -> list[ResourceRef]:
    return [make(r) for r in rows]
