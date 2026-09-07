"""Executable acceptance cases for the nine modularity review findings."""

from dataclasses import replace

import pytest

from app.agents.branching import ensure_private_resource_binding
from staffdeck_harness.composition.compiler import CompositionCompiler, compile_hooks
from staffdeck_harness.composition.slots import SlotDeclaration
from staffdeck_harness.composition.staff import SopView
from staffdeck_harness.contracts.hooks import HookContext, HookDecision
from staffdeck_harness.contracts.invocation import ModuleInvocation, ModuleResult
from staffdeck_harness.contracts.manifest import HookContribution, ModuleKind, SlotName
from staffdeck_harness.contracts.operations import OperationContract
from staffdeck_harness.interactions.pipeline_host import InteractionPipelineHost, PipelineState
from staffdeck_harness.modules import registry as registry_mod
from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install, manifest
from tests_harness.modules.conftest import FakeSettings
from tests_harness.test_capability_host_hardening import (
    db as db,
    _tool,
    _staff,
    _cap,
    _ctx,
    _host,
    _ref,
    _RecordingProvider,
    _install_registry,
)


def configured_host(db, monkeypatch, *, hooks=None):
    tool = _tool(db, "tool_a")
    ensure_private_resource_binding(db, "t1", "a1", "tool", tool.id)
    db.commit()
    provider = _RecordingProvider()
    _install_registry(monkeypatch, provider)
    snapshot = CompositionCompiler(hooks=()).compile(_staff([_cap("tool", tool.id)]))
    host = _host(db, snapshot, hooks=hooks)
    invocation = ModuleInvocation(
        "i1",
        "tool",
        "tool.invoke/v1",
        {"tool_id": tool.id},
        _ctx(),
        binding_id=tool.id,
        side_effecting=True,
    )
    return tool, provider, host, invocation


def test_replay_checks_current_permissions(db, monkeypatch):
    tool, provider, host, inv = configured_host(db, monkeypatch)
    assert host.invoke(inv)[0].success
    tool.enabled = False
    db.add(tool)
    db.commit()
    result, _ = host.invoke(replace(inv, invocation_id="i2"))
    assert result.error["code"] == "PERMISSION_DENIED"
    assert len(provider.calls) == 1


def test_post_tool_deny_keeps_completed_write_claim(db, monkeypatch):
    def hook(point, inv, result):
        return (
            HookDecision.deny("blocked output")
            if point == "post_tool"
            else HookDecision.passthrough()
        )

    _, provider, host, inv = configured_host(db, monkeypatch, hooks=hook)
    first, receipt = host.invoke(inv)
    replay, _ = host.invoke(replace(inv, invocation_id="i2"))
    assert first.error["code"] == replay.error["code"] == "POST_TOOL_DENIED"
    assert receipt.status == "completed", "a sent write cannot be made retryable by output policy"
    assert len(provider.calls) == 1


def test_only_supervised_projection_is_replayed(db, monkeypatch):
    def hook(point, inv, result):
        return (
            HookDecision(kind="modify", replacement=ModuleResult.ok({"redacted": True}))
            if point == "post_tool"
            else HookDecision.passthrough()
        )

    _, provider, host, inv = configured_host(db, monkeypatch, hooks=hook)
    assert host.invoke(inv)[0].data == {"redacted": True}
    host.hooks = None
    replay, receipt = host.invoke(replace(inv, invocation_id="i2"))
    assert replay.data["redacted"] and "echo" not in replay.data
    assert receipt.replayed_from and len(provider.calls) == 1


def test_turn_retains_original_provider_generation(db, monkeypatch):
    _, old, host, inv = configured_host(db, monkeypatch)
    new = _RecordingProvider()
    _install_registry(monkeypatch, new)
    assert host.invoke(inv)[0].success
    assert old.calls and not new.calls


