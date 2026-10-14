from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from staffdeck_sop_runtime.api import create_app

from app.api import skills
from app.db.models import (
    AgentProfile,
    APISOPDraft,
    Skill,
    SkillVersion,
    SopDefinitionRecord,
    SopDraftRecord,
    SopVersionRecord,
    Tenant,
    User,
)
from app.public_api import sops
from app.public_api.schemas import SOPPublishRequest, SOPStructuredCreate
from app.skills.skill_schema import SkillCreateRequest, SkillUpdateRequest
from app.skills.sop_management import SopManagementService, migrate_published, normalize_sop_write
from staffdeck_harness.sop.runtime_client import RuntimeClient

CONTENT = {
    "skill_id": "flow",
    "version": "1.0.0",
    "name": "Flow",
    "start_node_id": "done",
    "terminal_node_ids": ["done"],
    "nodes": [{"node_id": "done", "name": "Done", "type": "answer"}],
}


@pytest.fixture
def management(monkeypatch):
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db, TestClient(create_app(publish_token="publish-test")) as http:
        user = User(id="u", tenant_id="t", username="qa", password_hash="x", role="admin")
        db.add_all([Tenant(id="t", name="T"), user, AgentProfile(id="a", tenant_id="t", name="A")])
        db.commit()

        def transport(path, payload, headers):
            response = (
                http.get(path, headers=headers)
                if payload is None
                else http.post(path, json=payload, headers=headers)
            )
            return response.json()

        client = RuntimeClient("http://runtime", publish_token="publish-test", transport=transport)
        monkeypatch.setenv("SOP_RUNTIME_ENDPOINT", "http://runtime")
        monkeypatch.setenv("SOP_PUBLISH_TOKEN", "publish-test")
        monkeypatch.setenv("SOP_RUNTIME_LEGACY_WRITES", "disabled")
        monkeypatch.setattr("app.skills.sop_management.RuntimeClient", lambda *a, **k: client)
        yield db, user, client
    engine.dispose()


def test_internal_api_publishes_to_runtime_without_legacy_rows(management):
    db, user, client = management
    created = skills.create_skill(SkillCreateRequest(tenant_id="t", content=CONTENT), "a", db, user)
    assert created.status == "draft"
    published = skills.publish_skill("flow", "t", "a", db, user)
    assert published.status == "published"
    assert len(skills.list_skill_versions("flow", "t", db, "a")) == 1
    snapshot = client.operation(
        "internal",
        "prepare",
        request_id="prepare",
        turn_id="turn",
        sopId='["staffdeck","t","a","flow"]',
    )
    assert snapshot["state"]["active_node_id"] == "done"
    assert not db.exec(select(Skill)).all() and not db.exec(select(SkillVersion)).all()
    assert len(db.exec(select(SopDefinitionRecord)).all()) == 1
    assert len(db.exec(select(SopVersionRecord)).all()) == 1
    updated = {**deepcopy(CONTENT), "version": "1.0.1", "name": "Updated"}
    skills.update_skill("flow", SkillUpdateRequest(tenant_id="t", content=updated), "a", db, user)
    assert len(skills.list_skill_versions("flow", "t", db, "a")) == 2
    rolled = skills.rollback_skill_version("flow", "1.0.0", "t", db, "a", user)
    assert rolled.version == "1.0.2" and rolled.status == "draft"
    assert skills.archive_skill("flow", "t", "a", db, user).status == "archived"
    with pytest.raises(HTTPException, match="SOP_LEGACY_WRITES_DISABLED"):
        normalize_sop_write(CONTENT)


