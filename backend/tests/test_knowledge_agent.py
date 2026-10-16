from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app.knowledge import agent_tools as tools
from staffdeck_harness.contracts.invocation import ModuleInvocation, InvocationContext


@pytest.fixture
def setup(monkeypatch):
    from app.knowledge import http_adapter
    methods = ','.join(['query', *tools.SPECS])
    monkeypatch.setattr('app.config.get_settings', lambda: SimpleNamespace(knowledge_agent_methods=methods))
    calls = []
    class Adapter:
        url, headers, space_id = 'http://knowledge', {}, 'trusted'
        def __init__(self, *args, **kwargs): calls.append(('identity', args[1:4], kwargs))
        def require_base(self, base, action):
            if base != 'bound': raise HTTPException(403, 'Not bound')
        def call(self, method, path, body=None, **kwargs):
            calls.append((method, path, body, kwargs))
            return {'id': 'document', 'knowledge_base_id': 'other' if path.endswith('/outside') else 'bound', 'updated_at': 'revision'}
    monkeypatch.setattr('app.knowledge.http_adapter.StaffDeckKnowledgeAdapter', Adapter)
    import httpx
    monkeypatch.setattr(httpx, 'get', lambda *a, **kw: httpx.Response(200, json={'modules': {'concepts': True}}, request=httpx.Request('GET', 'http://knowledge')))
    host = SimpleNamespace(db=SimpleNamespace(get=lambda *args: 'trusted-user'),
        security_context=SimpleNamespace(actor_user_id='owner', principal_id='owner'),
        slot=SimpleNamespace(grants=lambda: [SimpleNamespace(resource_id='bound', operation=tools.operation(name)) for name in tools.SPECS]))
    def invoke(name, arguments):
        context = InvocationContext(tenant_id='tenant', agent_id='employee', user_id='owner', session_id='session', turn_id='turn', channel='web')
        return tools.invoke(host, ModuleInvocation('test', 'knowledge', tools.operation(name), arguments, context))
    return calls, invoke


def test_revision_and_identity_are_required_before_transport(setup):
    calls, invoke = setup
    assert not invoke('delete_document', {'knowledge_base_id': 'bound', 'document_id': 'document'}).success
    assert not calls
    assert not invoke('delete_document', {'knowledge_base_id': 'bound', 'document_id': 'document', 'expected_updated_at': 'v', 'actor': 'admin'}).success
    assert not calls
    assert not invoke('get_document', {'knowledge_base_id': 'not-bound', 'document_id': 'document'}).success
    assert not calls


def test_conditional_delete_scope_and_wiki_modes(setup):
    calls, invoke = setup
    assert not invoke('delete_document', {'knowledge_base_id': 'bound', 'document_id': 'outside', 'expected_updated_at': 'v'}).success
    assert not any(row[0] == 'DELETE' for row in calls)
    assert invoke('delete_document', {'knowledge_base_id': 'bound', 'document_id': 'document', 'expected_updated_at': 'v'}).success
    assert calls[-1] == ('DELETE', '/documents/document', None, {'params': {'expected_updated_at': 'v'}})
    args = {'knowledge_base_id': 'bound', 'document_id': 'document', 'concept_id': 'agent/page', 'content_md': '# Page', 'mode': 'edit'}
    assert not invoke('upsert_concept', args).success
    assert invoke('upsert_concept', {**args, 'mode': 'create'}).success
    assert calls[-1][2]['create_only'] is True


def test_default_agent_allowlist(monkeypatch):
    monkeypatch.setattr('app.config.get_settings', lambda: SimpleNamespace(knowledge_agent_methods='query'))
    assert tools.configured_methods() == ['query']
    monkeypatch.setattr('app.config.get_settings', lambda: SimpleNamespace(knowledge_agent_methods='invented'))
    with pytest.raises(ValueError, match='Unknown'): tools.configured_methods()


def test_write_policy_denial_does_not_send_a_mutation(setup, monkeypatch):
    calls, invoke = setup
    from app.knowledge.http_adapter import StaffDeckKnowledgeAdapter
    def denied(self, base, action):
        assert action == 'edit'
        raise HTTPException(403, 'Current user has no edit permission')
    monkeypatch.setattr(StaffDeckKnowledgeAdapter, 'require_base', denied)
    result = invoke('delete_document', {'knowledge_base_id': 'bound', 'document_id': 'document', 'expected_updated_at': 'v'})
    assert not result.success and result.error['code'] == 'KNOWLEDGE_HTTP_403'
    assert not any(row[0] == 'DELETE' for row in calls)


def test_disabled_wiki_module_hides_agent_tools_and_rejects_forged_calls(setup, monkeypatch):
    calls, invoke = setup
    import httpx
    monkeypatch.setenv('STAFFDECK_KM_URL', 'http://knowledge')
    monkeypatch.setattr(httpx, 'get', lambda *a, **kw: httpx.Response(200,
        json={'modules': {'concepts': False}, 'operations': {'query': {'method': 'POST', 'path': '/query'}}},
        request=httpx.Request('GET', 'http://knowledge')))
    assert tools.supported_methods() == {'query'}
    assert not invoke('get_concept', {'knowledge_base_id': 'bound', 'concept_id': 'page'}).success
    assert not any(row[0] == 'GET' for row in calls)
