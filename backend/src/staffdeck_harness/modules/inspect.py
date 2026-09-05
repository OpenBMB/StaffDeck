"""Dry-run an external module spec before it is loaded for real.

The candidate registrar is executed against a *throwaway* ``ModuleRegistry``
that already contains everything the saved assembly would build (builtins,
entry points, the other extra specs), so ``seal()`` reports exactly the
conflicts a restart would hit — without touching the live registry, the cached
``Settings`` or the Harness v3 process.

What cannot be sandboxed: importing the spec runs its module-level code in
this process (same trust as ``/restart``; admin only), and ``sys.modules``
keeps the import, so a package edited after an inspect needs a process
restart to be re-read.
"""

from __future__ import annotations

import time
from typing import Any

from staffdeck_harness.contracts.errors import ModuleSdkError
from staffdeck_harness.contracts.manifest import SlotName
from staffdeck_harness.modules.config import RuntimeOverrides, load_overrides, settings_view
from staffdeck_harness.modules.registry import ModuleRegistry, _load_callable, discover_and_install, validate_spec
from staffdeck_harness.modules.taxonomy import resolve_placement, movable

# Slots whose host calls every enabled provider (no "first wins" selection).
FANOUT_SLOTS = {SlotName.EVENT_OBSERVER.value, SlotName.HANDOFF_NOTIFIER.value, SlotName.HANDOFF_REPLY_ENDPOINT.value, SlotName.STAFF_CHANNEL.value, SlotName.STAFF_INGRESS.value}


def inspect_spec(settings: Any, spec: str, *, live_ids: set[str] | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    result: dict[str, Any] = {"spec": spec, "ok": False, "callable": None, "file": None, "modules": [], "errors": [], "warnings": [], "elapsed_ms": 0.0}
    try:
        spec = validate_spec(spec)
    except ValueError as exc:
        result["errors"].append({"code": "INVALID_SPEC", "message": str(exc), "phase": "validate"})
        return _done(result, started)
    result["spec"] = spec

    saved: RuntimeOverrides = load_overrides(settings)
    baseline = RuntimeOverrides(**{**{k: v for k, v in saved.__dict__.items() if k != "extra_modules"}, "extra_modules": [x for x in saved.extra_modules if x != spec]})
    view = settings_view(settings, baseline)

    reg = ModuleRegistry()
    try:
        discover_and_install(reg, view)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append({"code": "BASELINE_FAILED", "message": f"{type(exc).__name__}: {exc}", "phase": "baseline"})
        reg.dispose()
        return _done(result, started)
    before_ids = {m["module_id"] for m in reg.describe()}
    ops_before = {op: m["module_id"] for m in reg.describe() if m["enabled"] for op in m["provides"]}
    guarded_before = set(reg._guarded_slots)

    try:
        fn = _load_callable(spec)
        result["callable"] = f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__name__', '?')}"
        mod = __import__(fn.__module__, fromlist=["__file__"]) if getattr(fn, "__module__", None) else None
        result["file"] = getattr(mod, "__file__", None)
        with reg.installing_from(spec):
            fn(reg, {"settings": view, "disabled": set(saved.disabled_modules), "dry_run": True})
    except ModuleSdkError as exc:
        result["errors"].append({**exc.to_dict(), "phase": "register"})
    except Exception as exc:  # noqa: BLE001 — import errors, bad signatures, anything the plugin raises
        result["errors"].append({"code": type(exc).__name__.upper(), "message": str(exc) or type(exc).__name__, "phase": "import"})

    for m in reg.describe():
        if m["module_id"] not in before_ids and m["module_id"] in set(saved.disabled_modules):
            try:
                reg.set_enabled(m["module_id"], False)
            except Exception:  # noqa: BLE001
                pass
    placements = saved.placements
    for m in reg.describe():
        if m["module_id"] in before_ids:
            continue
        m = dict(m)
        try:
            slot = SlotName(m["slot"])
        except ValueError:
            slot = None
        m["movable"] = movable(m)
        hit = resolve_placement(m["module_id"], slot, category=str(m.get("category") or ""), override=placements.get(m["module_id"]) if m["movable"] else None)
        m["placement"] = {"big_id": hit[0].id, "sub_id": hit[1].id, "source": hit[2]} if hit else {"big_id": "unplaced", "sub_id": "unplaced.all", "source": "none"}
        m["already_installed"] = m["module_id"] in (live_ids or set())
        if m.get("category") and not (hit and hit[2] == "manifest") and m["placement"]["source"] in ("none", "slot"):
            result["warnings"].append({"code": "UNKNOWN_CATEGORY", "module_id": m["module_id"], "message": f"模块声明的类目 {m['category']!r} 不存在，将按接入点归类"})
        for op in m["provides"]:
            if op in ops_before and m["enabled"] and m["slot"] not in FANOUT_SLOTS:
                result["warnings"].append({"code": "OPERATION_SHADOWED", "module_id": m["module_id"], "message": f"{op} 已由 {ops_before[op]} 提供；运行时按先安装者优先，此模块不会被选中（可先停用 {ops_before[op]}）"})
        if not m["enabled"]:
            result["warnings"].append({"code": "DISABLED_BY_CONFIG", "module_id": m["module_id"], "message": "该模块在保存的装配里被停用"})
        result["modules"].append(m)
    if set(reg._guarded_slots) - guarded_before:
        result["warnings"].append({"code": "GUARD_SELF_DECLARED", "message": "模块自行把接入点标记为受权限保护，请确认宿主确实做了权限检查"})
    if not result["modules"] and not result["errors"]:
        result["warnings"].append({"code": "NO_MODULES", "message": "注册入口执行成功，但没有安装任何模块"})

    if not result["errors"]:
        try:
            reg.seal()
            result["ok"] = True
        except ModuleSdkError as exc:
            result["errors"].append({**exc.to_dict(), "phase": "seal"})
        except Exception as exc:  # noqa: BLE001
            result["errors"].append({"code": type(exc).__name__.upper(), "message": str(exc), "phase": "seal"})
    reg.dispose()
    return _done(result, started)


def _done(result: dict[str, Any], started: float) -> dict[str, Any]:
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result