def test_public_draft_publish_uses_new_management_records(management, monkeypatch):
    db, user, client = management
    principal = SimpleNamespace(tenant_id="t", actor_user=user, credential_id="credential")
    monkeypatch.setattr(sops, "enforce_agent_access", lambda *a, **k: None)
    monkeypatch.setattr(sops, "ensure_public_agent", lambda *a, **k: None)
    draft = sops._new_draft(
        db,
        tenant_id="t",
        agent_id="a",
        credential_id="credential",
        content=CONTENT,
        source="structured",
    )
    content = {**deepcopy(CONTENT), "description": "Updated draft"}
    changed = sops.replace_sop_draft(
        "a",
        "flow",
        SOPStructuredCreate(content=content),
        Response(),
        draft.id,
        draft.etag,
        principal,
        db,
    )
    assert changed["content"]["description"] == "Updated draft"
    published = sops.publish_sop("flow", SOPPublishRequest(draft_id=draft.id), "a", principal, db)
    assert published["sop"]["status"] == "published"
    assert (
        client.operation(
            "public", "prepare", request_id="p", turn_id="t", sopId='["staffdeck","t","a","flow"]'
        )["state"]["revision"]
        == 0
    )
    assert not db.exec(select(APISOPDraft)).all() and not db.exec(select(Skill)).all()
    assert db.exec(select(SopDraftRecord)).one().status == "published"


def test_migration_preserves_legacy_rows_and_is_repeatable(management, monkeypatch):
    db, user, _client = management
    monkeypatch.setenv("SOP_RUNTIME_LEGACY_WRITES", "compat")
    created = skills.create_skill(SkillCreateRequest(tenant_id="t", content=CONTENT), "a", db, user)
    skills.publish_skill("flow", "t", "a", db, user)
    draft = sops._new_draft(
        db, tenant_id="t", agent_id="a", credential_id=None, content=CONTENT, source="structured"
    )
    assert len(migrate_published(db, "t", "a")) == 1
    assert len(migrate_published(db, "t", "a")) == 1
    assert db.get(Skill, created.id) is not None and db.get(APISOPDraft, draft.id) is not None
    assert len(db.exec(select(SopDefinitionRecord)).all()) == 1
    assert len(db.exec(select(SopVersionRecord)).all()) == 1
    monkeypatch.setenv("SOP_RUNTIME_LEGACY_WRITES", "disabled")
    assert skills.get_skill("flow", "t", "a", db).status == "published"
    assert (
        sops._owned_draft(db, SimpleNamespace(tenant_id="t"), "a", "flow", draft.id).id == draft.id
    )


def test_failed_publish_does_not_activate_definition(management):
    db, user, _client = management
    content = deepcopy(CONTENT)
    content["nodes"][0]["capability_refs"] = {"tool_ids": ["ghost"], "required_tool_ids": ["ghost"]}
    skills.create_skill(SkillCreateRequest(tenant_id="t", content=content), "a", db, user)
    from staffdeck_harness.contracts.errors import ModuleSdkError

    with pytest.raises(ModuleSdkError):
        skills.publish_skill("flow", "t", "a", db, user)
    assert SopManagementService.get(db, "t", "a", "flow").status == "draft"
    assert not db.exec(select(SopVersionRecord)).all()


def test_nested_publication_copy_and_republication_preserve_child_version(management):
    db, user, client = management
    child = {**deepcopy(CONTENT), "skill_id": "child", "name": "Child"}
    parent = {
        **deepcopy(CONTENT),
        "skill_id": "parent",
        "name": "Parent",
        "start_node_id": "sub",
        "terminal_node_ids": ["sub"],
        "nodes": [{"node_id": "sub", "name": "Sub", "type": "subflow", "sub_sop_id": "child"}],
    }
    for content in (child, parent):
        skills.create_skill(SkillCreateRequest(tenant_id="t", content=content), "a", db, user)
        skills.publish_skill(content["skill_id"], "t", "a", db, user)
    skills.update_skill(
        "child",
        SkillUpdateRequest(
            tenant_id="t", content={**child, "version": "2.0.0", "name": "Changed child"}
        ),
        "a",
        db,
        user,
    )
    skills.publish_skill("parent", "t", "a", db, user)
    prepared = client.operation(
        "pinned-parent",
        "prepare",
        request_id="p",
        turn_id="t",
        sopId='["staffdeck","t","a","parent"]',
    )
    assert prepared["step"]["node"]["name"] == "Done"
    db.add(AgentProfile(id="b", tenant_id="t", name="B"))
    db.commit()
    SopManagementService.copy(db, "t", "a", "b", "parent")
    db.commit()
    copied = SopManagementService.get(db, "t", "b", "parent")
    assert copied.content_json["nodes"][0]["sub_sop_version"] == "1.0.0"
    assert SopManagementService.get(db, "t", "b", "child").version == "1.0.0"
    assert (
        client.operation(
            "copied-parent",
            "prepare",
            request_id="p",
            turn_id="t",
            sopId='["staffdeck","t","b","parent"]',
        )["state"]["status"]
        == "active"
    )


