"""StaffDeck integration adapter; private ORM/services stay on the host side of the SPI."""

from __future__ import annotations

from typing import Any

from staffdeck_harness.contracts.invocation import ModuleInvocation, ModuleResult


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
        aid = None if ctx.agent_id.endswith(":overall") else ctx.agent_id
        versions = visible_knowledge_base_versions(host.db, ctx.tenant_id, aid)
        return KnowledgeFacade(host._deps()).search(
            inv,
            allowed_ids=set(host.slot.allowed().get("knowledge_base", set())),
            version_by_base={k: v.id for k, v in versions.items()},
        )
    if inv.operation == "general_skill.consume/v1":
        return GeneralSkillFacade(host._deps(), host._workspace_root(inv.context)).consume(
            inv, expected_digest=digest
        )
    if inv.operation in {"tool.invoke/v1", "mcp.invoke/v1", "a2a.invoke/v1"}:
        return ToolFacade(host._deps()).invoke(
            inv, expected_digest=digest, active_skill_id=host.slot.active_sop_id
        )
    if inv.operation == "sandbox.execute/v1":
        return host.sandbox(inv.context).execute(inv)
    return ModuleResult.fail("UNSUPPORTED_CAPABILITY", f"no local service for {inv.operation}")
