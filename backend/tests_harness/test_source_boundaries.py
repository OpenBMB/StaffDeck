"""Replacement proofs: resources need not have rows in the local catalog."""
import subprocess
import sys

from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlmodel import Session, SQLModel, create_engine

from staffdeck_harness.capabilities.host import ActivationSlot, CapabilityHost, LifecycleFence
from staffdeck_harness.composition.compiler import CompositionCompiler
from staffdeck_harness.contracts.invocation import InvocationContext, ModuleInvocation, ModuleResult
from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.contracts.security import ResourceRef, SecurityContext
from staffdeck_harness.contracts.sources import ResourceDescriptor, SourceContext
from staffdeck_harness.contracts.staff import StaffComposition, SessionPolicy, CapabilityBindingView
from staffdeck_harness.modules.registry import ModuleRegistry, manifest
from staffdeck_harness.modules import registry as registry_module
from staffdeck_harness.security.oss_local import build_oss_local_profile
from staffdeck_harness.security.profile import Guard


def staff():
    ref = ResourceRef("agent", "external-staff", "t", {"owner_user_id": "u"})
    cap = CapabilityBindingView("tool", "external-tool", "installation-1",
        ResourceRef("tool", "external-tool", "t"), "Remote tool",
        metadata={"provider_module_id": "test.executor", "resource_digest": "version1"})
    return StaffComposition("t", ref.id, "Remote staff", False, "active", "Persona",
        {}, SessionPolicy(), (cap,), (), (), None, (), ref)


class Catalog:
    def __init__(self):
        self.value = ResourceDescriptor(ResourceRef("tool", "external-tool", "t",
            {"binding_status": "active", "private_to_agent": True}), "Remote tool", "tool.invoke/v1",
            input_schema={"type": "object", "properties": {"n": {"type": "integer"}},
                          "required": ["n"], "additionalProperties": False},
            digest="version1", side_effecting=True)

    def resolve(self, context, resource_type, resource_id, operation):
        return self.value


class Executor:
    def __init__(self):
        self.calls = 0

    def invoke(self, context, invocation):
        self.calls += 1
        return ModuleResult.ok({"n": invocation.arguments["n"]})


@pytest.fixture
def remote_host(monkeypatch):
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    catalog, executor, reg = Catalog(), Executor(), ModuleRegistry()
    reg.install(manifest("test.catalog", "External catalog", kind=ModuleKind.TRUSTED,
        slots=[SlotName.RESOURCE_CATALOG]), SimpleNamespace(build=lambda db: catalog), slot=SlotName.RESOURCE_CATALOG)
    reg.install(manifest("test.executor", "External executor", kind=ModuleKind.CODE,
        slots=[SlotName.STAFF_CAPABILITY], provides=["tool.invoke/v1"],
        policy_actions=["tool.invoke/v1"], metadata={"catalog_module_id": "test.catalog"}),
        executor, slot=SlotName.STAFF_CAPABILITY)
    reg.mark_guarded(SlotName.STAFF_CAPABILITY)
    reg.seal()
    monkeypatch.setattr(registry_module, "_active", reg)
    with Session(engine) as db:
        # Any hidden resource ORM lookup is a regression, including a lookup of an absent Staff.
        original_get = db.get
        def no_resource_get(model, *args, **kwargs):
            if model.__name__ in {"Tool", "GeneralSkill", "KnowledgeBase", "AgentProfile", "User", "Skill", "MCPServer"}:
                raise AssertionError("host must not read local resource rows")
            return original_get(model, *args, **kwargs)
        monkeypatch.setattr(db, "get", no_resource_get)
        snapshot = CompositionCompiler().compile(staff(), generation=reg.generation)
        identity = SecurityContext("u", "t")
        host = CapabilityHost(db, Guard("test", build_oss_local_profile()), identity,
            ActivationSlot(snapshot, reg.generation, "turn"), LifecycleFence(reg.generation))
        yield host, catalog, executor
    engine.dispose()


