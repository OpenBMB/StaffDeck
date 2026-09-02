"""Startup-level assembly: one call to bring the DSH runtime up and report its health.

Intended for the app lifespan (``app.main``) or an ops CLI. Keeps the legacy
app free of DSH imports: it is only called when ``settings.dsh_enabled``.
"""

from __future__ import annotations

from typing import Any

from staffdeck_dsh.bridge.engine_host import get_runtime, reset_runtime
from staffdeck_dsh.contracts.errors import EngineUnavailable
from staffdeck_dsh.security.profile import get_profile


def start_dsh_runtime(settings: Any) -> dict[str, Any]:
    profile = get_profile(settings)
    runtime = get_runtime(settings)
    return {"security_profile": profile.name, "mcp_url": runtime.mcp_url, "dsh_root": str(runtime.worker_config.dsh_root), "dsh_home": str(runtime.worker_config.dsh_home)}


def stop_dsh_runtime() -> None:
    reset_runtime()


def dsh_health(settings: Any) -> dict[str, Any]:
    try:
        cfg_ok = True
        runtime = get_runtime(settings)
        return {"ok": True, "config": cfg_ok, "mcp_url": runtime.mcp_url, "activations": len(runtime.registry), "security_profile": get_profile(settings).name}
    except EngineUnavailable as exc:
        return {"ok": False, "error": exc.to_dict()}
