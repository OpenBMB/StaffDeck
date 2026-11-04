import time

import pytest
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.qcc_connector import QCC_RESOURCE, get_qcc, install_qcc
from app.api.tools import sync_mcp_tools
from app.core.capability_discovery import project_capability_manifest, search_capability_descriptors
from app.core.task_request_compiler import CapabilityDescriptor, CapabilityManifest
from app.db.models import MCPServer, Tenant, Tool, User
from app.tools.mcp_oauth_store import save_credentials
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import MCPSyncRequest, ToolCall


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'qcc.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id='t', name='Test'))
        session.add(User(id='admin', tenant_id='t', username='admin', password_hash='test', role='admin'))
        session.add(User(id='member', tenant_id='t', username='member', password_hash='test', role='member'))
        session.commit()
        yield session


def test_preset_is_idempotent_oauth_only_and_tenant_scoped(db):
    admin = db.get(User, 'admin')
    first = install_qcc('t', db, admin)
    assert install_qcc('t', db, admin) == first
    assert len(db.exec(select(MCPServer)).all()) == 1
    server = db.get(MCPServer, first['server_id'])
    assert server.url == QCC_RESOURCE
    assert server.headers_json == {}
    assert get_qcc('t', db, admin) == first
    with pytest.raises(HTTPException) as denied:
        install_qcc('t', db, db.get(User, 'member'))
    assert denied.value.status_code == 403
    with pytest.raises(HTTPException):
        get_qcc('other', db, admin)
    server.url = 'https://other.example/mcp'
    db.add(server)
    db.commit()
    with pytest.raises(HTTPException) as collision:
        install_qcc('t', db, admin)
    assert collision.value.status_code == 409
    assert db.get(MCPServer, first['server_id']).url == 'https://other.example/mcp'


def test_186_tool_catalog_uses_native_lazy_schema_and_personal_oauth(db, monkeypatch):
    admin = db.get(User, 'admin')
    server_id = install_qcc('t', db, admin)['server_id']
    save_credentials(db, 't', 'admin', server_id, QCC_RESOURCE, {
        'tokens': {'access_token': 'test-only', 'expires_at': time.time() + 3600},
    })
    tools = [{'name': f'company_{i}', 'description': f'company data {i}',
              'input_schema': {'type': 'object'}, 'output_schema': {}} for i in range(185)]
    tools.append({'name': 'historical_company', 'description': '历史工商数据查询',
                  'input_schema': {'type': 'object', 'properties': {'searchKey': {'type': 'string'}}},
                  'output_schema': {}})
    captured = []

    def discover(config, **kwargs):
        captured.append(config)
        return {'tools': tools, 'server_capabilities': {}, 'server_info': {}}

    monkeypatch.setattr('app.api.tools.discover_mcp_server', discover)
    result = sync_mcp_tools(server_id, MCPSyncRequest(tenant_id='t'), db,
                            current_user=admin)
    assert result.success and len(result.imported) == 186
    assert captured[0]['headers']['Authorization'] == 'Bearer test-only'
    rows = db.exec(select(Tool)).all()
    descriptors = [CapabilityDescriptor(
        capability_id=row.id, name=row.name, kind='tool', available=True,
        capability_scope='general', description=row.description,
        input_schema=row.input_schema,
    ) for row in rows]
    projected = project_capability_manifest(CapabilityManifest(available=descriptors))
    assert projected.available == []  # No 186 full schemas in initial model context.
    assert projected.catalog_total == 186
    match = search_capability_descriptors(descriptors, '历史工商数据查询', limit=1)[0]
    assert 'historical_company' in match.name
    assert 'searchKey' in match.input_schema['properties']
    calls = []
    monkeypatch.setattr('app.tools.tool_executor.execute_mcp_tool',
                        lambda config, args, **kw: calls.append((config, args, kw)) or {'ok': True})
    result = ToolExecutor(db).execute('t', ToolCall(name=match.name,
                        arguments={'searchKey': 'test-company'}), user_id='admin')
    assert result.success
    assert calls[0][2]['tool_name'] == 'historical_company'
    assert calls[0][0]['url'] == QCC_RESOURCE
    assert calls[0][0]['headers']['Authorization'] == 'Bearer test-only'
    assert all(row.headers_json == {} and row.auth_json == {} for row in rows)
