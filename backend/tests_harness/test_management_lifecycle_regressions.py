"""Failures observed through the live create/publish/test lifecycle."""
import json
import pytest
from datetime import datetime, timezone

from sqlmodel import Session, SQLModel, create_engine

from app.api import general_skills
from app.db.models import AgentProfile, GeneralSkill, Tenant, User
from app.general_skills.schema import GeneralSkillImportRequest
from staffdeck_harness.contracts.invocation import Receipt


def test_receipt_wire_value_can_be_reused_in_next_sop_step():
    receipt = Receipt("call", "completed", "digest", started_at=datetime.now(timezone.utc),
                      finished_at=datetime.now(timezone.utc))
    wire = receipt.to_json()
    assert isinstance(json.loads(json.dumps(wire))["finished_at"], str)


def test_private_draft_skill_publish_changes_executable_package_state():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(id="u", tenant_id="t", username="qa", password_hash="x", role="admin")
        agent = AgentProfile(id="a", tenant_id="t", name="QA", metadata_json={"owner_user_id":"u"})
        db.add_all([Tenant(id="t", name="T"), user, agent])
        db.commit()
        skill = general_skills.import_general_skill(GeneralSkillImportRequest(tenant_id="t", agent_id="a",
            slug="qa-draft", name="QA", status="draft", markdown="# QA\nReply OK."), db=db, current_user=user)
        published = general_skills.publish_general_skill(skill.slug, "t", db=db, agent_id="a", current_user=user)
        assert published.status == "published"
        db.expire_all()
        assert db.get(GeneralSkill, skill.id).status == "published"
    engine.dispose()


@pytest.mark.parametrize("source_status",["draft","archived"])
def test_active_binding_cannot_mask_unpublished_skill_package(source_status):
    row=GeneralSkill(id="g",tenant_id="t",slug="g",name="G",status=source_status,skill_markdown="# G")
    assert general_skills.general_skill_read(row,status_override="published").status==source_status


def test_saved_tool_test_uses_registered_provider_and_schema(monkeypatch):
    from types import SimpleNamespace
    from app.agents.branching import ensure_private_resource_binding
    from app.api import tools as api
    from app.db.models import Tool, HarnessInvocationRecord
    from app.tools.tool_schema import ToolTestRequest
    from staffdeck_harness.contracts.invocation import ModuleResult
    from staffdeck_harness.contracts.manifest import SlotName, ModuleKind
    from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install, manifest
    from staffdeck_harness.modules import registry as registry_module
    from app.config import get_settings
    from sqlmodel import select

    calls=[]
    reg=discover_and_install(ModuleRegistry(), get_settings())
    reg.set_enabled("tool.local",False)
    reg.install(manifest("qa.tool", "QA provider", kind=ModuleKind.CODE, slots=[SlotName.STAFF_CAPABILITY],
        provides=["tool.invoke/v1"], policy_actions=["tool.invoke/v1"]),
        SimpleNamespace(invoke=lambda ctx, inv: (calls.append(inv.arguments) or ModuleResult.ok({"marker":"provider-used"}))),
        slot=SlotName.STAFF_CAPABILITY)
    reg.seal()
    monkeypatch.setattr(registry_module,"_active",reg)
    engine=create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        user=User(id="u",tenant_id="t",username="qa",password_hash="x",role="admin")
        tool=Tool(id="tool",tenant_id="t",name="qa-tool",method="GET",url="http://unused.invalid",
                  input_schema={"type":"object","properties":{"value":{"type":"string"}},"required":["value"]})
        db.add_all([Tenant(id="t",name="T"),user,AgentProfile(id="a",tenant_id="t",name="A"),tool])
        db.commit()
        ensure_private_resource_binding(db,"t","a","tool",tool.id)
        db.commit()
        result=api.test_tool(tool.id,ToolTestRequest(tenant_id="t",arguments={"value":42}),agent_id="a",db=db,current_user=user)
        assert not result.success and result.error.code=="INVALID_ARGUMENTS" and not calls
        result=api.test_tool(tool.id,ToolTestRequest(tenant_id="t",arguments={"value":"ok"}),agent_id="a",db=db,current_user=user)
        assert result.success and result.data=={"marker":"provider-used"} and len(calls)==1
        assert db.exec(select(HarnessInvocationRecord)).first().status=="completed"
    engine.dispose()


def test_missing_resource_family_never_grants_a_required_sop_dependency():
    from staffdeck_harness.contracts.staff import StaffComposition, SopView, SessionPolicy
    from staffdeck_harness.contracts.security import ResourceRef
    from staffdeck_harness.composition.slots import SlotDeclaration
    from staffdeck_harness.composition.compiler import CompositionCompiler
    from staffdeck_harness.contracts.errors import SlotNotBound
    sop=SopView("flow","row","1","Flow",{},None,{},
        (SlotDeclaration("tool:ghost","tool.invoke/v1",required=True,implicit=True),),
        ResourceRef("sop","row","t"))
    staff=StaffComposition("t","a","A",False,"active",None,{},SessionPolicy(),(),(sop,),(),None,(),ResourceRef("agent","a","t"))
    with pytest.raises(SlotNotBound):
        CompositionCompiler(hooks=()).compile(staff)
    runtime=CompositionCompiler(hooks=()).compile(staff,strict=False)
    assert not runtime.sops and not runtime.grants
    assert runtime.metadata["unavailable_sops"][0]["sop_id"]=="flow"


def test_sop_publish_rejects_missing_dependency_without_partial_activation():
    from fastapi import HTTPException
    from app.api.skills import create_skill, publish_skill
    from app.skills.skill_schema import SkillCreateRequest
    from app.db.models import AgentResourceBinding
    from sqlmodel import select
    engine=create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        user=User(id="u",tenant_id="t",username="qa",password_hash="x",role="admin")
        db.add_all([Tenant(id="t",name="T"),user,AgentProfile(id="a",tenant_id="t",name="A")])
        db.commit()
        row=create_skill(SkillCreateRequest(tenant_id="t",content={"skill_id":"broken","name":"Broken","version":"1",
            "nodes":[{"node_id":"invoke","name":"Invoke","capability_refs":{"tool_ids":["ghost"],"required_tool_ids":["ghost"]}}],
            "start_node_id":"invoke","terminal_node_ids":["invoke"]}),agent_id="a",db=db,current_user=user)
        with pytest.raises(HTTPException) as failure:
            publish_skill("broken","t",agent_id="a",db=db,current_user=user)
        assert failure.value.status_code==400
        binding=db.exec(select(AgentResourceBinding).where(AgentResourceBinding.resource_id==row.id)).first()
        assert binding.status=="inactive"
    engine.dispose()
