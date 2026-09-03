"""Startup-level assembly: bring the Harness v3 runtime up, mount the admin API, report health.

Called from ``app.main`` only when ``settings.harness_v3_enabled`` or
``settings.harness_admin_api_enabled`` is true, so a legacy deployment never imports
this package.

The assembly the process is running with is remembered here (``applied``) so
the admin page can tell whether the saved configuration differs from it and
offer a restart. ``restart_harness_runtime`` first *preflights* the saved assembly
(network + a throwaway registry seal), then builds the new registry/profile
into locals and swaps them in atomically — the old assembly keeps serving until
the swap, and a failed build leaves it untouched.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from staffdeck_harness.bridge.engine_host import get_runtime, reset_runtime
from staffdeck_harness.contracts.errors import EngineUnavailable
from staffdeck_harness.modules.config import (
    InvalidConnection,
    RuntimeOverrides,
    apply_overrides,
    defaults_from_settings,
    load_overrides,
    settings_view,
    snapshot_env,
)
from staffdeck_harness.modules.registry import ModuleRegistry, build_registry, install_registry, reset_registry
from staffdeck_harness.security.profile import build_profile, install_profile, reset_profile

logger = logging.getLogger(__name__)

_state_lock = threading.Lock()          # guards the bookkeeping below (never held across I/O)
_restart_lock = threading.Lock()        # serialises restarts
_applied: RuntimeOverrides | None = None
_started_at: str | None = None
_restart_count = 0
_last_restart_error: str | None = None
_restarting = False


class AssemblyFailed(RuntimeError):
    """The requested assembly could not be built; the previous one was restored (or never torn down)."""


def mount_admin_api(app: Any) -> None:
    from staffdeck_harness.api.admin import router

    app.include_router(router)


# -- preflight ------------------------------------------------------------------


def preflight_assembly(settings: Any, wanted: RuntimeOverrides, *, tenant_id: str | None = None, principal_id: str | None = None) -> dict[str, Any]:
    """Prove the saved assembly can be built *before* touching the running one.

    - BUSINESS_BASE needs a reachable permission centre and a valid decision token
      (network round-trip through ``preflight_base``);
    - the module set must seal (throwaway registry with exactly the guards the
      registrars declare — the same checks a real build would hit);
    - the security profile must build.
    Raises ``AssemblyFailed`` with an operator-facing (Chinese) message.
    """

    snapshot_env(settings)
    view = settings_view(settings, wanted)
    report: dict[str, Any] = {"base": None, "modules": None}
    if wanted.security_profile == "BUSINESS_BASE":
        from staffdeck_harness.security.base_preflight import preflight_base

        conn = wanted.base.effective(settings)
        if not conn.authz_url or not conn.decision_token:
            raise AssemblyFailed("无法切换到企业版权限：请先填写权限中心地址和决策令牌，并通过测试连接")
        try:
            pre = preflight_base(conn, tenant_id=tenant_id, principal_id=principal_id)
        except InvalidConnection as exc:
            raise AssemblyFailed(f"无法切换到企业版权限：{exc}") from exc
        report["base"] = pre.to_dict()
        if not pre.ok:
            raise AssemblyFailed(f"无法切换到企业版权限：{pre.first_failure() or '权限中心连接测试未通过'}")
    try:
        reg = build_registry(view)
        build_profile(view, registry=reg)
    except Exception as exc:  # noqa: BLE001 — any seal/profile failure is a preflight failure
        raise AssemblyFailed(f"装配无法启动：{type(exc).__name__}: {exc}") from exc
    report["modules"] = {"total": len(reg.describe()), "enabled": sum(1 for m in reg.describe() if m["enabled"])}
    return report


# -- build / swap ---------------------------------------------------------------


def _build_components(settings: Any, overrides: RuntimeOverrides) -> tuple[ModuleRegistry, Any]:
    """Build registry + profile for ``overrides`` against a settings *view*; nothing global is touched."""

    view = settings_view(settings, overrides)
    reg = build_registry(view)
    profile = build_profile(view, registry=reg)
    return reg, profile


def _activate(settings: Any, overrides: RuntimeOverrides, reg: ModuleRegistry, profile: Any) -> dict[str, Any]:
    """Make the prepared components live and (if enabled) boot the Harness v3 runtime."""

    apply_overrides(settings, overrides)
    install_registry(reg)
    install_profile(profile)
    info: dict[str, Any] = {"security_profile": profile.name, "modules": len(reg.describe()), "registry_generation": reg.generation}
    if bool(getattr(settings, "harness_v3_enabled", False)):
        try:
            runtime = get_runtime(settings)
            info.update({"mcp_url": runtime.mcp_url, "harness_v3_root": str(runtime.worker_config.harness_v3_root), "harness_v3_home": str(runtime.worker_config.harness_v3_home)})
        except EngineUnavailable as exc:
            if bool(getattr(settings, "harness_v3_fallback_to_v2", True)):
                logger.warning("Harness v3 runtime unavailable (%s); turns fall back to Harness v2", exc)
                info["runtime_error"] = exc.to_dict()
            else:
                raise
    return info


def _build(settings: Any, overrides: RuntimeOverrides) -> dict[str, Any]:
    reg, profile = _build_components(settings, overrides)
    return _activate(settings, overrides, reg, profile)


def start_harness_runtime(settings: Any) -> dict[str, Any]:
    """Apply the saved assembly, seal the module registry, pick the security profile, and (if enabled) boot DSH.

    A saved assembly that cannot be built (e.g. BUSINESS_BASE whose permission
    centre is gone) must not brick the process: we fall back to the deployment
    defaults, keep the saved file untouched (so it shows as pending) and record
    the error for the admin page.
    """

    global _applied, _started_at, _last_restart_error
    snapshot_env(settings)
    overrides = load_overrides(settings)
    defaults = defaults_from_settings(settings)
    try:
        info = _build(settings, overrides)
        applied = overrides
        fallback = None
    except Exception as exc:  # noqa: BLE001
        if defaults.same_assembly(overrides):
            raise
        fallback = f"启动时无法应用已保存的装配，已回退到部署默认值：{type(exc).__name__}: {exc}"
        logger.exception("saved assembly failed at startup; falling back to deployment defaults")
        reset_runtime()
        reset_profile()
        reset_registry()
        info = _build(settings, defaults)
        info["fallback"] = fallback
        applied = defaults
    with _state_lock:
        _applied = applied
        _started_at = datetime.now(timezone.utc).isoformat()
        if fallback:
            _last_restart_error = fallback
    return info


def stop_harness_runtime() -> None:
    global _applied
    reset_runtime()
    reset_profile()
    reset_registry()
    with _state_lock:
        _applied = None


def restart_harness_runtime(settings: Any, *, tenant_id: str | None = None, principal_id: str | None = None, drain_timeout_seconds: float = 20.0) -> dict[str, Any]:
    """Rebuild the runtime from the saved assembly.

    Order: preflight (no global state touched) → build new registry/profile in
    locals → drain in-flight Harness v3 turns (bounded) → stop the Harness v3 process →
    swap registry/profile/settings atomically → boot Harness v3. Hosts see either the old or
    the new assembly, never a default one. Turns still running when the drain window
    expires lose their Harness v3 process (their capability calls get ENGINE_UNAVAILABLE);
    the count is reported as ``interrupted_turns``. If the swap step still fails, the
    previous components are re-installed.
    """

    global _applied, _started_at, _restart_count, _last_restart_error, _restarting
    if not _restart_lock.acquire(blocking=False):
        raise AssemblyFailed("已有一次重启正在进行，请稍候")
    try:
        with _state_lock:
            previous = _applied
            _restarting = True
        wanted = load_overrides(settings)
        try:
            preflight_assembly(settings, wanted, tenant_id=tenant_id, principal_id=principal_id)
            reg, profile = _build_components(settings, wanted)
        except AssemblyFailed as exc:
            with _state_lock:
                _last_restart_error = str(exc)
            raise
        except Exception as exc:  # noqa: BLE001
            msg = f"装配无法启动：{type(exc).__name__}: {exc}"
            with _state_lock:
                _last_restart_error = msg
            raise AssemblyFailed(msg) from exc

        from staffdeck_harness.modules.registry import peek_registry
        from staffdeck_harness.security.profile import peek_profile

        old_reg, old_profile = peek_registry(), peek_profile()
        interrupted = _drain_live_turns(drain_timeout_seconds)
        reset_runtime()
        try:
            info = _activate(settings, wanted, reg, profile)
        except Exception as exc:  # noqa: BLE001 — Harness v3 boot failed without fallback: restore
            msg = f"{type(exc).__name__}: {exc}"
            logger.exception("runtime activation failed; restoring previous assembly")
            reset_runtime()
            if previous is not None and old_reg is not None and old_profile is not None:
                _activate(settings, previous, old_reg, old_profile)
            with _state_lock:
                _last_restart_error = msg
            raise AssemblyFailed(msg) from exc
        with _state_lock:
            _applied = wanted
            _started_at = datetime.now(timezone.utc).isoformat()
            _restart_count += 1
            _last_restart_error = None
        info.update({"restarted_at": _started_at, "restart_count": _restart_count, "interrupted_turns": interrupted})
        return info
    finally:
        with _state_lock:
            _restarting = False
        _restart_lock.release()


def _drain_live_turns(timeout_seconds: float) -> int:
    """Wait (bounded) for in-flight Harness v3 turns to finish before the runtime is torn down.

    The bridge's ``ActivationRegistry`` holds one entry per live turn. While the drain runs, the
    ``restarting`` flag is already set, so ``assembly_state`` shows the console what is happening.
    Returns the number of turns still live when the window closed (0 = a clean drain).
    """

    import time

    from staffdeck_harness.bridge import engine_host

    runtime = getattr(engine_host, "_runtime", None)
    if runtime is None:
        return 0
    deadline = time.monotonic() + max(0.0, float(timeout_seconds or 0.0))
    while True:
        live = len(runtime.registry)
        if live == 0 or time.monotonic() >= deadline:
            if live:
                logger.warning("restart drain window expired with %d live turn(s); they will lose their engine process", live)
            return live
        time.sleep(0.2)


def clear_restart_error() -> None:
    global _last_restart_error
    with _state_lock:
        _last_restart_error = None


def assembly_state(settings: Any) -> dict[str, Any]:
    """Saved vs applied assembly, for the admin page. Never blocks on a restart."""

    saved = load_overrides(settings)
    with _state_lock:
        applied = _applied
        started_at = _started_at
        restarts = _restart_count
        last_error = _last_restart_error
        restarting = _restarting
    return {
        "saved": saved.to_dict(settings=settings),
        "applied": applied.to_dict(settings=settings) if applied is not None else None,
        "pending": not saved.same_assembly(applied),
        "started_at": started_at,
        "restart_count": restarts,
        "last_restart_error": last_error,
        "restarting": restarting,
    }


def harness_health(settings: Any) -> dict[str, Any]:
    from staffdeck_harness.modules.registry import get_registry
    from staffdeck_harness.security.profile import get_profile

    try:
        runtime = get_runtime(settings)
        return {"ok": True, "mcp_url": runtime.mcp_url, "activations": len(runtime.registry), "security_profile": get_profile(settings).name, "modules": len(get_registry(settings).describe())}
    except EngineUnavailable as exc:
        return {"ok": False, "error": exc.to_dict()}
