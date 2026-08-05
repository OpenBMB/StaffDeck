from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api import ui_config as ui_config_module
from app.api.ui_config import UIConfigUpdateRequest, ui_config_read
from app.core.agent_loop import AgentLoop
from app.db.models import UIConfig
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

    result = ui_config_read(UIConfig(tenant_id="tenant_demo"))

    assert result.sandbox_setup_required is True
    assert "PowerShell 或 CMD" in (result.sandbox_setup_instructions or "")
    assert "node srt-cli.js windows-install" in (result.sandbox_setup_instructions or "")
