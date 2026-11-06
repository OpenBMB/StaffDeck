"""A SOP's delegated resource configuration must not become another SOP's default."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from staffdeck_harness.composition.compiler import CompositionCompiler
from staffdeck_harness.composition.slots import sop_slots
from staffdeck_harness.composition.sources import combined_capabilities
from staffdeck_harness.contracts.errors import ContractIncompatible, PermissionDenied
from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.contracts.security import ResourceRef
from staffdeck_harness.contracts.staff import (
    CapabilityBindingView,
    SessionPolicy,
    SopView,
    StaffComposition,
)
from staffdeck_harness.modules.registry import ModuleRegistry, manifest


@pytest.fixture
def registry(monkeypatch):
    value = ModuleRegistry()
    for name, namespace in (("default", "base"), ("first", "base"),
                            ("second", "base"), ("slot", "base"), ("local", "local")):
        value.install(manifest(f"tool.{name}", name, kind=ModuleKind.CODE,
            slots=[SlotName.STAFF_CAPABILITY], provides=["tool.invoke/v1"],
            metadata={"resource_namespaces": [namespace]}),
            SimpleNamespace(), slot=SlotName.STAFF_CAPABILITY)
    monkeypatch.setattr("staffdeck_harness.modules.registry._active", value)
    return value


def capability(name, *, scope="sop_specific", metadata=None):
    return CapabilityBindingView("tool", "shared", f"binding-{name}",
        ResourceRef("tool", "shared", "tenant"), f"Tool {name}", scope,
        metadata if metadata is not None else {
            "provider_module_id": f"tool.{name}", "resource_namespace": "base",
            "resource_digest": f"digest-{name}", "provider_config": {"value": name},
        })


def sop(name, cap, *, slot_metadata=None):
    content = {"nodes": [{"node_id": "call", "metadata": {"slots": [
        {"name": "action", "operation": "tool.invoke/v1", "required": True}]} }]}
    return SopView(name, name, "1", name, content, None,
        {"action": {"resource_id": "shared", **(slot_metadata or {})}},
        tuple(sop_slots(content)), ResourceRef("sop", name, "tenant"), capabilities=(cap,))


def staff(sops, capabilities=()):
    value = StaffComposition("tenant", "staff", "Staff", False, "active", None,
        {}, SessionPolicy(), tuple(capabilities), tuple(sops), (), None, (),
        ResourceRef("agent", "staff", "tenant"))
    return replace(value, capabilities=combined_capabilities(value, value.sops))


def scoped_grants(value):
    return {grant.sop_id: grant for grant in CompositionCompiler(hooks=()).compile(value).grants
            if grant.scope == "sop_specific"}


@pytest.mark.parametrize("reverse", [False, True])
def test_same_resource_retains_each_sop_provider_digest_config_and_binding(registry, reverse):
    definitions = [sop("a", capability("first")), sop("b", capability("second"))]
    value = staff(definitions[::-1] if reverse else definitions)
    grants = scoped_grants(value)
    for sop_id, name in (("a", "first"), ("b", "second")):
        grant = grants[sop_id]
        assert (grant.provider_module_id, grant.resource_digest, grant.provider_config,
                grant.binding_id, grant.name) == (
            f"tool.{name}", f"digest-{name}", {"value": name}, f"binding-{name}", f"Tool {name}")
    snapshot = CompositionCompiler(hooks=()).compile(value)
    assert snapshot.allowed_resource_ids() == {}
    assert snapshot.allowed_resource_ids(sop_id="a", node_id="call") == {"tool": {"shared"}}
    assert snapshot.allowed_resource_ids(sop_id="b", node_id="call") == {"tool": {"shared"}}


def test_unpinned_sop_uses_assembly_default_not_its_sibling_pin(registry):
    second = capability("second", metadata={"resource_namespace": "base"})
    value = staff([sop("a", capability("first")), sop("b", second)])
    grant = scoped_grants(value)["b"]
    assert grant.provider_module_id == "tool.default"
    assert grant.resource_digest is None
    assert grant.provider_config == {}
    assert grant.binding_id == "binding-second"


def test_sop_configuration_overrides_staff_defaults_without_changing_general_grant(registry):
    value = staff([sop("a", capability("second"))], [capability("first", scope="general")])
    snapshot = CompositionCompiler(hooks=()).compile(value)
    general, delegated = snapshot.grants
    assert general.provider_module_id == "tool.first"
    assert general.resource_digest == "digest-first"
    assert general.binding_id == "binding-first"
    assert delegated.provider_module_id == "tool.second"
    assert delegated.resource_digest == "digest-second"
    assert delegated.provider_config == {"value": "second"}
    assert delegated.binding_id == "binding-second"
    assert snapshot.grants_for(sop_id="a", node_id="call") == (delegated,)


@pytest.mark.parametrize("config", [{"value": "slot"}, {}])
def test_explicit_slot_overrides_sop_and_can_clear_provider_config(registry, config):
    value = staff([sop("a", capability("first"), slot_metadata={
        "provider_module_id": "tool.slot", "resource_digest": "digest-slot",
        "provider_config": config, "binding_id": "binding-slot",
        "module_version": registry.get("tool.slot").manifest.version,
    })])
    grant = scoped_grants(value)["a"]
    assert grant.provider_module_id == "tool.slot"
    assert grant.resource_digest == "digest-slot"
    assert grant.provider_config == config
    assert grant.binding_id == "binding-slot"


@pytest.mark.parametrize("origin", ["sop", "slot"])
def test_current_sop_namespace_is_checked_after_selecting_its_provider(registry, origin):
    cap = capability("second")
    overrides = {"provider_module_id": "tool.local"}
    if origin == "sop":
        cap = replace(cap, metadata={**cap.metadata, **overrides})
        overrides = {}
    value = staff([sop("a", capability("first")), sop("b", cap, slot_metadata=overrides)])
    with pytest.raises(ContractIncompatible, match="命名空间"):
        scoped_grants(value)


@pytest.mark.parametrize("origin", ["sop", "slot"])
def test_current_sop_requested_module_version_remains_enforced(registry, origin):
    cap = capability("second")
    overrides = {"module_version": "999.0.0"}
    if origin == "sop":
        cap = replace(cap, metadata={**cap.metadata, **overrides})
        overrides = {}
    with pytest.raises(ContractIncompatible, match="version unavailable"):
        scoped_grants(staff([sop("a", cap, slot_metadata=overrides)]))


def test_combined_view_preserves_ids_without_promoting_sop_execution_metadata():
    value = staff([sop("a", capability("first")), sop("b", capability("second"))])
    assert value.visible_resource_ids() == {"tool": {"shared"}, "skill": {"a", "b"}}
    assert len(value.capabilities) == 1
    assert value.capabilities[0].metadata == {}
    assert value.capabilities[0].binding_id is None
    assert value.sops[0].capabilities[0].metadata["provider_module_id"] == "tool.first"


def test_custom_operation_contract_survives_visibility_projection(registry):
    from staffdeck_harness.contracts.operations import OperationContract

    operation = "custom.query/v1"
    registry.register_operation(OperationContract(operation, "custom_resource", "query"))
    registry.install(manifest("custom.executor", "Custom", kind=ModuleKind.CODE,
        slots=[SlotName.STAFF_CAPABILITY], provides=[operation]),
        SimpleNamespace(), slot=SlotName.STAFF_CAPABILITY)
    cap = replace(capability("first"), resource_type="custom_resource",
        ref=ResourceRef("custom_resource", "shared", "tenant"),
        metadata={"operation": operation, "provider_module_id": "custom.executor"})
    content = {"nodes": [{"node_id": "call", "metadata": {"slots": [
        {"name": "action", "operation": operation, "required": True}]}}]}
    definition = replace(sop("custom", cap), content=content,
        declared_slots=tuple(sop_slots(content)))
    value = staff([definition])
    assert value.capabilities[0].metadata == {"operation": operation}
    assert scoped_grants(value)["custom"].provider_module_id == "custom.executor"


@pytest.mark.parametrize("change", [
    {"ref": ResourceRef("tool", "shared", "other")},
    {"capability_scope": "general"},
    {"ref": ResourceRef("tool", "different", "tenant")},
])
def test_combined_view_still_rejects_invalid_delegation(change):
    with pytest.raises(PermissionDenied):
        staff([sop("a", replace(capability("first"), **change))])
