import asyncio
from contextlib import nullcontext
from types import SimpleNamespace as NS

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse
from app.api_enterprise.compat import BusinessContractMiddleware, Plan
from staffdeck_harness.contracts.runtime_services import ServiceResponse


def test_replayed_body_preserves_real_disconnect():
    async def check():
        calls = []
        async def original():
            calls.append(True)
            return {'type': 'http.disconnect'}
        receive = BusinessContractMiddleware._replay(b'changed', original)
        assert (await receive())['body'] == b'changed'
        assert (await receive())['type'] == 'http.disconnect'
        assert calls == [True]
    asyncio.run(check())


def test_shared_chat_sse_still_finishes_after_body_rewrite():
    app = FastAPI()
    from fastapi import Request
    @app.post('/api/chat/stream')
    async def stream(request: Request):
        assert (await request.json())['message'] == 'hello'
        async def events():
            await asyncio.sleep(0)
            yield 'event: complete\ndata: {"reply":"ok"}\n\n'
        return StreamingResponse(events(), media_type='text/event-stream')
    with TestClient(BusinessContractMiddleware(app)) as client:
        result = client.post('/api/agent-platform/chat/stream', json={'tenant_id': 't', 'message': 'hello'})
    assert result.status_code == 200 and '"reply":"ok"' in result.text


@pytest.mark.parametrize('path', ['/api/knowledge/v1/knowledge-bases/square', '/api/skills/skills?view=plaza',
    '/api/admin/plaza-categories', '/api/organization/tree', '/api/agent-tools/connectors'])
def test_missing_enterprise_feature_is_explicit_not_empty_success(path):
    with TestClient(BusinessContractMiddleware(FastAPI())) as client:
        result = client.get(path)
    assert result.status_code == 501
    assert result.json()['detail']['code'] == 'FEATURE_UNAVAILABLE'


def test_native_resource_contract_preserves_query_body_and_response(monkeypatch):
    from staffdeck_harness.modules import registry
    from staffdeck_harness.runtime import control_auth
    commands = []
    def handle(command):
        commands.append(command)
        return ServiceResponse(200, {'content-type': 'application/json'},
            b'{"items":[{"id":"k","permissions":{"canEdit":false}}],"next_cursor":"next"}')
    item = NS(provider=NS(handle_frontend=handle), manifest=NS(metadata={'management_domain': 'knowledge'}))
    monkeypatch.setattr(registry, 'peek_registry', lambda: NS(providers=lambda slot: [item], turn_lease=nullcontext))
    monkeypatch.setattr(control_auth, 'provider', lambda: NS(current=lambda token: NS(tenant_id='t', user_id='u', provider='base_identity')))
    with TestClient(BusinessContractMiddleware(FastAPI())) as client:
        result = client.patch('/api/knowledge/v1/knowledge-bases/k?limit=1&cursor=next&tag=a&tag=b',
            headers={'Authorization': 'Bearer verified'}, json={'capabilities': ['ontology'], 'accessScope': 'department'})
    assert result.status_code == 200
    assert result.json()['items'][0]['permissions']['canEdit'] is False
    assert commands[0].query[-2:] == (('tag', 'a'), ('tag', 'b'))
    assert b'ontology' in commands[0].body


def test_native_management_never_accepts_unauthenticated_requests(monkeypatch):
    from staffdeck_harness.modules import registry
    item = NS(provider=NS(handle_frontend=lambda _: pytest.fail('not authenticated')), manifest=NS(metadata={'management_domain': 'skill'}))
    monkeypatch.setattr(registry, 'peek_registry', lambda: NS(providers=lambda slot: [item]))
    with TestClient(BusinessContractMiddleware(FastAPI())) as client:
        result = client.get('/api/skills/skills/local/name/versions/v1/file')
    assert result.status_code == 401


def test_projection_failure_is_a_contract_error_not_original_200():
    async def check():
        sent = []
        async def send(message): sent.append(message)
        def broken(*args): raise ValueError('bad shape')
        projected = BusinessContractMiddleware._projecting_send(send, Plan(response=broken), {'rel': '/resource'})
        await projected({'type': 'http.response.start', 'status': 200, 'headers': [(b'content-type', b'application/json')]})
        await projected({'type': 'http.response.body', 'body': b'{"old_shape":true}'})
        assert sent[0]['status'] == 502 and b'FRONTEND_RESPONSE_INVALID' in sent[1]['body']
    asyncio.run(check())


def test_runtime_owned_endpoints_do_not_forward_to_resource_services():
    from staffdeck_harness.runtime.frontend_management import shared_target, frontend_domain
    assert shared_target('/api/agent-control-plane/agents/a/work-record', 'GET') == '/api/enterprise/agents/a/work-record'
    assert shared_target('/api/agent-tools/tools/t/test', 'POST') == '/api/enterprise/tools/t/test'
    assert frontend_domain('/api/agent-platform/chat/stream') is None
    assert shared_target('/api/agent-control-plane/agents/gallery', 'GET') == '/api/enterprise/agents/gallery'


def test_directory_projection_preserves_exact_source_actions():
    from app.api_enterprise.projections import agent_to_business
    row = {'id': 'a', 'metadata': {'directory_access': {'allowed_actions': ['view', 'edit']},
                                 'directory_statistics': {'chat_count': 12}}}
    result = agent_to_business(row, None)
    assert result['allowed_actions'] == ['view', 'edit']
    assert result['chat_count'] == 12
