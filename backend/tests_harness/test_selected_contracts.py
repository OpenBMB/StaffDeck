from types import SimpleNamespace as NS
import pytest
from staffdeck_harness.contracts.errors import ContractIncompatible
from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.modules.registry import ModuleRegistry, manifest
from staffdeck_harness.modules.compatibility import validate_compatibility


def install(registry, name, slot, metadata, provider=None):
    registry.install(manifest(name, name, kind=ModuleKind.TRUSTED, slots=[slot], metadata=metadata),
        provider or NS(), slot=slot)


def test_contract_can_be_satisfied_by_an_independently_named_implementation():
    registry = ModuleRegistry()
    install(registry, 'customer.staff', SlotName.STAFF_SOURCE,
        {'exports_features': ['staff.binding-management/v1']}, NS(binding_manager=lambda *args: None))
    install(registry, 'customer.knowledge', SlotName.MODULE_MANAGEMENT,
        {'management_domain': 'knowledge', 'requires_slot_features': {SlotName.STAFF_SOURCE.value: ['staff.binding-management/v1']}})
    assert 'staff.binding-management/v1' in validate_compatibility(registry)['features']


def test_export_on_an_unrelated_module_cannot_satisfy_selected_source_contract():
    registry = ModuleRegistry()
    install(registry, 'customer.staff', SlotName.STAFF_SOURCE, {})
    install(registry, 'customer.other', SlotName.RUNTIME_KERNEL,
        {'exports_features': ['staff.sop-references/v1']})
    install(registry, 'customer.sop', SlotName.SOP_SOURCE,
        {'requires_slot_features': {SlotName.STAFF_SOURCE.value: ['staff.sop-references/v1']}})
    with pytest.raises(ContractIncompatible):
        validate_compatibility(registry)


def test_declared_binding_contract_requires_an_actual_entrypoint():
    registry = ModuleRegistry()
    install(registry, 'customer.staff', SlotName.STAFF_SOURCE,
        {'exports_features': ['staff.binding-management/v1']})
    with pytest.raises(ContractIncompatible):
        validate_compatibility(registry)


@pytest.mark.parametrize('namespace', ['base', 'local'])
def test_assembly_chooses_implementation_and_checks_resource_namespace(monkeypatch, namespace):
    from staffdeck_harness.composition.compiler import CompositionCompiler
    from staffdeck_harness.contracts.staff import StaffComposition, SessionPolicy, CapabilityBindingView
    from staffdeck_harness.contracts.security import ResourceRef
    registry = ModuleRegistry()
    registry.install(manifest('customer.tool', 'Alternative tool', kind=ModuleKind.CODE,
        slots=[SlotName.STAFF_CAPABILITY], provides=['tool.invoke/v1'],
        metadata={'resource_namespaces': [namespace]}), NS(), slot=SlotName.STAFF_CAPABILITY)
    monkeypatch.setattr('staffdeck_harness.modules.registry._active', registry)
    capability = CapabilityBindingView('tool', 'remote', None, ResourceRef('tool', 'remote', 'tenant'),
        'remote', metadata={'resource_namespace': 'base'})
    staff = StaffComposition('tenant', 'staff', 'Staff', False, 'active', None, {}, SessionPolicy(),
        (capability,), (), (), None, (), ResourceRef('agent', 'staff', 'tenant'))
    if namespace == 'local':
        with pytest.raises(ContractIncompatible):
            CompositionCompiler(hooks=()).compile(staff)
    else:
        assert CompositionCompiler(hooks=()).compile(staff).grants[0].provider_module_id == 'customer.tool'


def test_catalog_selected_by_contract_not_builtin_module_id():
    registry = ModuleRegistry()
    install(registry, 'customer.catalog', SlotName.RESOURCE_CATALOG,
        {'exports_features': ['catalog.descriptor.base/v1']})
    install(registry, 'customer.consumer', SlotName.STAFF_CAPABILITY,
        {'catalog_contract': 'catalog.descriptor.base/v1'})
    assert registry.resolve_catalog_provider(registry.get('customer.consumer')).manifest.module_id == 'customer.catalog'
    install(registry, 'customer.ambiguous', SlotName.RESOURCE_CATALOG,
        {'exports_features': ['catalog.descriptor.base/v1']})
    with pytest.raises(ContractIncompatible):
        registry.resolve_catalog_provider(registry.get('customer.consumer'))
