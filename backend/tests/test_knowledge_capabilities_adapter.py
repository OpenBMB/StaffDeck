import base64

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from app.api import knowledge_capabilities as module
from app.db import get_session
from app.db.models import AgentModelBinding, AgentProfile, ModelConfig


def test_hosted_model_selection_and_parser_do_not_access_knowledge_tables(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'host.sqlite'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine, tables=[AgentProfile.__table__, AgentModelBinding.__table__, ModelConfig.__table__])
    with Session(engine) as db:
        db.add(AgentProfile(id="employee", tenant_id="tenant", name="Employee"))
        db.add(AgentModelBinding(tenant_id="tenant", agent_id="employee", model_config_id="selected"))
        db.commit()
    def session():
        with Session(engine) as db:
            yield db
    selected = []
    monkeypatch.setenv("STAFFDECK_KM_CAPABILITIES_BEARER", "hosted-token")
    from types import SimpleNamespace
    monkeypatch.setattr(module, "resolve_model_config_for_runtime", lambda db, tenant, identifier: selected.append((tenant, identifier)) or SimpleNamespace(id=identifier, api_protocol=SimpleNamespace(value="openai_chat_completions"), model="selected"))
    class Model:
        def __init__(self, config):
            self.config = config.id
        def generate_json(self, prompt, payload):
            return {"model": self.config, "payload": payload}
    monkeypatch.setattr(module, "LLMClient", Model)
    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[get_session] = session
    body = {"tenant_id": "tenant", "space_id": "sd:tenant:employee"}
    headers = {"Authorization": "Bearer hosted-token"}
    with TestClient(app) as client:
        assert client.post("/api/internal/km/capabilities/model", json=body).status_code == 401
        result = client.post("/api/internal/km/capabilities/model", json={**body, "payload": {"query": "policy"}}, headers=headers)
        assert result.status_code == 200, result.text
        assert result.json()["model"] == "selected"
        assert selected == [("tenant", "selected")]
        result = client.post("/api/internal/km/capabilities/parse", json={**body,
            "filename": "policy.md", "content_base64": base64.b64encode(b"# Policy").decode()}, headers=headers)
        assert result.json() == {"text": "# Policy", "file_type": "md"}
        assert client.post("/api/internal/km/capabilities/model", json={**body, "space_id": "local"}, headers=headers).status_code == 403
    engine.dispose()
