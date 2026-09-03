"""Module Registry: the one place modules are installed, validated and resolved.

Everything that the architecture calls "pluggable" registers here with a
``ModuleManifest`` plus its implementation object, keyed by the slot it attaches
to. Hosts never import a concrete module; they ask the registry for the
provider bound to a slot (or the ordered providers for a multi-slot).

Rules enforced at ``seal()`` (startup) — a violation aborts boot, which is the
acceptance criterion "缺少 PEP Binding 的模块无法启动或注册":

- every module that declares ``policy_actions`` must be attached under a host
  that holds a ``Guard`` (we record the guard per slot when the host installs);
- ``requires_operations`` must be satisfied by some registered provider;
- contract versions must be in ``SUPPORTED_CONTRACTS``;
- single-provider slots (``runtime.engine``, ``security.pep``) accept exactly
  one active module;
- module ids are unique.

Plugins are discovered from three sources, in order:
1. built-ins (``staffdeck_harness.modules.builtin``),
2. Python entry points in group ``staffdeck_harness.modules``,
3. the ``STAFFDECK_HARNESS_MODULES`` env var / ``harness_modules`` setting
   (comma-separated ``pkg.module:register`` callables).

Registration is configuration, not runtime hot swapping: after ``seal()`` the
registry is immutable; changing modules means a new worker generation.
"""

from __future__ import annotations

import importlib
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from importlib import metadata as importlib_metadata
from typing import Any, Callable, Iterable, Mapping

from staffdeck_harness.contracts.errors import ContractIncompatible, ModuleSdkError, PepBindingMissing
from staffdeck_harness.contracts.manifest import HookContribution, ModuleKind, ModuleManifest, SlotName

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "staffdeck_harness.modules"
ENV_MODULES = "STAFFDECK_HARNESS_MODULES"

SINGLE_PROVIDER_SLOTS = {SlotName.RUNTIME_ENGINE, SlotName.SECURITY_PEP}

# "pkg.mod:register" (attr optional, defaults to ``register``)
SPEC_RE = re.compile(r"^[A-Za-z_][\w]*(\.[A-Za-z_]\w*)*(:[A-Za-z_]\w*)?$")
MODULE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
SEMVER_RE = re.compile(r"^\d+(\.\d+){0,2}(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$")
SUPPORTED_CONTRACT_VERSIONS = {"v1"}

# operation name -> supported versions (shared with the composition compiler)
SUPPORTED_CONTRACTS: dict[str, set[str]] = {
    "knowledge.search": {"v1"},
    "general_skill.consume": {"v1"},
    "tool.invoke": {"v1"},
    "mcp.invoke": {"v1"},
    "a2a.invoke": {"v1"},
    "sandbox.execute": {"v1"},
    "artifact.publish": {"v1"},
    "handoff.request": {"v1"},
    "handoff.assign": {"v1"},
    "handoff.reply": {"v1"},
    "sop.execute": {"v1"},
    "channel.receive": {"v1"},
    "channel.send": {"v1"},
    "memory.read": {"v1"},
    "memory.write": {"v1"},
    "team.delegate": {"v1"},
    "model.use": {"v1"},
    "runtime.turn": {"v1"},
    "capability.describe": {"v1"},
    "task.finish": {"v1"},
    "event.observe": {"v1"},
    "hook.contribute": {"v1"},
    "knowledge.import": {"v1"},
}


class RegistrySealed(ModuleSdkError):
    code = "REGISTRY_SEALED"


class DuplicateModule(ModuleSdkError):
    code = "DUPLICATE_MODULE"


class SlotConflict(ModuleSdkError):
    code = "SLOT_CONFLICT"


class UnsatisfiedRequirement(ModuleSdkError):
    code = "UNSATISFIED_REQUIREMENT"


@dataclass(frozen=True)
class Installed:
    manifest: ModuleManifest
    provider: Any
    slot: SlotName
    enabled: bool = True
    config: Mapping[str, Any] = field(default_factory=dict)
    source: str = "builtin"
    spec: str = "builtin"        # the discovery context (builtin / entry_point:<name> / "pkg.mod:register")


class ModuleRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_id: dict[str, Installed] = {}
        self._by_slot: dict[SlotName, list[Installed]] = {}
        self._guarded_slots: set[SlotName] = set()
        self._sealed = False
        self.generation = 0
        self._install_source = "builtin"

    # -- install -------------------------------------------------------------------

    def install(self, manifest: ModuleManifest, provider: Any, *, slot: SlotName, enabled: bool = True, config: Mapping[str, Any] | None = None, source: str | None = None) -> Installed:
        with self._lock:
            if self._sealed:
                raise RegistrySealed("module registry is sealed; changing modules requires a new worker generation")
            if not MODULE_ID_RE.match(manifest.module_id):
                raise ContractIncompatible(f"invalid module_id {manifest.module_id!r}: expected lowercase dotted name like vendor.name", details={"module_id": manifest.module_id})
            if not SEMVER_RE.match(manifest.version):
                raise ContractIncompatible(f"module {manifest.module_id}: version {manifest.version!r} is not semver", details={"version": manifest.version})
            if manifest.contract_version not in SUPPORTED_CONTRACT_VERSIONS:
                raise ContractIncompatible(f"module {manifest.module_id}: contract_version {manifest.contract_version!r} unsupported", details={"supported": sorted(SUPPORTED_CONTRACT_VERSIONS)})
            if manifest.module_id in self._by_id:
                raise DuplicateModule(f"module already installed: {manifest.module_id}")
            if slot not in manifest.attaches_to:
                raise SlotConflict(f"module {manifest.module_id} does not declare slot {slot.value}", details={"declared": [s.value for s in manifest.attaches_to]})
            for op in (*manifest.provides_operations, *manifest.requires_operations):
                name, _, version = op.partition("/")
                if name not in SUPPORTED_CONTRACTS or version not in SUPPORTED_CONTRACTS[name]:
                    raise ContractIncompatible(f"module {manifest.module_id}: unsupported contract {op}", details={"operation": op})
            # The registrar currently running decides the source unless the caller is explicit.
            item = Installed(manifest=manifest, provider=provider, slot=slot, enabled=enabled, config=dict(config or {}), source=source or self._install_source, spec=self._install_source)
            self._by_id[manifest.module_id] = item
            self._by_slot.setdefault(slot, []).append(item)
            return item

    def installing_from(self, source: str):
        """Context manager: every ``install()`` without an explicit ``source`` inside records this source."""

        registry = self

        class _Ctx:
            def __enter__(self_inner):
                self_inner.prev = registry._install_source
                registry._install_source = source
                return registry

            def __exit__(self_inner, *exc):
                registry._install_source = self_inner.prev
                return False

        return _Ctx()

    def mark_guarded(self, slot: SlotName) -> None:
        """A host installed with a Guard declares that this slot is PEP-protected."""

        with self._lock:
            self._guarded_slots.add(slot)

    def seal(self) -> None:
        with self._lock:
            if self._sealed:
                return
            provided = {op for item in self._by_id.values() if item.enabled for op in item.manifest.provides_operations}
            for item in self._by_id.values():
                if not item.enabled:
                    continue
                m = item.manifest
                if m.policy_actions and item.slot not in self._guarded_slots:
                    raise PepBindingMissing(f"module {m.module_id} declares policy actions but slot {item.slot.value} has no PEP-bound host", details={"slot": item.slot.value})
                missing = [op for op in m.requires_operations if op not in provided]
                if missing:
                    raise UnsatisfiedRequirement(f"module {m.module_id} requires unprovided operations: {missing}", details={"missing": missing})
            for slot in SINGLE_PROVIDER_SLOTS:
                active = [i for i in self._by_slot.get(slot, ()) if i.enabled]
                if len(active) > 1:
                    raise SlotConflict(f"slot {slot.value} accepts exactly one active module; got {[i.manifest.module_id for i in active]}")
            self._sealed = True
            self.generation += 1

    @property
    def sealed(self) -> bool:
        return self._sealed

    # -- resolve -------------------------------------------------------------------

    def get(self, module_id: str) -> Installed | None:
        return self._by_id.get(module_id)

    def providers(self, slot: SlotName) -> list[Installed]:
        return [i for i in self._by_slot.get(slot, ()) if i.enabled]

    def provider(self, slot: SlotName) -> Installed | None:
        items = self.providers(slot)
        return items[0] if items else None

    def for_operation(self, operation: str) -> Installed | None:
        for item in self._by_id.values():
            if item.enabled and operation in item.manifest.provides_operations:
                return item
        return None

    def hooks(self) -> tuple[HookContribution, ...]:
        out: list[HookContribution] = []
        for item in self.providers(SlotName.STAFF_INTERACTION):
            out.extend(item.manifest.hooks)
        return tuple(out)

    def hook_handlers(self) -> dict[str, Callable[..., Any]]:
        handlers: dict[str, Callable[..., Any]] = {}
        for item in self.providers(SlotName.STAFF_INTERACTION):
            provided = getattr(item.provider, "handlers", None)
            if isinstance(provided, Mapping):
                handlers.update(provided)
        return handlers

    def describe(self) -> list[dict[str, Any]]:
        out = []
        for item in self._by_id.values():
            m = item.manifest
            out.append({
                "module_id": m.module_id, "name": m.name, "summary": str(m.metadata.get("summary", "")), "version": m.version, "kind": m.kind.value, "contract_version": m.contract_version,
                "slot": item.slot.value, "enabled": item.enabled, "source": item.source, "spec": item.spec,
                "category": str(m.metadata.get("category", "") or ""),
                "switchable": bool(m.kind is ModuleKind.CODE or m.metadata.get("switchable")),
                "metadata": {k: v for k, v in m.metadata.items() if k not in ("summary", "category", "switchable") and isinstance(v, (str, int, float, bool))},
                "provides": list(m.provides_operations), "requires": list(m.requires_operations),
                "hooks": [f"{h.point}:{h.handler}" for h in m.hooks], "policy_actions": list(m.policy_actions),
                "guarded": item.slot in self._guarded_slots,
            })
        return out

    # -- disable/enable (pre-seal only) -----------------------------------------------

    def set_enabled(self, module_id: str, enabled: bool) -> None:
        with self._lock:
            if self._sealed:
                raise RegistrySealed("cannot toggle modules after seal")
            item = self._by_id[module_id]
            replaced = Installed(manifest=item.manifest, provider=item.provider, slot=item.slot, enabled=enabled, config=item.config, source=item.source, spec=item.spec)
            self._by_id[module_id] = replaced
            self._by_slot[item.slot] = [replaced if i.manifest.module_id == module_id else i for i in self._by_slot[item.slot]]


