from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api import ui_config as ui_config_module
from app.api.ui_config import UIConfigUpdateRequest, ui_config_read
from app.api.ui_config import update_enterprise_ui_config
from app.core.agent_loop import AgentLoop
from app.db.models import Tenant, UIConfig, User
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine
from app.harness.sandbox import SandboxDiagnostics


def test_runtime_settings_action_limit_matches_backend_contract() -> None:
    request = UIConfigUpdateRequest(tenant_id="tenant_demo")

    assert request.agent_loop_max_actions == 32
    assert UIConfig(tenant_id="tenant_demo").agent_loop_max_actions == 32
    with pytest.raises(ValidationError):
        UIConfigUpdateRequest(tenant_id="tenant_demo", agent_loop_max_actions=101)


def test_agent_loop_honors_runtime_settings_action_limit() -> None:
    class FakeDatabase:
        def get(self, _model: object, _tenant_id: str) -> UIConfig:
            return UIConfig(tenant_id="tenant_demo", agent_loop_max_actions=100)

    loop = object.__new__(AgentLoop)
    loop.db = FakeDatabase()

    assert loop._get_agent_loop_max_actions("tenant_demo") == 100


def test_ui_config_read_fails_closed_for_unknown_network_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ui_config_module,
        "diagnostics",
        lambda: SandboxDiagnostics(
            status="ready",
            code=None,
            message="沙盒可用（srt）。",
            backend="srt",
        ),
    )
    row = UIConfig(
        tenant_id="tenant_demo",
        sandbox_enabled=True,
        sandbox_network_mode="legacy",
        sandbox_allowed_domains=[" api.example.com ", "", "*.example.org"],
    )

    result = ui_config_read(row)

    assert result.sandbox_network_mode == "deny"
    assert result.sandbox_allowed_domains == ["api.example.com", "*.example.org"]
    assert result.sandbox_status == "ready"
    assert result.sandbox_backend == "srt"


def test_windows_setup_prompt_is_based_on_backend_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ui_config_module.sys, "platform", "win32")
    monkeypatch.setattr(
        ui_config_module,
        "diagnostics",
        lambda: SandboxDiagnostics(
            status="unavailable",
            code="SANDBOX_WINDOWS_SETUP_REQUIRED",
            message="Windows sandbox setup is required.",
            remediation="Run the installer as administrator.",
            backend="srt",
        ),
    )
    monkeypatch.setattr(
        ui_config_module,
        "windows_install_command",
        lambda: "node srt-cli.js windows-install",
    )

    result = ui_config_read(UIConfig(tenant_id="tenant_demo", sandbox_enabled=True))

    assert result.sandbox_setup_required is True
    assert "PowerShell 或 CMD" in (result.sandbox_setup_instructions or "")
    assert "node srt-cli.js windows-install" in (result.sandbox_setup_instructions or "")


def test_sandbox_is_disabled_by_default_without_running_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ui_config_module,
        "diagnostics",
        lambda: pytest.fail("disabled sandbox must not be probed"),
    )

    result = ui_config_read(UIConfig(tenant_id="tenant_demo"))

    assert result.sandbox_enabled is False
    assert result.sandbox_status == "disabled"
    assert result.sandbox_backend == "disabled"


def test_sandbox_toggle_schedules_application_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    scheduled: list[bool] = []
    monkeypatch.setattr(ui_config_module, "_schedule_application_restart", lambda: scheduled.append(True))
    monkeypatch.setattr(
        ui_config_module,
        "diagnostics",
        lambda: SandboxDiagnostics(status="ready", code=None, message="ok", backend="srt"),
    )
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.commit()
        admin = User(
            id="user_admin",
            tenant_id="tenant_demo",
            username="admin",
            password_hash="unused",
            role="admin",
        )
        result = update_enterprise_ui_config(
            UIConfigUpdateRequest(tenant_id="tenant_demo", sandbox_enabled=True),
            db,
            admin,
        )

    assert result.restart_scheduled is True
    assert scheduled == [True]
