"""Internal scope-verifier process: shared storage, no second AgentLoop or scheduler.

Run on a private interface. A separate worker pool avoids Runtime -> Base -> Runtime
authorization callbacks deadlocking a saturated chat/API thread pool.
"""
import threading
import logging
import re
import traceback
import uuid

from fastapi import FastAPI
from starlette.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.channels.execution_scope import router
from app.channels.storage import applied_fingerprint, install_connector_assembly
from staffdeck_harness.modules.registry import peek_registry

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(router)
_fingerprint = None
_reload_lock = threading.Lock()
logger = logging.getLogger(__name__)


def _load_applied_assembly():
    global _fingerprint
    try:
        fingerprint = applied_fingerprint()
        with _reload_lock:
            if fingerprint == _fingerprint:
                return None
            old = peek_registry()
            if old is not None and old.live_turns:
                return {"code": "CHANNEL_SCOPE_DRAINING", "message": "渠道范围校验服务正在等待现有请求结束，请稍后重试"}
            install_connector_assembly(fingerprint)
            _fingerprint = fingerprint
            if old is not None:
                try:
                    old.dispose()
                except Exception as exc:
                    logger.warning('Old channel scope registry cleanup failed type=%s', type(exc).__name__)
    except Exception as exc:
        error_id = uuid.uuid4().hex
        raw_code = str(getattr(exc, 'code', ''))
        cause_code = raw_code if re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}', raw_code) else type(exc).__name__
        # Exception messages and source lines can contain credentials. Retain
        # frame locations and a correlation ID, not arbitrary exception text.
        frames = [(frame.filename, frame.lineno, frame.name) for frame in traceback.extract_tb(exc.__traceback__)]
        logger.error('Channel scope assembly load failed error_id=%s cause=%s frames=%s', error_id, cause_code, frames)
        return {'code': 'CHANNEL_SCOPE_ASSEMBLY_UNAVAILABLE',
                'message': '渠道范围校验服务无法加载当前装配，请检查配套进程版本及配置',
                'cause_code': cause_code, 'error_id': error_id}
    return None


@app.middleware("http")
async def refresh_assembly(request, call_next):
    error = await run_in_threadpool(_load_applied_assembly)
    if error is not None:
        return JSONResponse({'detail': error}, status_code=503)
    return await call_next(request)
