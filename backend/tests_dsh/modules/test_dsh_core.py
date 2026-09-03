"""Module test: ``dsh.core`` (Harness v3 引擎核心, kernel, slot runtime.kernel).

Provider ``DshCoreModule`` represents the external Node DSH engine itself. Its
only surface is ``version()``, read from ``<root>/apps/cli/package.json`` — the
module is registered so the tree is complete and the engine core can be
switched off with ``dsh_enabled``, never replaced by a plugin.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from staffdeck_dsh.contracts.errors import PermissionDenied
from staffdeck_dsh.contracts.manifest import ModuleKind, SlotName
from staffdeck_dsh.contracts.security import ResourceRef
from staffdeck_dsh.modules.kernel import DshCoreModule
from staffdeck_dsh.modules.registry import ModuleRegistry, discover_and_install
from staffdeck_dsh.modules.taxonomy import tree

MODULE_ID = "dsh.core"
CJK = re.compile(r"[一-鿿]")
REPO_ROOT = Path(__file__).resolve().parents[3]
VENDORED_DSH = REPO_ROOT / ".codex-tmp" / "dsh-inspect" / "deepseek-harness-dsh-v0.1.2-alpha.2"


def _described(registry, module_id: str = MODULE_ID) -> dict:
    hit = [m for m in registry.describe() if m["module_id"] == module_id]
    assert len(hit) == 1
    return hit[0]


def _placed(registry, module_id: str = MODULE_ID) -> tuple[dict, dict, dict]:
    for big in tree(registry.describe()):
        for sub in big["subs"]:
            for m in sub["modules"]:
                if m["module_id"] == module_id:
                    return big, sub, m
    raise AssertionError(f"{module_id} not placed")


def _guarded_registry(settings) -> ModuleRegistry:
    reg = discover_and_install(ModuleRegistry(), settings)
    for slot in SlotName:
        reg.mark_guarded(slot)
    reg.seal()
    return reg


@pytest.fixture(autouse=True)
def _no_dsh_root_env(monkeypatch):
    # version() falls back to $DSH_ROOT when the module was built without a root; keep the developer's env out
    monkeypatch.delenv("DSH_ROOT", raising=False)


@pytest.fixture
def fake_root(tmp_path) -> Path:
    root = tmp_path / "dsh-root"
    (root / "apps" / "cli").mkdir(parents=True)
    (root / "apps" / "cli" / "package.json").write_text(json.dumps({"name": "@deepseek-ai/dsh", "version": "9.9.9-test.1"}))
    return root


# --------------------------------------------------------------------------- 1. manifest


def test_manifest_dsh_core(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind is ModuleKind.KERNEL
    assert item.slot is SlotName.RUNTIME_KERNEL
    assert m.attaches_to == (SlotName.RUNTIME_KERNEL,)
    assert m.provides_operations == ()
    assert m.requires_operations == ()
    assert m.policy_actions == ()
    assert m.hooks == ()
    assert isinstance(item.provider, DshCoreModule)
    assert item.provider.module_id == MODULE_ID
    assert item.provider.root == "", "root comes from settings.dsh_root (empty in FakeSettings)"
    assert item.enabled is False, "dsh_enabled=False → engine core installed but inactive"

    d = _described(registry)
    assert d["kind"] == "K"
    assert d["slot"] == "runtime.kernel"
    assert d["name"] == "Harness v3 引擎核心"
    assert d["summary"] and CJK.search(d["summary"])
    assert re.match(r"^\d+\.\d+\.\d+$", d["version"])
    assert d["contract_version"] == "v1"
    assert d["provides"] == [] and d["requires"] == [] and d["policy_actions"] == [] and d["hooks"] == []
    assert d["guarded"] is True  # fixture marks every slot guarded; no policy actions of its own
    assert d["enabled"] is False
    assert d["source"] == "builtin"


def test_manifest_dsh_core_root_and_enabled_follow_settings(settings):
    settings.dsh_enabled = True
    settings.dsh_root = "/opt/dsh"
    reg = _guarded_registry(settings)
    item = reg.get(MODULE_ID)
    assert item is not None and item.enabled is True
    assert item.provider.root == "/opt/dsh"
    assert [i.manifest.module_id for i in reg.providers(SlotName.RUNTIME_KERNEL)] == ["runtime.coordinator", "dsh.core"]


# --------------------------------------------------------------------------- 2. placement


def test_placement_dsh_core(registry):
    big, sub, m = _placed(registry)
    assert big["id"] == "runtime"
    assert sub["id"] == "runtime.agentloop"
    assert sub["kind"] == "K"
    assert sub["slots"] == [], "placed by explicit module id, not by slot"
    assert m["placement"] == {"big_id": "runtime", "sub_id": "runtime.agentloop", "source": "taxonomy"}
    assert m["switchable"] is False
    assert m["movable"] is False
    assert sub["total"] == 1 and sub["enabled"] == 0


def test_placement_dsh_core_not_under_coordinator_sub(registry):
    """runtime.kernel slot would default to runtime.coordinator; the taxonomy pins dsh.core elsewhere."""

    _, sub, _ = _placed(registry)
    assert sub["id"] != "runtime.coordinator"
    coordinator_sub = next(s for b in tree(registry.describe()) for s in b["subs"] if s["id"] == "runtime.coordinator")
    assert MODULE_ID not in [x["module_id"] for x in coordinator_sub["modules"]]


# --------------------------------------------------------------------------- 3. disable


def test_disable_dsh_core_not_switchable(registry):
    d = _described(registry)
    assert d["kind"] == "K"
    assert d["switchable"] is False, "admin refuses: 是平台核心组成部分，不能停用"
    assert d["metadata"].get("switchable") is None


def test_disable_dsh_core_is_toggled_by_dsh_enabled_only(settings):
    settings.dsh_enabled = False
    assert _guarded_registry(settings).get(MODULE_ID).enabled is False
    settings.dsh_enabled = True
    assert _guarded_registry(settings).get(MODULE_ID).enabled is True


# --------------------------------------------------------------------------- 4. provider


def test_provider_dsh_core_version_unknown_when_root_missing(module):
    provider = module(MODULE_ID).provider
    assert provider.root == ""
    assert provider.version() == "unknown"
    assert DshCoreModule("/definitely/not/a/dsh/checkout").version() == "unknown"


def test_provider_dsh_core_version_reads_package_json(fake_root):
    assert DshCoreModule(str(fake_root)).version() == "9.9.9-test.1"


def test_provider_dsh_core_version_falls_back_to_env_root(monkeypatch, fake_root):
    monkeypatch.setenv("DSH_ROOT", str(fake_root))
    assert DshCoreModule("").version() == "9.9.9-test.1"
    # an explicit root wins over the environment
    assert DshCoreModule("/nope").version() == "unknown"


def test_provider_dsh_core_version_tolerates_bad_package_json(tmp_path):
    root = tmp_path / "broken"
    (root / "apps" / "cli").mkdir(parents=True)
    (root / "apps" / "cli" / "package.json").write_text("{not json")
    assert DshCoreModule(str(root)).version() == "unknown"
    (root / "apps" / "cli" / "package.json").write_text(json.dumps({"name": "x"}))
    assert DshCoreModule(str(root)).version() == "unknown", "package.json without a version → unknown"


@pytest.mark.skipif(not (VENDORED_DSH / "apps" / "cli" / "package.json").is_file(), reason="vendored DSH checkout not present")
def test_provider_dsh_core_version_of_vendored_checkout():
    assert DshCoreModule(str(VENDORED_DSH)).version() == "0.1.2-alpha.2"


def test_provider_dsh_core_version_is_what_the_registry_reports(settings, fake_root):
    settings.dsh_enabled = True
    settings.dsh_root = str(fake_root)
    reg = _guarded_registry(settings)
    assert reg.get(MODULE_ID).provider.version() == "9.9.9-test.1"


# --------------------------------------------------------------------------- 5. events / pep


def test_events_or_pep_dsh_core_has_no_policy_actions_and_cannot_bypass_guard(registry, guard, security_ctx):
    d = _described(registry)
    assert d["policy_actions"] == [], "the engine core is guarded by the engine.dsh bridge (staff.use/v1), not by itself"
    # any operation the core could try to run through a Guard is either mapped (and tenant-bound) or refused outright
    g = guard(MODULE_ID)
    with pytest.raises(PermissionDenied) as exc:
        g.require(security_ctx(tenant_id="t1"), "staff.use/v1", ResourceRef(type="agent", id="a1", tenant_id="t2"))
    assert exc.value.details["profile"] == "OSS_LOCAL"
    with pytest.raises(KeyError):
        g.require(security_ctx(tenant_id="t1"), "dsh.core/v1", ResourceRef(type="runtime", id="x", tenant_id="t1"))
