from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app.a2a import recover_codex_a2a_tasks, stop_codex_a2a_tasks
from app.a2a import router as a2a_router
from app.api import (
    agent_apps,
    agent_shares,
    agents,
    ai_review,
    api_key_applications,
    app_updates,
    auth,
    channels,
    chat,
    evolution,
    feedback,
    gallery,
    general_skill_shares,
    general_skills,
    knowledge,
    knowledge_bases,
    laya,
    memories,
    mock,
    model_configs,
    oa_assistant,
    persona,
    plaza_hot,
    plaza_news,
    scheduled_task_monitor,
    scheduled_tasks,
    sessions,
    skills,
    teams,
    tool_access,
    tools,
    traces,
    ui_config,
)
from app.async_jobs import shutdown_async_jobs, start_async_jobs
from app.channels import start_channel_services, stop_channel_services
from app.config import get_settings
from app.core.engine.recovery import (
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
from app.security.auth import read_token_payload, share_scope_from_payload
from app.teams.sweeper import start_timeout_sweeper, stop_timeout_sweeper
from app.tools.a2a_recovery import recover_a2a_client_tasks
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

# 分享访客令牌允许访问的路径前缀:只开放对话链路与分享自身的接口。
# 访客身份是一条真实的 users 行,没有这层白名单就等于把整个租户 API 交出去。
_SHARE_SCOPE_ALLOWED_PREFIXES = (
    "/api/chat/",
    "/api/public/agent-shares/",
)

# 对话链路里的「例外」:定时任务的创建/草案接口就挂在 /api/chat/ 下,
# 但它是持久化的写操作(会被后台调度反复执行),分享访客一律不可达。
_SHARE_SCOPE_DENIED_PREFIXES = (
    "/api/chat/scheduled-task",
)


def share_scope_allows_path(path: str) -> bool:
    """分享访客令牌可达的路径:只放行对话链路与分享自身接口。"""
    if path.startswith(_SHARE_SCOPE_DENIED_PREFIXES):
        return False
    return path.startswith(_SHARE_SCOPE_ALLOWED_PREFIXES)


@app.middleware("http")
async def restrict_share_scope(request: Request, call_next):
    header = request.headers.get("authorization") or ""
    if header[:7].lower() == "bearer ":
        payload = read_token_payload(header[7:].strip())
        if payload is not None and share_scope_from_payload(payload) is not None:
            if not share_scope_allows_path(request.url.path):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Share link cannot access this resource"},
                )
    return await call_next(request)


@app.on_event("startup")
def on_startup() -> None:
    acquire_runtime_instance_lock()
    try:
        start_async_jobs()
        init_db()
        with Session(engine) as db:
            seed_demo_data(db)
            recover_orphan_harness_runs(db, startup=True)
            if settings.scheduler_backend == "rq":
                # rq 后端：主进程只负责按 PG 全量重建 Redis 调度；
                # 实际触发与执行由独立进程 app.scheduled_tasks.rq_worker 承担。
                from app.scheduled_tasks import rq_dispatch

                rq_dispatch.sync_all_on_startup(db)
        recover_codex_a2a_tasks()
        recover_a2a_client_tasks()
        if settings.scheduler_backend != "rq":
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
app.include_router(agent_shares.router)
app.include_router(agent_shares.public_router)
app.include_router(chat.router)
app.include_router(agents.chat_router)
app.include_router(ui_config.chat_router)
app.include_router(auth.router)
app.include_router(agents.scope_router)
app.include_router(agents.enterprise_router)
app.include_router(general_skills.router)
app.include_router(general_skill_shares.router)
app.include_router(general_skill_shares.public_router)
app.include_router(knowledge_bases.router)
app.include_router(knowledge.router)
app.include_router(skills.router)
app.include_router(model_configs.router)
app.include_router(plaza_news.router)
app.include_router(plaza_hot.router)
app.include_router(api_key_applications.router)
app.include_router(laya.router)
app.include_router(oa_assistant.router)
app.include_router(ai_review.router)
app.include_router(memories.router)
app.include_router(evolution.router)
app.include_router(feedback.router)
app.include_router(persona.router)
app.include_router(scheduled_tasks.enterprise_router)
app.include_router(scheduled_tasks.chat_router)
app.include_router(scheduled_tasks.chat_draft_router)
app.include_router(scheduled_task_monitor.enterprise_router)
app.include_router(ui_config.enterprise_router)
app.include_router(channels.router)
app.include_router(teams.router)
app.include_router(teams.threads_router)
app.include_router(tools.router)
app.include_router(tools.mcp_router)
app.include_router(tool_access.router)
app.include_router(tool_access.notifications_router)
app.include_router(gallery.router)
app.include_router(agent_apps.router)
app.include_router(sessions.router)
app.include_router(traces.router)
app.include_router(mock.router)
app.include_router(a2a_router)

if settings.public_api_enabled:
    app.mount("/api/v1", create_public_api_app())
