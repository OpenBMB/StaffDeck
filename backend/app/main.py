from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session

from app.api import (
    auth,
    chat,
    feedback,
    general_skills,
    knowledge,
    memories,
    mock,
    model_configs,
    persona,
    sessions,
    skills,
    tools,
    traces,
    ui_config,
)
from app.async_jobs import shutdown_async_jobs
from app.config import get_settings
from app.db import engine, init_db
from app.db.seed import seed_demo_data

settings = get_settings()

app = FastAPI(title=settings.app_name, version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    with Session(engine) as db:
        seed_demo_data(db)


@app.on_event("shutdown")
def on_shutdown() -> None:
    shutdown_async_jobs()


@app.get("/api/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(chat.router)
app.include_router(ui_config.chat_router)
app.include_router(auth.router)
app.include_router(general_skills.router)
app.include_router(knowledge.router)
app.include_router(skills.router)
app.include_router(model_configs.router)
app.include_router(memories.router)
app.include_router(feedback.router)
app.include_router(persona.router)
app.include_router(ui_config.enterprise_router)
app.include_router(tools.router)
app.include_router(sessions.router)
app.include_router(traces.router)
app.include_router(mock.router)
