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