def test_gallery_publication_is_available_to_overall_agent(management):
    db, user, client = management
    db.add(AgentProfile(id="overall", tenant_id="t", name="Overall", is_overall=True))
    db.commit()
    skills.create_skill(SkillCreateRequest(tenant_id="t", content=CONTENT), None, db, user)
    skills.publish_skill("flow", "t", None, db, user)
    prepared = client.operation(
        "overall-session",
        "prepare",
        request_id="p",
        turn_id="t",
        sopId='["staffdeck","t","overall","flow"]',
    )
    assert prepared["state"]["active_node_id"] == "done"


def test_migrated_nested_definition_can_be_republished_without_snapshot_conflict(
    management, monkeypatch
):
    db, user, client = management
    monkeypatch.setenv("SOP_RUNTIME_LEGACY_WRITES", "compat")
    child = {**deepcopy(CONTENT), "skill_id": "child", "name": "Child"}
    parent = {
        **deepcopy(CONTENT),
        "skill_id": "parent",
        "name": "Parent",
        "start_node_id": "sub",
        "terminal_node_ids": ["sub"],
        "nodes": [{"node_id": "sub", "name": "Sub", "type": "subflow", "sub_sop_id": "child"}],
    }
    for content in (child, parent):
        skills.create_skill(SkillCreateRequest(tenant_id="t", content=content), "a", db, user)
        skills.publish_skill(content["skill_id"], "t", "a", db, user)
    migrate_published(db, "t", "a")
    monkeypatch.setenv("SOP_RUNTIME_LEGACY_WRITES", "disabled")
    skills.publish_skill("parent", "t", "a", db, user)
    assert (
        SopManagementService.get(db, "t", "a", "parent").content_json["nodes"][0]["sub_sop_version"]
        == "1.0.0"
    )
    assert (
        client.operation(
            "migrated-parent",
            "prepare",
            request_id="p",
            turn_id="t",
            sopId='["staffdeck","t","a","parent"]',
        )["state"]["status"]
        == "active"
    )


def test_employee_learning_sync_promote_rollback_and_copy_use_management(management):
    from app.agents.branching import copy_overall_scope_to_agent
    from app.agents.schema import AgentResourceImportRequest, AgentSkillRollbackRequest
    from app.api import agents
    from app.db.models import AgentResourceBinding, AgentSkillBranch, AgentSkillBranchVersion

    db, user, _client = management
    overall = AgentProfile(id="overall", tenant_id="t", name="Gallery", is_overall=True)
    target = AgentProfile(id="b", tenant_id="t", name="B")
    duplicate = AgentProfile(id="c", tenant_id="t", name="C")
    db.add_all([overall, target, duplicate])
    db.commit()
    skills.create_skill(SkillCreateRequest(tenant_id="t", content=CONTENT), None, db, user)
    skills.publish_skill("flow", "t", None, db, user)
    assert (
        agents.sync_agent_skill_from_overall("a", "flow", "t", db, user)["head_version"] == "1.0.0"
    )
    changed = {**deepcopy(CONTENT), "version": "1.0.1", "name": "Employee change"}
    skills.update_skill("flow", SkillUpdateRequest(tenant_id="t", content=changed), "a", db, user)
    promoted = agents.promote_agent_skill_to_overall("a", "flow", "t", db, user)
    assert promoted["version"] == "1.1.0"
    assert SopManagementService.get(db, "t", None, "flow").name == "Employee change"
    imported = agents.import_agent_resources(
        "b",
        AgentResourceImportRequest(
            tenant_id="t", source_agent_id="a", resource_type="skill", resource_ids=["flow"]
        ),
        db,
        user,
    )
    assert len(imported["imported"]) == 1 and not imported["missing"]
    copied = SopManagementService.get(db, "t", "b", "flow")
    binding = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.agent_id == "b", AgentResourceBinding.resource_id == copied.id
        )
    ).one()
    assert agents._resource_binding_visible_in_agent_summary(db, "t", target, binding)
    agents._copy_agent_scope_from_source(db, "t", target, duplicate)
    db.commit()
    assert SopManagementService.get(db, "t", "c", "flow").status == "published"
    rolled = agents.rollback_agent_skill(
        "a", "flow", AgentSkillRollbackRequest(tenant_id="t", version="1.0.0"), db, user
    )
    assert rolled["head_version"] == "1.1.0"
    assert len(agents.list_agent_skill_versions("a", "flow", "t", db, user)) == 3
    copy_overall_scope_to_agent(db, "t", target)
    db.commit()
    assert not db.exec(select(Skill)).all()
    assert not db.exec(select(AgentSkillBranch)).all()
    assert not db.exec(select(AgentSkillBranchVersion)).all()