def test_provider_version_pin_is_enforced(db, monkeypatch):
    _, provider, host, inv = configured_host(db, monkeypatch)
    host.slot.snapshot = replace(
        host.slot.snapshot,
        grants=tuple(replace(g, provider_version="9.9.9") for g in host.slot.snapshot.grants),
    )
    result, receipt = host.invoke(inv)
    assert result.error["code"] == "PROVIDER_VERSION_CHANGED"
    assert not provider.calls and receipt.side_effect_key is None


def test_nested_tool_id_cannot_replace_authorized_target(db, monkeypatch):
    _, _, host, _ = configured_host(db, monkeypatch)
    result, _ = host.invoke_proxy(
        "tool_invoke",
        {"tool_id": "tool_a", "arguments": {"tool_id": "other"}},
        _ctx(trace_id="nested"),
    )
    assert result.data["echo"]["tool_id"] == "tool_a"


def test_dependency_order_wins_over_numeric_priority():
    hooks = [
        HookContribution("pre_step", "business", 0, ("guard",)),
        HookContribution("pre_step", "guard", 100),
    ]
    assert [h.handler for h in compile_hooks(hooks).order["pre_step"]] == ["guard", "business"]


@pytest.mark.parametrize("critical,expected", [(True, "deny"), (False, "pass")])
def test_hook_failure_policy_is_explicit(critical, expected):
    def broken(*args):
        raise RuntimeError("broken")

    plan = compile_hooks([HookContribution("pre_step", "broken", critical=critical)])
    pipeline = InteractionPipelineHost(plan, {"broken": broken})
    snapshot = CompositionCompiler(hooks=()).compile(_staff())
    ctx = HookContext("pre_step", "t1", "a1", "s1", "turn1", 1, snapshot.snapshot_id, {})
    assert pipeline.run("pre_step", ctx, PipelineState(snapshot)).kind == expected


def test_disabled_interaction_bundle_is_not_resurrected(monkeypatch):
    reg = discover_and_install(ModuleRegistry(), FakeSettings())
    reg.set_enabled("interaction.default", False)
    reg.seal()
    monkeypatch.setattr(registry_mod, "_active", reg)
    assert reg.hooks() == ()
    assert all(not hs for hs in CompositionCompiler().compile(_staff()).hooks.order.values())


def test_single_interaction_component_can_be_removed(monkeypatch):
    reg = discover_and_install(ModuleRegistry(), FakeSettings())
    reg.set_enabled("interaction.memory_recall", False)
    reg.seal()
    monkeypatch.setattr(registry_mod, "_active", reg)
    names = {h.handler for h in reg.hooks()}
    assert "memory.recall" not in names and "sop.output_supervisor" in names


def test_channel_disable_affects_real_adapter_lookup(monkeypatch):
    from app.channels.adapters import get_channel_adapter

    reg = discover_and_install(ModuleRegistry(), FakeSettings())
    reg.set_enabled("channel.feishu", False)
    reg.seal()
    monkeypatch.setattr(registry_mod, "_active", reg)
    with pytest.raises(ValueError, match="适配模块"):
        get_channel_adapter("feishu")
    assert get_channel_adapter("wecom") is reg.get("channel.wecom").provider


def test_sop_slot_overrides_staff_provider_and_config(db, monkeypatch):
    tool = _tool(db, "tool_a")
    ensure_private_resource_binding(db, "t1", "a1", "tool", tool.id)
    db.commit()
    default, scoped = _RecordingProvider(), _RecordingProvider()
    _install_registry(monkeypatch, default, extra=[("tool.scoped", scoped, ("tool.invoke/v1",))])
    sop = SopView(
        "sop1",
        "row1",
        "1",
        "SOP",
        {},
        None,
        {
            "orders": {
                "resource_id": tool.id,
                "provider_module_id": "tool.scoped",
                "provider_config": {"region": "sh"},
            }
        },
        (SlotDeclaration("orders", "tool.invoke/v1", True, "n1"),),
        _ref("sop", "sop1"),
    )
    snapshot = CompositionCompiler(hooks=()).compile(
        replace(_staff([_cap("tool", tool.id)]), sops=(sop,))
    )
    grants = snapshot.grants_for(sop_id="sop1", node_id="n1")
    assert len(grants) == 1 and grants[0].provider_module_id == "tool.scoped"
    assert grants[0].provider_config == {"region": "sh"}
    host = _host(db, snapshot, active_sop_id="sop1")
    assert host.invoke(
        ModuleInvocation(
            "i1", "tool", "tool.invoke/v1", {"tool_id": tool.id}, _ctx(), binding_id=tool.id
        )
    )[0].success
    assert scoped.calls and not default.calls
    assert any(b.parent_id == "sop1" and b.module_id == "tool.scoped" for b in snapshot.bindings)


