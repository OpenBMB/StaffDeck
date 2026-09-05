"""Built-in modules: the shipped implementation of every slot in the architecture.

Each entry is *one* manifest + *one* provider object. Deployments may disable
any of them (``harness_disabled_modules``) and third parties add more through the
``staffdeck_harness.modules`` entry-point group. Hosts resolve providers from the
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

from staffdeck_harness.composition.compiler import DEFAULT_HOOKS
from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.modules.registry import ModuleRegistry, manifest

CHANNEL_LABEL = {"feishu": "飞书", "dingtalk": "钉钉", "wecom": "企业微信", "wechat": "微信", "wechat_kf": "微信客服"}


# --------------------------------------------------------------------------- capability providers

class LocalCapabilityProvider:
    """Public SPI adapter: storage and implementation remain behind the host service port."""

    def invoke(self, context: Any, inv: Any) -> Any:
        return context.call_local()


class KnowledgeProvider(LocalCapabilityProvider):
    module_id = "knowledge.local"


class GeneralSkillProvider(LocalCapabilityProvider):
    module_id = "general_skill.local"


class ToolProvider(LocalCapabilityProvider):
    module_id = "tool.local"


class SandboxProvider(LocalCapabilityProvider):
    module_id = "sandbox.local"


# --------------------------------------------------------------------------- interactions

class DefaultInteractions:
    """Persona / memory / SOP slice / allowlist / ledger / citations / supervisor / handoff-detect."""

    module_id = "interaction.default"

    @property
    def handlers(self) -> Mapping[str, Any]:
        from staffdeck_harness.interactions.pipeline_host import DEFAULT_HANDLERS

        return DEFAULT_HANDLERS


class InteractionComponent:
    def __init__(self, handler: str):
        from staffdeck_harness.interactions.pipeline_host import DEFAULT_HANDLERS

        self.handlers = {handler: DEFAULT_HANDLERS[handler]}


# --------------------------------------------------------------------------- observers

class TraceObserver:
    """Mirrors relayed Harness v3 events into the legacy trace vocabulary (already done by the relay's trace sink); kept as the canonical observer id."""

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
    module_id = "engine.harness_v2"

    def open(self, loop: Any, request: Any, agent_id: str | None) -> Any:
        from app.core.harness_v2_engine import HarnessV2Engine

        return HarnessV2Engine(loop)


class HarnessV3BridgeEngine:
    module_id = "engine.harness_v3"

    def open(self, loop: Any, request: Any, agent_id: str | None) -> Any:
        from app.config import get_settings
        from staffdeck_harness.bridge.engine_host import HarnessV3Engine, get_runtime

        return HarnessV3Engine(loop, runtime=get_runtime(get_settings()))


# --------------------------------------------------------------------------- security

class OssLocalProfileModule:
    module_id = "security.oss_local"

    def build(self, settings: Any) -> Any:
        from staffdeck_harness.security.oss_local import build_oss_local_profile

        return build_oss_local_profile()


class BusinessBaseProfileModule:
    module_id = "security.business_base"

    def build(self, settings: Any) -> Any:
        from staffdeck_harness.security.profile import build_profile

        return build_profile(settings)


def register(registry: ModuleRegistry, ctx: Mapping[str, Any]) -> None:
    from staffdeck_harness.modules import kernel

    settings = ctx.get("settings")
    profile_name = str(getattr(settings, "security_profile", None) or "OSS_LOCAL").upper()
    harness_v3_enabled = bool(getattr(settings, "harness_v3_enabled", False))

    # L3 capability providers (attach to staff.capability; SOP slots reuse them through the compiler)
    registry.install(manifest("knowledge.local", "知识库检索", summary="在员工绑定的知识库中检索资料，并把引用来源带回回答。", kind=ModuleKind.CODE, slots=[SlotName.STAFF_CAPABILITY, SlotName.SOP_SLOT_KNOWLEDGE], provides=["knowledge.search/v1"], policy_actions=["knowledge.search/v1"]), KnowledgeProvider(), slot=SlotName.STAFF_CAPABILITY)
    registry.install(manifest("general_skill.local", "通用技能", summary="读取员工绑定的技能包，让员工按技能说明行事。", kind=ModuleKind.CODE, slots=[SlotName.STAFF_CAPABILITY, SlotName.SOP_SLOT_SKILL], provides=["general_skill.consume/v1"], policy_actions=["general_skill.consume/v1"]), GeneralSkillProvider(), slot=SlotName.STAFF_CAPABILITY)
    registry.install(manifest("tool.local", "业务工具调用", summary="调用 HTTP 接口、MCP 服务或其他智能体，完成查询与操作。", kind=ModuleKind.CODE, slots=[SlotName.STAFF_CAPABILITY, SlotName.SOP_SLOT_ACTION], provides=["tool.invoke/v1", "mcp.invoke/v1", "a2a.invoke/v1"], policy_actions=["tool.invoke/v1", "mcp.invoke/v1", "a2a.invoke/v1"]), ToolProvider(), slot=SlotName.STAFF_CAPABILITY)
    registry.install(manifest("sandbox.local", "受控执行环境", summary="在隔离环境中执行命令、读写文件并输出工作产物。", kind=ModuleKind.TRUSTED, slots=[SlotName.STAFF_CAPABILITY, SlotName.SOP_SLOT_ACTION], provides=["sandbox.execute/v1", "artifact.publish/v1"], policy_actions=["sandbox.execute/v1"], metadata={"switchable": True}), SandboxProvider(), slot=SlotName.STAFF_CAPABILITY)
    registry.mark_guarded(SlotName.STAFF_CAPABILITY)

    # L4 interactions (hooks)
    components = {h.handler: f"interaction.{h.handler.replace('.', '_')}" for h in DEFAULT_HOOKS}
    labels = {"persona": "员工人设注入", "memory.recall": "记忆上下文注入", "sop.execution_slice": "SOP 输入片段",
              "activation.allowlist": "激活范围校验", "capability.pep": "调用观测", "ledger.record": "能力回执收集",
              "citations.collect": "知识引用收集", "sop.output_supervisor": "SOP 输出监管", "handoff.detect": "人工介入检测"}
    registry.install(manifest("interaction.default", "对话介入规则（默认组合）", summary="由独立的人设、记忆、SOP、调用审计、监管和人工介入模块组合。", kind=ModuleKind.CODE, slots=[SlotName.STAFF_INTERACTION], provides=["hook.contribute/v1"], hooks=DEFAULT_HOOKS, metadata={"members": list(components.values())}), DefaultInteractions(), slot=SlotName.STAFF_INTERACTION)
    for hook in DEFAULT_HOOKS:
        registry.install(manifest(components[hook.handler], labels[hook.handler], kind=ModuleKind.CODE, slots=[SlotName.STAFF_INTERACTION], provides=["hook.contribute/v1"], hooks=[hook]), InteractionComponent(hook.handler), slot=SlotName.STAFF_INTERACTION)

    # L4 runtime memory (recall provider; capture stays async for the builtin). Not wrapped in
    # try/except on purpose: a memory module that fails to register must fail the assembly loudly.
    from staffdeck_harness.memory import install as install_memory

    install_memory(registry, settings)
    registry.mark_guarded(SlotName.RUNTIME_MEMORY)

    # L4 handoff slots
    from staffdeck_harness.handoff.core import ChannelCommandReplyResolver, ChannelNotifier, DefaultAssignment, WebInboxNotifier, WebReplyResolver

    registry.install(manifest("handoff.assignment.default", "处理人指派（默认）", summary="需要人工介入时，按默认规则选择处理人。", kind=ModuleKind.CODE, slots=[SlotName.HANDOFF_ASSIGNMENT], provides=["handoff.assign/v1"]), DefaultAssignment(), slot=SlotName.HANDOFF_ASSIGNMENT)
    registry.install(manifest("handoff.notifier.web", "站内通知", summary="通过网页收件箱通知处理人。", kind=ModuleKind.CODE, slots=[SlotName.HANDOFF_NOTIFIER], provides=["handoff.request/v1"]), WebInboxNotifier(), slot=SlotName.HANDOFF_NOTIFIER)
    try:
        from app.channels.service_outbox import HANDOFF_NOTIFY_CHANNELS
    except Exception:  # channel package optional
        HANDOFF_NOTIFY_CHANNELS = frozenset()
    for ch in ("feishu", "dingtalk", "wecom", "wechat"):
        supported = ch in HANDOFF_NOTIFY_CHANNELS
        summary = f"通过{CHANNEL_LABEL[ch]}通知处理人。" if supported else f"{CHANNEL_LABEL[ch]}暂不支持主动私聊通知；接入后自动启用。"
        registry.install(manifest(f"handoff.notifier.{ch}", f"{CHANNEL_LABEL[ch]}通知", summary=summary, kind=ModuleKind.CODE, slots=[SlotName.HANDOFF_NOTIFIER], provides=["handoff.request/v1"], metadata={"supported": supported}), ChannelNotifier(ch), slot=SlotName.HANDOFF_NOTIFIER, enabled=supported)
    registry.install(manifest("handoff.reply.web", "网页回复", summary="处理人在网页上回复后，员工继续对话。", kind=ModuleKind.CODE, slots=[SlotName.HANDOFF_REPLY_ENDPOINT], provides=["handoff.reply/v1"], policy_actions=["handoff.reply/v1"]), WebReplyResolver(), slot=SlotName.HANDOFF_REPLY_ENDPOINT)
    registry.install(manifest("handoff.reply.channel_command", "渠道内回复", summary="处理人在飞书、钉钉等渠道里引用消息或使用「/回复」指令回复。", kind=ModuleKind.CODE, slots=[SlotName.HANDOFF_REPLY_ENDPOINT], provides=["handoff.reply/v1"], policy_actions=["handoff.reply/v1"]), ChannelCommandReplyResolver(), slot=SlotName.HANDOFF_REPLY_ENDPOINT)
    registry.mark_guarded(SlotName.HANDOFF_REPLY_ENDPOINT)
    registry.mark_guarded(SlotName.HANDOFF_ASSIGNMENT)
    registry.mark_guarded(SlotName.HANDOFF_NOTIFIER)

    # L5 channel adapters (legacy registry objects, surfaced as modules)
    try:
        from app.channels.adapters.base import get_builtin_channel_adapter
        import app.channels.adapters.dingtalk, app.channels.adapters.feishu, app.channels.adapters.wechat, app.channels.adapters.wechat_kf, app.channels.adapters.wecom  # noqa: E401,F401  registration side effects

        for ch in ("feishu", "dingtalk", "wecom", "wechat", "wechat_kf"):
            try:
                adapter = get_builtin_channel_adapter(ch)
            except ValueError:
                continue
            registry.install(manifest(f"channel.{ch}", f"{CHANNEL_LABEL.get(ch, ch)}接入", summary=f"接收{CHANNEL_LABEL.get(ch, ch)}消息并回复，处理附件与卡片。", kind=ModuleKind.CODE, slots=[SlotName.STAFF_CHANNEL], provides=["channel.receive/v1", "channel.send/v1"], policy_actions=["channel.receive/v1", "channel.send/v1"]), adapter, slot=SlotName.STAFF_CHANNEL)
    except Exception:  # channel package optional in some deployments
        pass
    registry.mark_guarded(SlotName.STAFF_CHANNEL)

    # observers
    registry.install(manifest("observer.trace", "执行轨迹记录", summary="把每轮对话的执行过程记录成可回看的轨迹。", kind=ModuleKind.TRUSTED, slots=[SlotName.EVENT_OBSERVER], provides=["event.observe/v1"]), TraceObserver(), slot=SlotName.EVENT_OBSERVER)
    registry.install(manifest("observer.feedback", "反馈收集", summary="收集用户反馈，供后续分析与改进。", kind=ModuleKind.CODE, slots=[SlotName.EVENT_OBSERVER], provides=["event.observe/v1"]), FeedbackObserver(), slot=SlotName.EVENT_OBSERVER)

    # engines: exactly one active
    registry.install(manifest("engine.harness_v2", "Harness v2 引擎", summary="平台内置的上一代执行引擎，在主进程内运行。", kind=ModuleKind.TRUSTED, slots=[SlotName.RUNTIME_ENGINE], provides=["runtime.turn/v1"]), LegacyEngine(), slot=SlotName.RUNTIME_ENGINE, enabled=not harness_v3_enabled)
    registry.install(manifest("engine.harness_v3", "Harness v3 引擎", summary="新一代执行引擎，以独立进程运行，负责规划步骤和调用能力。", kind=ModuleKind.TRUSTED, slots=[SlotName.RUNTIME_ENGINE], provides=["runtime.turn/v1"], policy_actions=["staff.use/v1"]), HarnessV3BridgeEngine(), slot=SlotName.RUNTIME_ENGINE, enabled=harness_v3_enabled)
    registry.mark_guarded(SlotName.RUNTIME_ENGINE)

    # security: exactly one active
    registry.install(manifest("security.oss_local", "开源版权限（本地）", summary="按租户、角色、员工归属和绑定关系判断谁能使用什么。", kind=ModuleKind.KERNEL, slots=[SlotName.SECURITY_PEP]), OssLocalProfileModule(), slot=SlotName.SECURITY_PEP, enabled=profile_name == "OSS_LOCAL")
    registry.install(manifest("security.business_base", "企业版权限（统一权限中心）", summary="由企业权限中心统一判定；权限中心不可用时拒绝访问，不会放行。", kind=ModuleKind.KERNEL, slots=[SlotName.SECURITY_PEP]), BusinessBaseProfileModule(), slot=SlotName.SECURITY_PEP, enabled=profile_name == "BUSINESS_BASE")

    # Kernel / trusted leaves: registered so the module tree is complete and disable-able, never swappable.
    kernel.register(registry, ctx)
