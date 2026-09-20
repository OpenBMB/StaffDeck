"""Small deployable StaffDeck Knowledge module process.

This entrypoint is intentionally limited to the Knowledge module protocol. It
uses the same database models, ingestion worker, authorization boundary, and
router as the full StaffDeck application while leaving unrelated Harness and
channel services out of a Knowledge-only deployment.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlmodel import Session

from app.api.module_knowledge import router as knowledge_module_router
from app.async_jobs import shutdown_async_jobs, start_async_jobs
from app.db import engine, init_db
from app.db.models import Tenant, User
from app.db.seed import seed_demo_data
from app.runtime_lock import acquire_runtime_instance_lock, release_runtime_instance_lock
from app.security.auth import hash_password


def _ensure_protocol_identity() -> None:
    """Create only the tenant/user required by the module protocol.

    Full StaffDeck seeding is deliberately not part of this process. The
    module contract receives an explicit actor identity and the native owner
    still performs all tenant and role checks.
    """

    if os.getenv("STAFFDECK_KNOWLEDGE_SEED", "true").strip().lower() in {"1", "true", "yes", "on"}:
        with Session(engine) as db:
            seed_demo_data(db)
        return
    tenant_id = "tenant_demo"
    user_id = os.getenv("STAFFDECK_KNOWLEDGE_USER_ID", "admin").strip() or "admin"
    with Session(engine) as db:
        if db.get(Tenant, tenant_id) is None:
            db.add(Tenant(id=tenant_id, name="Knowledge E2E Tenant"))
        if db.get(User, user_id) is None:
            db.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    username=user_id,
                    display_name="Knowledge E2E Admin",
                    role="admin",
                    password_hash=hash_password("module-only"),
                )
            )
        db.commit()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    acquire_runtime_instance_lock()
    try:
        init_db()
        _ensure_protocol_identity()
        start_async_jobs()
        yield
    finally:
        shutdown_async_jobs()
        release_runtime_instance_lock()


app = FastAPI(
    title="StaffDeck Knowledge Module",
    version="staffdeck.knowledge/v1",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.get("/api/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok", "app": "StaffDeck Knowledge Module"}


app.include_router(knowledge_module_router)