def test_staff_empty_slot_disables_inherited_memory(monkeypatch):
    reg = discover_and_install(ModuleRegistry(), FakeSettings())
    reg.seal()
    monkeypatch.setattr(registry_mod, "_active", reg)
    staff = replace(_staff(), metadata={"module_bindings": {"runtime.memory": []}})
    snapshot = CompositionCompiler().compile(staff)
    assert reg.selected(SlotName.RUNTIME_MEMORY, snapshot) == []


def test_extension_contract_reaches_provider_without_host_changes(db, monkeypatch):
    tool = _tool(db, "tool_a")
    ensure_private_resource_binding(db, "t1", "a1", "tool", tool.id)
    db.commit()
    received = []

    class Extension:
        def invoke(self, context, inv):
            assert not hasattr(context, "db") and not hasattr(context, "guard")
            received.append(inv.arguments)
            return ModuleResult.ok({"forecast": "sunny"})

    reg = ModuleRegistry()
    reg.register_operation(
        OperationContract(
            "weather.lookup/v1",
            "tool",
            "use",
            parameters={
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        )
    )
    reg.install(
        manifest(
            "acme.weather",
            "Weather",
            kind=ModuleKind.CODE,
            slots=[SlotName.STAFF_CAPABILITY],
            provides=["weather.lookup/v1"],
            policy_actions=["weather.lookup/v1"],
        ),
        Extension(),
        slot=SlotName.STAFF_CAPABILITY,
    )
    reg.mark_guarded(SlotName.STAFF_CAPABILITY)
    reg.seal()
    monkeypatch.setattr(registry_mod, "_active", reg)
    snapshot = CompositionCompiler(hooks=()).compile(
        _staff([_cap("tool", tool.id, {"operation": "weather.lookup/v1"})])
    )
    host = _host(db, snapshot)
    result, _ = host.invoke_proxy(
        "capability_invoke",
        {
            "operation": "weather.lookup/v1",
            "resource_id": tool.id,
            "arguments": {"city": "Shanghai"},
        },
        _ctx(),
    )
    assert result.success and received == [{"city": "Shanghai"}]
    assert host.describe()["items"][0]["proxy"] == "capability_invoke"


def test_lifecycle_drains_admission_and_disposes_in_reverse_order():
    events = []

    class Module:
        def __init__(self, name):
            self.name = name

        def start_module(self, config):
            events.append("start:" + self.name)

        def stop_module(self):
            events.append("stop:" + self.name)

        def dispose_module(self):
            events.append("dispose:" + self.name)

    reg = ModuleRegistry()
    for name in ("a", "b"):
        reg.install(
            manifest("test." + name, name, kind=ModuleKind.CODE, slots=[SlotName.EVENT_OBSERVER]),
            Module(name),
            slot=SlotName.EVENT_OBSERVER,
        )
    reg.seal()
    reg.start()
    with reg.turn_lease():
        assert reg.live_turns == 1
        reg.begin_drain()
        with pytest.raises(Exception, match="restarting"):
            with reg.turn_lease():
                pass
    assert reg.live_turns == 0
    reg.dispose()
    reg.dispose()
    assert events == ["start:a", "start:b", "stop:b", "dispose:b", "stop:a", "dispose:a"]


def test_memory_management_and_runtime_share_the_same_provider(db, monkeypatch):
    from app.api.memories import list_memories, clear_my_memories
    from app.db.models import User
    from staffdeck_harness.memory import for_staff

    calls = []

    class Memory:
        def invoke(self, context, call):
            assert not hasattr(context, "db") and not hasattr(call, "request")
            calls.append(call)
            return (
                {"deleted": 2}
                if call.operation == "clear"
                else [{"id": "remote", "content": "remote data", "kind": "fact"}]
            )

    reg = discover_and_install(ModuleRegistry(), FakeSettings())
    reg.set_enabled("memory.default", False)
    reg.install(
        manifest(
            "memory.remote",
            "Remote memory",
            kind=ModuleKind.CODE,
            slots=[SlotName.RUNTIME_MEMORY],
            provides=["memory.read/v1", "memory.write/v1"],
        ),
        Memory(),
        slot=SlotName.RUNTIME_MEMORY,
    )
    reg.seal()
    monkeypatch.setattr(registry_mod, "_active", reg)
    user = db.get(User, "u1")
    assert (
        for_staff(db, "t1", "a1").context_memories("t1", "u1", agent_id="a1")[0].content
        == "remote data"
    )
    assert list_memories("t1", "a1", "u1", None, None, 100, user, db)[0]["id"] == "remote"
    assert clear_my_memories("t1", "a1", user, db) == {"deleted": 2}
    assert [c.operation for c in calls] == ["recall", "list", "clear"]


def test_api_pep_denies_object_access_and_filters_catalog_outside_loop(db, monkeypatch):
    from fastapi import APIRouter, Depends, FastAPI
    from fastapi.testclient import TestClient
    from app.db import get_session
    from app.db.models import User
    from app.security.auth import get_current_user
    from app.security.api_policy import module_policy, install_response_filters
    from staffdeck_harness.contracts.security import Decision
    from staffdeck_harness.security.oss_local import build_oss_local_profile

    _tool(db, "tool_a")
    _tool(db, "tool_b")
    checked = []

    class Pep:
        def authorize(self, ctx, module, action, resource):
            checked.append((module, action, resource.id))
            return (
                Decision.deny("revoked", source="BUSINESS_BASE")
                if resource.id == "tool_a"
                else Decision.allow("allowed", source="BUSINESS_BASE")
            )

    reg = ModuleRegistry()
    reg.security_profile = replace(build_oss_local_profile(), name="BUSINESS_BASE", pep=Pep())
    monkeypatch.setattr(registry_mod, "_active", reg)
    app = FastAPI()
    router = APIRouter(prefix="/tools")

    @router.get("")
    def listing():
        return [{"id": "tool_a"}, {"id": "tool_b"}]

    @router.get("/{tool_id}")
    def detail(tool_id: str):
        return {"id": tool_id}

    app.include_router(router, dependencies=[Depends(module_policy("tool"))])
    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, "u1")
    install_response_filters(app)
    client = TestClient(app)
    assert client.get("/tools/tool_a").status_code == 403
    assert client.get("/tools").json() == [{"id": "tool_b"}]
    assert ("tool", "view", "tool_a") in checked


