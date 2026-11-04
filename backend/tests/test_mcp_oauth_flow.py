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
        assert bearer(db, 't', 'u', 's', RESOURCE, client) is None


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
