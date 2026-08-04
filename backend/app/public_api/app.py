from __future__ import annotations

from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from sqlmodel import Session

from app.config import get_settings
from app.db import engine
from app.public_api import agents, credentials, examples, jobs, operations, resources, runs, sessions, sops, webhooks
from app.public_api.errors import (
    PublicAPIError,
    public_api_error_handler,
    public_http_error_handler,
    public_validation_error_handler,
)
from app.public_api.utils import audit_request


def create_public_api_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=f"{settings.app_name} Open API",
        description="数字员工、Harness v2、SOP、知识与自动任务开放 API。",
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.add_exception_handler(PublicAPIError, public_api_error_handler)
    app.add_exception_handler(HTTPException, public_http_error_handler)
    app.add_exception_handler(RequestValidationError, public_validation_error_handler)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = request.headers.get("X-Request-ID") or f"req_{uuid4().hex}"
        started = perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request.state.request_id
            return response
        finally:
            try:
                with Session(engine) as db:
                    audit_request(
                        db,
                        request,
                        getattr(request.state, "public_principal", None),
                        status_code=status_code,
                        duration_ms=(perf_counter() - started) * 1000,
                    )
            except Exception:
                pass

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": "v1", "engine": "harness_v2"}

    app.include_router(credentials.router)
    app.include_router(agents.router)
    app.include_router(sessions.router)
    app.include_router(runs.router)
    app.include_router(jobs.router)
    app.include_router(sops.router)
    app.include_router(resources.router)
    app.include_router(operations.router)
    app.include_router(webhooks.router)
    app.include_router(examples.router)
    return app