def invocation():
    return ModuleInvocation("call1", "tool", "tool.invoke/v1", {"n": 3},
        InvocationContext("t", "external-staff", "u", "session", "turn", "web"),
        binding_id="external-tool")


def test_external_catalog_discovery_authorization_execution_without_local_rows(remote_host):
    host, _, executor = remote_host
    assert host.describe()["items"][0]["input_schema"]["required"] == ["n"]
    result, receipt = host.invoke(invocation())
    assert result.success and receipt.status == "completed"
    assert executor.calls == 1
    assert host.results[-1]["tool_name"] == "Remote tool"


@pytest.mark.parametrize("change,code", [
    ({"available": False}, "RESOURCE_UNAVAILABLE"),
    ({"digest": "version2"}, "CAPABILITY_SNAPSHOT_CHANGED"),
    ({"ref": ResourceRef("tool", "external-tool", "another")}, "RESOURCE_CONTRACT_INVALID"),
    ({"ref": ResourceRef("tool", "external-tool", "t", {"binding_status": "disabled"})}, "PERMISSION_DENIED"),
])
def test_catalog_failure_never_dispatches(remote_host, change, code):
    host, catalog, executor = remote_host
    catalog.value = replace(catalog.value, **change)
    result, _ = host.invoke(invocation())
    assert not result.success and result.error["code"] == code
    assert executor.calls == 0


def test_external_schema_is_enforced_before_dispatch(remote_host):
    host, _, executor = remote_host
    result, _ = host.invoke(replace(invocation(), arguments={"n": "wrong"}))
    assert not result.success and result.error["code"] == "INVALID_ARGUMENTS"
    assert executor.calls == 0


def test_staff_source_replacement_never_calls_local_projection(monkeypatch):
    from staffdeck_harness.composition.sources import resolve_staff
    from staffdeck_harness.composition import staff as local
    monkeypatch.setattr(local, "project_staff", lambda *a, **kw: pytest.fail("local projection"))
    value = staff()
    reg = ModuleRegistry()
    for name, slot, provider in (
        ("test.staff", SlotName.STAFF_SOURCE, SimpleNamespace(reference=lambda c: value.ref, resolve=lambda c: value, model=lambda *a: None)),
        ("test.sop", SlotName.SOP_SOURCE, SimpleNamespace(resolve=lambda c, s: (), reference=lambda *a: None)),
        ("test.identity", SlotName.IDENTITY_SOURCE, SimpleNamespace(resolve=lambda c, i: SecurityContext("u", "t"))),
    ):
        reg.install(manifest(name, name, kind=ModuleKind.TRUSTED, slots=[slot]),
            SimpleNamespace(build=lambda db, p=provider: p), slot=slot)
    reg.seal()
    actual, identity = resolve_staff(reg, None, SourceContext("t", "external-staff", user_id="u"), build_oss_local_profile())
    assert actual.staff_id == value.staff_id and identity.principal_id == "u"


