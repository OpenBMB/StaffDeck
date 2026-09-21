import httpx
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.agents.branching import ensure_open_gallery_binding
from app.core.capability_manifest import CapabilityManifestBuilder
from app.core.harness_capability_invoker import HarnessCapabilityInvoker
from app.db.models import AgentProfile, ChatSession, ModelConfig, Skill, Tenant, Tool
from app.skills.nesting import expand_sop_for_execution
from app.skills.tool_authorization import current_sop_tool_authorization
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall, ToolResult


@pytest.fixture
def setup_nested(monkeypatch, tmp_path):
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant", name="Tenant"))
        db.add(AgentProfile(id="agent", tenant_id="tenant", name="Gallery", is_overall=True))
        tool = Tool(
            id="lookup",
            tenant_id="tenant",
            name="child.lookup",
            method="POST",
            url="https://provider.test/lookup",
            capability_scope="sop_specific",
            allowed_skills_json=["child"],
        )
        db.add(tool)
        db.flush()
        ensure_open_gallery_binding(db, "tenant", "tool", tool.id)
        db.commit()
        child = Skill(
            tenant_id="tenant",
            skill_id="child",
            name="Child",
            status="published",
            content_json={
                "start_node_id": "lookup",
                "terminal_node_ids": ["lookup"],
                "nodes": [
                    {
                        "node_id": "lookup",
                        "name": "Lookup",
                        "capability_refs": {"tool_ids": [tool.id]},
                    }
                ],
                "edges": [],
            },
        )
        parent = Skill(
            tenant_id="tenant",
            skill_id="parent",
            name="Parent",
            status="published",
            content_json={
                "start_node_id": "nested",
                "terminal_node_ids": ["other"],
                "nodes": [
                    {
                        "node_id": "nested",
                        "name": "Nested",
                        "type": "subflow",
                        "sub_sop_id": "child",
                    },
                    {"node_id": "other", "name": "Other"},
                ],
                "edges": [{"source_node_id": "nested", "next_node_id": "other"}],
            },
        )
        yield db, tool, expand_sop_for_execution(parent, [parent, child])
    engine.dispose()


def make_invoker(db, skill, step="nested::child::lookup"):
    manifest = CapabilityManifestBuilder(db).build("tenant", "agent", skill, step)
    return HarnessCapabilityInvoker(
        db,
        tenant_id="tenant",
        session=ChatSession(id="session", tenant_id="tenant", user_id="user"),
        task_frame_id="task",
        model_config=ModelConfig(
            tenant_id="tenant", name="test", model="test", api_key_encrypted="test"
        ),
        manifest=manifest,
        active_skill=skill,
        active_step_id=step,
        agent_id="agent",
    )


@pytest.mark.parametrize("grant", ["parent", "child"])
@pytest.mark.parametrize("protocol", ["http", "mcp", "a2a", "detached"])
def test_nested_grant_reaches_real_executor_dispatch(setup_nested, monkeypatch, grant, protocol):
    db, tool, skill = setup_nested
    tool.allowed_skills_json = [grant]
    tool.tool_type = "http" if protocol == "detached" else protocol
    if protocol == "detached":
        tool.config_json = {"execution": {"execution_mode": "detached"}}
    db.add(tool)
    db.commit()
    dispatched = []

    def fake_dispatch(self, row, arguments, **kwargs):
        dispatched.append(row.id)
        return ToolResult(tool_name=row.name, success=True, data={"ok": True})

    if protocol in {"mcp", "a2a"}:
        monkeypatch.setattr(ToolExecutor, f"_execute_{protocol}_tool", fake_dispatch)
    elif protocol == "http":

        def send(_self, method, url, **kwargs):
            dispatched.append(tool.id)
            return httpx.Response(200, json={"ok": True}, request=httpx.Request(method, url))

        monkeypatch.setattr(httpx.Client, "request", send)
    invoker = make_invoker(db, skill)
    assert tool.name in invoker.manifest.allowed_names()
    result = invoker.invoke(tool.name, {})
    assert result["success"] is True, result
    if protocol == "detached":
        assert result["data"]["detached"] is True
    else:
        assert dispatched == [tool.id]


