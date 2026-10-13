from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine
from app.db import get_session
from app.db.models import AgentProfile, Tenant, User
from app.public_api.app import create_public_api_app
from app.public_api.auth import PublicPrincipal, get_public_principal
from app.public_api.credential_profiles import AGENT_RUNTIME_SCOPES, USER_FULL_ACCESS_SCOPES
from app.public_api import staffdeck_facade as facade
from app.public_api import knowledge_pep
from app.public_api import resources as public_resources
from app.public_api.errors import PublicAPIError


CARD = {
    "skill_id": "sop_test",
    "name": "Test SOP",
    "version": "1.0.0",
    "nodes": [{"node_id": "start", "type": "respond", "name": "Start", "instruction": "Answer"}],
    "edges": [],
    "start_node_id": "start",
    "terminal_node_ids": ["start"],
}


class PublicStaffDeckFacadeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.actor = SimpleNamespace(id="actor", tenant_id="tenant", role="admin")
        self.principal = PublicPrincipal("tenant", self.actor, frozenset(USER_FULL_ACCESS_SCOPES))
        self.db = object()

    def test_only_account_profile_gains_sop_cancel(self) -> None:
        self.assertIn("sops:cancel", USER_FULL_ACCESS_SCOPES)
        self.assertNotIn("sops:cancel", AGENT_RUNTIME_SCOPES)
        self.assertNotIn("jobs:cancel", USER_FULL_ACCESS_SCOPES)
        self.assertNotIn("*", USER_FULL_ACCESS_SCOPES)

    def test_cancel_knowledge_job_checks_resource_owner_before_mutation(self) -> None:
        row = SimpleNamespace(tenant_id="tenant", knowledge_base_id="private-a")
        db = SimpleNamespace(get=lambda model, key: row)
        with patch.object(facade, "enforce_public_knowledge_pep", side_effect=PublicAPIError(
            404, "RESOURCE_NOT_FOUND", "Unavailable"
        )) as pep, patch.object(facade.native_knowledge, "cancel_job") as cancel:
            with self.assertRaises(PublicAPIError):
                facade.cancel_knowledge_job_facade("agent-b", "job-a", self.principal, db)
        pep.assert_called_once_with(db, self.principal, "agent-b", write=True,
                                    knowledge_base_id="private-a")
        cancel.assert_not_called()

    def test_cancel_knowledge_job_rejects_foreign_tenant_before_mutation(self) -> None:
        db = SimpleNamespace(get=lambda model, key: SimpleNamespace(
            tenant_id="other", knowledge_base_id="private-a"))
        with patch.object(facade, "enforce_public_knowledge_pep") as pep, patch.object(
            facade.native_knowledge, "cancel_job"
        ) as cancel:
            with self.assertRaises(PublicAPIError) as raised:
                facade.cancel_knowledge_job_facade("agent-b", "job-a", self.principal, db)
        self.assertEqual(raised.exception.status_code, 404)
        pep.assert_not_called()
        cancel.assert_not_called()

    def test_fixed_public_routes_are_registered(self) -> None:
        paths = create_public_api_app().openapi()["paths"]
        expected = {
            "/agents/{agent_id}/sops:preview-generate": "post",
            "/agents/{agent_id}/sops/{sop_id}:preview-rewrite": "post",
            "/agents/{agent_id}/sop-preview-jobs/{job_id}/events": "get",
            "/agents/{agent_id}/sops/{sop_id}:move-to-draft": "post",
            "/agents/{agent_id}/sops/{sop_id}": "delete",
            "/agents/{agent_id}/sops/{sop_id}:sync-from-overall": "post",
            "/agents/{agent_id}/sops/{sop_id}:promote-to-overall": "post",
            "/agents/{agent_id}/sops/{sop_id}/versions/{version}": "delete",
            "/agents/{agent_id}/tools:probe": "post",
            "/agents/{agent_id}/tools/{tool_id}": "delete",
            "/agents/{agent_id}/sops:extract-file": "post",
            "/team/tools": "get",
            "/team/general-skills": "get",
            "/team/knowledge-bases": "get",
            "/team/sops": "get",
            "/team/sops/{sop_id}/versions": "get",
            "/team/sops/{sop_id}/versions/{version}": "get",
            "/agents/{agent_id}/knowledge-bases/{knowledge_base_id}/documents/{document_id}": "get",
            "/agents/{agent_id}/knowledge-bases/{knowledge_base_id}/okf:import": "post",
            "/agents/{agent_id}/knowledge-documents/{document_id}/buckets": "get",
            "/agents/{agent_id}/knowledge-buckets/{bucket_id}/chunks": "get",
            "/agents/{agent_id}/knowledge-chunks/{chunk_id}": "put",
            "/agents/{agent_id}/knowledge-bases/{knowledge_base_id}/concepts/{concept_id}": "get",
            "/agents/{agent_id}/knowledge-bases/{knowledge_base_id}/okf/export": "get",
            "/agents/{agent_id}/knowledge-jobs": "get",
            "/agents/{agent_id}/knowledge-discoveries": "get",
            "/team/sops:preview-generate": "post",
            "/team/sops/{sop_id}:preview-rewrite": "post",
            "/team/sop-preview-jobs/{job_id}/events": "get",
        }
        for path, method in expected.items():
            self.assertIn(method, paths[path], path)

    def test_dirty_rewrite_passes_current_skill_and_conversation_without_save(self) -> None:
        body = facade.PreviewRewrite.model_validate({
            "current_skill": CARD,
            "instruction": "Rewrite this unsaved graph",
            "target_path": "nodes[0]",
            "conversation": [{"role": "user", "content": "Keep my edit"}],
        })
        with patch.object(facade, "_agent"), patch.object(
            facade.native_skills, "create_rewrite_job", return_value={"job_id": "preview"}
        ) as create, patch.object(facade.native_skills, "create_skill") as save:
            result = facade.preview_rewrite("agent", "sop_test", body, self.principal, self.db)
        self.assertEqual(result, {"job_id": "preview"})
        request = create.call_args.args[1]
        self.assertEqual(request.tenant_id, "tenant")
        self.assertEqual(request.agent_id, "agent")
        self.assertEqual(request.current_skill.skill_id, "sop_test")
        self.assertEqual(request.conversation[0]["content"], "Keep my edit")
        save.assert_not_called()

    def test_preview_schema_rejects_identity_override(self) -> None:
        with self.assertRaises(Exception):
            facade.PreviewRewrite.model_validate({
                "tenant_id": "other", "current_skill": CARD, "instruction": "change"
            })

    def test_preview_job_path_cannot_switch_agent(self) -> None:
        facade._register_preview_job("bound-preview", "agent-a")
        with patch.object(facade, "_agent"), patch.object(
            facade.native_skills, "_owned_stream_job"
        ) as native_owner:
            with self.assertRaises(PublicAPIError) as raised:
                facade._preview_job(self.principal, "agent-b", "bound-preview", self.db)
        self.assertEqual(raised.exception.code, "JOB_NOT_FOUND")
        native_owner.assert_not_called()

    def test_team_preview_keeps_none_scope_and_rejects_agent_credential(self) -> None:
        body = facade.PreviewRewrite.model_validate({
            "current_skill": CARD, "instruction": "Rewrite team draft",
            "conversation": [{"role": "user", "content": "unsaved"}],
        })
        with patch.object(facade, "_team"), patch.object(
            facade.native_skills, "create_rewrite_job", return_value={"job_id": "team-preview"}
        ) as create:
            result = facade.team_preview_rewrite("sop_test", body, self.principal, self.db)
        self.assertEqual(result, {"job_id": "team-preview"})
        self.assertIsNone(create.call_args.args[1].agent_id)
        self.assertEqual(create.call_args.args[1].conversation[0]["content"], "unsaved")
        scoped = PublicPrincipal("tenant", self.actor, frozenset(USER_FULL_ACCESS_SCOPES), agent_id="agent")
        with self.assertRaises(PublicAPIError) as raised:
            facade._team(self.db, scoped)
        self.assertEqual(raised.exception.code, "TEAM_SCOPE_REQUIRES_ACCOUNT")

    def test_team_version_read_uses_original_none_branch_and_exact_version(self) -> None:
        rows = [SimpleNamespace(version="1.0.1", model_dump=lambda **_kw: {"version": "1.0.1", "content": CARD})]
        with patch.object(facade, "_team"), patch.object(
            facade.native_skills, "list_skill_versions", return_value=rows
        ) as versions:
            result = facade.get_team_sop_version("sop_test", "1.0.1", self.principal, self.db)
        self.assertEqual(result["version"], "1.0.1")
        versions.assert_called_once_with("sop_test", "tenant", self.db, None)

    def test_http_preview_preserves_dirty_input_and_enforces_scope(self) -> None:
        app = create_public_api_app()
        app.dependency_overrides[get_session] = lambda: self.db
        app.dependency_overrides[get_public_principal] = lambda: self.principal
        with TestClient(app) as client, patch.object(facade, "_agent"), patch.object(
            facade.native_skills, "create_rewrite_job", return_value={"job_id": "transient"}
        ) as create:
            response = client.post(
                "/agents/agent/sops/sop_test:preview-rewrite",
                json={"current_skill": CARD, "instruction": "rewrite unsaved text",
                      "conversation": [{"role": "user", "content": "dirty"}]},
            )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json(), {"job_id": "transient"})
        self.assertEqual(create.call_args.args[1].current_skill.skill_id, "sop_test")

        app.dependency_overrides[get_public_principal] = lambda: PublicPrincipal(
            "tenant", self.actor, frozenset({"sops:read"})
        )
        with TestClient(app) as client:
            denied = client.post(
                "/agents/agent/sops/sop_test:preview-rewrite",
                json={"current_skill": CARD, "instruction": "rewrite"},
            )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["code"], "INSUFFICIENT_SCOPE")

    def test_original_remove_operation_receives_principal_actor_and_target(self) -> None:
        with patch.object(facade, "_agent") as check, patch.object(
            facade.native_skills, "delete_skill", return_value={"status": "hidden"}
        ) as remove:
            result = facade.remove_sop("agent", "sop_test", self.principal, self.db)
        self.assertEqual(result, {"status": "hidden"})
        check.assert_called_once_with(self.db, self.principal, "agent", write=True)
        remove.assert_called_once_with("sop_test", "tenant", self.db, "agent", self.actor)

    def test_knowledge_pep_replays_native_view_dependency_and_rejects_wrong_document(self) -> None:
        db = SimpleNamespace(get=lambda _model, _id: SimpleNamespace(
            tenant_id="tenant", knowledge_base_id="other-base"
        ))
        with patch.object(knowledge_pep, "enforce_agent_access"), patch.object(
            knowledge_pep, "ensure_public_agent"
        ), patch.object(knowledge_pep, "require_agent_scope_viewer") as viewer, patch.object(
            knowledge_pep.native_bases, "get_knowledge_base"
        ) as base:
            with self.assertRaises(PublicAPIError) as raised:
                knowledge_pep.enforce_public_knowledge_pep(
                    db, self.principal, "agent", knowledge_base_id="base", document_id="document"
                )
        self.assertEqual(raised.exception.code, "DOCUMENT_NOT_FOUND")
        viewer.assert_called_once_with("tenant", "agent", self.actor, db)
        base.assert_called_once_with("base", "tenant", "agent", db)

    def test_knowledge_body_cannot_override_path_or_principal_scope(self) -> None:
        with self.assertRaises(PublicAPIError) as raised:
            knowledge_pep.reject_public_scope_override(
                {"knowledge_base_id": "other"}, "tenant_id", "knowledge_base_id"
            )
        self.assertEqual(raised.exception.code, "PUBLIC_SCOPE_OVERRIDE")

    def test_knowledge_document_update_keeps_target_branch_and_original_conflict_field(self) -> None:
        body = {"content_md": "new content", "expected_updated_at": "2026-09-27T01:02:03"}
        visible_version = SimpleNamespace(id="version")
        visible_document = SimpleNamespace(
            id="doc", tenant_id="tenant", knowledge_base_id="base",
            knowledge_base_version_id="version",
        )
        self.db = SimpleNamespace(get=lambda model, key: visible_document if key == "doc" else None)
        with patch.object(public_resources, "enforce_public_knowledge_pep") as pep, patch.object(
            public_resources.internal_knowledge_bases, "_visible_knowledge_version",
            return_value=visible_version,
        ), patch.object(
            public_resources.internal_knowledge, "update_document", return_value={"id": "doc"}
        ) as owner:
            result = public_resources.update_knowledge_document(
                "target-agent", "base", "doc", body, self.principal, self.db
            )
        self.assertEqual(result, {"id": "doc"})
        pep.assert_called_once_with(
            self.db, self.principal, "target-agent", write=True,
            knowledge_base_id="base", document_id="doc",
        )
        self.assertEqual(owner.call_args.args[0], "doc")
        self.assertEqual(owner.call_args.args[1].expected_updated_at, body["expected_updated_at"])
        self.assertEqual(owner.call_args.kwargs["agent_id"], "target-agent")

    def test_public_knowledge_list_rejects_member_without_native_view_permission(self) -> None:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(engine)
        with Session(engine) as db:
            db.add(Tenant(id="tenant", name="Tenant"))
            db.add(User(id="owner", tenant_id="tenant", username="owner", role="member", password_hash="x"))
            db.add(User(id="visitor", tenant_id="tenant", username="visitor", role="member", password_hash="x"))
            db.add(AgentProfile(id="agent", tenant_id="tenant", name="Employee", status="active",
                                is_overall=False, metadata_json={"owner_user_id": "owner"}))
            db.commit()
            visitor = db.get(User, "visitor")
            owner = db.get(User, "owner")
        app = create_public_api_app()
        def session_override():
            with Session(engine) as db:
                yield db
        app.dependency_overrides[get_session] = session_override
        app.dependency_overrides[get_public_principal] = lambda: PublicPrincipal(
            "tenant", visitor, frozenset({"knowledge:read", "tools:read", "skills:read", "sops:read"}), allowed_agent_ids=frozenset({"agent"})
        )
        with TestClient(app) as client:
            denied = client.get("/agents/agent/knowledge-bases")
            denied_tools = client.get("/agents/agent/tools")
            denied_skills = client.get("/agents/agent/general-skills")
            denied_sops = client.get("/agents/agent/sops")
            denied_draft = client.get("/agents/agent/sops/s/drafts/d")
            denied_versions = client.get("/sops/s/versions?agent_id=agent")
            denied_version = client.get("/sops/s/versions/1.0.0?agent_id=agent")
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied_tools.status_code, 403, denied_tools.text)
        self.assertEqual(denied_skills.status_code, 403, denied_skills.text)
        self.assertEqual(denied_sops.status_code, 403, denied_sops.text)
        self.assertEqual(denied_draft.status_code, 403, denied_draft.text)
        self.assertEqual(denied_versions.status_code, 403, denied_versions.text)
        self.assertEqual(denied_version.status_code, 403, denied_version.text)
        app.dependency_overrides[get_public_principal] = lambda: PublicPrincipal(
            "tenant", owner, frozenset({"knowledge:read"}), allowed_agent_ids=frozenset({"agent"})
        )
        with TestClient(app) as client:
            allowed = client.get("/agents/agent/knowledge-bases")
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(allowed.json()["data"], [])


if __name__ == "__main__":
    unittest.main()
