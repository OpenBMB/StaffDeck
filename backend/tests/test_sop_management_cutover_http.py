from fastapi.testclient import TestClient
from sqlmodel import Session, select
from staffdeck_sop_runtime.api import create_app
from test_public_api_v1 import _client, _skill_card, _tenant_key

from app.db.models import APISOPDraft, AgentResourceBinding, Skill, SkillVersion, SopDefinitionRecord, SopDraftRecord
from staffdeck_harness.sop.runtime_client import RuntimeClient


def test_public_http_create_patch_publish_versions_archive_without_legacy_writes(
    monkeypatch, tmp_path, independent_knowledge_service
):
    independent_knowledge_service('tenant_api')
    monkeypatch.setenv("SOP_RUNTIME_LEGACY_WRITES", "disabled")
    monkeypatch.setenv("SOP_RUNTIME_ENDPOINT", "http://runtime")
    with TestClient(create_app()) as runtime_http:

        def transport(path, payload, headers):
            response = (
                runtime_http.get(path, headers=headers)
                if payload is None
                else runtime_http.post(path, json=payload, headers=headers)
            )
            return response.json()

        runtime = RuntimeClient("http://runtime", transport=transport)
        monkeypatch.setattr("app.skills.sop_management.RuntimeClient", lambda *a, **k: runtime)
        client, engine, token = _client(
            monkeypatch, database_url=f"sqlite:///{tmp_path / 'cutover.sqlite'}"
        )
        headers = {
            "Authorization": f"Bearer {_tenant_key(client, token, ['sops:read', 'sops:write', 'sops:publish'])}"
        }
        created = client.post(
            "/agents/agent_api/sops", headers=headers, json={"content": _skill_card()}
        )
        assert created.status_code == 201, created.text
        draft = created.json()
        changed = client.patch(
            f"/agents/agent_api/sops/expense_policy_v1?draft_id={draft['id']}",
            headers={
                **headers,
                "If-Match": draft["etag"],
                "Content-Type": "application/json-patch+json",
            },
            json=[{"op": "replace", "path": "/description", "value": "Changed"}],
        )
        assert changed.status_code == 200, changed.text
        published = client.post(
            "/sops/expense_policy_v1:publish?agent_id=agent_api",
            headers=headers,
            json={"draft_id": draft["id"]},
        )
        assert published.status_code == 200, published.text
        with Session(engine) as db:
            db.info['staffdeck_actor_id'] = 'user_api_admin'
            from app.agents.summary import summary_facts
            from app.api.agents import _local_agent_resource_timeline_events, _resource_binding_visible_in_agent_summary
            from app.db.models import AgentProfile
            definition = db.exec(select(SopDefinitionRecord)).one()
            binding = db.exec(select(AgentResourceBinding).where(
                AgentResourceBinding.resource_id == definition.id)).one()
            facts = summary_facts(db, 'tenant_api', [binding])
            assert facts['resources'][('skill', definition.id)].name == definition.name
            assert _resource_binding_visible_in_agent_summary(
                db, 'tenant_api', db.get(AgentProfile, 'agent_api'), binding, facts=facts)
            assert any(event.kind == 'sop' and event.label == definition.name
                       for event in _local_agent_resource_timeline_events(db, 'tenant_api', 'agent_api'))
        listing = client.get("/agents/agent_api/sops", headers=headers)
        assert listing.status_code == 200, listing.text
        assert listing.json()["data"][0]["content"]["description"] == "Changed"
        versions = client.get(
            "/sops/expense_policy_v1/versions?agent_id=agent_api", headers=headers
        )
        assert versions.status_code == 200 and len(versions.json()["data"]) == 1
        sop_id = '["staffdeck","tenant_api","agent_api","expense_policy_v1"]'
        assert (
            runtime.operation("http-session", "prepare", request_id="p", turn_id="t", sopId=sop_id)[
                "state"
            ]["revision"]
            == 0
        )
        archived = client.post(
            "/sops/expense_policy_v1:archive?agent_id=agent_api", headers=headers
        )
        assert archived.status_code == 200, archived.text
        with Session(engine) as db:
            assert not db.exec(select(Skill)).all()
            assert not db.exec(select(SkillVersion)).all()
            assert not db.exec(select(APISOPDraft)).all()
            assert db.exec(select(SopDefinitionRecord)).one().status == "archived"
            assert db.exec(select(SopDraftRecord)).one().status == "published"
