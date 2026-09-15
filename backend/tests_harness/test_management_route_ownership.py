from types import SimpleNamespace as NS

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from staffdeck_harness.runtime.management import ModuleManagementMiddleware, is_shared_management_route


@pytest.mark.parametrize('path', [
    '/api/enterprise/agents',
    '/api/enterprise/ui-config',
    '/api/chat/ui-config',
    '/api/enterprise/agents/gallery',
    '/api/enterprise/agents/shared-with-me',
    '/api/chat/agents',
    '/api/enterprise/agents/remote/work-record',
    '/api/enterprise/agents/remote/work-record/',
    '/api/enterprise/agents/remote/evolution/proposals',
    '/api/enterprise/agents/remote/api-credentials',
    '/api/enterprise/tools/remote/test',
    '/api/enterprise/general-skills/remote/run/stream',
    '/api/chat/sessions',
])
def test_shared_runtime_routes_never_reach_resource_proxy(monkeypatch, path):
    from staffdeck_harness.modules import registry
    def forbidden(*args):
        pytest.fail('Shared execution/history must not consult management proxies')
    monkeypatch.setattr(registry, 'peek_registry', lambda: NS(providers=forbidden, installed=forbidden))
    app = FastAPI()
    app.add_middleware(ModuleManagementMiddleware)
    app.add_api_route(path, lambda: {'owner': 'shared-runtime'}, methods=['GET'])
    with TestClient(app, root_path='/test') as client:
        response = client.get('/test' + path)
        assert response.status_code == 200
        assert response.json()['owner'] == 'shared-runtime'


@pytest.mark.parametrize('path', ['/api/enterprise/agents', '/api/enterprise/agents/a/resources',
    '/api/enterprise/skills/a/publish', '/api/enterprise/general-skills/a', '/api/chat/agents'])
def test_resource_management_stays_with_selected_provider(path):
    assert not is_shared_management_route(path)


def test_directory_read_does_not_steal_employee_creation():
    assert is_shared_management_route('/api/enterprise/agents', 'GET')
    assert not is_shared_management_route('/api/enterprise/agents', 'POST')


def test_upstream_failure_is_structured_and_still_a_failure(monkeypatch):
    from contextlib import nullcontext
    from staffdeck_harness.modules import registry
    from staffdeck_harness.runtime import control_auth, services
    from staffdeck_harness.contracts.runtime_services import ServiceResponse
    upstream = NS(provider=NS(matches=lambda *args: True,
        handle=lambda *args: ServiceResponse(500, {'content-type': 'text/plain'}, b'Internal Server Error: private trace')),
        manifest=NS(module_id='example.staff', metadata={'management_domain': 'staff'}))
    monkeypatch.setattr(registry, 'peek_registry', lambda: NS(providers=lambda *args: [upstream], turn_lease=nullcontext))
    monkeypatch.setattr(control_auth, 'provider', lambda: NS(current=lambda token: NS()))
    monkeypatch.setattr(control_auth, 'project_subject', lambda *args: None)
    monkeypatch.setattr(services, 'bind_authenticated_session', lambda *args: None)
    app = FastAPI()
    app.add_middleware(ModuleManagementMiddleware)
    with TestClient(app) as client:
        response = client.get('/api/enterprise/agents/a/resources', headers={'Authorization': 'Bearer test'})
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'RESOURCE_SERVICE_UNAVAILABLE'
    assert response.json()['detail']['module_id'] == 'example.staff'
    assert 'private trace' not in response.text
