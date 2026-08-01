"""OIDC 集成测试:内嵌一个最小 RS256 mock IdP(discovery/jwks/token/userinfo),

覆盖完整授权码 + PKCE 流程:authorize 302 → 回调换 token → ID token 校验 →
用户自动创建/稳定映射 → StaffDeck JWT 签发,以及 state 失效/重放等安全路径。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from joserfc.jwk import RSAKey
from joserfc.jwt import encode as jwt_encode
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.api.auth as auth_api
import app.api.oidc_auth as oidc_api
from app.config import get_settings
from app.db import get_session
from app.db.models import Tenant, User
from app.security.oidc import clear_oidc_cache

CLIENT_ID = "test-client"
CLIENT_SECRET = "test-secret"


class MockIdP:
    """线程内运行的极简 OIDC 身份提供方。"""

    def __init__(self) -> None:
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "mock-key-1"
        self._codes: dict[str, dict] = {}  # code -> claims
        self._nonce_by_code: dict[str, str] = {}

        server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self.server = server
        self.issuer = f"http://127.0.0.1:{server.server_address[1]}"
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)

    def __enter__(self) -> "MockIdP":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def discovery_url(self) -> str:
        return f"{self.issuer}/.well-known/openid-configuration"

    def _sign_id_token(self, claims: dict) -> str:
        pem = self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        key = RSAKey.import_key(pem, {"kid": self.kid, "use": "sig", "alg": "RS256"})
        return jwt_encode({"alg": "RS256", "kid": self.kid}, claims, key)

    def _jwks(self) -> dict:
        pem = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        jwk = RSAKey.import_key(pem).as_dict()
        return {"keys": [{**jwk, "kid": self.kid, "use": "sig", "alg": "RS256"}]}

    def _discovery(self) -> dict:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "userinfo_endpoint": f"{self.issuer}/userinfo",
            "jwks_uri": f"{self.issuer}/jwks",
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "code_challenge_methods_supported": ["S256"],
        }

    def issue_code(self, nonce: str, sub: str, preferred_username: str, display_name: str) -> str:
        """按 IDP 侧语义生成一次性授权码(测试直接调用,模拟用户完成 IdP 侧认证)。"""
        code = f"mock-code-{len(self._codes) + 1}"
        now = int(time.time())
        self._codes[code] = {
            "iss": self.issuer,
            "sub": sub,
            "aud": CLIENT_ID,
            "exp": now + 3600,
            "iat": now,
            "nonce": nonce,
            "preferred_username": preferred_username,
            "email": f"{preferred_username}@example.com",
            "name": display_name,
        }
        self._nonce_by_code[code] = nonce
        return code

    def handle(self, path: str, method: str, body: bytes | None, auth_header: str | None):
        if path == "/.well-known/openid-configuration" and method == "GET":
            return 200, self._discovery()
        if path == "/jwks" and method == "GET":
            return 200, self._jwks()
        if path == "/userinfo" and method == "GET":
            token = (auth_header or "").removeprefix("Bearer ").strip()
            code = token.removeprefix("mock-access-")
            claims = self._codes.get(code)
            if not claims:
                return 401, {"error": "invalid_token"}
            return 200, {
                "sub": claims["sub"],
                "preferred_username": claims["preferred_username"],
                "email": claims["email"],
                "name": claims["name"],
            }
        if path == "/token" and method == "POST":
            params = parse_qs(body.decode("utf-8")) if body else {}
            code = params.get("code", [""])[0]
            claims = self._codes.get(code)
            if not claims:
                return 400, {"error": "invalid_grant"}
            return 200, {
                "access_token": f"mock-access-{code}",
                "token_type": "Bearer",
                "expires_in": 3600,
                "id_token": self._sign_id_token(claims),
            }
        return 404, {"error": "not_found"}


def _make_handler(idp: MockIdP):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002 - 静默标准库日志
            pass

        def _respond(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            status, payload = idp.handle(
                urlparse(self.path).path,
                self.command,
                body,
                self.headers.get("Authorization"),
            )
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._respond()

        def do_POST(self):
            self._respond()

    return Handler


@pytest.fixture()
def oidc_env(monkeypatch):
    """启用 OIDC 配置并指向 mock IdP;测试结束清理 settings/discovery 缓存。"""
    with MockIdP() as idp:
        monkeypatch.setenv("OIDC_ENABLED", "true")
        monkeypatch.setenv("OIDC_ISSUER", idp.issuer)
        monkeypatch.setenv("OIDC_CLIENT_ID", CLIENT_ID)
        monkeypatch.setenv("OIDC_CLIENT_SECRET", CLIENT_SECRET)
        monkeypatch.setenv("OIDC_TENANT_ID", "tenant_demo")
        monkeypatch.setenv("OIDC_DEFAULT_ROLE", "member")
        monkeypatch.setenv("OIDC_AUTO_PROVISION", "true")
        get_settings.cache_clear()
        clear_oidc_cache()
        yield idp
        get_settings.cache_clear()
        clear_oidc_cache()


@pytest.fixture()
def client(oidc_env):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.commit()

    app = FastAPI()
    app.include_router(auth_api.router)
    app.include_router(oidc_api.router)

    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    return TestClient(app), engine


def _parse_authorize_location(location: str) -> tuple[str, str, str]:
    """从 IdP 授权 URL 提取 state/nonce/code_challenge。"""
    params = parse_qs(urlparse(location).query)
    return params["state"][0], params["nonce"][0], params["code_challenge"][0]


def _extract_oidc_token(location: str) -> str:
    assert location.startswith("/login#oidc_token="), location
    return location.removeprefix("/login#oidc_token=")


def _run_full_login(client: TestClient, idp: MockIdP, sub: str, username: str, name: str) -> str:
    """完整 SSO 流程:authorize → 模拟 IdP 认证签发 code → callback → 返回 StaffDeck JWT。"""
    resp = client.get("/api/auth/oidc/authorize", follow_redirects=False)
    assert resp.status_code == 302
    state, nonce, challenge = _parse_authorize_location(resp.headers["location"])
    assert challenge, "PKCE S256 challenge 应存在"
    code = idp.issue_code(nonce, sub, username, name)
    resp = client.get(f"/api/auth/oidc/callback?code={code}&state={state}", follow_redirects=False)
    assert resp.status_code == 302, resp.text
    return _extract_oidc_token(resp.headers["location"])


def test_config_disabled_by_default(monkeypatch):
    monkeypatch.delenv("OIDC_ENABLED", raising=False)
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    get_settings.cache_clear()
    clear_oidc_cache()
    app = FastAPI()
    app.include_router(oidc_api.router)
    resp = TestClient(app).get("/api/auth/oidc/config")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "name": "SSO"}
    get_settings.cache_clear()


def test_config_enabled(oidc_env, client):
    test_client, _ = client
    resp = test_client.get("/api/auth/oidc/config")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["enabled"] is True
    assert payload["name"] == f"127.0.0.1:{urlparse(oidc_env.issuer).port}"


def test_authorize_redirects_to_idp(oidc_env, client):
    test_client, _ = client
    resp = test_client.get("/api/auth/oidc/authorize", follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith(f"{oidc_env.issuer}/authorize?")
    params = parse_qs(urlparse(location).query)
    assert params["client_id"][0] == CLIENT_ID
    assert params["response_type"][0] == "code"
    assert params["scope"][0] == "openid profile email"
    assert params["code_challenge_method"][0] == "S256"
    assert params["redirect_uri"][0] == "http://testserver/api/auth/oidc/callback"


def test_full_login_flow_auto_provisions_user(oidc_env, client):
    test_client, engine = client
    token = _run_full_login(test_client, oidc_env, sub="sub-1", username="alice", name="Alice")

    me = test_client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    user = me.json()
    assert user["username"] == "alice"
    assert user["display_name"] == "Alice"
    assert user["role"] == "member"
    assert user["source"] == "oidc"
    assert user["tenant_id"] == "tenant_demo"

    with Session(engine) as db:
        rows = db.exec(select(User)).all()
        assert len(rows) == 1
        assert rows[0].oidc_sub == "sub-1"
        assert rows[0].username == "alice"


def test_same_sub_maps_to_same_user(oidc_env, client):
    test_client, engine = client
    first = _run_full_login(test_client, oidc_env, sub="sub-2", username="bob", name="Bob")
    second = _run_full_login(test_client, oidc_env, sub="sub-2", username="bob", name="Bob")

    me1 = test_client.get("/api/auth/me", headers={"Authorization": f"Bearer {first}"}).json()
    me2 = test_client.get("/api/auth/me", headers={"Authorization": f"Bearer {second}"}).json()
    assert me1["id"] == me2["id"]
    with Session(engine) as db:
        rows = db.exec(select(User)).all()
        assert len(rows) == 1


def test_username_collision_gets_suffix(oidc_env, client):
    test_client, engine = client
    # 预置一个同 usernam 的 web 账号,触发自动建号时加后缀
    with Session(engine) as db:
        db.add(
            User(id="user_existing", tenant_id="tenant_demo", username="alice", password_hash="x")
        )
        db.commit()

    _run_full_login(test_client, oidc_env, sub="sub-3", username="alice", name="Alice")
    with Session(engine) as db:
        rows = db.exec(select(User).where(User.source == "oidc")).all()
        assert len(rows) == 1
        assert rows[0].username == "alice_2"
        assert rows[0].oidc_sub == "sub-3"


def test_invalid_state_redirects_to_error(oidc_env, client):
    test_client, _ = client
    resp = test_client.get(
        "/api/auth/oidc/callback?code=mock-code-9&state=forged-state",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/login?oidc_error=")


def test_state_replay_is_rejected(oidc_env, client):
    test_client, _ = client
    resp = test_client.get("/api/auth/oidc/authorize", follow_redirects=False)
    state, nonce, _ = _parse_authorize_location(resp.headers["location"])
    code = oidc_env.issue_code(nonce, "sub-4", "carol", "Carol")

    first = test_client.get(
        f"/api/auth/oidc/callback?code={code}&state={state}", follow_redirects=False
    )
    assert first.status_code == 302
    assert first.headers["location"].startswith("/login#oidc_token=")

    # state 已一次性消费:重放必须被拒绝
    replay = test_client.get(
        f"/api/auth/oidc/callback?code={code}&state={state}", follow_redirects=False
    )
    assert replay.status_code == 302
    assert replay.headers["location"].startswith("/login?oidc_error=")


def test_auto_provision_disabled_rejects_unknown_user(oidc_env, client, monkeypatch):
    monkeypatch.setenv("OIDC_AUTO_PROVISION", "false")
    get_settings.cache_clear()
    clear_oidc_cache()
    test_client, _ = client
    resp = test_client.get("/api/auth/oidc/authorize", follow_redirects=False)
    state, nonce, _ = _parse_authorize_location(resp.headers["location"])
    code = oidc_env.issue_code(nonce, "sub-5", "dave", "Dave")
    resp = test_client.get(
        f"/api/auth/oidc/callback?code={code}&state={state}", follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/login?oidc_error=")
