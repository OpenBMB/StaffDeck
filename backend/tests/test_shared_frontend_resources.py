"""UI spelling aliases must traverse the same resource handlers and authorization."""
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from app.api_enterprise.compat import BusinessContractMiddleware
from app.api_enterprise.projections import Actor


@pytest.fixture
def client(monkeypatch):
    from staffdeck_harness.modules import registry
    monkeypatch.setattr(registry, 'peek_registry', lambda: None)
    async def actor(*args):
        return Actor('user', 'tenant', 'member')
    monkeypatch.setattr(BusinessContractMiddleware, '_actor', actor)
    app = FastAPI()
    def authorize(request):
        if request.headers.get('authorization') != 'Bearer allowed':
            raise HTTPException(401, 'login required')
        if request.query_params.get('tenant_id') != 'tenant':
            raise HTTPException(403, 'tenant mismatch')
    @app.get('/api/enterprise/general-skills')
    def skills(request: Request):
        authorize(request)
        return [{'id': str(i), 'slug': f'skill-{i}', 'name': f'Title {i}', 'status': status,
                 'description': 'searchable', 'updated_at': f'2026-09-{i:02}',
                 'metadata': {'gallery_category': 'dev'}, 'skill_markdown': '# Real content'}
                for i, status in enumerate(['published', 'published', 'draft'])]
    @app.get('/api/enterprise/general-skills/{slug}')
    def skill(slug: str, request: Request):
        authorize(request)
        if slug != 'skill-0':
            raise HTTPException(404, 'missing')
        return skills(request)[0]
    @app.get('/api/enterprise/skills')
    def sops(request: Request):
        authorize(request)
        return [{'skill_id': 's1', 'name': 'Real SOP', 'status': 'published'},
                {'skill_id': 's2', 'name': 'Draft', 'status': 'draft'}]
    @app.get('/api/enterprise/knowledge-bases')
    def knowledge(request: Request):
        authorize(request)
        return [{'id': 'k1', 'name': 'Real knowledge', 'status': 'active', 'document_count': 3,
                 'metadata': {'owner_user_id': 'owner'}}]
    @app.get('/api/enterprise/knowledge/documents')
    def docs(request: Request):
        authorize(request)
        assert request.query_params['knowledge_base_id'] == 'k1'
        return [{'id': 'd1', 'knowledge_base_id': 'k1', 'filename': 'test.pdf'}]
    @app.get('/api/enterprise/tools')
    def tools(request: Request):
        authorize(request)
        return [{'id': 'http1', 'name': 'HTTP', 'enabled': True, 'tool_type': 'http',
                 'auth': {'secret': 'never-return-this'}},
                {'id': 'child', 'name': 'MCP child', 'enabled': True, 'tool_type': 'mcp', 'mcp_server_id': 'm1'}]
    @app.get('/api/enterprise/mcp-servers')
    def servers(request: Request):
        authorize(request)
        return [{'id': 'm1', 'name': 'MCP', 'enabled': True, 'headers': {'secret': 'never-return-this'}}]
    with TestClient(BusinessContractMiddleware(app), headers={'Authorization': 'Bearer allowed'}) as c:
        yield c


def test_skill_catalog_filters_and_paginates_authorized_rows(client):
    raw = client.get('/api/enterprise/general-skills?tenant_id=tenant').json()
    reply = client.get('/api/skills/skills?view=plaza&limit=1&offset=1&category_group=dev')
    assert reply.status_code == 200
    body = reply.json()
    assert body['meta']['total'] == 2 and [x['skillId'] for x in body['data']] == [raw[1]['id']]
    assert body['data'][0]['latestVersionId'] is None
    assert 'skill_markdown' not in body['data'][0]
    assert body['data'][0]['contract']['actions'] == ['view']
    assert 'canEdit' not in reply.text and 'installed' not in body['data'][0]
    assert client.get('/api/skills/skills?view=plaza&q=Title%201').json()['meta']['total'] == 1


def test_skill_detail_uses_canonical_resource_identifier(client):
    reply = client.get('/api/skills/skills/framework/skill-0')
    assert reply.status_code == 200 and reply.json()['data']['skill_markdown'] == '# Real content'
    assert client.get('/api/skills/skills/framework/missing').status_code == 404


def test_sop_list_uses_same_records_and_honors_max_results(client):
    reply = client.get('/api/agent-sops/skills/available?max_results=0')
    assert reply.status_code == 200 and reply.json()['total'] == 1 and reply.json()['items'] == []
    assert client.get('/api/agent-sops/skills?tenant_id=tenant').json() == client.get('/api/enterprise/skills?tenant_id=tenant').json()


def test_knowledge_and_documents_use_same_common_routes(client):
    body = client.get('/api/knowledge/v1/knowledge-bases/square').json()
    assert body[0]['id'] == 'k1' and body[0]['documentCount'] == 3
    assert body[0]['permissions'] is None
    assert client.get('/api/knowledge/v1/documents?kbId=k1').json()[0]['fileName'] == 'test.pdf'


def test_connectors_are_real_public_units_without_credentials(client):
    reply = client.get('/api/agent-tools/connectors?view=plaza')
    assert reply.status_code == 200
    assert [row['id'] for row in reply.json()['items']] == ['http1', 'm1']
    assert 'never-return-this' not in reply.text
    assert all(row['connStatus'] is None for row in reply.json()['items'])


@pytest.mark.parametrize('path', ['/api/skills/skills?view=plaza', '/api/knowledge/v1/knowledge-bases/square',
                                  '/api/agent-sops/skills/available', '/api/agent-tools/connectors?view=plaza'])
def test_aliases_preserve_auth_and_tenant_denials(client, path):
    assert client.get(path, headers={'Authorization': ''}).status_code == 401
    separator = '&' if '?' in path else '?'
    assert client.get(path + separator + 'tenant_id=other').status_code == 403


@pytest.mark.parametrize('query', ['limit=-1', 'offset=abc', 'max_results=100001', 'limit=%C2%B2'])
def test_bad_pagination_is_not_internal_error(client, query):
    assert client.get('/api/skills/skills?view=plaza&' + query).status_code == 422


@pytest.mark.parametrize('path', ['/api/skills/skills?view=shared', '/api/skills/skills?installed=true',
    '/api/skills/skills?view=workspace', '/api/knowledge/v1/knowledge-bases/shared',
    '/api/agent-tools/connectors?view=shared', '/api/skills/skills/framework/x/versions/v1'])
def test_non_equivalent_enterprise_features_are_not_faked(client, path):
    assert client.get(path).status_code == 501
