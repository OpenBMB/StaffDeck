"""StaffComposition: the read-only projection of one digital employee.

Built from ``AgentProfile`` + binding tables. Nothing here writes. A
composition is what the compiler turns into an immutable snapshot for a turn.

Layout follows the architecture's L1/L2:

    StaffComposition
    ├── persona           (identity prompt / persona_prompt / tenant PersonaConfig)
    ├── model_route       (role -> model_config_id; default always present)
    ├── session_policy    (max actions, context budget, sandbox policy)
    ├── capabilities[]    (direct bindings: knowledge/skill/tool/mcp)
    ├── sops[]            (bound SOP definitions + per-staff slot bindings)
    ├── channels[]        (ChannelBinding rows)
    ├── team              (team membership for TL sessions)
    └── interactions[]    (fixed: sop_adapter, memory, supervisor, handoff)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from sqlmodel import Session, select

from app.agents.branching import get_agent, visible_published_skills
from app.core.agent_loop import _agent_identity_prompt
from app.db.models import AgentProfile, AgentResourceBinding, PersonaConfig, Skill, Team, TeamMember, UIConfig
from staffdeck_dsh.composition import projection
from staffdeck_dsh.composition.slots import SlotDeclaration, sop_slots
from staffdeck_dsh.contracts.security import ResourceRef

MAX_ACTIONS_LIMIT = 100


@dataclass(frozen=True)
class SessionPolicy:
    max_actions: int = 32
    context_token_budget: int = 32_000
    compaction_trigger_ratio: float = 0.70
    recent_round_limit: int = 6
    step_timeout_seconds: int | None = None
    sandbox_enabled: bool = False
    sandbox_network_mode: str = "all"
    sandbox_allowed_domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityBindingView:
    resource_type: str
    resource_id: str
    binding_id: str | None
    ref: ResourceRef
    name: str
    capability_scope: str = "general"
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SopView:
    skill_id: str
    row_id: str
    version: str
    name: str
    content: Mapping[str, Any]
    binding_id: str | None
    slot_bindings: Mapping[str, str]        # logical slot name -> resource id
    declared_slots: tuple[SlotDeclaration, ...]
    ref: ResourceRef
    capability_scope: str = "general"


@dataclass(frozen=True)
class ChannelView:
    binding_id: str
    channel: str
    status: str
    team_id: str | None
    ref: ResourceRef


@dataclass(frozen=True)
class TeamView:
    team_id: str
    role: str
    ref: ResourceRef


@dataclass(frozen=True)
class StaffComposition:
    tenant_id: str
    staff_id: str                   # AgentProfile.id; None-agent turns use the overall agent
    name: str
    is_overall: bool
    status: str
    persona: str | None
    model_route: Mapping[str, str]
    session_policy: SessionPolicy
    capabilities: tuple[CapabilityBindingView, ...]
    sops: tuple[SopView, ...]
    channels: tuple[ChannelView, ...]
    team: TeamView | None
    interactions: tuple[str, ...]
    ref: ResourceRef
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def capability_ids(self, resource_type: str) -> set[str]:
        return {c.resource_id for c in self.capabilities if c.resource_type == resource_type}

    def visible_resource_ids(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for c in self.capabilities:
            out.setdefault(c.resource_type, set()).add(c.resource_id)
        out.setdefault("skill", set()).update(s.skill_id for s in self.sops)
        return out


def _persona(db: Session, tenant_id: str, agent: AgentProfile | None) -> str | None:
    if agent is not None and not agent.is_overall:
        return _agent_identity_prompt(agent)
    if agent is not None and agent.is_overall and agent.persona_prompt:
        return agent.persona_prompt
    row = db.get(PersonaConfig, tenant_id)
    return row.system_prompt if row else None


def _session_policy(db: Session, tenant_id: str, agent: AgentProfile | None) -> SessionPolicy:
    ui = db.get(UIConfig, tenant_id)
    max_actions = int(agent.harness_max_actions) if agent is not None else int(getattr(ui, "agent_loop_max_actions", 32) if ui else 32)
    return SessionPolicy(
        max_actions=max(1, min(max_actions, MAX_ACTIONS_LIMIT)),
        context_token_budget=int(getattr(ui, "context_token_budget", 32_000) if ui else 32_000),
        compaction_trigger_ratio=float(getattr(ui, "context_compaction_trigger_ratio", 0.70) if ui else 0.70),
        recent_round_limit=int(getattr(ui, "context_recent_round_limit", 6) if ui else 6),
        sandbox_enabled=bool(getattr(ui, "sandbox_enabled", False) if ui else False),
        sandbox_network_mode=str(getattr(ui, "sandbox_network_mode", "all") if ui else "all"),
        sandbox_allowed_domains=tuple(str(d) for d in (getattr(ui, "sandbox_allowed_domains", []) if ui else []) if str(d).strip()),
    )


def _sop_binding_row(db: Session, tenant_id: str, agent_id: str, skill_id: str) -> AgentResourceBinding | None:
    return db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == agent_id,
            AgentResourceBinding.resource_type == "skill",
            AgentResourceBinding.resource_id == skill_id,
            AgentResourceBinding.status != "deleted",
        )
    ).first()


def _expand_sop_content(published: list[Skill]) -> list[tuple[Skill, dict[str, Any]]]:
    """Dict-level SOP expansion, immune to SQLAlchemy's detached-row GC bug.

    ``app.skills.nesting.expand_sop_for_execution`` deep-copies ORM ``Skill``
    rows into a detached instance; assigning ``content_json`` on the copy then
    raises ``ObjectDereferencedError`` once the parent is collected. The pure
    helpers ``validate_sop_nesting`` (dict rows) and ``_expand_content``
    (attribute rows) do the same job without touching the ORM, so the projection
    works on content dicts and returns (original row, expanded content) pairs.
    """

    from copy import deepcopy

    from app.skills.nesting import _expand_content, validate_sop_nesting

    class _Row:
        __slots__ = ("skill_id", "content_json")

        def __init__(self, skill_id: str, content: dict[str, Any]):
            self.skill_id = skill_id
            self.content_json = content

    contents: dict[str, dict[str, Any]] = {}
    for sk in published:
        contents[sk.skill_id] = deepcopy(sk.content_json or {})
    dict_rows = [{"skill_id": sid, "status": "published", "content": c} for sid, c in contents.items()]
    out: list[tuple[Skill, dict[str, Any]]] = []
    for sk in published:
        sid = sk.skill_id
        validate_sop_nesting(sid, contents[sid], dict_rows)
        by_id = {k: _Row(k, v) for k, v in contents.items()}
        expanded = _expand_content(sid, deepcopy(contents[sid]), by_id, path=[sid])
        out.append((sk, expanded))
    return out


def _sops(db: Session, tenant_id: str, agent: AgentProfile | None) -> tuple[SopView, ...]:
    agent_id = agent.id if agent is not None else None
    published = list(visible_published_skills(db, tenant_id, agent_id))
    expanded_by_id = {sk.skill_id: content for sk, content in _expand_sop_content(published)}
    out: list[SopView] = []
    for skill in published:
        content = dict(expanded_by_id.get(skill.skill_id) or skill.content_json or {})
        binding = _sop_binding_row(db, tenant_id, agent_id, skill.skill_id) if agent_id else None
        slot_bindings = dict(((binding.metadata_json or {}).get("slot_bindings") or {}) if binding else {})
        ref = projection.bound_resource_ref(db, tenant_id, "sop", skill, agent=agent, binding=binding)
        out.append(
            SopView(
                skill_id=skill.skill_id,
                row_id=skill.id,
                version=skill.version,
                name=skill.name,
                content=content,
                binding_id=binding.id if binding else None,
                slot_bindings=slot_bindings,
                declared_slots=tuple(sop_slots(content)),
                ref=ref,
                capability_scope=str(content.get("capability_scope") or "general"),
            )
        )
    return tuple(out)


def _capabilities(db: Session, tenant_id: str, agent: AgentProfile | None) -> tuple[CapabilityBindingView, ...]:
    agent_id = agent.id if agent is not None else None
    out: list[CapabilityBindingView] = []
    for rtype in ("knowledge_base", "general_skill", "tool", "mcp_server"):
        for row, binding, ref in projection.bound_resources(db, tenant_id, agent_id, rtype):
            out.append(
                CapabilityBindingView(
                    resource_type=rtype,
                    resource_id=str(row.id),
                    binding_id=binding.id if binding else None,
                    ref=ref,
                    name=str(getattr(row, "name", None) or getattr(row, "slug", None) or row.id),
                    capability_scope=str(getattr(row, "capability_scope", "general") or "general"),
                    metadata=dict(binding.metadata_json or {}) if binding else {},
                )
            )
    return tuple(out)


def _team(db: Session, tenant_id: str, agent: AgentProfile | None) -> TeamView | None:
    if agent is None:
        return None
    member = db.exec(select(TeamMember).where(TeamMember.agent_id == agent.id)).first()
    if member is None:
        return None
    team = db.get(Team, member.team_id)
    if team is None or team.tenant_id != tenant_id:
        return None
    return TeamView(team_id=team.id, role=member.role, ref=projection.team_ref(team))


DEFAULT_INTERACTIONS: tuple[str, ...] = ("sop_adapter", "memory", "output_supervisor", "handoff")


def project_staff(db: Session, tenant_id: str, agent_id: str | None) -> StaffComposition:
    agent = get_agent(db, tenant_id, agent_id) if agent_id else projection.get_overall_agent(db, tenant_id)
    if agent is None and agent_id:
        raise LookupError(f"agent {agent_id!r} not found in tenant {tenant_id!r}")
    staff_id = agent.id if agent is not None else f"{tenant_id}:overall"
    ref = projection.agent_ref(agent) if agent is not None else ResourceRef(type="agent", id=staff_id, tenant_id=tenant_id, attributes={"is_overall": True})
    channels = tuple(
        ChannelView(binding_id=c.id, channel=c.channel, status=c.status, team_id=c.team_id, ref=projection.channel_ref(c))
        for c in (projection.agent_channels(db, tenant_id, agent.id) if agent is not None else [])
    )
    return StaffComposition(
        tenant_id=tenant_id,
        staff_id=staff_id,
        name=agent.name if agent is not None else "overall",
        is_overall=bool(agent.is_overall) if agent is not None else True,
        status=agent.status if agent is not None else "active",
        persona=_persona(db, tenant_id, agent),
        model_route=projection.agent_model_route(db, tenant_id, agent.id if agent is not None else None),
        session_policy=_session_policy(db, tenant_id, agent),
        capabilities=_capabilities(db, tenant_id, agent),
        sops=_sops(db, tenant_id, agent),
        channels=channels,
        team=_team(db, tenant_id, agent),
        interactions=DEFAULT_INTERACTIONS,
        ref=ref,
        metadata=dict(agent.metadata_json or {}) if agent is not None else {},
    )
