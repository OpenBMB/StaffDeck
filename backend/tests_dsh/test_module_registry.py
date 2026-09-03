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
    reg.install(__import__("staffdeck_dsh.modules.registry", fromlist=["manifest"]).manifest("test.e1", "E1", kind="K", slots=[SlotName.RUNTIME_ENGINE], provides=["runtime.turn/v1"]), LegacyEngine(), slot=SlotName.RUNTIME_ENGINE)
    reg.install(__import__("staffdeck_dsh.modules.registry", fromlist=["manifest"]).manifest("test.e2", "E2", kind="T", slots=[SlotName.RUNTIME_ENGINE], provides=["runtime.turn/v1"], policy_actions=["staff.use/v1"]), DshBridgeEngine(), slot=SlotName.RUNTIME_ENGINE)
    reg.mark_guarded(SlotName.RUNTIME_ENGINE)
    with pytest.raises(SlotConflict):
        reg.seal()


def test_module_requiring_unprovided_operation_fails_seal() -> None:
    reg = ModuleRegistry()
    reg.install(__import__("staffdeck_dsh.modules.registry", fromlist=["manifest"]).manifest("test.needy", "Needy", kind="A", slots=[SlotName.STAFF_INTERACTION], requires=["knowledge.search/v1"]), object(), slot=SlotName.STAFF_INTERACTION)
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


def test_install_validates_module_identity() -> None:
    from staffdeck_dsh.contracts.errors import ContractIncompatible
    from staffdeck_dsh.modules.registry import manifest as mk

    reg = ModuleRegistry()
    with pytest.raises(ContractIncompatible):
        reg.install(mk("NoDots", "x", kind="A", slots=[SlotName.EVENT_OBSERVER], provides=["event.observe/v1"]), object(), slot=SlotName.EVENT_OBSERVER)
    with pytest.raises(ContractIncompatible):
        reg.install(mk("acme.sink", "x", kind="A", slots=[SlotName.EVENT_OBSERVER], provides=["event.observe/v1"], version="latest"), object(), slot=SlotName.EVENT_OBSERVER)
    with pytest.raises(ContractIncompatible):
        reg.install(mk("acme.sink", "x", kind="A", slots=[SlotName.EVENT_OBSERVER], provides=["event.observe/v1"], contract_version="v9"), object(), slot=SlotName.EVENT_OBSERVER)
    with reg.installing_from("acme_pkg:register"):
        item = reg.install(mk("acme.sink", "x", kind="A", slots=[SlotName.EVENT_OBSERVER], provides=["event.observe/v1"], metadata={"category": "governance.trace", "vendor": "ACME"}), object(), slot=SlotName.EVENT_OBSERVER)
    assert item.source == "acme_pkg:register"
    desc = next(m for m in reg.describe() if m["module_id"] == "acme.sink")
    assert desc["category"] == "governance.trace" and desc["source"] == "acme_pkg:register" and desc["metadata"] == {"vendor": "ACME"} and desc["switchable"] is True


def test_env_snapshot_lets_admin_values_be_cleared_back_to_env(tmp_path) -> None:
    from staffdeck_dsh.modules import config as cfg

    class S:
        dsh_enabled = False
        security_profile = "OSS_LOCAL"
        dsh_disabled_modules = ""
        dsh_modules = ""
        dsh_home = str(tmp_path)
        dsh_runtime_config_path = ""
        base_authz_url = "http://env-base:9200"
        base_authz_decision_token = "ENV-TOKEN"
        base_authz_control_token = ""
        base_authz_timeout_seconds = 3.0
        base_authz_pending_timeout_seconds = 3.0
        base_identity_internal_url = ""
        base_identity_runtime_client_id = ""
        base_identity_runtime_client_secret = ""
        base_workload_identity_audience = "aud"

    s = S()
    cfg.reset_env_snapshot()
    cfg.snapshot_env(s)
    admin = cfg.RuntimeOverrides(base=cfg.BaseConnection(authz_url="http://admin-base:9200", decision_token="ADMIN-TOKEN"))
    cfg.apply_overrides(s, admin)
    assert s.base_authz_url == "http://admin-base:9200" and s.base_authz_decision_token == "ADMIN-TOKEN"
    cleared = cfg.RuntimeOverrides(base=admin.base.merge_update({"authz_url": None, "decision_token": None}))
    eff = cleared.base.effective(s)
    assert eff.authz_url == "http://env-base:9200" and eff.decision_token == "ENV-TOKEN"
    masked = cleared.base.masked(s)
    assert masked["authz_url_source"] == "env" and masked["authz_url"] == "http://env-base:9200"
    cfg.apply_overrides(s, cleared)
    assert s.base_authz_url == "http://env-base:9200" and s.base_authz_decision_token == "ENV-TOKEN"
    cfg.reset_env_snapshot()


def test_preflight_rejects_policy_actions_on_unguarded_slot(tmp_path, monkeypatch) -> None:
    """A module that declares policy_actions on a slot no host guards fails the same way at preflight and at build."""

    import sys

    from staffdeck_dsh.modules import config as cfg
    from staffdeck_dsh.modules.inspect import inspect_spec
    from staffdeck_dsh.runtime.assembly import AssemblyFailed, preflight_assembly

    pkg = tmp_path / "vendor_audit"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "from staffdeck_dsh.contracts.manifest import ModuleKind, SlotName\n"
        "from staffdeck_dsh.modules.registry import manifest\n"
        "class Sink:\n    name='vendor.audit'\n    def on_event(self,*a,**k): pass\n"
        "def register(registry, ctx):\n"
        "    registry.install(manifest('vendor.audit','x',kind=ModuleKind.CODE,slots=[SlotName.EVENT_OBSERVER],provides=['event.observe/v1'],policy_actions=['event.observe/v1']), Sink(), slot=SlotName.EVENT_OBSERVER)\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("vendor_audit", None)

    class S:
        dsh_enabled = False
        security_profile = "OSS_LOCAL"
        dsh_disabled_modules = ""
        dsh_modules = ""
        dsh_home = str(tmp_path)
        dsh_runtime_config_path = str(tmp_path / "rt.json")
        base_authz_url = ""
        base_authz_decision_token = ""
        base_authz_control_token = ""
        base_authz_timeout_seconds = 3.0
        base_authz_pending_timeout_seconds = 3.0
        base_identity_internal_url = ""
        base_identity_runtime_client_id = ""
        base_identity_runtime_client_secret = ""
        base_workload_identity_audience = "aud"

        def model_copy(self, update=None):
            import copy

            v = copy.copy(self)
            for k, val in (update or {}).items():
                setattr(v, k, val)
            return v

    s = S()
    cfg.reset_env_snapshot()
    res = inspect_spec(s, "vendor_audit:register")
    assert res["ok"] is False and any(e["code"] == "PEP_BINDING_MISSING" for e in res["errors"]), res
    wanted = cfg.RuntimeOverrides(extra_modules=["vendor_audit:register"])
    with pytest.raises(AssemblyFailed) as ei:
        preflight_assembly(s, wanted)
    assert "PEP_BINDING_MISSING" in str(ei.value) or "PepBindingMissing" in str(ei.value)
    cfg.reset_env_snapshot()