def test_real_handoff_paths_use_assignment_notifier_and_reply_modules(db, monkeypatch):
    from types import SimpleNamespace
    from app.core.agent_loop import AgentLoop
    from app.core.human_handoff_service import HumanHandoffService
    from app.core.handoff_reply_service import apply_handoff_reply
    from app.db.models import ChatSession
    from app.session.session_schema import StepAgentResult

    calls = []

    class Module:
        name = "web"

        def invoke(self, context, call):
            assert not hasattr(context, "db")
            calls.append(call.operation)
            if call.operation == "handoff.assign/v1":
                return "u1"
            if call.operation == "handoff.reply/v1":
                return call.payload["handoff_id"], call.payload["reply"] + " normalized"
            return "notice1"

    reg = discover_and_install(ModuleRegistry(), FakeSettings())
    for mid in ("handoff.assignment.default", "handoff.notifier.web", "handoff.reply.web"):
        reg.set_enabled(mid, False)
    for slot, op, name in (
        (SlotName.HANDOFF_ASSIGNMENT, "handoff.assign/v1", "assign"),
        (SlotName.HANDOFF_NOTIFIER, "handoff.request/v1", "notify"),
        (SlotName.HANDOFF_REPLY_ENDPOINT, "handoff.reply/v1", "reply"),
    ):
        reg.install(
            manifest("test." + name, name, kind=ModuleKind.CODE, slots=[slot], provides=[op]),
            Module(),
            slot=slot,
        )
    reg.seal()
    monkeypatch.setattr(registry_mod, "_active", reg)
    session = ChatSession(id="s1", tenant_id="t1", user_id="u1", agent_id="a1", channel="web")
    db.add(session)
    db.commit()
    row = HumanHandoffService(db, SimpleNamespace(record=lambda *a: None)).create(
        "t1",
        session,
        StepAgentResult(action="reply", reply="needs approval"),
        current_step_resolver=lambda: None,
        assignee_resolver=lambda *a: None,
        context_summary=lambda s: "summary",
        pending_question=lambda *a: "question",
    )
    db.commit()
    assert row.assignee_user_id == "u1"
    AgentLoop(db)._maybe_notify_handoff_assignee("t1", session, row)
    assert row.notify_message_id == "notice1"
    resumed = []
    apply_handoff_reply(db, row, "approved", answered_by_user_id="u1", resume=resumed.append)
    assert row.human_reply == "approved normalized" and resumed == [row.id]
    assert calls == ["handoff.assign/v1", "handoff.request/v1", "handoff.reply/v1"]


