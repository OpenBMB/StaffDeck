import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "knowledge_core" / "src"))

from fastapi.testclient import TestClient
from sqlalchemy import MetaData
from sqlmodel import Session, SQLModel, create_engine

from app.api.knowledge import get_job, search_knowledge, upload_document
from app.api.knowledge_bases import create_knowledge_base, list_knowledge_bases
from app.db.models import AgentProfile, AgentResourceBinding, Tenant, User
from app.knowledge.schema import (
    KnowledgeBaseCreateRequest, KnowledgeDocumentUploadRequest, KnowledgeSearchRequest,
)


def test_legacy_calls_use_http_and_do_not_need_knowledge_tables(tmp_path, monkeypatch):
    from knowledge_core.api import create_app
    from knowledge_core.runtime import Runtime
    from knowledge_core.configuration import Configuration
    from knowledge_core.identity import Principal, StaticBearerIdentity
    import app.knowledge.http_adapter as adapter
    import httpx
    import time

    host_engine = create_engine(f"sqlite:///{tmp_path / 'host.sqlite'}")
    SQLModel.metadata.create_all(host_engine, tables=[table for table in SQLModel.metadata.sorted_tables
        if not table.name.startswith("knowledge_") and table.name != "agent_knowledge_branches"])
    runtime = Runtime(f"sqlite:///{tmp_path / 'km.sqlite'}", configuration=Configuration('full', {'authentication': True}), identity=StaticBearerIdentity({
        "host-token": Principal("host", "tenant", spaces=frozenset({"*"})),
    }))
    monkeypatch.setenv("STAFFDECK_KM_URL", "http://knowledge")
    monkeypatch.setenv("STAFFDECK_KM_BEARER", "host-token")
    with TestClient(create_app(runtime)) as client, Session(host_engine) as db:
        user = User(id="writer", tenant_id="tenant", username="writer",
                    password_hash="", role="admin")
        db.add(Tenant(id="tenant", name="Tenant"))
        db.add(user)
        db.add(AgentProfile(id="employee", tenant_id="tenant", name="Employee"))
        db.commit()
        db.info["staffdeck_actor_id"] = user.id
        monkeypatch.setattr(adapter.httpx, "request", lambda method, url, **kwargs: client.request(
            method, url.removeprefix("http://knowledge"),
            **{key: value for key, value in kwargs.items() if key != "timeout"},
        ))
        base = create_knowledge_base(
            KnowledgeBaseCreateRequest(tenant_id="tenant", name="Legacy"),
            "employee", db, user,
        )
        assert base.tenant_id == "tenant"
        assert list_knowledge_bases("tenant", "employee", db)[0].id == base.id
        job = upload_document(KnowledgeDocumentUploadRequest(
            tenant_id="tenant", knowledge_base_id=base.id, filename="expense.md",
            content_base64=base64.b64encode(
                b"# Expense policy\n\nExpense policy requires receipts."
            ).decode(),
        ), "employee", db, user)
        for _ in range(100):
            result = get_job(job.id, "tenant", "employee", db)
            if result.status in {"succeeded", "failed"}:
                break
            time.sleep(.02)
        assert result.status == "succeeded", result
        found = search_knowledge(
            KnowledgeSearchRequest(tenant_id="tenant", agent_id="employee",
                                   query="Expense policy receipts"), db, user,
        )
        assert found.chunks
        for selectors in [{"knowledge_base_ids": ["missing"]}, {"knowledge_base_version_ids": ["missing"]}]:
            invisible = search_knowledge(KnowledgeSearchRequest(tenant_id="tenant", agent_id="employee",
                query="Expense receipts", **selectors), db, user)
            assert invisible.chunks == [] and invisible.evidence_pack == []
        from app.api.module_knowledge import _dispatch
        module_input = {"tenantId": "tenant", "agentId": "employee", "actorUserId": "writer"}
        assert _dispatch("list_bases", module_input, db)[0].id == base.id
        assert _dispatch("query", {**module_input, "query": "Expense receipts"}, db).chunks
        from staffdeck_harness.modules.builtin import KnowledgeProvider
        from staffdeck_harness.runtime.knowledge_write import KnowledgePublicationContext
        db.add(AgentProfile(id="overall", tenant_id="tenant", name="Overall", is_overall=True))
        db.commit()
        gallery = adapter.StaffDeckKnowledgeAdapter(db, "tenant", "overall", user, write=True)
        gallery.ensure_space()
        gallery.call("POST", f"/knowledge-bases/{base.id}/bindings", {"space_id": gallery.space_id},
                     scoped=False, params={"source_space_id": "sd:tenant:employee"})
        from app.api.knowledge_bases import update_knowledge_base, get_knowledge_base, promote_knowledge_base_to_overall
        from app.knowledge.schema import KnowledgeBaseUpdateRequest
        renamed = update_knowledge_base(base.id, KnowledgeBaseUpdateRequest(tenant_id="tenant", name="Employee draft"), "employee", db, user)
        assert renamed.name == "Employee draft"
        assert get_knowledge_base(base.id, "tenant", "overall", db).name == "Legacy"
        promote_knowledge_base_to_overall(base.id, "tenant", "employee", db, user)
        assert get_knowledge_base(base.id, "tenant", "overall", db).name == "Employee draft"
        provider = KnowledgeProvider()
        context = KnowledgePublicationContext(db, user)
        assert provider.publication_targets(context)["targets"][0]["id"] == base.id
        team_job = provider.publish_document(context, {"tenant_id": "tenant", "knowledge_base_id": base.id,
            "filename": "team.md", "content_base64": base64.b64encode(b"# Team policy\n\nKeep receipts.").decode()})
        assert team_job["knowledge_base_id"] == base.id
        from app.api.agents import import_agent_resources, AgentResourceImportRequest
        db.add(AgentProfile(id="target", tenant_id="tenant", name="Target"))
        db.commit()
        imported = import_agent_resources("target", AgentResourceImportRequest(
            tenant_id="tenant", source_agent_id="employee", resource_type="knowledge_base",
            resource_ids=[base.id],
        ), db, user)
        assert imported["imported"][0]["resource_id"] == base.id
        assert list_knowledge_bases("tenant", "target", db)[0].id == base.id
        from app.api.agents import get_agent_resources, update_agent_resources
        from app.agents.schema import AgentResourcesUpdateRequest
        resources = get_agent_resources("target", "tenant", db, user)
        assert resources[0].resource_id == base.id
        updated = update_agent_resources("target", AgentResourcesUpdateRequest(
            tenant_id="tenant", resources=[{
                "resource_type": "knowledge_base", "resource_id": base.id, "status": "inactive",
            }],
        ), db, user)
        assert updated[0].status == "inactive"
        hidden = update_agent_resources("target", AgentResourcesUpdateRequest(
            tenant_id="tenant", resources=[],
        ), db, user)
        assert hidden[0].status == "deleted"
        restored = update_agent_resources("target", AgentResourcesUpdateRequest(
            tenant_id="tenant", resources=[{
                "resource_type": "knowledge_base", "resource_id": base.id, "status": "active",
            }],
        ), db, user)
        assert restored[0].status == "active"
        from app.agents.branching import visible_knowledge_base_versions
        from staffdeck_harness.composition.projection import bound_resources
        assert base.id in visible_knowledge_base_versions(db, "tenant", "target")
        assert bound_resources(db, "tenant", "target", "knowledge_base")[0][0].id == base.id
        from staffdeck_harness.capabilities.facade import FacadeDeps, KnowledgeFacade
        from staffdeck_harness.contracts.invocation import InvocationContext, ModuleInvocation
        from staffdeck_harness.contracts.security import SecurityContext
        from staffdeck_harness.security.oss_local import build_oss_local_profile
        from staffdeck_harness.security.profile import Guard
        profile = build_oss_local_profile()
        identity = SecurityContext("writer", "tenant", tenant_role="admin")
        facade = KnowledgeFacade(FacadeDeps(db=db, guard=Guard("knowledge.local", profile),
            security_context=identity, agent_row=db.get(AgentProfile, "target")))
        invocation = ModuleInvocation("invoke", "knowledge.local", "knowledge.search/v1",
            {"query": "Expense receipts"}, InvocationContext(
                "tenant", "target", "writer", "session", "turn", "web"))
        result = facade.search(invocation, allowed_ids={base.id})
        assert result.success, result.error
        assert result.extensions["evidence"]["chunks"]
        from app.capabilities.contracts import CapabilityContext, KnowledgeSearchQuery
        from app.capabilities.local_knowledge import LocalKnowledgeRuntime
        context = CapabilityContext("request", "tenant", "target", "writer", "session", "turn", "web")
        local = LocalKnowledgeRuntime(lambda _: (_ for _ in ()).throw(AssertionError("Native knowledge accessed")), db)
        assert local.list_scopes(context)[0].scope_id == base.id
        answer = local.search(context, KnowledgeSearchQuery("Expense receipts", query_type="skill_discovery"))
        assert answer.items
        assert local.resolve_citation(context, answer.items[0].hit_id).content
        from app.api.knowledge_bases import upsert_okf_concept
        from app.knowledge.schema import KnowledgeConceptUpdateRequest
        concept = upsert_okf_concept(base.id, "topics/new-page", KnowledgeConceptUpdateRequest(
            tenant_id="tenant", content_md="# New page\n\nKeep receipts."), "target", db, user)
        assert concept.concept_id == "topics/new-page"
        from app.db.models import ChatSession, Message
        from app.session.message_read import message_read
        db.add(ChatSession(id="session", tenant_id="tenant", agent_id="target", user_id="writer"))
        db.commit()
        read = message_read(Message(tenant_id="tenant", session_id="session", role="assistant",
            content="Expense policy [1]", metadata_json={"knowledge_citations": [{
                "label": "[1]", "chunk_id": answer.items[0].hit_id,
            }]}), db=db)
        assert "receipts" in read.metadata["knowledge_citations"][0]["content"]
        from app.api.agents import create_agent, delete_agent
        from app.agents.schema import AgentProfileCreateRequest
        copied = create_agent(AgentProfileCreateRequest(tenant_id="tenant", name="Copied", copy_from_agent_id="target"), db, user)
        assert get_agent_resources(copied.id, "tenant", db, user)[0].resource_id == base.id
        delete_agent(copied.id, "tenant", db, user)
        scope = f"sd:tenant:{copied.id}"
        assert client.get("/api/km/v1/bindings", params={"space_id": scope},
                          headers={"Authorization": "Bearer host-token"}).json()[0]["hidden"]
    host_engine.dispose()
