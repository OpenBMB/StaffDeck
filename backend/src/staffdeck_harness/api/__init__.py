"""Admin API for the Harness v3 runtime (mounted by runtime.assembly when enabled)."""

from staffdeck_harness.api.admin import router as admin_router

__all__ = ["admin_router"]
