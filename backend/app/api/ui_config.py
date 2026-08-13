from __future__ import annotations

import os
import signal
import sys
import threading
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session

from app import paths
from app.db import get_session
from app.db.models import UIConfig, User, utc_now
from app.harness.sandbox import diagnostics, windows_install_command
from app.security.auth import get_current_user, require_current_tenant
from app.security.permissions import ensure_tenant_admin
from app.security.tenant import ensure_tenant

enterprise_router = APIRouter(
    prefix="/api/enterprise/ui-config",
    tags=["enterprise:ui-config"],
    dependencies=[Depends(get_current_user)],
)
chat_router = APIRouter(prefix="/api/chat/ui-config", tags=["chat:ui-config"])


class UIConfigRead(BaseModel):
    tenant_id: str
    show_thinking_trace: bool
    show_skill_trace: bool
    show_tool_trace: bool
    reflection_max_rounds: int
    agent_loop_max_actions: int
    sandbox_enabled: bool = False
    harness_storage_path: str = ""
    effective_harness_storage_path: str = ""
    restart_scheduled: bool = False
    sandbox_network_mode: Literal["all", "allowlist", "deny"] = "all"
    sandbox_allowed_domains: list[str] = Field(default_factory=list)
    sandbox_backend: str | None = None
    sandbox_setup_required: bool = False
    sandbox_setup_instructions: str | None = None
    sandbox_status: str = "unavailable"
    sandbox_status_code: str | None = None
    sandbox_status_message: str | None = None
    sandbox_status_remediation: str | None = None
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class UIConfigUpdateRequest(BaseModel):
    tenant_id: str
    show_thinking_trace: bool = True
    show_skill_trace: bool = True
    show_tool_trace: bool = True
    reflection_max_rounds: int = Field(default=1, ge=0, le=5)
    agent_loop_max_actions: int = Field(default=32, ge=1, le=100)
    sandbox_enabled: bool = False
    harness_storage_path: str = Field(default="", max_length=1024)
    sandbox_network_mode: Literal["all", "allowlist", "deny"] = "all"
    sandbox_allowed_domains: list[str] = Field(default_factory=list, max_length=200)


def ui_config_read(row: UIConfig, *, restart_scheduled: bool = False) -> UIConfigRead:
    report = diagnostics() if row.sandbox_enabled else None
    windows_setup_required = (
        row.sandbox_enabled
        and sys.platform == "win32"
        and report is not None
        and report.code == "SANDBOX_WINDOWS_SETUP_REQUIRED"
    )
    return UIConfigRead(
        tenant_id=row.tenant_id,
        show_thinking_trace=row.show_thinking_trace,
        show_skill_trace=row.show_skill_trace,
        show_tool_trace=row.show_tool_trace,
        reflection_max_rounds=row.reflection_max_rounds,
        agent_loop_max_actions=row.agent_loop_max_actions,
        sandbox_enabled=bool(row.sandbox_enabled),
        harness_storage_path=str(row.harness_storage_path or ""),
        effective_harness_storage_path=_effective_storage_path(row),
        restart_scheduled=restart_scheduled,
        sandbox_network_mode=(
            row.sandbox_network_mode
            if row.sandbox_network_mode in {"all", "allowlist", "deny"}
            else "deny"
        ),
        sandbox_allowed_domains=[
            str(item).strip()
            for item in (row.sandbox_allowed_domains or [])
            if str(item).strip()
        ],
        sandbox_backend=report.backend if report is not None else "disabled",
        sandbox_setup_required=windows_setup_required,
        sandbox_setup_instructions=(
            "StaffDeck 服务运行在 Windows 主机上，首次启用安全执行环境需要一次管理员确认。\n"
            "请在这台 Windows 电脑上打开 PowerShell 或 CMD（右键并选择‘以管理员身份运行’），执行：\n"
            f"{windows_install_command()}\n"
            "确认 UAC 后等待初始化完成，然后重启 StaffDeck 服务。"
            if windows_setup_required
            else (
                f"沙盒状态：{report.message}\n{report.remediation}"
                if report is not None and report.status != "ready"
                else None
            )
        ),
        sandbox_status=report.status if report is not None else "disabled",
        sandbox_status_code=report.code if report is not None else None,
        sandbox_status_message=(
            report.message if report is not None else "沙盒已由管理员关闭。"
        ),
        sandbox_status_remediation=report.remediation if report is not None else None,
        updated_at=row.updated_at.isoformat(),
    )


