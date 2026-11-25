from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import MCPServer, User
from app.tools.mcp_oauth import OAuthError, bearer, complete, prepare
from app.tools.mcp_oauth_store import load_credentials, save_credentials

RESOURCE = 'https://resource.example/mcp'


def test_authorization_state_refresh_and_persistent_recovery(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'flow.db'}")
    SQLModel.metadata.create_all(engine)
    requests = []

    def provider(request):
        requests.append(request)
        path = request.url.path
        if path == '/mcp':
            return httpx.Response(401, headers={'WWW-Authenticate':
                'Bearer resource_metadata="https://resource.example/mounted/metadata"'})
        if path == '/mounted/metadata':
            body = {'resource': RESOURCE, 'authorization_servers': ['https://auth.example'],
                    'scopes_supported': ['mcp:tools']}
        elif 'oauth-authorization-server' in path:
            body = {'issuer': 'https://auth.example', 'authorization_endpoint': 'https://auth.example/authorize',
                    'token_endpoint': 'https://auth.example/token',
                    'registration_endpoint': 'https://auth.example/register',
                    'code_challenge_methods_supported': ['S256']}
        elif path == '/register':
            body = {'client_id': 'test-client'}
        else:
            form = parse_qs(request.content.decode())
            if form['grant_type'] == ['authorization_code']:
                assert form['code_verifier'][0]
                assert form['resource'] == [RESOURCE]
            body = {'access_token': 'access', 'token_type': 'Bearer',
                    'refresh_token': 'refresh', 'expires_in': 3600}
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        with Session(engine) as db:
            db.add(User(id='u', tenant_id='t', username='u', password_hash='test'))
            db.add(MCPServer(id='s', tenant_id='t', name='s', url=RESOURCE))
            db.commit()
            url = prepare(db, 't', 'u', 's', RESOURCE, 'http://127.0.0.1:1234/callback', client)
            query = parse_qs(urlsplit(url).query)
            assert query['code_challenge_method'] == ['S256']
            state = query['state'][0]
            with pytest.raises(OAuthError, match='state'):
                complete(db, 't', 'u', 's', RESOURCE, 'wrong', 'code', client)
            complete(db, 't', 'u', 's', RESOURCE, state, 'code', client)
            with pytest.raises(OAuthError, match='state'):
                complete(db, 't', 'u', 's', RESOURCE, state, 'code', client)
        with Session(engine) as db:
            assert bearer(db, 't', 'u', 's', RESOURCE, client) == 'access'
            data = load_credentials(db, 't', 'u', 's')
            data['tokens']['expires_at'] = 0
            save_credentials(db, 't', 'u', 's', RESOURCE, data)
            assert bearer(db, 't', 'u', 's', RESOURCE, client) == 'access'
            assert parse_qs(requests[-1].content.decode())['grant_type'] == ['refresh_token']
            count = len(requests)
            assert bearer(db, 't', 'u', 's', RESOURCE, client) == 'access'
            assert len(requests) == count