def test_migration_cli_publishes_scoped_definitions(management, monkeypatch, capsys):
    import sys

    from app.skills import migrate_sop_runtime as cli

    db, user, _client = management
    monkeypatch.setenv("SOP_RUNTIME_LEGACY_WRITES", "compat")
    skills.create_skill(SkillCreateRequest(tenant_id="t", content=CONTENT), "a", db, user)
    skills.publish_skill("flow", "t", "a", db, user)
    monkeypatch.setattr(cli, "engine", db.get_bind())
    monkeypatch.setattr(sys, "argv", ["migrate_sop_runtime", "--tenant-id", "t", "--agent-id", "a"])
    cli.main()
    assert "Published 1 immutable SOP definitions" in capsys.readouterr().out
    assert len(db.exec(select(SopDefinitionRecord)).all()) == 1


def test_internal_published_create_also_exports_in_compat_mode(management, monkeypatch):
    db, user, client = management
    monkeypatch.setenv("SOP_RUNTIME_LEGACY_WRITES", "compat")
    skills.create_skill(
        SkillCreateRequest(tenant_id="t", content=CONTENT, status="published"), "a", db, user
    )
    assert (
        client.operation(
            "compat", "prepare", request_id="p", turn_id="t", sopId='["staffdeck","t","a","flow"]'
        )["state"]["status"]
        == "active"
    )


def test_knowledge_snapshot_converts_required_capability_to_host_tool():
    content = {
        "skill_id": "knowledge",
        "version": "1",
        "name": "Knowledge",
        "start_node_id": "query",
        "nodes": [{"node_id": "query", "type": "knowledge_query"}],
        "terminal_node_ids": ["query"],
    }
    normalized = SopManagementService.normalize(content)
    snapshot = SopManagementService.snapshot(
        SimpleNamespace(skill_id="knowledge", version="1", name="Knowledge", content=normalized),
        "t",
        "a",
    )
    assert {r["capability_id"] for r in snapshot["capability_requirements"]} == {"knowledge_search"}


def test_cutover_missing_endpoint_never_uses_legacy_runtime(management, monkeypatch):
    from staffdeck_harness.contracts.errors import ModuleSdkError
    from staffdeck_harness.sop.contracts import SopDependencies
    from staffdeck_harness.sop.module import SopRuntimeModule

    monkeypatch.delenv("SOP_RUNTIME_ENDPOINT")
    with pytest.raises(ModuleSdkError) as failure:
        SopRuntimeModule().build(SopDependencies(SimpleNamespace(), lambda *a: None))
    assert failure.value.code == "SOP_RUNTIME_UNAVAILABLE"


def test_cutover_storage_guard_rejects_old_writers(management):
    db, _user, _client = management
    db.add(Skill(tenant_id="t", skill_id="bypass", name="Bypass", content_json=CONTENT))
    with pytest.raises(HTTPException) as failure:
        db.commit()
    assert failure.value.detail == "SOP_LEGACY_WRITES_DISABLED"
    db.rollback()
    assert not db.exec(select(Skill)).all()
