import base64
import time

from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine

from app.api.knowledge import get_job, search_knowledge, upload_document
from app.api.knowledge_bases import create_knowledge_base
from app.db.models import Tenant, User, AgentProfile
from app.knowledge.schema import KnowledgeBaseCreateRequest, KnowledgeDocumentUploadRequest, KnowledgeSearchRequest
from app.knowledge.public_host_selection import use_public_host_retrieval


def test_chat_excludes_sop_knowledge_and_verified_execution_can_read_it(tmp_path, monkeypatch):
    from knowledge_core.api import create_app
    from knowledge_core.runtime import Runtime
    from knowledge_core.configuration import Configuration
    from knowledge_core.identity import Principal, StaticBearerIdentity
    import app.knowledge.http_adapter as adapter

    engine = create_engine(f"sqlite:///{tmp_path / 'host.sqlite'}")
    SQLModel.metadata.create_all(engine)
    runtime = Runtime(f"sqlite:///{tmp_path / 'km.sqlite'}", configuration=Configuration('full', {'authentication': True}),
                      identity=StaticBearerIdentity({'host': Principal('host', 'tenant', spaces=frozenset({'*'}))}))
    monkeypatch.setenv('STAFFDECK_KM_URL', 'http://knowledge')
    monkeypatch.setenv('STAFFDECK_KM_BEARER', 'host')
    with TestClient(create_app(runtime)) as client, Session(engine) as db:
        monkeypatch.setattr(adapter.httpx, 'request', lambda method, url, **kwargs: client.request(
            method, url.removeprefix('http://knowledge'),
            **{key: value for key, value in kwargs.items() if key != 'timeout'}))
        actor = User(id='actor', tenant_id='tenant', username='actor', role='admin', password_hash='unused')
        db.add_all([Tenant(id='tenant', name='Tenant'), actor,
                    AgentProfile(id='a', tenant_id='tenant', name='A')])
        db.commit()
        db.info['staffdeck_actor_id'] = actor.id
        bases = {}
        for scope in ['general', 'sop_specific']:
            base = create_knowledge_base(KnowledgeBaseCreateRequest(tenant_id='tenant', name=scope, capability_scope=scope), 'a', db, actor)
            bases[scope] = base.id
            job = upload_document(KnowledgeDocumentUploadRequest(tenant_id='tenant', knowledge_base_id=base.id,
                filename=f'{scope}.md', capability_scope=scope,
                content_base64=base64.b64encode(f'# Verification\n\nverification marker {scope}'.encode()).decode()), 'a', db, actor)
            for _ in range(100):
                result = get_job(job.id, 'tenant', 'a', db)
                if result.status in {'succeeded', 'failed'}:
                    break
                time.sleep(.02)
            assert result.status == 'succeeded', result
        query = KnowledgeSearchRequest(tenant_id='tenant', agent_id='a', query='verification marker')
        assert {chunk.knowledge_base_id for chunk in search_knowledge(query, db, actor).chunks} == {bases['general']}
        forced = KnowledgeSearchRequest(tenant_id='tenant', agent_id='a', query='verification marker',
                                       knowledge_base_ids=[bases['sop_specific']])
        assert search_knowledge(forced, db, actor).chunks == []
        with use_public_host_retrieval():
            for mode in ['debug', 'skill_discovery']:
                override = forced.model_copy(update={'mode': mode})
                assert search_knowledge(override, db, actor).chunks == []
        forced._sop_authorized = True
        assert {chunk.knowledge_base_id for chunk in search_knowledge(forced, db, actor).chunks} == {bases['sop_specific']}
        assert '_sop_authorized' not in forced.model_dump()
    engine.dispose()
