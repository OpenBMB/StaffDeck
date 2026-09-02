from __future__ import annotations

import pytest

from staffdeck_dsh.contracts.manifest import SlotName
from staffdeck_dsh.modules import ModuleRegistry, discover_and_install, get_registry, reset_registry
from staffdeck_dsh.modules.builtin import DshBridgeEngine, LegacyEngine
from staffdeck_dsh.modules.registry import RegistrySealed, SlotConflict, UnsatisfiedRequirement


class _Settings:
    security_profile = "OSS_LOCAL"
    dsh_enabled = False
    dsh_disabled_modules = ""
    dsh_modules = ""


def test_builtin_registry_seals_with_exactly_one_engine_and_pep() -> None:
    from staffdeck_dsh.modules import reset_registry
    reset_registry()
    reg = discover_and_install(ModuleRegistry(), _Settings())
    reg.mark_guarded(SlotName.STAFF_CAPABILITY)
    reg.mark_guarded(SlotName.RUNTIME_ENGINE)
    reg.seal()
    engines = reg.providers(SlotName.RUNTIME_ENGINE)
    assert len(engines) == 1 and engines[0].manifest.module_id == "engine.legacy"
    pep = reg.providers(SlotName.SECURITY_PEP)
    assert len(pep) == 1 and pep[0].manifest.module_id == "security.oss_local"
    # every enabled module with policy_actions is under a guarded slot
    for item in reg.providers(SlotName.HANDOFF_REPLY_ENDPOINT):
        assert item.slot in reg._guarded_slots
    assert reg.sealed
    from staffdeck_dsh.contracts.manifest import ModuleManifest

    with pytest.raises(RegistrySealed):
        reg.install(ModuleManifest(module_id="engine.legacy", name="x", version="1", kind="K", contract_version="v1", attaches_to=[SlotName.RUNTIME_ENGINE]), LegacyEngine(), slot=SlotName.RUNTIME_ENGINE)


def test_dsh_enabled_selects_engine_dsh() -> None:
    s = _Settings()
    s.dsh_enabled = True
    reg = discover_and_install(ModuleRegistry(), s)
    reg.mark_guarded(SlotName.STAFF_CAPABILITY)
    reg.mark_guarded(SlotName.RUNTIME_ENGINE)
    reg.seal()
    assert reg.provider(SlotName.RUNTIME_ENGINE).manifest.module_id == "engine.dsh"


def test_business_pep_only_one_active() -> None:
    s = _Settings()
    s.security_profile = "BUSINESS_BASE"
    reg = discover_and_install(ModuleRegistry(), s)
    reg.mark_guarded(SlotName.STAFF_CAPABILITY)
    reg.mark_guarded(SlotName.RUNTIME_ENGINE)
    reg.seal()
    assert reg.provider(SlotName.SECURITY_PEP).manifest.module_id == "security.business_base"


def test_disabled_module_list_toggles_before_seal() -> None:
    s = _Settings()
    s.dsh_disabled_modules = "handoff.notifier.dingtalk,handoff.notifier.wecom"
    reg = discover_and_install(ModuleRegistry(), s)
    notifiers = reg.providers(SlotName.HANDOFF_NOTIFIER)
    enabled = [n.manifest.module_id for n in notifiers]
    assert "handoff.notifier.dingtalk" not in enabled and "handoff.notifier.wecom" not in enabled


def test_conflicting_two_engines_is_rejected() -> None:
    reg = ModuleRegistry()
    reg.install(__import__("staffdeck_dsh.modules.registry", fromlist=["manifest"]).manifest("e1", "E1", kind="K", slots=[SlotName.RUNTIME_ENGINE], provides=["runtime.turn/v1"]), LegacyEngine(), slot=SlotName.RUNTIME_ENGINE)
    reg.install(__import__("staffdeck_dsh.modules.registry", fromlist=["manifest"]).manifest("e2", "E2", kind="T", slots=[SlotName.RUNTIME_ENGINE], provides=["runtime.turn/v1"], policy_actions=["staff.use/v1"]), DshBridgeEngine(), slot=SlotName.RUNTIME_ENGINE)
    reg.mark_guarded(SlotName.RUNTIME_ENGINE)
    with pytest.raises(SlotConflict):
        reg.seal()


def test_module_requiring_unprovided_operation_fails_seal() -> None:
    reg = ModuleRegistry()
    reg.install(__import__("staffdeck_dsh.modules.registry", fromlist=["manifest"]).manifest("needy", "Needy", kind="A", slots=[SlotName.STAFF_INTERACTION], requires=["knowledge.search/v1"]), object(), slot=SlotName.STAFF_INTERACTION)
    with pytest.raises(UnsatisfiedRequirement):
        reg.seal()


def test_operation_resolution_prefers_registry_provider() -> None:
    from app.config import get_settings

    reset_registry()
    reg = get_registry(get_settings())
    assert reg.for_operation("knowledge.search/v1").manifest.module_id == "knowledge.local"
    assert reg.for_operation("tool.invoke/v1").manifest.module_id == "tool.local"
    assert reg.for_operation("sandbox.execute/v1").manifest.module_id == "sandbox.local"
    assert reg.hook_handlers().get("persona") is not None


def test_taxonomy_tree_covers_every_registered_module_once() -> None:
    from staffdeck_dsh.modules.taxonomy import TAXONOMY, tree

    s = _Settings()
    reg = discover_and_install(ModuleRegistry(), s)
    for slot in SlotName:
        reg.mark_guarded(slot)
    reg.seal()
    modules = reg.describe()
    t = tree(modules)
    placed = [m["module_id"] for big in t for sub in big["subs"] for m in sub["modules"]]
    assert sorted(placed) == sorted(m["module_id"] for m in modules), "every module appears exactly once"
    assert [b["id"] for b in t if b["id"] != "unplaced"] == [b.id for b in sorted(TAXONOMY, key=lambda b: b.order)]
    assert not any(b["id"] == "unplaced" for b in t), "no module should be unplaced"
    # every sub-module has at least one registered module behind it (tree is complete)
    empty = [f"{b['id']}/{sub['id']}" for b in t for sub in b["subs"] if sub["total"] == 0]
    assert not empty, f"empty sub-modules: {empty}"
