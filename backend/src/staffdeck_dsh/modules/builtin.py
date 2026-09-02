"""Built-in modules: the shipped implementation of every slot in the architecture.

Each entry is *one* manifest + *one* provider object. Deployments may disable
any of them (``dsh_disabled_modules``) and third parties add more through the
``staffdeck_dsh.modules`` entry-point group. Hosts resolve providers from the
registry, never by import.

Provider object contracts (duck-typed, documented on the host that consumes them):

- capability providers   ``invoke(host, inv) -> ModuleResult``   (see capabilities.host)
- interaction modules     ``handlers: dict[str, Handler]``       (see interactions.pipeline_host)
- handoff modules         ``AssignmentStrategy`` / ``Notifier`` / ``ReplyResolver``
- channel adapters        the legacy ``ChannelAdapter`` Protocol object
- observers               ``on_event(tenant_id, session_id, event_type, payload)``
- engines                 ``open(loop, request, agent_id) -> HarnessV2Engine``
- security profiles       ``build(settings) -> SecurityProfile``
"""

from __future__ import annotations

from typing import Any, Mapping

from staffdeck_dsh.composition.compiler import DEFAULT_HOOKS
from staffdeck_dsh.contracts.manifest import ModuleKind, SlotName
from staffdeck_dsh.modules.registry import ModuleRegistry, manifest


# --------------------------------------------------------------------------- capability providers

class KnowledgeProvider:
    module_id = "knowledge.local"

    def invoke(self, host: Any, inv: Any) -> Any:
        from app.agents.branching import visible_knowledge_base_versions
        from staffdeck_dsh.capabilities.facade import KnowledgeFacade

        ctx = inv.context
        agent_id = None if not ctx.agent_id or ctx.agent_id.endswith(":overall") else ctx.agent_id
        versions = visible_knowledge_base_versions(host.db, ctx.tenant_id, agent_id)
        return KnowledgeFacade(host._deps()).search(inv, allowed_ids=set(host.slot.allowed().get("knowledge_base", set())), version_by_base={k: v.id for k, v in versions.items()})


class GeneralSkillProvider:
    module_id = "general_skill.local"

    def invoke(self, host: Any, inv: Any) -> Any:
        from app.core.capability_manifest import general_skill_snapshot_digest
        from app.db.models import GeneralSkill
        from staffdeck_dsh.capabilities.facade import GeneralSkillFacade

        row = host.db.get(GeneralSkill, inv.binding_id) if inv.binding_id else None
        digest = general_skill_snapshot_digest(row) if row is not None else None
        return GeneralSkillFacade(host._deps(), host._workspace_root(inv.context)).consume(inv, expected_digest=digest)


class ToolProvider:
    """HTTP + MCP + A2A: ``ToolExecutor`` dispatches on ``tool_type``."""

    module_id = "tool.local"

    def invoke(self, host: Any, inv: Any) -> Any:
        from app.core.capability_manifest import tool_snapshot_digest
        from app.db.models import Tool
        from staffdeck_dsh.capabilities.facade import ToolFacade

        row = host.db.get(Tool, inv.binding_id) if inv.binding_id else None
        digest = tool_snapshot_digest(host.db, row) if row is not None else None
        return ToolFacade(host._deps()).invoke(inv, expected_digest=digest, active_skill_id=host.slot.active_sop_id)


class SandboxProvider:
    module_id = "sandbox.local"

    def invoke(self, host: Any, inv: Any) -> Any:
        return host.sandbox(inv.context).execute(inv)


# --------------------------------------------------------------------------- interactions

class DefaultInteractions:
    """Persona / memory / SOP slice / allowlist / ledger / citations / supervisor / handoff-detect."""

    module_id = "interaction.default"

    @property
    def handlers(self) -> Mapping[str, Any]:
        from staffdeck_dsh.interactions.pipeline_host import DEFAULT_HANDLERS

        return DEFAULT_HANDLERS


# --------------------------------------------------------------------------- observers

