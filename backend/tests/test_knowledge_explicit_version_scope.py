"""Exercise the production public/native query boundary over persistent version rows."""
import json
import os
import subprocess
import sys
import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from knowledge_core.models import (KnowledgeModel, KnowledgeBinding, KnowledgeBranch, Space, KnowledgeBase, KnowledgeBaseVersion, KnowledgeBucket, KnowledgeChunk, KnowledgeConcept, KnowledgeDocument)
from knowledge_core.api import create_app
from knowledge_core.runtime import Runtime
from knowledge_core.identity import Principal, StaticBearerIdentity
from knowledge_core.configuration import Configuration
from knowledge_core.conversion import legacy_scope_data, populate, read_database
import app.knowledge.http_adapter as adapter
from app.db import get_session
from app.db.models import AgentProfile, Tenant, User
from app.public_api.auth import PublicPrincipal, get_public_principal
from app.public_api.pilotdeck_knowledge_read import router


def seed(path):
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    KnowledgeModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant", name="Test"))
        db.add(User(id="admin", tenant_id="tenant", username="admin", role="admin", password_hash="unused"))
        db.add_all([AgentProfile(id=a, tenant_id="tenant", name=a) for a in ("target", "other")])
        for owner in ("target", "other"):
            db.add(Space(id=f"sd:tenant:{owner}", tenant_id="tenant", name=owner, owner_id="admin"))
        for label, owner, status in (("current", "target", "active"),
                                     ("private", "other", "active"),
                                     ("deleted", "target", "deleted"),
                                     ("concept", "other", "active")):
            base = KnowledgeBase(id=label, tenant_id="tenant", name=label)
            db.add(base)
            db.add(KnowledgeBaseVersion(id=f"v-{label}", tenant_id="tenant", knowledge_base_id=label,
                                       version="1.0.0", name=label))
            db.add(KnowledgeBranch(tenant_id="tenant", space_id=f"sd:tenant:{owner}", knowledge_base_id=label,
                                       head_version_id=f"v-{label}", base_version_id=f"v-{label}", status=status))
            db.flush()
            db.add(KnowledgeBinding(tenant_id="tenant", space_id=f"sd:tenant:{owner}", knowledge_base_id=label, status=status))
            if label == "concept":
                db.add(KnowledgeConcept(id="concept-only", tenant_id="tenant", knowledge_base_id=label,
                                       knowledge_base_version_id=f"v-{label}", concept_id="scope/concept",
                                       concept_type="Source Document", title="scope fact", content_md="scope fact concept"))
                continue
            db.add(KnowledgeDocument(id=f"doc-{label}", tenant_id="tenant", knowledge_base_id=label,
                                    knowledge_base_version_id=f"v-{label}", filename=f"{label}.md",
                                    file_type="md", title="scope fact", status="ready"))
            db.add(KnowledgeBucket(id=f"bucket-{label}", tenant_id="tenant", knowledge_base_id=label,
                                  knowledge_base_version_id=f"v-{label}", document_id=f"doc-{label}",
                                  bucket_key="one", title="scope fact", summary=f"scope fact {label}"))
            db.add(KnowledgeChunk(id=f"chunk-{label}", tenant_id="tenant", knowledge_base_id=label,
                                 knowledge_base_version_id=f"v-{label}", document_id=f"doc-{label}",
                                 bucket_id=f"bucket-{label}", chunk_index=0, content=f"scope fact {label}"))
            db.add(KnowledgeConcept(id=f"concept-{label}", tenant_id="tenant", knowledge_base_id=label,
                                   knowledge_base_version_id=f"v-{label}", document_id=f"doc-{label}",
                                   concept_id=f"scope/{label}", concept_type="Source Document",
                                   title="scope fact", content_md=f"scope fact {label}"))
        db.commit()
    engine.dispose()
    data = legacy_scope_data(read_database(path))
    with sqlite3.connect(path) as connection:
        for name in read_database(path):
            connection.execute('DROP TABLE "' + name + '"')
    runtime = Runtime(f"sqlite:///{path}", configuration=Configuration('full', {'authentication': True}),
                      identity=StaticBearerIdentity({'scope-token': Principal('host', 'tenant')}))
    runtime.initialize()
    populate(runtime, data)
    runtime.close()


