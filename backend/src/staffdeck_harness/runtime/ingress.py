"""Actual ingress module dispatch, before any engine/model work."""

from __future__ import annotations

from typing import Any

from staffdeck_harness.contracts.errors import ModuleSdkError
from staffdeck_harness.contracts.manifest import SlotName


def accept(registry: Any, request: Any) -> Any:
    if registry is None:
        return request
    channel = request.channel
    name = (
        "scheduler"
        if channel == "scheduled_task"
        else "public_api"
        if channel == "public_api"
        else "web"
        if channel in {"web", "human_handoff_resume", "skill_test", "enterprise_debug", "team"}
        else None
    )
    if name is None:
        # Native channel ingress has its own adapter/identity/receive Host.
        from app.channels.adapters import get_channel_adapter

        get_channel_adapter(channel)
        return request
    items = [
        i
        for i in registry.providers(SlotName.STAFF_INGRESS)
        if i.manifest.metadata.get("ingress", i.manifest.module_id.removeprefix("ingress.")) == name
    ]
    if len(items) != 1:
        raise ModuleSdkError(f"入口 {name} 未启用或装配冲突", code="INGRESS_UNAVAILABLE")
    result = items[0].provider.accept(request)
    for key in ("tenant_id", "user_id", "agent_id", "session_id", "channel", "client_turn_id"):
        if getattr(result, key, None) != getattr(request, key, None):
            raise ModuleSdkError(
                f"ingress cannot replace trusted {key}", code="INVALID_INGRESS_CONTEXT"
            )
    return result