def test_disabling_all_notifiers_does_not_restore_defaults(db, monkeypatch):
    from staffdeck_harness.handoff.core import build_handoff_core
    from staffdeck_harness.security.profile import Guard
    from staffdeck_harness.security.oss_local import build_oss_local_profile

    reg = discover_and_install(ModuleRegistry(), FakeSettings())
    for slot in (SlotName.HANDOFF_NOTIFIER, SlotName.HANDOFF_REPLY_ENDPOINT):
        for item in reg.providers(slot):
            reg.set_enabled(item.manifest.module_id, False)
    reg.seal()
    monkeypatch.setattr(registry_mod, "_active", reg)
    core = build_handoff_core(db, Guard("test", build_oss_local_profile()))
    assert not core.notifiers and not core.resolvers


def test_pool_stop_closes_checked_out_workers_and_blocks_admission():
    from pathlib import Path
    from tests_harness.test_engine_turn_ownership import _pool, _cfg

    pool = _pool()
    worker = pool.acquire(
        _cfg(), "t1", mcp_url="url", cwd=Path("/tmp"), register=lambda token: None
    )
    closed = []
    pool.close_all(on_close=closed.append)
    pool.release(worker, on_close=closed.append)
    assert worker.process.closed and closed == [worker.token]
    with pytest.raises(RuntimeError, match="draining"):
        pool.acquire(_cfg(), "t1", mcp_url="url", cwd=Path("/tmp"), register=lambda token: None)


