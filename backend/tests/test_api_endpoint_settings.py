from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

import desktop_launcher
from app.api.ui_config import network_router
from app.db.models import User
from app.security.auth import get_current_user


def _client_for(user: User) -> TestClient:
    """Build an isolated authenticated app so endpoint authorization is exercised directly."""
    app = FastAPI()
    app.include_router(network_router)
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _set_active_runtime(monkeypatch, *, mode: str = "local", port: int = 6204) -> None:
    """Provide the launcher-owned active runtime data without relying on request headers."""
    monkeypatch.setenv(
        "STAFFDECK_RUNTIME_NETWORK",
        json.dumps(
            {
                "mode": mode,
                "host": "127.0.0.1" if mode == "local" else "0.0.0.0",
                "port": port,
                "public_url": "https://staff.example.com" if mode == "public" else "",
            },
            separators=(",", ":"),
        ),
    )


def _admin_user() -> User:
    """Create the tenant administrator used by the network-settings API contract tests."""
    return User(
        id="admin",
        tenant_id="tenant_demo",
        username="admin",
        password_hash="unused",
        role="admin",
    )


def test_network_settings_read_uses_launcher_runtime_and_never_exposes_a_key(
    monkeypatch, tmp_path
) -> None:
    """Return a copyable local endpoint even when a caller forges the Host header."""
    monkeypatch.setattr(desktop_launcher, "user_data_dir", lambda: tmp_path)
    _set_active_runtime(monkeypatch, port=6204)
    client = _client_for(_admin_user())

    response = client.get(
        "/api/enterprise/network-settings?tenant_id=tenant_demo",
        headers={"Host": "attacker.example:9999"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["active_base_url"] == "http://127.0.0.1:6204/api/v1"
    assert payload["active_docs_url"] == "http://127.0.0.1:6204/api/v1/docs"
    assert payload["active_openapi_url"] == "http://127.0.0.1:6204/api/v1/openapi.json"
    assert payload["active_public_base_url"] is None
    assert "key" not in json.dumps(payload).lower()


def test_network_settings_read_supports_web_runtime_without_desktop_snapshot(
    monkeypatch, tmp_path
) -> None:
    """Return configured web endpoints without trusting the caller's Host header."""
    monkeypatch.setattr(desktop_launcher, "user_data_dir", lambda: tmp_path)
    monkeypatch.delenv("STAFFDECK_RUNTIME_NETWORK", raising=False)
    monkeypatch.setenv("APP_HOST", "0.0.0.0")
    monkeypatch.setenv("APP_PORT", "6205")
    monkeypatch.setenv("STAFFDECK_PUBLIC_URL", "https://staff.example.com")

    response = _client_for(_admin_user()).get(
        "/api/enterprise/network-settings?tenant_id=tenant_demo",
        headers={"Host": "attacker.example:9999"},
    )

    assert response.status_code == 200
    assert response.json()["active_base_url"] == "http://127.0.0.1:6205/api/v1"
    assert response.json()["active_public_base_url"] == "https://staff.example.com/api/v1"


def test_network_settings_read_keeps_app_host_and_port_configuration_paired(
    monkeypatch, tmp_path
) -> None:
    """Use the APP_PORT default when APP_HOST selects the Web supervisor listener."""
    monkeypatch.setattr(desktop_launcher, "user_data_dir", lambda: tmp_path)
    monkeypatch.delenv("STAFFDECK_RUNTIME_NETWORK", raising=False)
    monkeypatch.setenv("APP_HOST", "0.0.0.0")
    monkeypatch.delenv("APP_PORT", raising=False)
    monkeypatch.setenv("ULTRARAG_PORT", "6206")
    monkeypatch.delenv("STAFFDECK_PUBLIC_URL", raising=False)

    response = _client_for(_admin_user()).get(
        "/api/enterprise/network-settings?tenant_id=tenant_demo"
    )

    assert response.status_code == 200
    assert response.json()["active_base_url"] == "http://127.0.0.1:5173/api/v1"


def test_network_settings_rejects_members_and_unauthenticated_callers(monkeypatch) -> None:
    """Enforce the administrator guard on endpoint reads rather than trusting console routing."""
    _set_active_runtime(monkeypatch)
    member = User(
        id="member",
        tenant_id="tenant_demo",
        username="member",
        password_hash="unused",
        role="member",
    )

    assert _client_for(member).get(
        "/api/enterprise/network-settings?tenant_id=tenant_demo"
    ).status_code == 403
    other_tenant_admin = User(
        id="other-admin",
        tenant_id="tenant_other",
        username="other-admin",
        password_hash="unused",
        role="admin",
    )
    assert _client_for(other_tenant_admin).get(
        "/api/enterprise/network-settings?tenant_id=tenant_demo"
    ).status_code == 403
    unauthenticated_app = FastAPI()
    unauthenticated_app.include_router(network_router)
    assert TestClient(unauthenticated_app).get(
        "/api/enterprise/network-settings?tenant_id=tenant_demo"
    ).status_code == 401


def test_network_settings_save_preserves_active_port_and_writes_next_launch_config(
    monkeypatch, tmp_path
) -> None:
    """Save only next-launch state, keeping the active runtime snapshot unchanged."""
    monkeypatch.setattr(desktop_launcher, "user_data_dir", lambda: tmp_path)
    _set_active_runtime(monkeypatch, port=6204)
    client = _client_for(_admin_user())

    response = client.put(
        "/api/enterprise/network-settings",
        json={"tenant_id": "tenant_demo", "mode": "local", "port": 6206, "public_url": ""},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["active_base_url"] == "http://127.0.0.1:6204/api/v1"
    assert payload["pending_base_url"] == "http://127.0.0.1:6206/api/v1"
    assert payload["restart_required"] is True
    assert json.loads((tmp_path / "network.json").read_text(encoding="utf-8")) == {
        "mode": "local",
        "host": "127.0.0.1",
        "port": 6206,
        "public_url": "",
    }


def test_network_settings_save_rejects_unsafe_or_busy_values_without_replacing_file(
    monkeypatch, tmp_path
) -> None:
    """Reject invalid and occupied next-launch settings before touching the persisted file."""
    monkeypatch.setattr(desktop_launcher, "user_data_dir", lambda: tmp_path)
    _set_active_runtime(monkeypatch, port=6204)
    config_path = desktop_launcher._save_network_config("local", "", 6201)
    previous_content = config_path.read_text(encoding="utf-8")
    monkeypatch.setattr(desktop_launcher, "port_in_use", lambda _host, port: port == 6205)
    client = _client_for(_admin_user())

    invalid = client.put(
        "/api/enterprise/network-settings",
        json={
            "tenant_id": "tenant_demo",
            "mode": "public",
            "port": 6206,
            "public_url": "https://user:secret@staff.example.com",
        },
    )
    busy = client.put(
        "/api/enterprise/network-settings",
        json={"tenant_id": "tenant_demo", "mode": "lan", "port": 6205, "public_url": ""},
    )

    assert invalid.status_code == 422
    assert busy.status_code == 409
    assert config_path.read_text(encoding="utf-8") == previous_content


def test_network_settings_allows_reselecting_the_current_active_port(monkeypatch, tmp_path) -> None:
    """Allow the listener's own port even though a generic availability probe reports it busy."""
    monkeypatch.setattr(desktop_launcher, "user_data_dir", lambda: tmp_path)
    _set_active_runtime(monkeypatch, port=6204)
    monkeypatch.setattr(desktop_launcher, "port_in_use", lambda _host, _port: True)

    response = _client_for(_admin_user()).put(
        "/api/enterprise/network-settings",
        json={"tenant_id": "tenant_demo", "mode": "local", "port": 6204, "public_url": ""},
    )

    assert response.status_code == 200
    assert response.json()["restart_required"] is False
