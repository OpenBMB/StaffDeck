"""SOP owns transitions; the registry selects implementations; the host owns PEP."""

from types import SimpleNamespace
import ast
from pathlib import Path

import pytest

from app.db.models import ChatSession, Skill
from app.core.task_request_compiler import TaskExecutionResult, TaskRequirement
from app.session.session_schema import RouterDecision
from staffdeck_harness.contracts.errors import PermissionDenied, ContractIncompatible
from staffdeck_harness.contracts.manifest import SlotName, ModuleKind
from staffdeck_harness.modules.registry import (
    ModuleRegistry,
    manifest,
    SlotConflict,
    RegistrySealed,
)
from staffdeck_harness.sop.contracts import SopDependencies
from staffdeck_harness.sop.host import SopHost
from staffdeck_harness.sop.lifecycle import SopRuntime


def deps(db):
    return SopDependencies(db, SimpleNamespace(record=lambda *a: None), lambda *a: None)


def workflow():
    return Skill(
        id="sop-row",
        tenant_id="t1",
        skill_id="flow",
        name="Flow",
        content_json={
            "start_node_id": "collect",
            "nodes": [
                {"node_id": "collect", "expected_user_info": ["answer"]},
                {"node_id": "done", "allowed_actions": ["answer_user"]},
            ],
            "edges": [{"source_node_id": "collect", "next_node_id": "done"}],
        },
    )


def test_lifecycle_runs_without_agentloop_and_preserves_wait_advance_complete(db):
    core = SopRuntime(db, deps(db).events, create_handoff=lambda *a: None)
    skill = workflow()
    session = ChatSession(id="s1", tenant_id="t1", active_skill_id="flow", active_step_id="collect")
    req = TaskRequirement(task_frame_id="tf1", kind="sop", goal="test", required_slots=["answer"])
    waiting = TaskExecutionResult(task_frame_id="tf1", status="completed", reply_fragment="answer?")
    outcome = core.after_execution(
        "t1",
        session,
        skill,
        req,
        waiting,
        RouterDecision(decision="continue_active"),
        remaining_actions=5,
    )
    assert waiting.status == "awaiting_user" and not outcome.continue_execution
    assert session.awaiting_input_json["expected_fields"] == ["answer"]
    resumed = TaskExecutionResult(
        task_frame_id="tf1", status="completed", slot_updates={"answer": "yes"}
    )
    outcome = core.after_execution(
        "t1",
        session,
        skill,
        req,
        resumed,
        RouterDecision(decision="continue_active"),
        remaining_actions=5,
    )
    assert outcome.continue_execution and session.active_step_id == "done"
    final = TaskExecutionResult(task_frame_id="tf1", status="completed", reply_fragment="done")
    outcome = core.after_execution(
        "t1",
        session,
        skill,
        req,
        final,
        RouterDecision(decision="continue_active"),
        remaining_actions=4,
    )
    assert not outcome.continue_execution and final.status == "completed"
    assert session.active_skill_id is None


def install(reg, name, provider, *, enabled=True):
    reg.install(
        manifest(
            name,
            "流程实现",
            kind=ModuleKind.TRUSTED,
            slots=[SlotName.RUNTIME_SOP],
            provides=["sop.lifecycle/v1"],
            policy_actions=["sop.execute/v1"],
        ),
        provider,
        slot=SlotName.RUNTIME_SOP,
        enabled=enabled,
    )
    reg.mark_guarded(SlotName.RUNTIME_SOP)


def test_registered_replacement_is_invoked_and_not_hot_swapped(db):
    from staffdeck_harness.sop.module import SopRuntimeModule

    calls = []

    class Alternate(SopRuntime):
        def complete_current_skill(self, session):
            calls.append(session.id)
            return super().complete_current_skill(session)

    class Provider:
        def build(self, ports):
            return Alternate(ports.db, ports.events, create_handoff=ports.create_handoff)

    reg = ModuleRegistry()
    install(reg, "sop.runtime", SopRuntimeModule(), enabled=False)
    install(reg, "test.sop", Provider())
    reg.seal()
    host = SopHost(deps(db), reg)
    session = ChatSession(id="s1", tenant_id="t1", active_skill_id="flow")
    host.complete_current_skill(session)
    assert calls == ["s1"] and session.active_skill_id is None
    with pytest.raises(RegistrySealed):
        reg.set_enabled("test.sop", False)


def test_ambiguous_or_incomplete_implementation_fails_closed(db):
    from staffdeck_harness.sop.module import SopRuntimeModule

    reg = ModuleRegistry()
    install(reg, "test.one", SopRuntimeModule())
    install(reg, "test.two", SopRuntimeModule())
    with pytest.raises(SlotConflict):
        reg.seal()
    reg = ModuleRegistry()
    install(reg, "test.bad", SimpleNamespace(build=lambda ports: object()))
    reg.seal()
    with pytest.raises(ContractIncompatible):
        SopHost(deps(db), reg)


def test_host_denial_precedes_module_state_mutation(db, registry, guard, security_ctx):
    host = SopHost(deps(db), registry)
    host.bind_security(guard("sop.runtime"), security_ctx(tenant_id="other"))
    skill = workflow()
    session = ChatSession(
        id="s1", tenant_id="t1", agent_id="a1", active_skill_id="flow", active_step_id="collect"
    )
    req = TaskRequirement(task_frame_id="tf1", kind="sop", goal="test")
    result = TaskExecutionResult(
        task_frame_id="tf1", status="completed", slot_updates={"answer": "injected"}
    )
    with pytest.raises(PermissionDenied):
        host.after_execution(
            "t1",
            session,
            skill,
            req,
            result,
            RouterDecision(decision="continue_active"),
            remaining_actions=3,
        )
    assert session.active_step_id == "collect" and not session.slots_json


def test_sop_package_has_no_legacy_loop_or_state_implementation_imports():
    import staffdeck_harness.sop

    forbidden = {
        "app.core.agent_loop",
        "app.core.turn_coordinator",
        "app.core.harness_v2_engine",
        "app.core.skill_runtime",
        "app.core.graph_rules",
        "app.core.turn_finalizer",
    }
    for path in Path(staffdeck_harness.sop.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in forbidden, (path.name, node.module)