def test_legacy_executor_uses_the_same_module_host(db, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from app.core.capability_manifest import CapabilityManifestBuilder
    from app.core.harness_capability_invoker import HarnessCapabilityInvoker
    from app.db.models import ChatSession, ModelConfig, HarnessInvocationRecord
    from sqlmodel import select
    from staffdeck_harness.capabilities.legacy_adapter import bind
    from staffdeck_harness.security.oss_local import build_oss_local_profile

    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    tool, provider, host, _ = configured_host(db, monkeypatch)
    session = ChatSession(id="s1", tenant_id="t1", user_id="u1", agent_id="a1")
    db.add(session)
    db.commit()
    model = db.get(ModelConfig, "m1")
    manifest_ = CapabilityManifestBuilder(db).build("t1", "a1", None, None)
    invoker = HarnessCapabilityInvoker(
        db,
        tenant_id="t1",
        session=session,
        task_frame_id="tf1",
        model_config=model,
        manifest=manifest_,
        active_skill=None,
        active_step_id=None,
        agent_id="a1",
        run_id="run1",
    )
    owner = SimpleNamespace(
        db=db,
        snapshot=host.slot.snapshot,
        registry=host.registry,
        profile=build_oss_local_profile(),
        user_message_id="turn1",
        _is_cancelled=lambda *a: False,
    )
    invoker.module_invoke = bind(
        owner,
        SimpleNamespace(tenant_id="t1", user_id="u1", channel="web"),
        session,
        SimpleNamespace(task_id="tf1", step_id=None),
        SimpleNamespace(id="run1"),
        None,
        model,
        lambda *a: None,
        None,
    )
    result = invoker.invoke(tool.name, {"order_id": "one"})
    assert result["success"] and provider.calls == ["tool.invoke/v1"]
    receipts = db.exec(select(HarnessInvocationRecord)).all()
    assert len(receipts) == 1 and receipts[0].approval_json["engine"] == "harness_v2"


def test_harness_v3_finish_requires_real_successful_capability_receipts(db, monkeypatch):
    from staffdeck_harness.bridge.task_agent import HarnessV3TaskAgent
    from app.core.task_request_compiler import TaskRequirement

    tool, _, host, inv = configured_host(db, monkeypatch)
    agent = HarnessV3TaskAgent.__new__(HarnessV3TaskAgent)
    agent._host = host
    host.slot.finish = {"status": "completed", "reply_fragment": "done"}
    requirement = TaskRequirement(
        task_frame_id="tf1", kind="sop", goal="call the tool", required_capability_names=[tool.name]
    )
    state = PipelineState(host.slot.snapshot)
    result = agent._result(requirement, host.slot, state, "done", "stop", 1, [], [])
    assert result.error["code"] == "REQUIRED_CAPABILITY_MISSING"
    assert host.invoke(inv)[0].success
    result = agent._result(requirement, host.slot, state, "done", "stop", 1, [], [])
    assert result.status == "completed" and result.capability_results[0]["receipt"]


def test_final_reply_supervision_is_not_bypassed_by_response_synthesis(db, monkeypatch):
    from types import SimpleNamespace
    from app.core.turn_coordinator import TurnCoordinator
    from staffdeck_harness.contracts.errors import ModuleSdkError

    reg = ModuleRegistry()
    from staffdeck_harness.sop.module import SopRuntimeModule
    reg.install(manifest("sop.runtime", "SOP runtime", kind=ModuleKind.TRUSTED,
                         slots=[SlotName.RUNTIME_SOP], provides=["sop.lifecycle/v1"]),
                SopRuntimeModule(), slot=SlotName.RUNTIME_SOP)
    hook = HookContribution("turn_stopping", "review")
    reg.install(
        manifest(
            "acme.review",
            "Review",
            kind=ModuleKind.CODE,
            slots=[SlotName.STAFF_INTERACTION],
            hooks=[hook],
        ),
        SimpleNamespace(handlers={"review": lambda ctx, state: HookDecision.deny("unsafe")}),
        slot=SlotName.STAFF_INTERACTION,
    )
    reg.seal()
    monkeypatch.setattr(registry_mod, "_active", reg)
    engine = TurnCoordinator(SimpleNamespace(db=db, events=SimpleNamespace()))
    engine.snapshot = CompositionCompiler().compile(_staff())
    with pytest.raises(ModuleSdkError, match="unsafe"):
        engine.supervise_reply(
            SimpleNamespace(tenant_id="t1"),
            SimpleNamespace(id="s1", slots_json={}),
            "unsafe reply",
            None,
            None,
        )


def test_generic_side_effect_identity_includes_bound_resource():
    one = ModuleInvocation(
        "i1",
        "weather",
        "weather.write/v1",
        {"value": 1},
        _ctx(),
        binding_id="r1",
        side_effecting=True,
    )
    two = replace(one, binding_id="r2")
    assert one.side_effect_key() != two.side_effect_key()