def test_child_grant_does_not_leak_to_parent_sibling_node(setup_nested):
    db, tool, skill = setup_nested
    # Even a parent grant cannot bypass the node-specific capability_refs.
    tool.allowed_skills_json = ["parent"]
    db.add(tool)
    db.commit()
    invoker = make_invoker(db, skill, "other")
    assert tool.name not in invoker.manifest.allowed_names()
    assert invoker.invoke(tool.name, {})["success"] is False
    result = ToolExecutor(db).execute(
        "tenant",
        ToolCall(name=tool.name),
        active_skill_id="parent",
        sop_authorization=current_sop_tool_authorization("tenant", skill, "other"),
    )
    assert result.error.code == "NOT_ALLOWED"


def test_unrelated_grant_and_untrusted_arguments_cannot_authorize(setup_nested):
    db, tool, skill = setup_nested
    tool.allowed_skills_json = ["unrelated"]
    db.add(tool)
    db.commit()
    invoker = make_invoker(db, skill)
    assert tool.name not in invoker.manifest.allowed_names()
    result = ToolExecutor(db).execute(
        "tenant",
        ToolCall(name=tool.name, arguments={"authorization_skill_ids": ["unrelated"]}),
        active_skill_id="parent",
        sop_authorization=current_sop_tool_authorization("tenant", skill, "nested::child::lookup"),
    )
    assert result.error.code == "NOT_ALLOWED"


def test_direct_executor_preserves_parent_only_check_without_context(setup_nested):
    db, tool, _ = setup_nested
    result = ToolExecutor(db).execute("tenant", ToolCall(name=tool.name), active_skill_id="parent")
    assert result.error.code == "NOT_ALLOWED"


def test_context_cannot_be_reused_for_other_parent_or_tenant(setup_nested):
    db, tool, skill = setup_nested
    context = current_sop_tool_authorization("tenant", skill, "nested::child::lookup")
    result = ToolExecutor(db).execute(
        "tenant", ToolCall(name=tool.name), active_skill_id="other", sop_authorization=context
    )
    assert result.error.code == "NOT_ALLOWED"
    with pytest.raises(ValueError, match="tenant mismatch"):
        current_sop_tool_authorization("other-tenant", skill, "nested::child::lookup")


def test_live_revocation_is_checked_before_nested_dispatch(setup_nested):
    db, tool, skill = setup_nested
    invoker = make_invoker(db, skill)
    tool.allowed_skills_json = ["unrelated"]
    db.add(tool)
    db.commit()
    result = invoker.invoke(tool.name, {})
    assert result["success"] is False
    assert result["error"]["code"] == "CAPABILITY_AUTHORIZATION_REVOKED"


@pytest.mark.parametrize("grant", ["parent", "child", "leaf"])
def test_deep_nested_grants_reach_executor(setup_nested, monkeypatch, grant):
    db, tool, _ = setup_nested
    leaf = Skill(
        tenant_id="tenant",
        skill_id="leaf",
        name="Leaf",
        status="published",
        content_json={
            "start_node_id": "lookup",
            "terminal_node_ids": ["lookup"],
            "nodes": [
                {
                    "node_id": "lookup",
                    "name": "Lookup",
                    "capability_refs": {"tool_ids": [tool.name]},
                }
            ],
            "edges": [],
        },
    )
    child = Skill(
        tenant_id="tenant",
        skill_id="child",
        name="Child",
        status="published",
        content_json={
            "start_node_id": "inner",
            "terminal_node_ids": ["inner"],
            "nodes": [
                {"node_id": "inner", "name": "Inner", "type": "subflow", "sub_sop_id": "leaf"}
            ],
            "edges": [],
        },
    )
    parent = Skill(
        tenant_id="tenant",
        skill_id="parent",
        name="Parent",
        status="published",
        content_json={
            "start_node_id": "outer",
            "terminal_node_ids": ["outer"],
            "nodes": [
                {"node_id": "outer", "name": "Outer", "type": "subflow", "sub_sop_id": "child"}
            ],
            "edges": [],
        },
    )
    tool.allowed_skills_json = [grant]
    db.add(tool)
    db.commit()
    expanded = expand_sop_for_execution(parent, [parent, child, leaf])
    step = "outer::child::inner::leaf::lookup"
    context = current_sop_tool_authorization("tenant", expanded, step)
    assert context.skill_ids == {"parent", "child", "leaf"}
    monkeypatch.setattr(
        httpx.Client,
        "request",
        lambda _self, method, url, **kwargs: httpx.Response(
            200, json={"ok": True}, request=httpx.Request(method, url)
        ),
    )
    result = make_invoker(db, expanded, step).invoke(tool.name, {})
    assert result["success"] is True, result
