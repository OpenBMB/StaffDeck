"""Saved capability tests use the runtime Host, without starting another AgentLoop."""
from __future__ import annotations
from dataclasses import replace
from types import SimpleNamespace

from app.db.models import ChatSession, new_id
from staffdeck_harness.capabilities.host import ActivationSlot, CapabilityHost, LifecycleFence
from staffdeck_harness.composition.compiler import CompositionCompiler
from staffdeck_harness.composition.sources import resolve_staff
from staffdeck_harness.contracts.errors import ModuleSdkError
from staffdeck_harness.contracts.invocation import InvocationContext, ModuleInvocation, ModuleResult
from staffdeck_harness.contracts.sources import SourceContext
from staffdeck_harness.security.profile import Guard


def test_saved_tool(db, *, registry, profile, tenant_id, agent_id, user_id, tool_id, arguments, invocation_id=None):
    """One authorized invocation, same catalog/schema/PEP/provider/ledger as conversation."""
    try:
        staff, identity = resolve_staff(registry, db, SourceContext(tenant_id, agent_id, user_id=user_id), profile)
        snapshot = CompositionCompiler().compile(staff, generation=registry.generation, strict=False)
        identifier = invocation_id or new_id("tooltest")
        # Use the existing hidden execution-test channel and persistence, not a new test runner.
        session = ChatSession(id=new_id("session"), tenant_id=tenant_id, agent_id=staff.staff_id,
                              user_id=user_id, channel="skill_test", title="工具测试")
        from staffdeck_harness.runtime.session_binding import bind_session
        bind_session(db, session, created=True)
        db.add(session)
        db.commit()
        from app.session.session_schema import ChatTurnRequest
        from staffdeck_harness.runtime.execution import begin, end
        request = ChatTurnRequest(tenant_id=tenant_id, agent_id=staff.staff_id, session_id=session.id,
            user_id=user_id, message="工具测试", client_turn_id=identifier, channel="skill_test")
        request._control_subject = db.info.get("staffdeck_control_subject")
        owner = SimpleNamespace(registry=registry, db=db, staff_composition=staff, security_context=identity)
        begin(owner, request, session, SimpleNamespace(id=identifier))
        host = CapabilityHost(db, Guard("capability.test", profile), owner.security_context,
            ActivationSlot(snapshot, registry.generation, identifier, session_id=session.id),
            LifecycleFence(registry.generation))
        ctx = InvocationContext(tenant_id, staff.staff_id, user_id, session.id, identifier,
                                "tool_test", task_frame_id=identifier, run_id=identifier,
                                execution=request._trusted_execution)
        try:
            result = host.invoke(ModuleInvocation(identifier, "tool", "tool.invoke/v1",
                {**arguments, "tool_id": tool_id}, ctx, binding_id=tool_id))[0]
            descriptor = host._descriptors.get(("tool.invoke/v1", tool_id))
            return replace(result, extensions={**dict(result.extensions),
                "tool_name": descriptor.name if descriptor else tool_id})
        finally:
            end(owner)
    except ModuleSdkError as exc:
        return ModuleResult.fail(exc.code, exc.message)
