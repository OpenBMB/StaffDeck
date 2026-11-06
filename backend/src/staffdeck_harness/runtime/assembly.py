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
    # Prove compatibility before sending credentials or probing any remote service.
    from staffdeck_harness.modules.compatibility import assembly_diff, validate_compatibility
    from staffdeck_harness.modules.registry import peek_registry
    reg = None
    try:
        reg = build_registry(view)
        build_profile(view, registry=reg)
        from staffdeck_harness.contracts.manifest import SlotName
        managed_connection = reg.provider(SlotName.SECURITY_PEP).manifest.metadata.get("connection_owner") == "module"
        report.update(validate_compatibility(reg))
        active = peek_registry()
        report["diff"] = assembly_diff(active, reg) if active else {
            "enable": [item.manifest.module_id for item in reg.installed() if item.enabled],
            "disable": [], "unchanged": [],
        }
        report["modules"] = {"total": len(reg.describe()), "enabled": sum(1 for m in reg.describe() if m["enabled"])}
        for item in reg.installed():
            preflight = getattr(item.provider, "preflight_module", None)
            if item.enabled and callable(preflight):
                preflight(item.config)
    except Exception as exc:
        raise AssemblyFailed(f"装配无法启动：{type(exc).__name__}: {exc}") from exc
    finally:
        if reg is not None:
            reg.dispose()
    if wanted.security_profile == "BUSINESS_BASE" and not managed_connection:
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
    return report


# -- build / swap ---------------------------------------------------------------


def _build_components(settings: Any, overrides: RuntimeOverrides) -> tuple[ModuleRegistry, Any]:
    """Build registry + profile for ``overrides`` against a settings *view*; nothing global is touched."""

    view = settings_view(settings, overrides)
    reg = build_registry(view)
    try:
        profile = build_profile(view, registry=reg)
    except BaseException:
        reg.dispose()
        raise
    return reg, profile


def _activate(settings: Any, overrides: RuntimeOverrides, reg: ModuleRegistry, profile: Any) -> dict[str, Any]:
    """Make the prepared components live and (if enabled) boot the Harness v3 runtime."""

    reg.start()
    reg.security_profile = profile
    apply_overrides(settings, overrides)
    install_registry(reg)
    install_profile(profile)
    info: dict[str, Any] = {"security_profile": profile.name, "modules": len(reg.describe()), "registry_generation": reg.generation}
    runtime = get_runtime(settings)
    info.update({"mcp_url": runtime.mcp_url, "harness_v3_root": str(runtime.worker_config.harness_v3_root), "harness_v3_home": str(runtime.worker_config.harness_v3_home)})
    return info


def _build(settings: Any, overrides: RuntimeOverrides) -> dict[str, Any]:
    reg, profile = _build_components(settings, overrides)
    return _activate(settings, overrides, reg, profile)


def start_harness_runtime(settings: Any) -> dict[str, Any]:
    """Restore the last successfully applied assembly, never an unapproved draft."""

    global _applied, _started_at, _last_restart_error
    snapshot_env(settings)
    from staffdeck_harness.modules.config import load_applied, save_applied
    applied = load_applied(settings)
    info = _build(settings, applied)
    save_applied(settings, applied)
    with _state_lock:
        _applied = applied
        _started_at = datetime.now(timezone.utc).isoformat()
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
        from app.channels import channel_services_running, stop_channel_services, start_channel_services
        channels_were_running = channel_services_running()
        if channels_were_running and not stop_channel_services(timeout_seconds=drain_timeout_seconds):
            reg.dispose()
            raise AssemblyFailed("渠道后台仍在排空，未切换装配；请待连接器退出后重试")
        interrupted = _drain_live_turns(drain_timeout_seconds)
        if interrupted:
            from staffdeck_harness.bridge import engine_host

            if old_reg is not None:
                old_reg.end_drain()
            runtime = getattr(engine_host, "_runtime", None)
            if runtime is not None and runtime.pool is not None:
                runtime.pool.end_drain()
            reg.dispose()
            if channels_were_running:
                start_channel_services()
            raise AssemblyFailed(f"仍有 {interrupted} 个回合执行中，本次未切换装配，请稍后重试")
        reset_runtime()
        try:
            info = _activate(settings, wanted, reg, profile)
            from staffdeck_harness.modules.config import save_applied
            save_applied(settings, wanted)
            if channels_were_running:
                start_channel_services()
        except Exception as exc:  # noqa: BLE001 — Harness v3 boot failed without fallback: restore
            reg.dispose()
            msg = f"{type(exc).__name__}: {exc}"
            logger.exception("runtime activation failed; restoring previous assembly")
            reset_runtime()
            if previous is not None and old_reg is not None and old_profile is not None:
                _activate(settings, previous, old_reg, old_profile)
                from staffdeck_harness.modules.config import save_applied
                save_applied(settings, previous)
                if channels_were_running:
                    start_channel_services()
            with _state_lock:
                _last_restart_error = msg
            raise AssemblyFailed(msg) from exc
        with _state_lock:
            _applied = wanted
            _started_at = datetime.now(timezone.utc).isoformat()
            _restart_count += 1
            _last_restart_error = None
        info.update({"restarted_at": _started_at, "restart_count": _restart_count, "interrupted_turns": interrupted})
        if old_reg is not None:
            old_reg.dispose()
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
    from staffdeck_harness.modules.registry import peek_registry

    runtime = getattr(engine_host, "_runtime", None)
    modules = peek_registry()
    if modules is not None:
        modules.begin_drain()
    if runtime is not None and runtime.pool is not None:
        runtime.pool.begin_drain()
    deadline = time.monotonic() + max(0.0, float(timeout_seconds or 0.0))
    while True:
        live = modules.live_turns if modules is not None else (runtime.registry.live_count() if runtime is not None else 0)
        if live == 0 or time.monotonic() >= deadline:
            if live:
                logger.warning("restart drain window expired with %d live work item(s); keeping old generation", live)
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
