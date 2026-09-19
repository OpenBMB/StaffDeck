from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.auth import router
from app.db import get_session
from staffdeck_harness.runtime.control_auth import ControlLogin, ControlSubject


def test_control_refresh_is_httponly_path_scoped_and_logout_revokes(monkeypatch):
    from staffdeck_harness.runtime import control_auth
    subject = ControlSubject("user", "tenant", "operator", "Operator", "admin", "base_identity")
    calls = []
    class Provider:
        def login(self, *args):
            return ControlLogin("access-one", subject, "refresh-one")
        def refresh(self, token):
            calls.append(("refresh", token))
            return ControlLogin("access-two", subject, "refresh-two")
        def logout(self, token):
            calls.append(("logout", token))
    monkeypatch.setattr(control_auth, "provider", lambda: Provider())
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    sub = FastAPI()
    sub.include_router(router)
    def session():
        with Session(engine) as db:
            yield db
    sub.dependency_overrides[get_session] = session
    app = FastAPI()
    app.mount("/test", sub)
    with TestClient(app) as client:
        login = client.post("/test/api/auth/login", json={"tenant_id": "tenant", "username": "operator", "password": "test-only"})
        assert login.status_code == 200
        assert "refresh-one" not in login.text
        assert "HttpOnly" in login.headers["set-cookie"] and "Path=/test/api/auth" in login.headers["set-cookie"]
        rejected = client.post("/test/api/auth/refresh", headers={"Origin": "https://another.test"})
        assert rejected.status_code == 403 and not calls
        refreshed = client.post("/test/api/auth/refresh")
        assert refreshed.json()["token"] == "access-two"
        assert "refresh-two" not in refreshed.text
        assert client.post("/test/api/auth/logout").status_code == 200
        assert calls == [("refresh", "refresh-one"), ("logout", "refresh-two")]
        assert client.post("/test/api/auth/refresh").status_code == 401
    engine.dispose()


def test_first_password_challenge_is_opt_in_and_does_not_create_a_session(monkeypatch):
    from fastapi import HTTPException
    from staffdeck_harness.runtime import control_auth
    from staffdeck_harness.runtime.control_auth import ControlPasswordChange
    calls = []
    class Provider:
        def begin_login(self, *args):
            return ControlPasswordChange('one-use-challenge', 'user')
        def login(self, *args):
            raise HTTPException(409, 'Complete first password change')
        def confirm_temporary_password(self, token, password):
            calls.append((token, password))
    monkeypatch.setattr(control_auth, 'provider', lambda: Provider())
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(router)
    def session():
        with Session(engine) as db: yield db
    app.dependency_overrides[get_session] = session
    with TestClient(app) as client:
        body = {'tenant_id': 'tenant', 'username': 'operator', 'password': 'initial-password'}
        assert client.post('/api/auth/login', json=body).status_code == 409
        result = client.post('/api/auth/login', json=body, headers={'X-StaffDeck-Auth-Contract': 'session-v1'})
        assert result.status_code == 200 and result.json()['password_change_required'] is True
        assert 'set-cookie' not in result.headers and 'token' not in result.json()
        confirm = {'token': result.json()['password_change_token'], 'new_password': 'test-new-password'}
        assert client.post('/api/auth/temporary-password/confirm', json=confirm, headers={'Origin': 'https://foreign.test'}).status_code == 403
        assert not calls
        assert client.post('/api/auth/temporary-password/confirm', json=confirm).status_code == 200
        assert calls == [('one-use-challenge', 'test-new-password')]
    engine.dispose()