def test_concurrent_refresh_resource_binding_and_disconnect(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from app.tools.mcp_oauth import disconnect

    engine = create_engine(f"sqlite:///{tmp_path / 'concurrent.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id='u', tenant_id='t', username='u', password_hash='test'))
        db.add(MCPServer(id='s', tenant_id='t', name='s', url=RESOURCE))
        db.commit()
        save_credentials(db, 't', 'u', 's', RESOURCE, {
            'client_id': 'client', 'token_endpoint': 'https://auth.example/token',
            'revocation_endpoint': 'https://auth.example/revoke',
            'tokens': {'access_token': 'old', 'refresh_token': 'refresh', 'expires_at': 0},
        })
    requests = []

    def provider(request):
        requests.append(request)
        if request.url.path == '/revoke':
            return httpx.Response(200)
        return httpx.Response(200, json={'access_token': 'fresh', 'token_type': 'Bearer',
                                        'expires_in': 3600, 'refresh_token': 'rotated'})

    def read_token(_):
        with Session(engine) as db, httpx.Client(transport=httpx.MockTransport(provider)) as client:
            return bearer(db, 't', 'u', 's', RESOURCE, client)

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(read_token, range(8))) == ['fresh'] * 8
    assert len(requests) == 1
    with Session(engine) as db, httpx.Client(transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(OAuthError, match='resource mismatch'):
            bearer(db, 't', 'u', 's', 'https://other.example/mcp', client)
        assert disconnect(db, 't', 'u', 's', client) is True
        assert parse_qs(requests[-1].content.decode())['token'] == ['rotated']
        with pytest.raises(OAuthError, match='authorization required'):
            bearer(db, 't', 'u', 's', RESOURCE, client)
        assert load_credentials(db, 't', 'u', 's') == {'disconnected': True}


def test_provider_errors_are_sanitized():
    from app.tools.mcp_oauth import _json, secure_url

    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
        400, json={'error': 'sensitive-provider-body'},
    ))) as client:
        with pytest.raises(OAuthError) as caught:
            _json(client, 'POST', 'https://auth.example/token')
        assert 'sensitive' not in str(caught.value)
    with pytest.raises(OAuthError):
        secure_url('http://remote.example/token')
    with pytest.raises(OAuthError):
        secure_url('https://user:password@remote.example/token')


def test_executor_uses_session_owner_and_keeps_shared_headers_private(tmp_path, monkeypatch):
    from app.db.models import ChatSession, Tool
    from app.tools.tool_executor import ToolExecutor
    from app.tools.tool_schema import ToolCall

    engine = create_engine(f"sqlite:///{tmp_path / 'executor.db'}")
    SQLModel.metadata.create_all(engine)
    captured = []
    monkeypatch.setattr('app.tools.tool_executor.execute_mcp_tool',
                        lambda config, *a, **kw: captured.append(config) or {'ok': True})
    with Session(engine) as db:
        db.add(User(id='alice', tenant_id='t', username='alice', password_hash='test'))
        db.add(User(id='bob', tenant_id='t', username='bob', password_hash='test'))
        server = MCPServer(id='s', tenant_id='t', name='s', url=RESOURCE)
        db.add(server)
        db.add(Tool(id='tool', tenant_id='t', name='lookup', tool_type='mcp',
                    mcp_server_id='s', method='POST', url='mcp://s/lookup',
                    config_json={'tool': 'lookup'}))
        db.add(ChatSession(id='chat', tenant_id='t', user_id='alice'))
        db.commit()
        save_credentials(db, 't', 'alice', 's', RESOURCE, {
            'tokens': {'access_token': 'alice-only', 'expires_at': 9999999999},
        })
        result = ToolExecutor(db).execute('t', ToolCall(name='lookup', arguments={}), session_id='chat')
        assert result.success
        assert captured[-1]['headers']['Authorization'] == 'Bearer alice-only'
        result = ToolExecutor(db).execute('t', ToolCall(name='lookup', arguments={}), user_id='bob')
        assert result.success
        assert 'Authorization' not in captured[-1].get('headers', {})
        db.refresh(server)
        assert server.headers_json == {}