def test_public_source_contracts_do_not_import_application_or_orm():
    result = subprocess.run(
        [sys.executable, "-c", (
            "import sys; import staffdeck_harness.contracts.sources; "
            "import staffdeck_harness.sop.contracts; "
            "import staffdeck_harness.composition.compiler; "
            "assert 'app.db.models' not in sys.modules; "
            "assert 'sqlmodel' not in sys.modules"
        )], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_sop_definition_and_grants_stay_pinned_per_instance():
    from staffdeck_harness.composition.pins import pin_sop
    from staffdeck_harness.composition.compiler import SopExecutionPlan
    from staffdeck_harness.contracts.sop import SopDefinition
    snapshot = CompositionCompiler(hooks=()).compile(staff())
    plan = SopExecutionPlan("flow", "1", "Flow", {"nodes": []}, (), ())
    snapshot = replace(snapshot, sops=(plan,))
    session = SimpleNamespace(id="s", tenant_id="t", context_state_json={})
    row = SimpleNamespace(kind="sop", task_id="task1", skill_id="flow")
    skill = SopDefinition("row", "t", "flow", "1", "Flow", {"nodes": []})
    first, _ = pin_sop(snapshot, staff(), session, row, skill)
    newer = replace(snapshot, sops=(replace(plan, version="2", content={"nodes": [{"node_id": "new"}]}),))
    second, resumed = pin_sop(newer, staff(), session, row, replace(skill, version="2"))
    assert second.snapshot_id == first.snapshot_id
    assert resumed.version == "1" and resumed.content_json == {"nodes": []}
    with pytest.raises(Exception, match="解绑"):
        pin_sop(newer, replace(staff(), capabilities=()), session, row, skill)


def test_sop_module_cannot_mutate_identity_or_apply_events_before_validation():
    from staffdeck_harness.sop.host import SopHost
    from staffdeck_harness.sop.contracts import SopDependencies
    from staffdeck_harness.sop.lifecycle import SopRuntime
    from staffdeck_harness.contracts.sop import SopState
    events = []
    class BadRuntime(SopRuntime):
        def complete_current_skill(self, state):
            assert isinstance(state, SopState)
            state.slots_json["bad"] = True
            state.tenant_id = "other"
            self.events.record("t", "s", "changed", {})
    reg = ModuleRegistry()
    reg.install(manifest("test.bad_sop", "Bad", kind=ModuleKind.TRUSTED,
        slots=[SlotName.RUNTIME_SOP], provides=["sop.lifecycle/v2"]),
        SimpleNamespace(build=lambda deps: BadRuntime(None, deps.events, create_handoff=deps.create_handoff)),
        slot=SlotName.RUNTIME_SOP)
    reg.seal()
    host = SopHost(SopDependencies(SimpleNamespace(record=lambda *a: events.append(a)), lambda *a: None), reg)
    session = SopState("s", "t")
    with pytest.raises(Exception, match="更改执行身份"):
        host.complete_current_skill(session)
    assert session.tenant_id == "t" and session.slots_json == {} and events == []


def test_legacy_sop_plugin_contract_rejected_at_registration():
    from staffdeck_harness.contracts.errors import ContractIncompatible
    with pytest.raises(ContractIncompatible):
        ModuleRegistry().install(manifest("test.old_sop", "Old", kind=ModuleKind.TRUSTED,
            slots=[SlotName.RUNTIME_SOP], provides=["sop.lifecycle/v1"]),
            object(), slot=SlotName.RUNTIME_SOP)


def test_workload_principal_is_not_silently_sent_as_user():
    from staffdeck_harness.security.business_base import BasePep
    client = SimpleNamespace(check=lambda *a: pytest.fail("must not issue user check"))
    decision = BasePep(client).authorize(SecurityContext("worker", "t", principal_type="workload",
        actor_user_id="u", agent_id="a", run_id="r"), "test", "use", ResourceRef("agent", "a", "t"))
    assert not decision.allowed and "delegated" in decision.reason


def test_handoff_effect_returns_durable_id_after_host_applies_transition():
    from staffdeck_harness.sop.host import SopHost
    from staffdeck_harness.sop.contracts import SopDependencies
    from staffdeck_harness.contracts.sop import SopDefinition, SopState
    from app.core.task_request_compiler import TaskExecutionResult, TaskRequirement
    from app.session.session_schema import RouterDecision
    def create_handoff(tenant, session, skill, step):
        session.awaiting_input_json = {"handoff_id": "handoff-new"}
        session.status = "handoff"
    host = SopHost(SopDependencies(SimpleNamespace(record=lambda *a: None), create_handoff))
    skill = SopDefinition("row", "t", "flow", "1", "Flow",
        {"nodes": [{"node_id": "human", "type": "handoff"}], "start_node_id": "human"})
    session = SopState("s", "t", active_skill_id="flow", active_step_id="human")
    result = TaskExecutionResult(task_frame_id="task", status="handoff")
    host.after_execution("t", session, skill, TaskRequirement(task_frame_id="task", kind="sop", goal="handoff"),
        result, RouterDecision(decision="handoff_human"), remaining_actions=3)
    assert result.artifacts == [{"type": "human_handoff", "handoff_id": "handoff-new"}]
