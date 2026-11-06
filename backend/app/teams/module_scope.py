"""One admission gate for shared team execution, recovery and maintenance."""

from staffdeck_harness.contracts.errors import ModuleSdkError
from staffdeck_harness.contracts.manifest import SlotName


def team_module_enabled(db=None, *, registry=None) -> bool:
    if registry is None:
        from staffdeck_harness.modules.registry import peek_registry

        registry = (db.info.get("staffdeck_registry") if db is not None else None) or peek_registry()
    # Standalone callers retain their existing behavior; an assembled deployment
    # must have an enabled implementation, never a fallback to the built-in one.
    return registry is None or bool(registry.providers(SlotName.STAFF_TEAM))


def require_team_module(db=None, *, registry=None) -> None:
    if not team_module_enabled(db, registry=registry):
        raise ModuleSdkError("团队协作模块未启用，待处理任务已保留", code="TEAM_MODULE_DISABLED")