def test_admin_and_member_authorize_independently_and_disconnect_blocks_calls(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI, Header
    from fastapi.testclient import TestClient

    from app.agents.branching import ensure_private_resource_binding
    from app.api import mcp_oauth as routes, tools
    from app.db import get_session
    from app.db.models import AgentProfile, AgentResourceBinding, ChatSession, Tenant, Tool
    from app.security.auth import get_current_user
    from app.tools.tool_executor import ToolExecutor
    from app.tools.tool_schema import ToolCall

    engine = create_engine(f"sqlite:///{tmp_path / 'integrated.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([
            Tenant(id="t", name="OAuth fixture"),
            User(id="alice", tenant_id="t", username="alice", role="admin", password_hash="fixture"),
            User(id="bob", tenant_id="t", username="bob", role="member", password_hash="fixture"),
            AgentProfile(id="bob-staff", tenant_id="t", name="Employee",
                         metadata_json={"owner_user_id": "bob"}),
            MCPServer(id="s", tenant_id="t", name="Remote test", url=RESOURCE,
                      headers_json={"Authorization": "Bearer shared-fixture"}),
            Tool(id="tool", tenant_id="t", name="lookup", url="mcp://s/lookup", method="POST",
                 tool_type="mcp", mcp_server_id="s", config_json={"tool": "lookup"}),
            ChatSession(id="bob-chat", tenant_id="t", agent_id="bob-staff", user_id="bob"),
        ])
        db.flush()
        ensure_private_resource_binding(db, "t", "bob-staff", "tool", "tool")
        db.commit()

    calls = []
    revocations = []

    def provider(request):
        path = request.url.path
        if path == "/mcp" and request.method == "GET":
            return httpx.Response(401, headers={"WWW-Authenticate":
                'Bearer scope="read:tools", resource_metadata="https://resource.example/metadata"'})
        if path == "/metadata":
            return httpx.Response(200, json={"resource": RESOURCE,
                "authorization_servers": ["https://auth.example"], "scopes_supported":["read:tools", "admin:all"]})
        if "oauth-authorization-server" in path:
            return httpx.Response(200, json={
                "issuer":"https://auth.example", "authorization_endpoint":"https://auth.example/login",
                "token_endpoint":"https://auth.example/token",
                "registration_endpoint":"https://auth.example/register",
                "revocation_endpoint":"https://auth.example/revoke",
                "code_challenge_methods_supported":["S256"],
            })
        if path == "/register":
            return httpx.Response(200, json={"client_id":"fixture"})
        if path == "/token":
            code = parse_qs(request.content.decode())["code"][0]
            return httpx.Response(200, json={"access_token":code, "token_type":"Bearer"})
        if path == "/revoke":
            revocations.append(parse_qs(request.content.decode())["token"][0])
            return httpx.Response(503)
        if path == "/mcp":
            import json

            data = json.loads(request.content)
            if "id" not in data:
                return httpx.Response(202)
            if data["method"] == "tools/call":
                auth = request.headers.get("Authorization")
                calls.append(auth)
                result = {"content":[{"type":"text", "text":auth}]}
            else:
                result = {"protocolVersion":"2025-03-26", "capabilities":{}, "serverInfo":{}}
            return httpx.Response(200, json={"jsonrpc":"2.0","id":data["id"],"result":result})
        pytest.fail(f"Unexpected provider request {path}")

    def mock_client(*, timeout=15, resource=None, admit=None):
        return httpx.Client(
            transport=httpx.MockTransport(provider),
            event_hooks={"request":[lambda _: admit()]} if admit else None,
        )

    monkeypatch.setattr("app.tools.mcp_oauth_http.oauth_http_client", mock_client)
    monkeypatch.setattr(routes, "oauth_http_client", mock_client)
    monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(tool_base_url="http://127.0.0.1:5173"))
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(routes.callback_router)
    app.include_router(tools.router)

    def sessions():
        with Session(engine) as db:
            yield db

    def actor(x_user: str = Header()):
        with Session(engine) as db:
            return db.get(User, x_user)

    app.dependency_overrides[get_session] = sessions
    app.dependency_overrides[get_current_user] = actor
    client = TestClient(app)
    endpoint = "/api/enterprise/mcp-oauth/s"
    assert client.post(endpoint + "/start?tenant_id=t", headers={"X-User":"bob"}).status_code == 404
    probe = client.post("/api/enterprise/tools/tool/test?agent_id=bob-staff",
                        headers={"X-User":"bob"}, json={"tenant_id":"t","arguments":{}})
    assert probe.status_code == 200 and not probe.json()["success"]
    assert calls == []

    callbacks = {}
    for user, scope in [("alice", ""), ("bob", "&agent_id=bob-staff")]:
        response = client.post(endpoint + "/start?tenant_id=t" + scope, headers={"X-User":user})
        assert response.status_code == 200, response.text
        query = parse_qs(urlsplit(response.json()["authorization_url"]).query)
        assert query["scope"] == ["read:tools"]
        redirect = urlsplit(query["redirect_uri"][0])
        callbacks[user] = (redirect.path, dict(parse_qs(redirect.query), state=query["state"], code=[user + "-token"]))
    path, bob_query = callbacks["bob"]
    wrong_user = {**bob_query, "user_id": ["alice"]}
    assert client.get(path, params=wrong_user).status_code == 400
    invalid_state = client.get(path, params={**bob_query, "state":["无效"]})
    assert invalid_state.status_code == 400 and invalid_state.headers["cache-control"] == "no-store"
    for path, values in callbacks.values():
        response = client.get(path, params=values)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"

    call = ToolCall(name="lookup", arguments={})
    with Session(engine) as db:
        executor = ToolExecutor(db)
        assert executor.execute("t", call, user_id="alice").success
        mismatch = executor.execute("t", call, session_id="bob-chat", agent_id="bob-staff", user_id="alice")
        assert not mismatch.success
    probe = client.post("/api/enterprise/tools/tool/test?agent_id=bob-staff",
                        headers={"X-User":"bob"}, json={"tenant_id":"t","arguments":{}})
    assert probe.status_code == 200 and probe.json()["success"], probe.text
    assert calls == ["Bearer alice-token", "Bearer bob-token"]
    from app.tools.mcp_client import MCPClientError, execute_mcp_tool
    from app.tools.mcp_oauth import authorized_config

    with Session(engine) as cached_db:
        config = authorized_config(cached_db, "t", "alice", "s",
                                   {"url":RESOURCE, "transport":"streamable_http"})
        response = client.delete(endpoint + "?tenant_id=t", headers={"X-User":"alice"})
        assert response.status_code == 200 and not response.json()["remote_revocation_confirmed"]
        with pytest.raises(MCPClientError, match="authorization changed"):
            execute_mcp_tool(config, {}, tool_name="lookup")
    assert revocations == ["alice-token"]
    assert not client.get(endpoint + "/status?tenant_id=t", headers={"X-User":"alice"}).json()["authorized"]
    with Session(engine) as db:
        assert load_credentials(db, "t", "alice", "s") == {"disconnected":True}
        executor = ToolExecutor(db)
        assert not executor.execute("t", call, user_id="alice").success
        assert executor.execute("t", call, session_id="bob-chat", agent_id="bob-staff").success
        assert db.get(MCPServer, "s").headers_json == {"Authorization":"Bearer shared-fixture"}
    assert calls == ["Bearer alice-token", "Bearer bob-token", "Bearer bob-token"]
    with Session(engine) as db:
        db.add(AgentResourceBinding(
            tenant_id="t", agent_id="bob-staff", resource_type="mcp_server", resource_id="s",
            status="deleted",
        ))
        db.commit()
    probe = client.post("/api/enterprise/tools/tool/test?agent_id=bob-staff",
                        headers={"X-User":"bob"}, json={"tenant_id":"t","arguments":{}})
    assert probe.status_code == 200 and not probe.json()["success"]
    assert calls == ["Bearer alice-token", "Bearer bob-token", "Bearer bob-token"]


def test_oauth_transport_pins_public_peer_and_rejects_private_or_foreign_peers(monkeypatch):
    import socket
    from app.tools.mcp_oauth_http import _pin_public_endpoint

    def resolve(host, port, **kwargs):
        address = "127.0.0.1" if host == "private.example" else "1.1.1.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    request = httpx.Request("POST", "https://resource.example/mcp")
    _pin_public_endpoint(request, resource=RESOURCE)
    assert request.url.host == "1.1.1.1"
    assert request.headers["Host"] == "resource.example"
    assert request.extensions["sni_hostname"] == "resource.example"
    with pytest.raises(httpx.ConnectError, match="public endpoint"):
        _pin_public_endpoint(httpx.Request("GET", "https://private.example/metadata"), resource=None)
    with pytest.raises(httpx.ConnectError, match="cross resource"):
        _pin_public_endpoint(httpx.Request("POST", "https://other.example/messages"), resource=RESOURCE)
