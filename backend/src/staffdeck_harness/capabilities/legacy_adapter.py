"""Adapt the legacy executor vocabulary to the same module/PEP/ledger boundary.

Discovery and progressive activation stay in the legacy invoker. Business capabilities
enter this adapter only after that allowlist was checked, before its old execution ledger.
"""

from __future__ import annotations

from dataclasses import asdict

from app.db.models import User, new_id
from staffdeck_harness.capabilities.host import ActivationSlot, CapabilityHost, LifecycleFence
from staffdeck_harness.composition.compiler import compile_hooks
from staffdeck_harness.contracts.hooks import HookContext
from staffdeck_harness.contracts.invocation import InvocationContext
from staffdeck_harness.interactions.pipeline_host import InteractionPipelineHost, PipelineState
from staffdeck_harness.security.profile import Guard, get_profile


def bind(coordinator, request, session, frame, run, active_skill, model_config, trace, deadline):
    host = None

    def invoke(name, arguments, descriptor):
        nonlocal host
        if descriptor.kind not in {"tool", "general_skill", "knowledge", "file"}:
            return None
        snapshot, registry = coordinator.snapshot, coordinator.registry
        sop_id = active_skill.skill_id if active_skill else None
        if host is None:
            profile = getattr(coordinator, "profile", None) or get_profile()
            user = coordinator.db.get(User, request.user_id) if request.user_id else None
            ctx = (
                profile.identity.from_user(user, channel=request.channel)
                if user
                else profile.identity.from_service("staffdeck.runtime", request.tenant_id)
            )
            slot = ActivationSlot(
                snapshot,
                snapshot.generation,
                coordinator.user_message_id or session.id,
                session_id=session.id,
                active_sop_id=sop_id,
                active_node_id=frame.step_id,
                deadline_monotonic=deadline,
            )
            host = CapabilityHost(
                coordinator.db,
                Guard("runtime.capability", profile),
                ctx,
                slot,
                LifecycleFence(
                    snapshot.generation, lambda: coordinator._is_cancelled(request, session)
                ),
                model_config=model_config,
                trace=trace,
                run_id=run.id,
                execution_engine="harness_v2",
            )
            host.registry = registry
            pipeline = InteractionPipelineHost(
                compile_hooks(registry.hooks(snapshot, sop_id=sop_id)),
                registry.hook_handlers(snapshot, sop_id=sop_id),
            )
            state = PipelineState(
                snapshot,
                session_slots=dict(session.slots_json or {}),
                active_sop_id=sop_id,
                active_node_id=frame.step_id,
            )

            def hooks(point, inv, result):
                payload = {
                    "name": inv.metadata.get("proxy_name", inv.operation),
                    "operation": inv.operation,
                    "arguments": dict(inv.arguments),
                    "binding_id": inv.binding_id,
                }
                if result is not None:
                    payload.update(
                        {
                            "success": result.success,
                            "data": result.data,
                            "error": result.error,
                            "receipt": asdict(host.current_receipt)
                            if host.current_receipt
                            else None,
                            "citations": list(result.citations),
                        }
                    )
                return pipeline.run(
                    point,
                    HookContext(
                        point,
                        request.tenant_id,
                        snapshot.staff_id,
                        session.id,
                        coordinator.user_message_id or session.id,
                        1,
                        snapshot.snapshot_id,
                        payload,
                    ),
                    state,
                )

            host.hooks = hooks
        ctx = InvocationContext(
            request.tenant_id,
            snapshot.staff_id,
            request.user_id or "anonymous",
            session.id,
            coordinator.user_message_id or session.id,
            request.channel,
            task_frame_id=frame.task_id,
            step_id=frame.step_id,
            run_id=run.id,
            snapshot_id=snapshot.snapshot_id,
            trace_id=new_id("hcall"),
        )
        grant = next(
            (g for g in host.slot.grants() if g.resource_id == descriptor.capability_id), None
        )
        if grant and grant.operation in registry.operations:
            proxy, args = (
                "capability_invoke",
                {
                    "operation": grant.operation,
                    "resource_id": descriptor.capability_id,
                    "arguments": arguments,
                },
            )
        elif descriptor.kind == "tool":
            proxy, args = (
                "tool_invoke",
                {"tool_id": descriptor.capability_id, "arguments": arguments},
            )
        elif descriptor.kind == "general_skill":
            proxy, args = "general_skill_read", {**arguments, "skill_id": descriptor.capability_id}
        elif descriptor.kind == "knowledge":
            proxy, args = "knowledge_search", arguments
        else:
            proxy, args = "sandbox_execute", {"tool": name, "arguments": arguments}
        result, receipt = host.invoke_proxy(proxy, args, ctx)
        return {
            "success": result.success,
            "data": result.data,
            "error": result.error,
            "citations": list(result.citations),
            "artifacts": list(result.artifacts),
            "receipt": asdict(receipt) if receipt else None,
        }

    return invoke