def probe(path):
    path = Path(path)
    host_path = path.with_name('host.sqlite')
    engine = create_engine(f"sqlite:///{host_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        if not db.get(User, 'admin'):
            db.add(Tenant(id='tenant', name='Test'))
            db.add(User(id='admin', tenant_id='tenant', username='admin', role='admin', password_hash='unused'))
            db.add_all([AgentProfile(id=a, tenant_id='tenant', name=a) for a in ('target', 'other')])
            db.commit()
    app = FastAPI()
    app.include_router(router)
    runtime = Runtime(f"sqlite:///{path}", configuration=Configuration('full', {'authentication': True}),
                      identity=StaticBearerIdentity({"scope-token": Principal("host", "tenant", spaces=frozenset({"*"}))}))
    os.environ["STAFFDECK_KM_URL"] = "http://knowledge"
    os.environ["STAFFDECK_KM_BEARER"] = "scope-token"
    original_request = adapter.httpx.request
    with TestClient(create_app(runtime)) as core, Session(engine) as db:
        adapter.httpx.request = lambda method, url, **kwargs: core.request(method, url.removeprefix("http://knowledge"), **{key: value for key, value in kwargs.items() if key != "timeout"})
        actor = db.get(User, "admin")
        principal = PublicPrincipal(tenant_id="tenant", actor_user=actor,
                                    scopes=frozenset({"knowledge:read"}), agent_id="target")
        app.dependency_overrides[get_public_principal] = lambda: principal
        app.dependency_overrides[get_session] = lambda: db
        with TestClient(app) as client:
            def query(selectors, expected):
                body = {"kind": "request", "method": "module_call", "messageId": "m", "runId": "r",
                        "operationId": "o", "requestId": "q", "module": "knowledge",
                        "payload": {"operation": "query", "input": {"query": "scope fact", **selectors}}}
                response = client.post("/agents/target/knowledge-module/v2/module/call", json=body)
                assert response.status_code == 200, response.text
                result = response.json()["payload"]["result"]
                if not expected:
                    assert not result["chunks"] and not result["evidence_pack"] and not result["okf_citations"], result
                else:
                    assert result["chunks"], result
                    assert all(c["knowledge_base_id"] == "current" for c in result["chunks"]), result
                return body, result

            for label in ("private", "deleted"):
                for surface in ({}, {"documentIds": [f"doc-{label}"]}):
                    _, result = query({"knowledgeBaseIds": [label], "knowledgeBaseVersionIds": [f"v-{label}"], **surface}, False)
                    assert result["trace"][0]["phase"] == "no_visible_knowledge"
            query({"knowledgeBaseIds": ["current"], "knowledgeBaseVersionIds": ["unknown"]}, False)
            _, result = query({"knowledgeBaseIds": ["concept"], "knowledgeBaseVersionIds": ["v-concept"]}, False)
            assert result["trace"][0]["phase"] == "no_visible_knowledge"
            query({"knowledgeBaseIds": ["private"], "knowledgeBaseVersionIds": ["v-current"]}, False)
            query({"knowledgeBaseIds": ["current"], "knowledgeBaseVersionIds": ["v-current", "v-private", "unknown"]}, True)
            query({"knowledgeBaseIds": ["current"], "knowledgeBaseVersionIds": ["v-current"]}, True)
            body, _ = query({}, True)
            body["payload"]["input"]["tenantId"] = "tenant"
            assert client.post("/agents/target/knowledge-module/v2/module/call", json=body).status_code == 400
            principal = PublicPrincipal(tenant_id="tenant", actor_user=actor, scopes=frozenset({"knowledge:read"}), agent_id="other")
            assert client.post("/agents/target/knowledge-module/v2/module/call", json=body | {"payload": {"operation": "query", "input": {"query": "scope fact"}}}).status_code == 403
    adapter.httpx.request = original_request
    engine.dispose()
    return {"query_cases": 10, "guards": 2}


def test_explicit_version_scope_and_two_process_restarts(tmp_path):
    path = tmp_path / "knowledge.sqlite"
    seed(path)
    assert probe(path)["query_cases"] == 10
    for _ in range(2):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--probe", str(path)],
                                capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout.splitlines()[-1]) == {"query_cases": 10, "guards": 2}


if __name__ == "__main__":
    assert sys.argv[1] == "--probe"
    print(json.dumps(probe(sys.argv[2])))
