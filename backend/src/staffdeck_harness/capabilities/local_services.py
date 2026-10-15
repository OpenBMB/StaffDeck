"""StaffDeck integration adapter; private ORM/services stay on the host side of the SPI."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from staffdeck_harness.contracts.invocation import ModuleInvocation, ModuleResult


def _knowledge_route_deps(host: Any):
    """Keep configured model routing, but bound nested v3 provider work.

    The normal Harness v3 turn already owns a provider request. Knowledge
    routing may make an optional second request; it must not hold the MCP tool
    result forever when that nested request is stalled. Independent KM keeps
    its lexical fallback when this bounded request raises.
    """

    deps = host._deps()
    if getattr(host, "execution_engine", "harness_v3") != "harness_v3" or deps.model_config is None:
        return deps
    from app.config import get_settings

    ceiling = max(0.1, float(get_settings().harness_v3_knowledge_route_timeout_seconds))
    remaining = deps.remaining_seconds() if deps.remaining_seconds else None
    limits = [ceiling]
    if remaining is not None:
        limits.append(max(0.1, float(remaining)))
    configured = getattr(deps.model_config, "timeout_seconds", None)
    if configured:
        limits.append(max(0.1, float(configured)))
    timeout_seconds = min(limits)
    if hasattr(deps.model_config, "model_copy"):
        route_model = deps.model_config.model_copy(update={"timeout_seconds": timeout_seconds})
    else:
        route_model = replace(deps.model_config, timeout_seconds=timeout_seconds)
    return replace(deps, model_config=route_model)


def invoke_local(host: Any, inv: ModuleInvocation) -> ModuleResult:
    from staffdeck_harness.capabilities.facade import (
        GeneralSkillFacade,
        KnowledgeFacade,
        ToolFacade,
    )

    grants = host.slot.grants() if callable(getattr(host.slot, "grants", None)) else ()
    digest = next((g.resource_digest for g in grants if g.resource_id == inv.binding_id), None)
    if inv.operation == "knowledge.search/v1":
        from app.agents.branching import visible_knowledge_base_versions

        ctx = inv.context
        from app.knowledge.http_adapter import enabled
        if enabled():
            identity = host.security_context
            host.db.info["staffdeck_actor_id"] = identity.actor_user_id or identity.principal_id
        aid = None if ctx.agent_id.endswith(":overall") else ctx.agent_id
        versions = visible_knowledge_base_versions(host.db, ctx.tenant_id, aid)
        return KnowledgeFacade(_knowledge_route_deps(host)).search(
            inv,
            allowed_ids=set(host.slot.allowed().get("knowledge_base", set())),
            version_by_base={k: v.id for k, v in versions.items()},
        )
    if inv.operation == "general_skill.consume/v1":
        return GeneralSkillFacade(host._deps(), host._workspace_root(inv.context), host.sandbox(inv.context)).consume(
            inv, expected_digest=digest
        )
    if inv.operation in {"tool.invoke/v1", "mcp.invoke/v1", "a2a.invoke/v1"}:
        return ToolFacade(host._deps()).invoke(
            inv, expected_digest=digest, active_skill_id=host.slot.active_sop_id
        )
    if inv.operation == "sandbox.execute/v1":
        return host.sandbox(inv.context).execute(inv)
    return ModuleResult.fail("UNSUPPORTED_CAPABILITY", f"no local service for {inv.operation}")
