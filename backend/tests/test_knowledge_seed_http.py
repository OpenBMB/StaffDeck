import sys
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).parents[2] / "knowledge_core" / "src"))


def test_gallery_seed_uses_http_owner_and_repeats_without_knowledge_tables(tmp_path, monkeypatch):
    from app.db.models import AgentProfile, Tenant, User
    from app.db.staffdeck_seed import _seed_agent_resource_bindings, _seed_knowledge_http
    from app.knowledge import http_adapter
    from knowledge_core.api import create_app
    from knowledge_core.identity import Principal, StaticBearerIdentity
    from knowledge_core.runtime import Runtime
    from knowledge_core.configuration import Configuration

    engine = create_engine(f"sqlite:///{tmp_path / 'host.sqlite'}")
    SQLModel.metadata.create_all(engine, tables=[Tenant.__table__, User.__table__, AgentProfile.__table__])
    runtime = Runtime(f"sqlite:///{tmp_path / 'core.sqlite'}", configuration=Configuration('full', {'authentication': True}), identity=StaticBearerIdentity({
        "host-token": Principal("host", "tenant_demo"),
    }))
    monkeypatch.setenv("STAFFDECK_KM_URL", "http://knowledge")
    monkeypatch.setenv("STAFFDECK_KM_BEARER", "host-token")
    with TestClient(create_app(runtime)) as client, Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(User(id="admin", tenant_id="tenant_demo", username="admin", role="admin", password_hash=""))
        db.add(AgentProfile(id="overall", tenant_id="tenant_demo", name="Overall", is_overall=True))
        db.add(AgentProfile(id="employee", tenant_id="tenant_demo", name="Employee"))
        db.commit()
        monkeypatch.setattr(http_adapter.httpx, "request", lambda method, url, **kwargs: client.request(
            method, url.removeprefix("http://knowledge"),
            **{key: value for key, value in kwargs.items() if key != "timeout"}))
        data = {"knowledge_bases": [{"id": "kb-source", "name": "Seed"}],
            "knowledge_documents": [{"id": "doc-source", "knowledge_base_id": "kb-source",
                "filename": "expense.md", "metadata_json": {"raw_text": "# Expenses\n\nKeep receipts."}}]}
        mappings = {"knowledge_base": {}, "agent": {"agent-source": "employee"}}
        resources = {"knowledge_base": {"kb-source"}}
        for _ in range(2):
            _seed_knowledge_http(db, data, resources, mappings)
            _seed_agent_resource_bindings(db, [{"resource_type": "knowledge_base",
                "resource_id": "kb-source", "agent_id": "agent-source"}], mappings)
        headers = {"Authorization": "Bearer host-token"}
        response = client.post("/api/km/v1/query", headers=headers,
            json={"space_id": "sd:tenant_demo:employee", "query": "Expenses receipts"})
        assert response.status_code == 200, response.text
        assert response.json()["chunks"]
        assert len(client.get("/api/km/v1/knowledge-bases", headers=headers,
                              params={"space_id": "sd:tenant_demo:overall"}).json()) == 1
        assert len(client.get("/api/km/v1/jobs", headers=headers,
                              params={"space_id": "sd:tenant_demo:overall"}).json()) == 1
    engine.dispose()
