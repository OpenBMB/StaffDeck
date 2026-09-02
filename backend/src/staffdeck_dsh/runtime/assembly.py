"""Startup-level assembly: bring the DSH runtime up, mount the admin API, report health.

Called from ``app.main`` only when ``settings.dsh_enabled`` or
``settings.dsh_admin_api_enabled`` is true, so a legacy deployment never imports
this package.
"""

from __future__ import annotations

import logging
from typing import Any

from staffdeck_dsh.bridge.engine_host import get_runtime, reset_runtime
from staffdeck_dsh.contracts.errors import EngineUnavailable
from staffdeck_dsh.modules.registry import get_registry, reset_registry
from staffdeck_dsh.security.profile import get_profile, reset_profile

logger = logging.getLogger(__name__)


def mount_admin_api(app: Any) -> None:
    from staffdeck_dsh.api.admin import router

    app.include_router(router)


def start_dsh_runtime(settings: Any) -> dict[str, Any]:
    """Seal the module registry, pick the security profile, and (if enabled) boot the DSH runtime."""

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


def stop_dsh_runtime() -> None:
    reset_runtime()
    reset_profile()
    reset_registry()


def dsh_health(settings: Any) -> dict[str, Any]:
    try:
        runtime = get_runtime(settings)
        return {"ok": True, "mcp_url": runtime.mcp_url, "activations": len(runtime.registry), "security_profile": get_profile(settings).name, "modules": len(get_registry(settings).describe())}
    except EngineUnavailable as exc:
        return {"ok": False, "error": exc.to_dict()}
