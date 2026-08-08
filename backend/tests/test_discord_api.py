from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.api.channels as channels_api
from app.channels.adapters.discord import DiscordPermanentError
from app.channels.crypto import decrypt_channel_secret
from app.db import get_session
from app.db.models import AgentProfile, ChannelBinding, Tenant, User
from app.security.auth import create_access_token


def _engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _client(engine) -> TestClient:
    app = FastAPI()
    app.include_router(channels_api.router)

    def override_session():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


def _seed(engine) -> User:
    with Session(engine) as db:
        db.add(Tenant(id="tenant_a", name="A"))
        owner = User(
            id="user_owner",
            tenant_id="tenant_a",
            username="owner",
            password_hash="x",
        )
        db.add(owner)
        db.add(
            AgentProfile(
                id="agent_a",
                tenant_id="tenant_a",
                name="Agent A",
                metadata_json={"owner_user_id": owner.id},
            )
        )
        db.commit()
        db.refresh(owner)
        db.expunge(owner)
        return owner


def _auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user)}"}


def _create_binding(client: TestClient, owner: User) -> str:
    created = client.post(
        "/api/enterprise/channels",
        json={"tenant_id": "tenant_a", "agent_id": "agent_a", "channel": "discord"},
        headers=_auth(owner),
    )
    assert created.status_code == 200
    return created.json()["id"]


def test_discord_binding_credentials_activate_without_exposing_secret(monkeypatch) -> None:
    engine = _engine()
    owner = _seed(engine)
    client = _client(engine)
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: False)
    monkeypatch.setattr(
        channels_api,
        "validate_discord_credentials",
        lambda token: {"bot_id": "bot-123", "bot_name": "StaffDeck Bot"},
    )

    binding_id = _create_binding(client, owner)
    response = client.post(
        f"/api/enterprise/channels/{binding_id}/discord/credentials",
        json={"tenant_id": "tenant_a", "bot_token": "secret-token"},
        headers=_auth(owner),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "active"
    assert payload["bot_id"] == "bot-123"
    assert payload["bot_name"] == "StaffDeck Bot"
    assert "secret-token" not in response.text
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        assert decrypt_channel_secret(binding.credentials_enc) == "secret-token"
        assert binding.external_account_key == "discord:bot:7:bot-123"
        assert binding.config_revision == 1


def test_discord_empty_token_rejected(monkeypatch) -> None:
    engine = _engine()
    owner = _seed(engine)
    client = _client(engine)
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: False)
    binding_id = _create_binding(client, owner)
    response = client.post(
        f"/api/enterprise/channels/{binding_id}/discord/credentials",
        json={"tenant_id": "tenant_a", "bot_token": "   "},
        headers=_auth(owner),
    )
    assert response.status_code == 400
    assert "Bot Token" in response.json()["detail"]


def test_discord_permanent_validation_error_is_400(monkeypatch) -> None:
    engine = _engine()
    owner = _seed(engine)
    client = _client(engine)
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: False)
    monkeypatch.setattr(
        channels_api,
        "validate_discord_credentials",
        lambda token: (_ for _ in ()).throw(DiscordPermanentError("无效或已被吊销")),
    )
    binding_id = _create_binding(client, owner)
    response = client.post(
        f"/api/enterprise/channels/{binding_id}/discord/credentials",
        json={"tenant_id": "tenant_a", "bot_token": "bad-token"},
        headers=_auth(owner),
    )
    assert response.status_code == 400
    assert "无效" in response.json()["detail"]


def test_discord_bot_id_is_immutable_after_activation(monkeypatch) -> None:
    engine = _engine()
    owner = _seed(engine)
    with Session(engine) as db:
        binding = ChannelBinding(
            id="chan_discord",
            tenant_id="tenant_a",
            agent_id="agent_a",
            channel="discord",
            status="active",
            config_json={"bot_id": "bot-old"},
            created_by_user_id=owner.id,
        )
        db.add(binding)
        db.commit()
    client = _client(engine)
    called = False

    def validate(_token):
        nonlocal called
        called = True
        return {"bot_id": "bot-new", "bot_name": "Bot"}

    monkeypatch.setattr(channels_api, "validate_discord_credentials", validate)
    response = client.post(
        "/api/enterprise/channels/chan_discord/discord/credentials",
        json={"tenant_id": "tenant_a", "bot_token": "secret"},
        headers=_auth(owner),
    )
    assert response.status_code == 400
    assert called is True


def test_channel_meta_exposes_discord_secret_field() -> None:
    engine = _engine()
    owner = _seed(engine)
    response = _client(engine).get(
        "/api/enterprise/channels/meta?tenant_id=tenant_a",
        headers=_auth(owner),
    )
    assert response.status_code == 200
    discord = next(row for row in response.json() if row["channel"] == "discord")
    fields = {field["key"]: field for field in discord["credential_fields"]}
    assert fields["bot_token"]["secret"] is True
