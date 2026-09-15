"""Internal scope-verifier process: shared storage, no second AgentLoop or scheduler.

Run on a private interface. A separate worker pool avoids Runtime -> Base -> Runtime
authorization callbacks deadlocking a saturated chat/API thread pool.
"""
import threading

from fastapi import FastAPI
from starlette.responses import JSONResponse

from app.channels.execution_scope import router
from app.channels.storage import applied_fingerprint, install_connector_assembly
from staffdeck_harness.modules.registry import peek_registry

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(router)
_fingerprint = None
_reload_lock = threading.Lock()


@app.middleware("http")
async def refresh_assembly(request, call_next):
    global _fingerprint
    fingerprint = applied_fingerprint()
    if fingerprint != _fingerprint:
        with _reload_lock:
            old = peek_registry()
            if old is not None and old.live_turns:
                return JSONResponse({"detail": "Scope verifier is draining"}, status_code=503)
            try:
                install_connector_assembly(fingerprint)
            except Exception:
                return JSONResponse({"detail": "Scope verifier cannot load the applied assembly"}, status_code=503)
            _fingerprint = fingerprint
            if old is not None:
                old.dispose()
    return await call_next(request)