# --------------------------------------------------------------------------- discovery

Registrar = Callable[[ModuleRegistry, Mapping[str, Any]], None]


def _iter_entry_points() -> Iterable[Any]:
    try:
        eps = importlib_metadata.entry_points()
        if hasattr(eps, "select"):
            return eps.select(group=ENTRY_POINT_GROUP)
        return eps.get(ENTRY_POINT_GROUP, [])  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover
        return []


def validate_spec(spec: str) -> str:
    spec = str(spec or "").strip()
    if not SPEC_RE.match(spec):
        raise ValueError(f"module spec must look like package.module:register — got {spec!r}")
    return spec


def _load_callable(spec: str) -> Registrar:
    module_name, _, attr = validate_spec(spec).partition(":")
    mod = importlib.import_module(module_name)
    fn = getattr(mod, attr or "register")
    if not callable(fn):
        raise TypeError(f"{spec} is not callable")
    return fn


def discover_and_install(registry: ModuleRegistry, settings: Any, *, include_builtin: bool = True, extra_specs: Iterable[str] = ()) -> ModuleRegistry:
    """Install builtin modules, entry-point modules and env/setting-listed modules; does not seal."""

    ctx = {"settings": settings, "disabled": set(str(x).strip() for x in str(getattr(settings, "harness_disabled_modules", "") or os.environ.get("STAFFDECK_HARNESS_DISABLED_MODULES", "")).split(",") if str(x).strip())}
    if include_builtin:
        from staffdeck_harness.modules import builtin

        with registry.installing_from("builtin"):
            builtin.register(registry, ctx)
    for ep in _iter_entry_points():
        try:
            fn = ep.load()
            with registry.installing_from(f"entry_point:{ep.name}"):
                fn(registry, ctx)
            logger.info("installed staffdeck_harness modules from entry point %s", ep.name)
        except Exception:
            logger.exception("failed to load staffdeck_harness module entry point %s", getattr(ep, "name", ep))
            raise
    specs = [s.strip() for s in (str(getattr(settings, "harness_modules", "") or os.environ.get(ENV_MODULES, "")).split(",")) if s.strip()]
    specs.extend(extra_specs)
    for spec in specs:
        with registry.installing_from(spec):
            _load_callable(spec)(registry, ctx)
        logger.info("installed staffdeck_harness modules from %s", spec)
    for mid in ctx["disabled"]:
        item = registry.get(mid)
        if item is None:
            continue
        # Engines / PEP providers are chosen, never disabled; kernel pieces cannot be switched off.
        if item.slot in SINGLE_PROVIDER_SLOTS or (item.manifest.kind is ModuleKind.KERNEL and not item.manifest.metadata.get("switchable")):
            logger.warning("ignoring disabled_modules entry %s: module is not switchable", mid)
            continue
        registry.set_enabled(mid, False)
    return registry


