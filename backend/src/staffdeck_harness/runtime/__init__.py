"""Deployment assembly helpers for the Harness v3 runtime (start/stop/restart, admin API, health)."""

__all__ = ["AssemblyFailed", "assembly_state", "harness_health", "mount_admin_api", "preflight_assembly", "restart_harness_runtime", "start_harness_runtime", "stop_harness_runtime"]


def __getattr__(name):
    # Importing a service contract/helper must not eagerly import AgentLoop, HTTP
    # handlers and channel workers. Lifecycle entry points are loaded only on use.
    if name not in __all__:
        raise AttributeError(name)
    from importlib import import_module
    value = getattr(import_module("staffdeck_harness.runtime.assembly"), name)
    globals()[name] = value
    return value
