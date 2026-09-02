"""Admin API for the DSH runtime (mounted by runtime.assembly when enabled)."""

from staffdeck_dsh.api.admin import router as admin_router

__all__ = ["admin_router"]
