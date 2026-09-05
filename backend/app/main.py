from __future__ import annotations

import logging

from fastapi import Depends, FastAPI
from app.security.api_policy import install_response_filters, module_policy
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
    teams,
    tools,
    traces,
    ui_config,
    wechat_kf,
)
from app.async_jobs import shutdown_async_jobs, start_async_jobs
from app.a2a import recover_codex_a2a_tasks, router as a2a_router, stop_codex_a2a_tasks
from app.channels import start_channel_services, stop_channel_services
from app.config import get_settings
from app.core.harness_recovery import (
    recover_orphan_harness_runs,
    start_harness_recovery_sweeper,
    stop_harness_recovery_sweeper,
)
from app.db import engine, init_db
from app.db.seed import seed_demo_data
from app.public_api import create_public_api_app
from app.public_api.jobs import cleanup_public_api_records, recover_public_jobs
from app.public_api.maintenance import start_public_api_maintenance, stop_public_api_maintenance
from app.public_api.webhooks import enqueue_due_webhook_deliveries
from app.runtime_lock import acquire_runtime_instance_lock, release_runtime_instance_lock
from app.scheduled_tasks.worker import start_background_worker, stop_background_worker
from app.tools.a2a_recovery import recover_a2a_client_tasks
from app.teams.sweeper import start_timeout_sweeper, stop_timeout_sweeper
from app.version import app_version

logger = logging.getLogger(__name__)

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
        start_async_jobs()
        init_db()
        with Session(engine) as db:
            seed_demo_data(db)
            recover_orphan_harness_runs(db, startup=True)
        from staffdeck_harness.runtime import start_harness_runtime

        # Assemble the deployment PEP before background workers admit work.
        start_harness_runtime(settings)
        recover_codex_a2a_tasks()
        recover_a2a_client_tasks()
        start_background_worker()
        start_channel_services()
        start_timeout_sweeper()
        start_harness_recovery_sweeper()
        # Internal durable jobs (for example feedback analysis) use the same
        # recovery table even when the externally exposed API is disabled.
        recover_public_jobs()
        if settings.public_api_enabled:
            cleanup_public_api_records()
            enqueue_due_webhook_deliveries()
            start_public_api_maintenance()
    except Exception:
        release_runtime_instance_lock()
        raise


@app.on_event("shutdown")
def on_shutdown() -> None:
    try:
        if settings.harness_v3_enabled or settings.harness_admin_api_enabled:
            from staffdeck_harness.runtime import stop_harness_runtime

            stop_harness_runtime()
        stop_codex_a2a_tasks()
        stop_public_api_maintenance()
        stop_channel_services()
        stop_background_worker()
        stop_timeout_sweeper()
        stop_harness_recovery_sweeper()
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
app.include_router(agents.enterprise_router, dependencies=[Depends(module_policy("agent"))])
app.include_router(general_skills.router, dependencies=[Depends(module_policy("general_skill"))])
app.include_router(knowledge_bases.router, dependencies=[Depends(module_policy("knowledge_base"))])
app.include_router(knowledge.router, dependencies=[Depends(module_policy("knowledge_base", use_endpoints=("search_knowledge",)))])
app.include_router(skills.router, dependencies=[Depends(module_policy("sop"))])
app.include_router(model_configs.router, dependencies=[Depends(module_policy("model_config"))])
app.include_router(memories.router)
app.include_router(evolution.router)
app.include_router(feedback.router)
app.include_router(persona.router)
app.include_router(scheduled_tasks.enterprise_router)
app.include_router(scheduled_tasks.chat_router)
app.include_router(scheduled_tasks.chat_draft_router)
app.include_router(ui_config.enterprise_router)
app.include_router(channels.router)
app.include_router(wechat_kf.router)
app.include_router(teams.router, dependencies=[Depends(module_policy("team", use_endpoints=("tl_chat_endpoint", "create_team_task_endpoint")))])
app.include_router(teams.threads_router)
app.include_router(tools.router, dependencies=[Depends(module_policy("tool", use_endpoints=("test_tool", "probe_tool")))])
app.include_router(tools.mcp_router, dependencies=[Depends(module_policy("mcp_server"))])
app.include_router(sessions.router)
app.include_router(traces.router)
app.include_router(mock.router)
app.include_router(a2a_router)
if settings.harness_v3_enabled or settings.harness_admin_api_enabled:
    try:
        from staffdeck_harness.runtime import mount_admin_api
    except ImportError:
        if settings.harness_v3_enabled:
            raise
    else:
        mount_admin_api(app)

if settings.public_api_enabled:
    app.mount("/api/v1", create_public_api_app())

install_response_filters(app)