_active: ModuleRegistry | None = None
_active_lock = threading.Lock()


class RegistryNotBuilt(ModuleSdkError):
    code = "REGISTRY_NOT_BUILT"


def get_registry(settings: Any = None) -> ModuleRegistry:
    """The active registry.

    Only a caller that *owns* the assembly (``runtime.assembly``) passes
    ``settings`` and may trigger a build. Settings-less callers (hosts, relays,
    hooks) get the active registry or ``RegistryNotBuilt`` — they must never
    silently materialise a default assembly while a restart is in progress.
    """

    global _active
    with _active_lock:
        if _active is None:
            if settings is None:
                raise RegistryNotBuilt("module registry is not built yet (runtime starting or restarting)")
            reg = ModuleRegistry()
            discover_and_install(reg, settings)
            reg.seal()
            _active = reg
        return _active


def peek_registry() -> ModuleRegistry | None:
    """The active registry without building; ``None`` while it is being rebuilt."""

    with _active_lock:
        return _active


def build_registry(settings: Any) -> ModuleRegistry:
    """Build and seal a registry from ``settings`` without touching the active one."""

    reg = ModuleRegistry()
    discover_and_install(reg, settings)
    reg.seal()
    return reg


_generation = 0


def install_registry(reg: ModuleRegistry) -> ModuleRegistry:
    """Atomically make ``reg`` the active registry (used by restart to swap without a gap)."""

    global _active, _generation
    with _active_lock:
        _generation += 1
        reg.generation = max(reg.generation, _generation)
        _active = reg
        return reg


def reset_registry() -> None:
    global _active
    with _active_lock:
        _active = None


def manifest(module_id: str, name: str, *, kind: ModuleKind, slots: Iterable[SlotName], summary: str = "", provides: Iterable[str] = (), requires: Iterable[str] = (), hooks: Iterable[HookContribution] = (), policy_actions: Iterable[str] = (), version: str = "1.0.0", contract_version: str = "v1", metadata: Mapping[str, Any] | None = None) -> ModuleManifest:
    meta = dict(metadata or {})
    if summary:
        meta["summary"] = summary
    return ModuleManifest(
        module_id=module_id, name=name, version=version, kind=kind, contract_version=contract_version,
        attaches_to=tuple(slots), provides_operations=tuple(provides), requires_operations=tuple(requires),
        hooks=tuple(hooks), policy_actions=tuple(policy_actions), metadata=meta,
    )
