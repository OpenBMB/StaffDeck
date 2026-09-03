"""Startup-level assembly: bring the DSH runtime up, mount the admin API, report health.

Called from ``app.main`` only when ``settings.dsh_enabled`` or
``settings.dsh_admin_api_enabled`` is true, so a legacy deployment never imports
this package.

The assembly the process is running with is remembered here (``applied``) so
the admin page can tell whether the saved configuration differs from it and
offer a restart. ``restart_dsh_runtime`` rebuilds everything in-process and
rolls back to the previous assembly if the new one fails to seal.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from staffdeck_dsh.bridge.engine_host import get_runtime, reset_runtime
from staffdeck_dsh.contracts.errors import EngineUnavailable
from staffdeck_dsh.modules.config import RuntimeOverrides, apply_overrides, load_overrides
from staffdeck_dsh.modules.registry import get_registry, reset_registry
from staffdeck_dsh.security.profile import get_profile, reset_profile

logger = logging.getLogger(__name__)

_state_lock = threading.Lock()
_applied: RuntimeOverrides | None = None
_started_at: str | None = None
_restart_count = 0
_last_restart_error: str | None = None


class AssemblyFailed(RuntimeError):
    """The requested assembly could not be built; the previous one was restored."""


def mount_admin_api(app: Any) -> None:
    from staffdeck_dsh.api.admin import router

    app.include_router(router)


def _build(settings: Any, overrides: RuntimeOverrides) -> dict[str, Any]:
    apply_overrides(settings, overrides)
    registry = get_registry(settings)
    profile = get_profile(settings)
    info: dict[str, Any] = {"security_profile": profile.name, "modules": len(registry.describe()), "registry_generation": registry.generation}
    if bool(getattr(settings, "dsh_enabled", False)):
        try:
            runtime = get_runtime(settings)
            info.update({"mcp_url": runtime.mcp_url, "dsh_root": str(runtime.worker_config.dsh_root), "dsh_home": str(runtime.worker_config.dsh_home)})
        except EngineUnavailable as exc:
            if bool(getattr(settings, "dsh_fallback_to_legacy", True)):
                logger.warning("DSH runtime unavailable at startup (%s); turns fall back to legacy", exc)
                info["runtime_error"] = exc.to_dict()
            else:
                raise
    return info


def start_dsh_runtime(settings: Any) -> dict[str, Any]:
    """Apply the saved assembly, seal the module registry, pick the security profile, and (if enabled) boot DSH."""

    global _applied, _started_at
    overrides = load_overrides(settings)
    with _state_lock:
        info = _build(settings, overrides)
        _applied = overrides
        _started_at = datetime.now(timezone.utc).isoformat()
    return info


def stop_dsh_runtime() -> None:
    global _applied
    reset_runtime()
    reset_profile()
    reset_registry()
    with _state_lock:
        _applied = None


def restart_dsh_runtime(settings: Any) -> dict[str, Any]:
    """Tear the runtime down and rebuild it from the saved assembly.

    In-flight turns lose their DSH process (they fall back or fail); that is the
    documented cost of a restart. If the saved assembly cannot be built (bad
    module spec, conflicting providers) the previous assembly is rebuilt and an
    ``AssemblyFailed`` is raised so the caller can show the error.
    """

    global _applied, _started_at, _restart_count, _last_restart_error
    with _state_lock:
        previous = _applied
        wanted = load_overrides(settings)
        reset_runtime()
        reset_profile()
        reset_registry()
        try:
            info = _build(settings, wanted)
        except Exception as exc:  # noqa: BLE001 — anything that prevents sealing must roll back
            _last_restart_error = f"{type(exc).__name__}: {exc}"
            logger.exception("runtime restart failed; restoring previous assembly")
            reset_runtime()
            reset_profile()
            reset_registry()
            if previous is not None:
                _build(settings, previous)
                _applied = previous
            raise AssemblyFailed(_last_restart_error) from exc
        _applied = wanted
        _started_at = datetime.now(timezone.utc).isoformat()
        _restart_count += 1
        _last_restart_error = None
    info.update({"restarted_at": _started_at, "restart_count": _restart_count})
    return info


def assembly_state(settings: Any) -> dict[str, Any]:
    """Saved vs applied assembly, for the admin page."""

    saved = load_overrides(settings)
    with _state_lock:
        applied = _applied
        started_at = _started_at
        restarts = _restart_count
        last_error = _last_restart_error
    return {
        "saved": saved.to_dict(),
        "applied": applied.to_dict() if applied is not None else None,
        "pending": not saved.same_assembly(applied),
        "started_at": started_at,
        "restart_count": restarts,
        "last_restart_error": last_error,
    }


def dsh_health(settings: Any) -> dict[str, Any]:
    try:
        runtime = get_runtime(settings)
        return {"ok": True, "mcp_url": runtime.mcp_url, "activations": len(runtime.registry), "security_profile": get_profile(settings).name, "modules": len(get_registry(settings).describe())}
    except EngineUnavailable as exc:
        return {"ok": False, "error": exc.to_dict()}