def get_or_create_ui_config(db: Session, tenant_id: str) -> UIConfig:
    ensure_tenant(db, tenant_id)
    row = db.get(UIConfig, tenant_id)
    if not row:
        row = UIConfig(tenant_id=tenant_id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


@enterprise_router.get("", response_model=UIConfigRead, dependencies=[Depends(require_current_tenant)])
def get_enterprise_ui_config(
    tenant_id: str = Query(...), db: Session = Depends(get_session)
) -> UIConfigRead:
    return ui_config_read(get_or_create_ui_config(db, tenant_id))


@enterprise_router.put("", response_model=UIConfigRead)
def update_enterprise_ui_config(
    request: UIConfigUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> UIConfigRead:
    ensure_tenant_admin(request.tenant_id, current_user)
    row = get_or_create_ui_config(db, request.tenant_id)
    sandbox_changed = bool(row.sandbox_enabled) != request.sandbox_enabled
    storage_path = _validate_storage_path(request.harness_storage_path)
    row.show_thinking_trace = request.show_thinking_trace
    row.show_skill_trace = request.show_skill_trace
    row.show_tool_trace = request.show_tool_trace
    row.reflection_max_rounds = request.reflection_max_rounds
    row.agent_loop_max_actions = request.agent_loop_max_actions
    row.sandbox_enabled = request.sandbox_enabled
    row.harness_storage_path = storage_path
    row.sandbox_network_mode = request.sandbox_network_mode
    row.sandbox_allowed_domains = request.sandbox_allowed_domains
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    if sandbox_changed:
        _schedule_application_restart()
    return ui_config_read(row, restart_scheduled=sandbox_changed)


@chat_router.get("", response_model=UIConfigRead)
def get_chat_ui_config(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> UIConfigRead:
    if tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")
    return ui_config_read(get_or_create_ui_config(db, tenant_id))


def _validate_storage_path(value: str) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    candidate = Path(normalized).expanduser()
    if not candidate.is_absolute():
        raise HTTPException(status_code=422, detail="文件存储目录必须是绝对路径")
    resolved = candidate.resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise HTTPException(status_code=422, detail="文件存储目录不能是文件系统根目录")
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=422, detail=f"无法创建文件存储目录：{exc}") from exc
    if not resolved.is_dir() or not os.access(resolved, os.W_OK | os.X_OK):
        raise HTTPException(status_code=422, detail="文件存储目录不可写")
    return str(resolved)


def _effective_storage_path(row: UIConfig) -> str:
    if not row.sandbox_enabled and row.harness_storage_path:
        return str(Path(row.harness_storage_path).expanduser().resolve())
    try:
        return str((paths.user_data_dir() / "harness_workspaces").resolve())
    except OSError:
        # Diagnostics and API reads must remain side-effect safe even when the
        # configured home directory is not writable (for example unit tests).
        return ""


_restart_lock = threading.Lock()
_restart_scheduled = False


def _schedule_application_restart(delay_seconds: float = 1.0) -> None:
    """Exit after the response is flushed; the service supervisor restarts StaffDeck."""

    global _restart_scheduled
    with _restart_lock:
        if _restart_scheduled:
            return
        _restart_scheduled = True

    def restart() -> None:
        os.kill(os.getpid(), signal.SIGTERM)

    timer = threading.Timer(delay_seconds, restart)
    timer.daemon = True
    timer.start()
