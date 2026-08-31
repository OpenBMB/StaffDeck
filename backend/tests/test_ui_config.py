from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api import ui_config as ui_config_module
from app.api.ui_config import UIConfigUpdateRequest, ui_config_read, update_enterprise_ui_config
from app.core.agent_loop import AgentLoop
from app.core.harness_session_cleanup import harness_storage_root
from app.db.models import Tenant, UIConfig, User
from app.harness.sandbox import SandboxDiagnostics


def test_runtime_settings_action_limit_matches_backend_contract() -> None:
    request = UIConfigUpdateRequest(tenant_id="tenant_demo")
    row = UIConfig(tenant_id="tenant_demo")

    assert request.agent_loop_max_actions == 32
    assert row.agent_loop_max_actions == 32
    assert request.context_token_budget == row.context_token_budget == 32_000
    assert request.context_compaction_trigger_ratio == 0.70
    assert request.context_recent_round_limit == 6
    assert request.context_allowed_roles == ["user", "assistant"]
    with pytest.raises(ValidationError):
        UIConfigUpdateRequest(tenant_id="tenant_demo", agent_loop_max_actions=101)


def test_harness_workspace_config_uses_user_default_and_preserves_stable_path_contract(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The UI config field affects only the Harness root and rejects relative paths semantically."""

    home = tmp_path / "home"
    configured_root = tmp_path / "configured-harness-workspace"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "app-data"))

    default_read = ui_config_read(UIConfig(tenant_id="tenant_demo"))
    assert default_read.effective_harness_storage_path == str(home / ".staffdeck" / "workspaces")

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
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

        updated = update_enterprise_ui_config(
            UIConfigUpdateRequest(
                tenant_id="tenant_demo",
                harness_storage_path=str(configured_root),
            ),
            db,
            admin,
        )

        assert updated.harness_storage_path == str(configured_root.resolve())
        assert updated.effective_harness_storage_path == str(configured_root.resolve())
        assert harness_storage_root(tenant_id="tenant_demo", db=db) == configured_root.resolve()

        with pytest.raises(HTTPException) as error:
            update_enterprise_ui_config(
                UIConfigUpdateRequest(
                    tenant_id="tenant_demo",
                    harness_storage_path="relative-workspace",
                ),
                db,
                admin,
            )

    assert error.value.status_code == 422
    assert error.value.detail == {"code": "HARNESS_WORKSPACE_PATH_ABSOLUTE"}


def test_harness_workspace_config_treats_legacy_whitespace_as_the_default_root(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitespace-only stored path must not resolve to the server working directory."""

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    result = ui_config_read(
        UIConfig(
            tenant_id="tenant_demo",
            sandbox_enabled=False,
            harness_storage_path="  ",
        )
    )

    assert result.effective_harness_storage_path == str(home / ".staffdeck" / "workspaces")


def test_context_runtime_settings_validate_related_limits() -> None:
    with pytest.raises(ValidationError, match="不能超过上下文预算"):
        UIConfigUpdateRequest(
            tenant_id="tenant_demo",
            context_token_budget=1_000,
            context_long_summary_token_budget=600,
            context_medium_summary_token_budget=600,
        )
    with pytest.raises(ValidationError):
        UIConfigUpdateRequest(
            tenant_id="tenant_demo",
            context_allowed_roles=[],
        )


def test_agent_loop_reads_tenant_context_runtime_settings() -> None:
    class FakeDatabase:
        def get(self, _model: object, _tenant_id: str) -> UIConfig:
            return UIConfig(
                tenant_id="tenant_demo",
                context_token_budget=48_000,
                context_compaction_trigger_ratio=0.55,
                context_recent_round_limit=9,
                context_long_summary_token_budget=3_000,
                context_medium_summary_token_budget=2_000,
                context_allowed_roles=["assistant"],
                context_long_summary_prefix="长期：",
                context_medium_summary_prefix="近期：",
            )

    loop = object.__new__(AgentLoop)
    loop.db = FakeDatabase()

    settings = loop._get_conversation_context_settings("tenant_demo")

    assert settings.token_budget == 48_000
    assert settings.compaction_trigger_ratio == 0.55
    assert settings.recent_round_limit == 9
    assert settings.allowed_roles == frozenset({"assistant"})
    assert settings.long_summary_prefix == "长期："
    assert settings.medium_summary_prefix == "近期："


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


def test_context_runtime_settings_persist_without_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    scheduled: list[bool] = []
    monkeypatch.setattr(
        ui_config_module,
        "_schedule_application_restart",
        lambda: scheduled.append(True),
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
            UIConfigUpdateRequest(
                tenant_id="tenant_demo",
                context_token_budget=48_000,
                context_compaction_trigger_ratio=0.65,
                context_recent_round_limit=8,
                context_long_summary_token_budget=5_000,
                context_medium_summary_token_budget=3_000,
                context_allowed_roles=["user"],
                context_long_summary_prefix="长期记忆：",
                context_medium_summary_prefix="近期记忆：",
            ),
            db,
            admin,
        )

    assert result.context_token_budget == 48_000
    assert result.context_compaction_trigger_ratio == 0.65
    assert result.context_recent_round_limit == 8
    assert result.context_allowed_roles == ["user"]
    assert result.context_long_summary_prefix == "长期记忆："
    assert result.context_medium_summary_prefix == "近期记忆："
    assert result.restart_scheduled is False
    assert scheduled == []
