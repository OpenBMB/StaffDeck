from sqlmodel import SQLModel, Session, create_engine

from app.agents.branching import ensure_private_resource_binding, mark_resource_private_for_agent
from app.api.knowledge import search_knowledge
from app.db.models import (Tenant, User, AgentProfile, KnowledgeBase, KnowledgeBaseVersion,
                           AgentKnowledgeBranch, KnowledgeDocument, KnowledgeBucket, KnowledgeChunk)
from app.knowledge.schema import KnowledgeSearchRequest


def test_chat_excludes_sop_knowledge_and_verified_execution_can_read_it(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'scope.sqlite'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        actor = User(id='actor', tenant_id='tenant', username='actor', role='member', password_hash='unused')
        db.add_all([Tenant(id='tenant', name='Tenant'), actor,
                    AgentProfile(id='a', tenant_id='tenant', name='A', metadata_json={'owner_user_id': 'actor'})])
        for scope in ['general', 'sop_specific']:
            base = KnowledgeBase(id=scope, tenant_id='tenant', name=scope, capability_scope=scope)
            mark_resource_private_for_agent(base, 'a')
            db.add(base)
            db.flush()
            ensure_private_resource_binding(db, 'tenant', 'a', 'knowledge_base', scope)
            db.add_all([
                KnowledgeBaseVersion(id=f'{scope}-version', tenant_id='tenant', knowledge_base_id=scope,
                                     name=scope, version='1', capability_scope=scope),
                AgentKnowledgeBranch(id=f'{scope}-branch', tenant_id='tenant', agent_id='a',
                                     knowledge_base_id=scope, source_knowledge_base_id=scope, head_version='1'),
                KnowledgeDocument(id=f'{scope}-doc', tenant_id='tenant', knowledge_base_id=scope,
                    knowledge_base_version_id=f'{scope}-version', filename=f'{scope}.md', file_type='md',
                    status='ready', title='verification marker', metadata_json={'raw_text': f'verification marker {scope}'}),
                KnowledgeBucket(id=f'{scope}-bucket', tenant_id='tenant', knowledge_base_id=scope,
                    knowledge_base_version_id=f'{scope}-version', document_id=f'{scope}-doc',
                    bucket_key=scope, title='verification marker', summary=f'verification marker {scope}'),
                KnowledgeChunk(id=f'{scope}-chunk', tenant_id='tenant', knowledge_base_id=scope,
                    knowledge_base_version_id=f'{scope}-version', document_id=f'{scope}-doc',
                    bucket_id=f'{scope}-bucket', chunk_index=0, content=f'verification marker {scope}'),
            ])
        db.commit()
        query = KnowledgeSearchRequest(tenant_id='tenant', agent_id='a', query='verification marker')
        result = search_knowledge(query, db, actor)
        assert {chunk.knowledge_base_id for chunk in result.chunks} == {'general'}
        forced = KnowledgeSearchRequest(tenant_id='tenant', agent_id='a', query='verification marker',
                                       knowledge_base_ids=['sop_specific'])
        assert search_knowledge(forced, db, actor).chunks == []
        from app.knowledge.public_host_selection import use_public_host_retrieval
        with use_public_host_retrieval():
            for mode in ['debug', 'skill_discovery']:
                override = KnowledgeSearchRequest(tenant_id='tenant', agent_id='a',
                    query='verification marker', mode=mode, knowledge_base_ids=['sop_specific'])
                assert search_knowledge(override, db, actor).chunks == []
        authorized = KnowledgeSearchRequest(tenant_id='tenant', agent_id='a', query='verification marker',
                                           knowledge_base_ids=['sop_specific'])
        authorized._sop_authorized = True
        assert {chunk.knowledge_base_id for chunk in search_knowledge(authorized, db, actor).chunks} == {'sop_specific'}
        assert '_sop_authorized' not in authorized.model_dump()
    engine.dispose()
