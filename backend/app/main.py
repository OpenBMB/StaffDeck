from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session

from app.api import (
    agents,
    app_updates,
    auth,
    channels,
    chat,
    evolution,
    feedback,
    general_skills,
    knowledge,
    knowledge_bases,
    memories,
    mock,
    model_configs,
    persona,
    scheduled_tasks,
    sessions,
    skills,
    tools,
    traces,
    ui_config,
)
from app.async_jobs import shutdown_async_jobs
from app.channels import start_channel_services, stop_channel_services
from app.config import get_settings
from app.db import engine, init_db
from app.db.seed import seed_demo_data
from app.scheduled_tasks.worker import start_background_worker, stop_background_worker
from app.public_api import create_public_api_app
from app.public_api.jobs import cleanup_public_api_records, recover_public_jobs
from app.public_api.webhooks import enqueue_due_webhook_deliveries
from app.public_api.maintenance import start_public_api_maintenance, stop_public_api_maintenance
from app.runtime_lock import acquire_runtime_instance_lock, release_runtime_instance_lock
from app.version import app_version

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version=app_version(),
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    acquire_runtime_instance_lock()
    try:
        init_db()
        with Session(engine) as db:
            seed_demo_data(db)
        start_background_worker()
        start_channel_services()
        if settings.public_api_enabled:
            recover_public_jobs()
            cleanup_public_api_records()
            enqueue_due_webhook_deliveries()
            start_public_api_maintenance()
    except Exception:
        release_runtime_instance_lock()
        raise


@app.on_event("shutdown")
def on_shutdown() -> None:
    try:
        stop_public_api_maintenance()
        stop_channel_services()
        stop_background_worker()
        shutdown_async_jobs()
    finally:
        release_runtime_instance_lock()


@app.get("/api/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok", "app": "StaffDeck"}


app.include_router(app_updates.router)
app.include_router(chat.router)
app.include_router(agents.chat_router)
app.include_router(ui_config.chat_router)
app.include_router(auth.router)
app.include_router(agents.scope_router)
app.include_router(agents.enterprise_router)
app.include_router(general_skills.router)
app.include_router(knowledge_bases.router)
app.include_router(knowledge.router)
app.include_router(skills.router)
app.include_router(model_configs.router)
app.include_router(memories.router)
app.include_router(evolution.router)
app.include_router(feedback.router)
app.include_router(persona.router)
app.include_router(scheduled_tasks.enterprise_router)
app.include_router(scheduled_tasks.chat_router)
app.include_router(scheduled_tasks.chat_draft_router)
app.include_router(ui_config.enterprise_router)
app.include_router(channels.router)
app.include_router(tools.router)
app.include_router(tools.mcp_router)
app.include_router(sessions.router)
app.include_router(traces.router)
app.include_router(mock.router)

if settings.public_api_enabled:
    app.mount("/api/v1", create_public_api_app())
