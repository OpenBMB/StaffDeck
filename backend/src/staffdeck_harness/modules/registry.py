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
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib import metadata as importlib_metadata
from typing import Any, Callable, Iterable, Mapping
from staffdeck_harness.contracts.operations import SUPPORTED_CONTRACTS

from staffdeck_harness.contracts.errors import ContractIncompatible, ModuleSdkError, PepBindingMissing
from staffdeck_harness.contracts.manifest import HookContribution, ModuleKind, ModuleManifest, SlotName

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "staffdeck_harness.modules"
ENV_MODULES = "STAFFDECK_HARNESS_MODULES"

SINGLE_PROVIDER_SLOTS = {SlotName.RUNTIME_ENGINE, SlotName.SECURITY_PEP, SlotName.RUNTIME_SOP}

# "pkg.mod:register" (attr optional, defaults to ``register``)
SPEC_RE = re.compile(r"^[A-Za-z_][\w]*(\.[A-Za-z_]\w*)*(:[A-Za-z_]\w*)?$")
MODULE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
SEMVER_RE = re.compile(r"^\d+(\.\d+){0,2}(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$")
SUPPORTED_CONTRACT_VERSIONS = {"v1"}



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
        self._started: list[Installed] = []
        self._disposed = False
        self._accepting = True
        self._turns = 0
        self._calls = 0
        self.operations: dict[str, Any] = {}

    def register_operation(self, contract: Any) -> None:
        from staffdeck_harness.contracts.operations import OperationContract
        from staffdeck_harness.contracts.security import DEFAULT_ACTION_MAP

        if self._sealed:
            raise RegistrySealed("operation catalog is sealed")
        if not isinstance(contract, OperationContract) or "/" not in contract.operation or not contract.action:
            raise ContractIncompatible("operation requires a versioned contract and policy action")
        if contract.operation in self.operations or contract.operation in DEFAULT_ACTION_MAP:
            raise ContractIncompatible(f"operation already defined: {contract.operation}")
        self.operations[contract.operation] = contract

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
                if op not in self.operations and (name not in SUPPORTED_CONTRACTS or version not in SUPPORTED_CONTRACTS[name]):
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
                methods = {
                    SlotName.STAFF_CAPABILITY: ("invoke",),
                    SlotName.RUNTIME_MEMORY: ("invoke",),
                    SlotName.RUNTIME_ENGINE: ("open",),
                    SlotName.RUNTIME_SOP: ("build",),
                    SlotName.SECURITY_PEP: ("build",),
                    SlotName.STAFF_INGRESS: ("accept",),
                }.get(item.slot, ())
                # Metadata-only manifests are valid documentation, not executable providers.
                if m.provides_operations or item.slot == SlotName.SECURITY_PEP:
                    missing_methods = [name for name in methods if not callable(getattr(item.provider, name, None))]
                    if missing_methods:
                        raise ContractIncompatible(f"module {m.module_id} does not implement {missing_methods}")
            for slot in SINGLE_PROVIDER_SLOTS:
                active = [i for i in self._by_slot.get(slot, ()) if i.enabled]
                if slot in self._by_slot and len(active) != 1:
                    raise SlotConflict(f"slot {slot.value} accepts exactly one active module; got {[i.manifest.module_id for i in active]}")
            self._sealed = True
            self.generation += 1

    def start(self) -> None:
        """Only activation starts resources; registration/preflight must be declarative."""
        if self._disposed:
            raise RegistrySealed("cannot activate a disposed registry")
        if self._started:
            self._accepting = True
            return
        try:
            for item in self._by_id.values():
                if not item.enabled:
                    continue
                self._started.append(item)
                start = getattr(item.provider, "start_module", None)
                if callable(start):
                    start(item.config)
        except BaseException:
            self.dispose()
            raise

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        for item in reversed(list(self._by_id.values())):
            stop = getattr(item.provider, "stop_module", None)
            if item in self._started and callable(stop):
                try:
                    stop()
                except Exception:
                    logger.exception("module stop failed: %s", item.manifest.module_id)
            dispose = getattr(item.provider, "dispose_module", None)
            if callable(dispose):
                try:
                    dispose()
                except Exception:
                    logger.exception("module dispose failed: %s", item.manifest.module_id)
        self._started.clear()

    @contextmanager
    def turn_lease(self):
        with self._lock:
            if not self._accepting or self._disposed:
                raise ModuleSdkError("runtime is restarting", code="ENGINE_UNAVAILABLE")
            self._turns += 1
        try:
            yield self
        finally:
            with self._lock:
                self._turns -= 1

    def begin_drain(self) -> None:
        with self._lock:
            self._accepting = False

    def end_drain(self) -> None:
        with self._lock:
            if not self._disposed:
                self._accepting = True

    @property
    def live_turns(self) -> int:
        with self._lock:
            return self._turns + self._calls

    @contextmanager
    def work_lease(self):
        """Already admitted turns may finish calls during drain; disposal may not race them."""
        with self._lock:
            if self._disposed:
                raise ModuleSdkError("module generation was disposed", code="ENGINE_UNAVAILABLE")
            self._calls += 1
        try:
            yield self
        finally:
            with self._lock:
                self._calls -= 1

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

    def providers_for_operation(self, operation: str) -> tuple[Installed, ...]:
        """Every enabled module that provides ``operation``, in module_id order (stable, not dict order)."""

        return tuple(sorted(
            (i for i in self._by_id.values() if i.enabled and operation in i.manifest.provides_operations),
            key=lambda i: i.manifest.module_id,
        ))

    def resolve_operation_provider(self, operation: str, module_id: str | None) -> Installed | None:
        """The provider for ``operation``; pinned ``module_id`` wins, else the first enabled one.

        A pinned module_id that is disabled or does not provide the operation returns ``None`` so
        the caller can fail closed rather than silently switch providers under a running turn.
        """

        if module_id:
            item = self._by_id.get(module_id)
            if item is not None and item.enabled and operation in item.manifest.provides_operations:
                return item
            return None
        return self.for_operation(operation)

    def selected(self, slot: SlotName, snapshot: Any = None, *, sop_id: str | None = None) -> list[Installed]:
        def expand(items, path=()):
            out = []
            for item in items:
                mid = item.manifest.module_id
                if mid in path:
                    raise ContractIncompatible(f"composite module cycle: {path + (mid,)}")
                members = item.manifest.metadata.get("members")
                if members is not None:
                    children = []
                    for child_id in members:
                        child = self.get(child_id)
                        if child is None or child.slot != slot:
                            raise ContractIncompatible(f"composite {mid} has invalid member {child_id}")
                        if child.enabled:
                            children.append(child)
                    out.extend(expand(children, path + (mid,)))
                else:
                    out.append(item)
            return out

        child_ids = {mid for item in self._by_slot.get(slot, ()) for mid in item.manifest.metadata.get("members", ())}
        roots = [item for item in self.providers(slot) if item.manifest.module_id not in child_ids]
        if snapshot is None:
            return expand(roots)
        parents = (sop_id, snapshot.staff_id) if sop_id else (snapshot.staff_id,)
        for parent in parents:
            bindings = [b for b in snapshot.bindings if b.parent_id == parent and b.slot == slot]
            if bindings:
                out = []
                for b in bindings:
                    if not b.module_id:  # explicit disabled slot, no default resurrection
                        continue
                    item = self.get(b.module_id)
                    if item is None or not item.enabled or item.slot != slot or item.manifest.version != b.module_version:
                        raise ContractIncompatible(f"binding {b.binding_id} is no longer available")
                    out.append(item)
                return expand(out)
        return expand(roots)

    def hooks(self, snapshot: Any = None, *, sop_id: str | None = None) -> tuple[HookContribution, ...]:
        out: list[HookContribution] = []
        for item in self.selected(SlotName.STAFF_INTERACTION, snapshot, sop_id=sop_id):
            out.extend(item.manifest.hooks)
        return tuple(out)

    def hook_handlers(self, snapshot: Any = None, *, sop_id: str | None = None) -> dict[str, Callable[..., Any]]:
        handlers: dict[str, Callable[..., Any]] = {}
        for item in self.selected(SlotName.STAFF_INTERACTION, snapshot, sop_id=sop_id):
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
        if item.slot in {SlotName.RUNTIME_ENGINE, SlotName.SECURITY_PEP} or (item.manifest.kind is not ModuleKind.CODE and not item.manifest.metadata.get("switchable")):
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
    try:
        discover_and_install(reg, settings)
        reg.seal()
    except BaseException:
        reg.dispose()
        raise
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
        old = _active
        _active = None
    if old is not None:
        old.dispose()


def manifest(module_id: str, name: str, *, kind: ModuleKind, slots: Iterable[SlotName], summary: str = "", provides: Iterable[str] = (), requires: Iterable[str] = (), hooks: Iterable[HookContribution] = (), policy_actions: Iterable[str] = (), version: str = "1.0.0", contract_version: str = "v1", metadata: Mapping[str, Any] | None = None) -> ModuleManifest:
    meta = dict(metadata or {})
    if summary:
        meta["summary"] = summary
    return ModuleManifest(
        module_id=module_id, name=name, version=version, kind=kind, contract_version=contract_version,
        attaches_to=tuple(slots), provides_operations=tuple(provides), requires_operations=tuple(requires),
        hooks=tuple(hooks), policy_actions=tuple(policy_actions), metadata=meta,
    )
