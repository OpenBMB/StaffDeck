"""Declarative dependency validation for partial assemblies, without edition switches."""
from __future__ import annotations

from staffdeck_harness.contracts.errors import ContractIncompatible


def validate_compatibility(registry):
    active = [item for item in registry.installed() if item.enabled]
    management_domains = {}
    features = {str(feature) for item in active for feature in item.manifest.metadata.get("exports_features", ())}
    for item in active:
        meta = item.manifest.metadata
        from staffdeck_harness.contracts.manifest import SlotName
        if item.slot == SlotName.MODULE_MANAGEMENT:
            domain = meta.get('management_domain')
            if domain not in {'staff', 'sop', 'skill', 'knowledge', 'tool'}:
                raise ContractIncompatible(f'{item.manifest.name}：必须声明受支持的管理操作域')
            if domain in management_domains:
                raise ContractIncompatible(f'{domain} 管理操作存在多个实现，请明确选择')
            management_domains[domain] = item.manifest.module_id
        missing_slots = [slot for slot in meta.get("requires_slots", ()) if registry.provider(SlotName(slot)) is None]
        if missing_slots:
            raise ContractIncompatible(f"{item.manifest.name}：缺少必要接口 {', '.join(missing_slots)}", details={"module_id": item.manifest.module_id, "missing_slots": missing_slots})
        for slot, required in meta.get('requires_slot_features', {}).items():
            selected = registry.provider(SlotName(slot))
            available = set(selected.manifest.metadata.get('exports_features', ())) if selected else set()
            missing_contracts = set(required) - available
            if missing_contracts:
                raise ContractIncompatible(f'{item.manifest.name}：所选 {slot} 不满足契约 {sorted(missing_contracts)}',
                    details={'module_id': item.manifest.module_id, 'slot': slot,
                             'missing_contracts': sorted(missing_contracts)})
        if 'staff.binding-management/v1' in meta.get('exports_features', ()) and not callable(getattr(item.provider, 'binding_manager', None)):
            raise ContractIncompatible(f'{item.manifest.name}：声明了员工绑定契约但未实现 binding_manager')
        missing = set(meta.get("requires_features", ())) - features
        conflicts = set(meta.get("conflicts_features", ())) & features
        if missing or conflicts:
            providers = {feature: [candidate.manifest.name for candidate in registry.installed()
                          if feature in candidate.manifest.metadata.get("exports_features", ())]
                         for feature in sorted(missing)}
            hint = "；".join(f"缺少 {' / '.join(names) if names else feature}" for feature, names in providers.items())
            raise ContractIncompatible(
                f"{item.manifest.name}：{hint or '存在不兼容模块'}。请调整依赖模块或停用此可选模块。"
                f" ({item.manifest.module_id}: incompatible assembly; missing={sorted(missing)}, conflicts={sorted(conflicts)})",
                details={"module_id": item.manifest.module_id, "missing_features": sorted(missing),
                         "conflicting_features": sorted(conflicts), "missing_providers": providers},
            )
    return {"features": sorted(features)}


def assembly_diff(current, proposed):
    before = {m["module_id"]:m for m in current.describe() if m["enabled"]}
    after = {m["module_id"]:m for m in proposed.describe() if m["enabled"]}
    return {"enable": sorted(after.keys()-before.keys()), "disable": sorted(before.keys()-after.keys()),
            "unchanged": sorted(before.keys() & after.keys())}
