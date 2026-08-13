from __future__ import annotations

import pytest

from app.core.slash_commands import (
    SlashCommandError,
    build_slash_turn_plan,
    force_capability_for_requirement,
    parse_slash_command,
    resolve_capability,
    slash_command_catalog,
)
from app.core.task_request_compiler import (
    CapabilityCatalogEntry,
    CapabilityDescriptor,
    CapabilityManifest,
    TaskRequirement,
)
from app.db.models import ChatSession, Skill


def _sop(skill_id: str = "refund_v1", name: str = "退款流程") -> Skill:
    return Skill(
        tenant_id="tenant-demo",
        skill_id=skill_id,
        name=name,
        status="published",
        content_json={
            "start_node_id": "start",
            "nodes": [{"node_id": "start", "name": "开始"}],
        },
    )


def test_parse_slash_command_supports_all_resource_kinds() -> None:
    skill = parse_slash_command("/skill weather 查询北京天气")
    tool = parse_slash_command("/工具:price_query 查询 A3")
    sop = parse_slash_command("/sop refund_v1")

    assert skill and skill.kind == "skill" and skill.target == "weather"
    assert skill.prompt == "查询北京天气"
    assert tool and tool.kind == "tool" and tool.target == "price_query"
    assert sop and sop.kind == "sop" and sop.prompt == ""
    assert parse_slash_command("普通对话") is None


def test_parse_slash_command_rejects_missing_target() -> None:
    with pytest.raises(SlashCommandError) as exc_info:
        parse_slash_command("/skill")

    assert exc_info.value.code == "SLASH_COMMAND_TARGET_REQUIRED"


def test_build_slash_turn_plan_bypasses_intent_matching_for_sop() -> None:
    session = ChatSession(
        id="session-1",
        tenant_id="tenant-demo",
        user_id="user-1",
        agent_id="agent-1",
    )
    selection = parse_slash_command("/sop refund_v1 退款订单 A3")
    assert selection is not None

    plan = build_slash_turn_plan(selection, selection.prompt, session, [_sop()])

    assert plan.confidence == 1.0
    assert plan.task_frames[0].kind == "sop"
    assert plan.task_frames[0].target_skill_id == "refund_v1"
    assert plan.task_frames[0].target_step_id == "start"
    assert plan.task_frames[0].requirements == ["退款订单 A3"]


def test_build_slash_turn_plan_resumes_matching_pending_sop() -> None:
    session = ChatSession(
        id="session-1",
        tenant_id="tenant-demo",
        user_id="user-1",
        agent_id="agent-1",
    )
    selection = parse_slash_command("/sop refund_v1 继续")
    assert selection is not None

    plan = build_slash_turn_plan(
        selection,
        selection.prompt,
        session,
        [_sop()],
        [{"task_id": "task-pending", "skill_id": "refund_v1", "step_id": "confirm"}],
    )

    assert plan.decision == "switch_to_pending"
    assert plan.selected_task_id == "task-pending"
    assert plan.task_frames[0].target_step_id == "confirm"


def test_forced_skill_is_expanded_and_required() -> None:
    descriptor = CapabilityDescriptor(
        capability_id="genskill-weather",
        name="general_skill.weather",
        kind="general_skill",
        description="查询天气",
        input_schema={"type": "object"},
        metadata={"slug": "weather"},
    )
    full_manifest = CapabilityManifest(available=[descriptor], snapshot_revision="rev-1")
    selection = parse_slash_command("/skill weather 北京天气")
    assert selection is not None
    resolved = resolve_capability(selection, full_manifest)
    projected = CapabilityManifest(
        catalog=[
            CapabilityCatalogEntry(
                capability_id=descriptor.capability_id,
                name=descriptor.name,
                kind=descriptor.kind,
                description=descriptor.description,
            )
        ],
        snapshot_revision="rev-1",
    )
    requirement = TaskRequirement(
        task_frame_id="task-1",
        kind="conversation",
        goal="北京天气",
        capability_manifest=projected,
    )

    force_capability_for_requirement(requirement, projected, resolved)

    assert requirement.required_capability_names == ["general_skill.weather"]
    assert requirement.capability_manifest.allowed_names() == {"general_skill.weather"}
    assert requirement.capability_manifest.catalog == []


def test_slash_catalog_only_exposes_authorized_manifest_capabilities() -> None:
    manifest = CapabilityManifest(
        available=[
            CapabilityDescriptor(
                capability_id="tool-price",
                name="price_query",
                kind="tool",
                description="查询价格",
                metadata={"source_tool_name": "price_query", "display_name": "价格查询"},
            ),
            CapabilityDescriptor(
                capability_id="builtin.discovery.search",
                name="capability_search",
                kind="internal",
            ),
        ]
    )

    rows = slash_command_catalog([_sop()], manifest)

    assert [(row.kind, row.target) for row in rows] == [
        ("sop", "refund_v1"),
        ("tool", "price_query"),
    ]
    assert rows[1].label == "价格查询"