class TraceObserver:
    """Mirrors relayed DSH events into the legacy trace vocabulary (already done by the relay's trace sink); kept as the canonical observer id."""

    name = "observer.trace"

    def on_event(self, tenant_id: str, session_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        return None


class FeedbackObserver:
    name = "observer.feedback"

    def on_event(self, tenant_id: str, session_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        # Feedback analysis is enqueued by the legacy finalize path; the observer exists so a
        # deployment can swap in a different consumer without touching the relay.
        return None


# --------------------------------------------------------------------------- engines

class LegacyEngine:
    module_id = "engine.legacy"

    def open(self, loop: Any, request: Any, agent_id: str | None) -> Any:
        from app.core.harness_v2_engine import HarnessV2Engine

        return HarnessV2Engine(loop)


class DshBridgeEngine:
    module_id = "engine.dsh"

    def open(self, loop: Any, request: Any, agent_id: str | None) -> Any:
        from app.config import get_settings
        from staffdeck_dsh.bridge.engine_host import DshEngine, get_runtime

        return DshEngine(loop, runtime=get_runtime(get_settings()))


# --------------------------------------------------------------------------- security

class OssLocalProfileModule:
    module_id = "security.oss_local"

    def build(self, settings: Any) -> Any:
        from staffdeck_dsh.security.oss_local import build_oss_local_profile

        return build_oss_local_profile()


class BusinessBaseProfileModule:
    module_id = "security.business_base"

    def build(self, settings: Any) -> Any:
        from staffdeck_dsh.security.profile import build_profile

        return build_profile(settings)


def register(registry: ModuleRegistry, ctx: Mapping[str, Any]) -> None:
    settings = ctx.get("settings")
    profile_name = str(getattr(settings, "security_profile", None) or "OSS_LOCAL").upper()
    dsh_enabled = bool(getattr(settings, "dsh_enabled", False))

    # L3 capability providers (attach to staff.capability; SOP slots reuse them through the compiler)
    registry.install(manifest("knowledge.local", "Knowledge (local service)", kind=ModuleKind.CODE, slots=[SlotName.STAFF_CAPABILITY, SlotName.SOP_SLOT_KNOWLEDGE], provides=["knowledge.search/v1"], policy_actions=["knowledge.search/v1"]), KnowledgeProvider(), slot=SlotName.STAFF_CAPABILITY)
    registry.install(manifest("general_skill.local", "GeneralSkill (package reader)", kind=ModuleKind.CODE, slots=[SlotName.STAFF_CAPABILITY, SlotName.SOP_SLOT_SKILL], provides=["general_skill.consume/v1"], policy_actions=["general_skill.consume/v1"]), GeneralSkillProvider(), slot=SlotName.STAFF_CAPABILITY)
    registry.install(manifest("tool.local", "HTTP / MCP / A2A tools", kind=ModuleKind.CODE, slots=[SlotName.STAFF_CAPABILITY, SlotName.SOP_SLOT_ACTION], provides=["tool.invoke/v1", "mcp.invoke/v1", "a2a.invoke/v1"], policy_actions=["tool.invoke/v1", "mcp.invoke/v1", "a2a.invoke/v1"]), ToolProvider(), slot=SlotName.STAFF_CAPABILITY)
    registry.install(manifest("sandbox.local", "Sandboxed file & command tools", kind=ModuleKind.TRUSTED, slots=[SlotName.STAFF_CAPABILITY, SlotName.SOP_SLOT_ACTION], provides=["sandbox.execute/v1", "artifact.publish/v1"], policy_actions=["sandbox.execute/v1"]), SandboxProvider(), slot=SlotName.STAFF_CAPABILITY)
    registry.mark_guarded(SlotName.STAFF_CAPABILITY)

    # L4 interactions (hooks)
    registry.install(manifest("interaction.default", "Default interaction hooks", kind=ModuleKind.CODE, slots=[SlotName.STAFF_INTERACTION], provides=["hook.contribute/v1"], hooks=DEFAULT_HOOKS), DefaultInteractions(), slot=SlotName.STAFF_INTERACTION)

    # L4 handoff slots
    from staffdeck_dsh.handoff.core import ChannelCommandReplyResolver, ChannelNotifier, DefaultAssignment, WebInboxNotifier, WebReplyResolver

    registry.install(manifest("handoff.assignment.default", "Default assignment strategy", kind=ModuleKind.CODE, slots=[SlotName.HANDOFF_ASSIGNMENT], provides=["handoff.assign/v1"]), DefaultAssignment(), slot=SlotName.HANDOFF_ASSIGNMENT)
    registry.install(manifest("handoff.notifier.web", "Web inbox notifier", kind=ModuleKind.CODE, slots=[SlotName.HANDOFF_NOTIFIER], provides=["handoff.request/v1"]), WebInboxNotifier(), slot=SlotName.HANDOFF_NOTIFIER)
    for ch in ("feishu", "dingtalk", "wecom", "wechat"):
        registry.install(manifest(f"handoff.notifier.{ch}", f"{ch} notifier", kind=ModuleKind.CODE, slots=[SlotName.HANDOFF_NOTIFIER], provides=["handoff.request/v1"]), ChannelNotifier(ch), slot=SlotName.HANDOFF_NOTIFIER)
    registry.install(manifest("handoff.reply.web", "Web reply endpoint", kind=ModuleKind.CODE, slots=[SlotName.HANDOFF_REPLY_ENDPOINT], provides=["handoff.reply/v1"], policy_actions=["handoff.reply/v1"]), WebReplyResolver(), slot=SlotName.HANDOFF_REPLY_ENDPOINT)
    registry.install(manifest("handoff.reply.channel_command", "Channel /回复反馈 & quoted reply", kind=ModuleKind.CODE, slots=[SlotName.HANDOFF_REPLY_ENDPOINT], provides=["handoff.reply/v1"], policy_actions=["handoff.reply/v1"]), ChannelCommandReplyResolver(), slot=SlotName.HANDOFF_REPLY_ENDPOINT)
    registry.mark_guarded(SlotName.HANDOFF_REPLY_ENDPOINT)
    registry.mark_guarded(SlotName.HANDOFF_ASSIGNMENT)
    registry.mark_guarded(SlotName.HANDOFF_NOTIFIER)

    # L5 channel adapters (legacy registry objects, surfaced as modules)
    try:
        from app.channels.adapters import get_channel_adapter
        import app.channels.adapters.dingtalk, app.channels.adapters.feishu, app.channels.adapters.wechat, app.channels.adapters.wechat_kf, app.channels.adapters.wecom  # noqa: E401,F401  registration side effects

        for ch in ("feishu", "dingtalk", "wecom", "wechat", "wechat_kf"):
            try:
                adapter = get_channel_adapter(ch)
            except ValueError:
                continue
            registry.install(manifest(f"channel.{ch}", f"{ch} channel adapter", kind=ModuleKind.CODE, slots=[SlotName.STAFF_CHANNEL], provides=["channel.receive/v1", "channel.send/v1"], policy_actions=["channel.receive/v1", "channel.send/v1"]), adapter, slot=SlotName.STAFF_CHANNEL)
    except Exception:  # channel package optional in some deployments
        pass
    registry.mark_guarded(SlotName.STAFF_CHANNEL)

    # observers
    registry.install(manifest("observer.trace", "Trace projector", kind=ModuleKind.TRUSTED, slots=[SlotName.EVENT_OBSERVER], provides=["event.observe/v1"]), TraceObserver(), slot=SlotName.EVENT_OBSERVER)
    registry.install(manifest("observer.feedback", "Feedback consumer", kind=ModuleKind.CODE, slots=[SlotName.EVENT_OBSERVER], provides=["event.observe/v1"]), FeedbackObserver(), slot=SlotName.EVENT_OBSERVER)

    # engines: exactly one active
    registry.install(manifest("engine.legacy", "Harness v2 (in-process)", kind=ModuleKind.KERNEL, slots=[SlotName.RUNTIME_ENGINE], provides=["runtime.turn/v1"]), LegacyEngine(), slot=SlotName.RUNTIME_ENGINE, enabled=not dsh_enabled)
    registry.install(manifest("engine.dsh", "DSH bridge (Node subprocess)", kind=ModuleKind.TRUSTED, slots=[SlotName.RUNTIME_ENGINE], provides=["runtime.turn/v1"], policy_actions=["staff.use/v1"]), DshBridgeEngine(), slot=SlotName.RUNTIME_ENGINE, enabled=dsh_enabled)
    registry.mark_guarded(SlotName.RUNTIME_ENGINE)

    # security: exactly one active
    registry.install(manifest("security.oss_local", "OSS local PEP", kind=ModuleKind.KERNEL, slots=[SlotName.SECURITY_PEP]), OssLocalProfileModule(), slot=SlotName.SECURITY_PEP, enabled=profile_name == "OSS_LOCAL")
    registry.install(manifest("security.business_base", "Business Base PEP", kind=ModuleKind.KERNEL, slots=[SlotName.SECURITY_PEP]), BusinessBaseProfileModule(), slot=SlotName.SECURITY_PEP, enabled=profile_name == "BUSINESS_BASE")
