from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.api.channels as channels_api
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


def test_feishu_binding_credentials_activate_without_exposing_secret(monkeypatch) -> None:
    engine = _engine()
    owner = _seed(engine)
    client = _client(engine)
    monkeypatch.setattr(channels_api, "channel_services_enabled", lambda: False)
    monkeypatch.setattr(
        channels_api,
        "validate_feishu_credentials",
        lambda app_id, secret: {"bot_open_id": "ou_bot", "bot_name": "StaffDeck Bot"},
    )

    created = client.post(
        "/api/enterprise/channels",
        json={"tenant_id": "tenant_a", "agent_id": "agent_a", "channel": "feishu"},
        headers=_auth(owner),
    )
    assert created.status_code == 200
    binding_id = created.json()["id"]
    response = client.post(
        f"/api/enterprise/channels/{binding_id}/feishu/credentials",
        json={"tenant_id": "tenant_a", "app_id": "cli_app", "app_secret": "secret-value"},
        headers=_auth(owner),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "active"
    assert payload["app_id"] == "cli_app"
    assert payload["bot_open_id"] == "ou_bot"
    assert payload["bot_name"] == "StaffDeck Bot"
    assert payload["provider_tenant_key"] is None
    assert "secret-value" not in response.text
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        assert decrypt_channel_secret(binding.credentials_enc) == "secret-value"
        assert binding.external_account_key == "feishu:app:7:cli_app"
        assert binding.config_revision == 1


def test_rejected_credentials_are_structured_and_never_saved(monkeypatch,caplog) -> None:
    from app.channels.adapters.feishu import FeishuCredentialError
    engine=_engine();owner=_seed(engine);client=_client(engine)
    monkeypatch.setattr(channels_api,'channel_services_enabled',lambda:False)
    binding=client.post('/api/enterprise/channels',json={'tenant_id':'tenant_a','agent_id':'agent_a','channel':'feishu'},headers=_auth(owner)).json()
    def reject(*args):
        raise FeishuCredentialError('飞书拒绝校验',code='FEISHU_CREDENTIALS_REJECTED',stage='tenant_token',provider_code=10003)
    monkeypatch.setattr(channels_api,'validate_feishu_credentials',reject)
    result=client.post(f"/api/enterprise/channels/{binding['id']}/feishu/credentials",
        json={'tenant_id':'tenant_a','app_id':'cli_test','app_secret':'do-not-expose-secret'},headers=_auth(owner))
    assert result.status_code==400 and result.json()['detail']['code']=='FEISHU_CREDENTIALS_REJECTED'
    assert result.json()['detail']['stage']=='tenant_token' and result.json()['detail']['provider_code']==10003
    assert 'do-not-expose-secret' not in result.text+caplog.text
    with Session(engine) as db:
        row=db.get(ChannelBinding,binding['id'])
        assert not row.credentials_enc and row.config_revision==0 and not (row.config_json or {}).get('app_id')


def test_feishu_app_id_is_immutable_after_activation(monkeypatch) -> None:
    engine = _engine()
    owner = _seed(engine)
    with Session(engine) as db:
        binding = ChannelBinding(
            id="chan_feishu",
            tenant_id="tenant_a",
            agent_id="agent_a",
            channel="feishu",
            status="active",
            config_json={"app_id": "cli_old"},
            created_by_user_id=owner.id,
        )
        db.add(binding)
        db.commit()
    client = _client(engine)
    called = False

    def validate(_app_id, _secret):
        nonlocal called
        called = True
        return {"bot_open_id": "ou_bot", "bot_name": "Bot"}

    monkeypatch.setattr(channels_api, "validate_feishu_credentials", validate)
    response = client.post(
        "/api/enterprise/channels/chan_feishu/feishu/credentials",
        json={"tenant_id": "tenant_a", "app_id": "cli_new", "app_secret": "secret"},
        headers=_auth(owner),
    )
    assert response.status_code == 400
    assert called is False


def test_channel_meta_exposes_feishu_secret_field() -> None:
    engine = _engine()
    owner = _seed(engine)
    response = _client(engine).get(
        "/api/enterprise/channels/meta?tenant_id=tenant_a",
        headers=_auth(owner),
    )
    assert response.status_code == 200
    feishu = next(row for row in response.json() if row["channel"] == "feishu")
    fields = {field["key"]: field for field in feishu["credential_fields"]}
    assert fields["app_id"]["secret"] is False
    assert fields["app_secret"]["secret"] is True
