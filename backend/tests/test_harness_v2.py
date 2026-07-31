from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import ensure_open_gallery_binding
from app.core import harness_agent as harness_agent_module
from app.core.agent_loop import AgentLoop
from app.core.capability_manifest import (
    CapabilityManifestBuilder,
    _available_invocation_name,
    general_skill_snapshot_digest,
)
from app.core.harness_agent import HarnessTaskAgent
from app.core.harness_attachments import (
    ValidatedTaskImagePayload,
    materialize_task_attachments,
    validated_task_image_payloads,
)
from app.core.harness_capability_invoker import (
    HarnessCapabilityInvoker,
    _failure_was_not_sent,
)
from app.core.harness_session_cleanup import harness_task_workspace_path
from app.core.harness_v2_engine import (
    HarnessV2Engine,
    _with_recoverable_first_session,
)
from app.core.task_frame_store import (
    MAX_TASK_FRAMES_PER_TURN,
    TaskFrameClaimConflict,
    TaskFrameStore,
    planned_frame_from_record,
)
from app.core.task_request_compiler import (
    CapabilityDescriptor,
    CapabilityManifest,
    TaskRequestCompiler,
    TaskRequirement,
)
from app.core.turn_planner import TurnPlanner
from app.db.models import (
    AgentProfile,
    ChatSession,
    GeneralSkill,
    HarnessRunRecord,
    HarnessTaskFrameRecord,
    ModelConfig,
    Skill,
    Tenant,
    Tool,
    utc_now,
)
from app.session.session_schema import (
    ChatAttachmentRead,
    ChatTurnRequest,
    ChatTurnResponse,
    PlannedTaskFrame,
    SessionPublic,
    TurnPlan,
)


def test_first_harness_turn_derives_a_recoverable_session_id() -> None:
    request = ChatTurnRequest(
        tenant_id="tenant-demo",
        user_id="user-1",
        client_turn_id="client-turn-1",
        message="hello",
    )

    first = _with_recoverable_first_session(request)
    retry = _with_recoverable_first_session(request.model_copy())
    other_user = _with_recoverable_first_session(
        request.model_copy(update={"user_id": "user-2"})
    )

    assert first.session_id
    assert first.session_id == retry.session_id
    assert first.session_id != other_user.session_id
    assert "client-turn-1" not in first.session_id
    assert request.session_id is None


def test_first_harness_turn_recovers_from_a_concurrent_session_insert(
    tmp_path,
) -> None:
    database = create_engine(
        f"sqlite:///{tmp_path / 'harness-race.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(database)
    request = _with_recoverable_first_session(
        ChatTurnRequest(
            tenant_id="tenant-demo",
            user_id="user-1",
            agent_id="agent-1",
            client_turn_id="client-turn-race",
            message="hello",
        )
    )

    def concurrent_insert(_: ChatTurnRequest) -> ChatSession:
        with Session(database) as other_db:
            other_db.add(
                ChatSession(
                    id=str(request.session_id),
                    tenant_id=request.tenant_id,
                    user_id=request.user_id,
                    agent_id=request.agent_id,
                )
            )
            other_db.commit()
        raise IntegrityError(
            "INSERT INTO chat_sessions",
            {},
            RuntimeError("duplicate primary key"),
        )

    with Session(database) as db:
        harness_engine = object.__new__(HarnessV2Engine)
        harness_engine.db = db
        harness_engine.owner = SimpleNamespace(
            db=db,
            _get_or_create_session=concurrent_insert,
        )

        session = harness_engine._get_or_create_session(request)

        assert session.id == request.session_id
        assert session.tenant_id == request.tenant_id
        assert session.user_id == request.user_id


def test_harness_stream_retry_bootstraps_the_same_first_session(
    monkeypatch,
) -> None:
    engine = _test_engine()
    request = ChatTurnRequest(
        tenant_id="tenant-demo",
        user_id="user-1",
        agent_id="agent-1",
        client_turn_id="client-turn-stream",
        message="hello",
    )
    expected_session_id = _with_recoverable_first_session(request).session_id
    seen_session_ids: list[str] = []

    with Session(engine) as db:
        loop = AgentLoop(db)

        def fake_handle_turn(scoped: ChatTurnRequest) -> ChatTurnResponse:
            seen_session_ids.append(str(scoped.session_id))
            session = db.get(ChatSession, scoped.session_id)
            assert session is not None
            return ChatTurnResponse(
                reply="done",
                session_id=session.id,
                session_state=SessionPublic(
                    session_id=session.id,
                    tenant_id=session.tenant_id,
                    user_id=session.user_id,
                    agent_id=session.agent_id,
                ),
            )

        monkeypatch.setattr(loop, "handle_turn", fake_handle_turn)
        first_events = list(loop._handle_turn_stream_v2(request))
        retry_events = list(
            loop._handle_turn_stream_v2(request.model_copy())
        )
        sessions = db.exec(
            select(ChatSession).where(
                ChatSession.tenant_id == request.tenant_id
            )
        ).all()

    assert seen_session_ids == [expected_session_id, expected_session_id]
    assert [
        event
        for event in first_events
        if event["event"] == "session_created"
    ]
    assert not [
        event
        for event in retry_events
        if event["event"] == "session_created"
    ]
    assert [session.id for session in sessions] == [expected_session_id]


def test_turn_planner_falls_back_to_an_isolated_conversation_frame() -> None:
    session = _chat_session()
    plan = TurnPlan(
        decision="answer_only",
        user_intent="解释退款规则",
        task_frames=[],
    )

    normalized = TurnPlanner()._normalize(
        plan,
        "请解释退款规则",
        session,
        available_skills=[],
    )

    assert normalized.decision == "answer_only"
    assert len(normalized.task_frames) == 1
    frame = normalized.task_frames[0]
    assert frame.kind == "conversation"
    assert frame.decision == "answer_only"
    assert frame.task_id
    assert frame.requirements == ["解释退款规则"]
    assert frame.source_message == "请解释退款规则"
    assert frame.target_skill_id is None
    assert frame.target_step_id is None


def test_turn_planner_discards_an_unknown_sop_target() -> None:
    session = _chat_session()
    plan = TurnPlan(
        decision="start_new_task",
        user_intent="处理未知流程",
        task_frames=[
            PlannedTaskFrame(
                task_id="invalid-sop",
                kind="sop",
                decision="start_new_task",
                target_skill_id="missing-skill",
                target_step_id="missing-step",
                requirements=["执行不存在的 SOP"],
            )
        ],
    )

    normalized = TurnPlanner()._normalize(
        plan,
        "处理未知流程",
        session,
        available_skills=[_refund_skill()],
    )

    assert normalized.decision == "answer_only"
    assert len(normalized.task_frames) == 1
    frame = normalized.task_frames[0]
    assert frame.kind == "conversation"
    assert frame.task_id != "invalid-sop"
    assert frame.target_skill_id is None
    assert frame.requirements == ["处理未知流程"]


def test_turn_planner_and_store_bound_task_frames_per_turn() -> None:
    session = _chat_session()
    plan = TurnPlan(
        decision="answer_only",
        user_intent="批量处理任务",
        task_frames=[
            PlannedTaskFrame(
                task_id=f"model-task-{index}",
                kind="conversation",
                decision="answer_only",
                user_intent=f"任务 {index}",
                requirements=[f"完成任务 {index}"],
            )
            for index in range(MAX_TASK_FRAMES_PER_TURN + 4)
        ],
    )

    normalized = TurnPlanner()._normalize(
        plan,
        "批量处理任务",
        session,
        available_skills=[],
    )

    assert len(normalized.task_frames) == MAX_TASK_FRAMES_PER_TURN

    engine = _test_engine()
    with Session(engine) as db:
        db.add(session)
        db.commit()
        raw_records = TaskFrameStore(db).persist_plan(
            session,
            "turn-bounded",
            plan,
        )
        db.commit()

        assert len(raw_records) == MAX_TASK_FRAMES_PER_TURN


def test_task_request_compiler_builds_a_composite_requirement_without_outer_context() -> None:
    session = _chat_session(
        active_skill_id="refund",
        active_step_id="collect",
        slots_json={"order_id": "ORDER-1", "empty_value": ""},
    )
    frame = PlannedTaskFrame(
        task_id="task-refund",
        kind="sop",
        decision="continue_active",
        target_skill_id="refund",
        target_step_id="collect",
        user_intent="申请退款并查询物流",
        requirements=["同时查询当前物流状态", "以短信发送处理结果"],
        source_message="OUTER_CONTEXT_MUST_NOT_LEAK",
    )
    manifest = CapabilityManifest(
        available=[
            CapabilityDescriptor(
                capability_id="tool-logistics",
                name="logistics.lookup",
                kind="tool",
            )
        ],
        snapshot_revision="snapshot-1",
    )

    requirement = TaskRequestCompiler().compile(
        frame,
        session,
        _refund_skill(),
        manifest,
        memory_context=[
            {"kind": "preference", "content": " 用户偏好短信通知。 "},
            {"kind": "preference", "content": "用户偏好短信通知。"},
            {"kind": "empty", "content": "   "},
        ],
        prior_task_results=[
            {"task_frame_id": "task-prior", "task_summary": "身份已核验"}
        ],
        attachments=[
            {
                "attachment_id": "attachment-1",
                "filename": "evidence.txt",
                "workspace_path": "attachments/attachment-1-evidence.txt",
                "materialized": True,
            }
        ],
    )

    assert requirement.task_frame_id == "task-refund"
    assert requirement.goal == "完成 退款流程 的收集退款信息。"
    assert requirement.known_slots == {"order_id": "ORDER-1"}
    assert requirement.required_slots == ["refund_reason"]
    assert requirement.requirements == [
        "核对订单号并收集退款原因。",
        "补齐以下字段：refund_reason",
        "同时查询当前物流状态",
        "以短信发送处理结果",
    ]
    assert requirement.memory_projection == [
        {"kind": "preference", "content": "用户偏好短信通知。"}
    ]
    assert requirement.prior_task_results == [
        {"task_frame_id": "task-prior", "task_summary": "身份已核验"}
    ]
    assert requirement.attachments == [
        {
            "attachment_id": "attachment-1",
            "filename": "evidence.txt",
            "workspace_path": "attachments/attachment-1-evidence.txt",
            "materialized": True,
        }
    ]
    dumped = requirement.model_dump(mode="json")
    assert "source_message" not in dumped
    assert "conversation_context" not in dumped
    assert "OUTER_CONTEXT_MUST_NOT_LEAK" not in json.dumps(
        dumped, ensure_ascii=False
    )


def test_attachments_are_materialized_inside_only_the_task_workspace(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    descriptors = materialize_task_attachments(
        [
            ChatAttachmentRead(
                id="attachment/../../text",
                filename="../../evidence.txt",
                content_type="text/plain",
                size=8,
                kind="text",
                text="evidence",
            ),
            ChatAttachmentRead(
                id="binary",
                filename="image.png",
                content_type="image/png",
                size=4,
                kind="image",
            ),
        ],
        tenant_id="tenant-demo",
        session_id="session/unsafe",
        task_frame_id="task/unsafe",
    )
    workspace = harness_task_workspace_path(
        tenant_id="tenant-demo",
        session_id="session/unsafe",
        task_frame_id="task/unsafe",
    )

    assert descriptors[0]["materialized"] is True
    relative_path = str(descriptors[0]["workspace_path"])
    assert ".." not in relative_path
    assert (workspace / relative_path).read_text(encoding="utf-8") == "evidence"
    assert descriptors[1]["materialized"] is False
    assert not (workspace / "image.png").exists()


def test_capability_manifest_only_exposes_current_step_sop_specific_resources() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        db.add(Tenant(id="tenant-demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent-overall",
                tenant_id="tenant-demo",
                name="整体智能体",
                is_overall=True,
            )
        )
        resources: list[tuple[str, object]] = [
            (
                "general_skill",
                GeneralSkill(
                    id="general-shared",
                    tenant_id="tenant-demo",
                    slug="shared",
                    name="通用技能",
                    skill_markdown="# Shared",
                    status="published",
                    capability_scope="general",
                ),
            ),
            (
                "general_skill",
                GeneralSkill(
                    id="specific-first",
                    tenant_id="tenant-demo",
                    slug="first-only",
                    name="步骤一技能",
                    skill_markdown="# First",
                    status="published",
                    capability_scope="sop_specific",
                ),
            ),
            (
                "general_skill",
                GeneralSkill(
                    id="specific-second",
                    tenant_id="tenant-demo",
                    slug="second-only",
                    name="步骤二技能",
                    skill_markdown="# Second",
                    status="published",
                    capability_scope="sop_specific",
                ),
            ),
            (
                "tool",
                Tool(
                    id="tool-first",
                    tenant_id="tenant-demo",
                    name="refund.lookup",
                    method="POST",
                    url="https://example.test/refund",
                    capability_scope="sop_specific",
                ),
            ),
        ]
        for _, resource in resources:
            db.add(resource)
        db.flush()
        for resource_type, resource in resources:
            ensure_open_gallery_binding(
                db,
                "tenant-demo",
                resource_type,
                resource.id,  # type: ignore[attr-defined]
            )
        db.commit()

        skill = _scope_skill()
        first = CapabilityManifestBuilder(db).build(
            "tenant-demo",
            "agent-overall",
            skill,
            "first",
        )
        second = CapabilityManifestBuilder(db).build(
            "tenant-demo",
            "agent-overall",
            skill,
            "second",
        )
        conversation = CapabilityManifestBuilder(db).build(
            "tenant-demo",
            "agent-overall",
            None,
            None,
        )

    assert "general_skill.shared" in first.allowed_names()
    assert "general_skill.first-only" in first.allowed_names()
    assert "refund.lookup" in first.allowed_names()
    assert "general_skill.second-only" not in first.allowed_names()

    assert "general_skill.shared" in second.allowed_names()
    assert "general_skill.second-only" in second.allowed_names()
    assert "general_skill.first-only" not in second.allowed_names()
    assert "refund.lookup" not in second.allowed_names()

    assert "general_skill.shared" in conversation.allowed_names()
    assert "general_skill.first-only" not in conversation.allowed_names()
    assert "general_skill.second-only" not in conversation.allowed_names()
    assert "refund.lookup" not in conversation.allowed_names()


def test_external_tool_names_cannot_shadow_later_builtin_capabilities() -> None:
    available = [
        CapabilityDescriptor(
            capability_id="builtin.fs.read_file",
            name="read_file",
            kind="file",
        ),
        CapabilityDescriptor(
            capability_id="tool-existing",
            name="external_tool.tool-collision",
            kind="tool",
        ),
    ]

    assert (
        _available_invocation_name("knowledge_search", "tool-kb", available)
        == "external_tool.tool-kb"
    )
    assert (
        _available_invocation_name("read_file", "tool-collision", available)
        == "external_tool.tool-collision.2"
    )


def test_external_failure_claim_is_released_only_when_request_was_not_sent() -> None:
    assert _failure_was_not_sent(
        {"success": False, "error": {"code": "CAPABILITY_SNAPSHOT_CHANGED"}}
    )
    assert not _failure_was_not_sent(
        {"success": False, "error": {"code": "TIMEOUT"}}
    )
    assert not _failure_was_not_sent(
        {"success": False, "error": {"code": "HTTP_ERROR"}}
    )


def test_external_idempotency_key_is_stable_per_task_not_entire_session(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    tool = Tool(
        id="tool-write",
        tenant_id="tenant-demo",
        name="orders.create",
        method="POST",
        url="https://example.test/orders",
    )
    descriptor = CapabilityDescriptor(
        capability_id=tool.id,
        name=tool.name,
        kind="tool",
    )
    engine = _test_engine()
    with Session(engine) as db:
        db.add(tool)
        db.commit()
        first = HarnessCapabilityInvoker(
            db,
            tenant_id="tenant-demo",
            session=_chat_session(),
            task_frame_id="task-1",
            model_config=_model_config(),
            manifest=CapabilityManifest(available=[descriptor]),
            active_skill=None,
            active_step_id=None,
            agent_id=None,
        )
        retry = HarnessCapabilityInvoker(
            db,
            tenant_id="tenant-demo",
            session=_chat_session(),
            task_frame_id="task-1",
            model_config=_model_config(),
            manifest=CapabilityManifest(available=[descriptor]),
            active_skill=None,
            active_step_id=None,
            agent_id=None,
        )
        later_task = HarnessCapabilityInvoker(
            db,
            tenant_id="tenant-demo",
            session=_chat_session(),
            task_frame_id="task-2",
            model_config=_model_config(),
            manifest=CapabilityManifest(available=[descriptor]),
            active_skill=None,
            active_step_id=None,
            agent_id=None,
        )
        arguments = {"order_id": "ORDER-1"}

        first_key = first._logical_action_key(descriptor, arguments)
        retry_key = retry._logical_action_key(descriptor, arguments)
        later_key = later_task._logical_action_key(descriptor, arguments)

    assert first_key == retry_key
    assert first_key != later_key


def test_general_skill_harness_tool_reads_full_package_without_host_execution(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    skill = GeneralSkill(
        id="general-runner",
        tenant_id="tenant-demo",
        slug="runner",
        name="Runner",
        skill_markdown="# Runner",
        skill_files_json=[
            {"path": "SKILL.md", "content": "# Runner"},
            {"path": "scripts/run.sh", "content": "echo ok"},
        ],
        status="published",
    )
    descriptor = CapabilityDescriptor(
        capability_id=skill.id,
        name="general_skill.runner",
        kind="general_skill",
        metadata={
            "slug": skill.slug,
            "content_digest": general_skill_snapshot_digest(skill),
        },
    )
    engine = _test_engine()
    with Session(engine) as db:
        db.add(skill)
        db.commit()
        invoker = HarnessCapabilityInvoker(
            db,
            tenant_id="tenant-demo",
            session=_chat_session(user_id="user-1"),
            task_frame_id="task-skill",
            model_config=_model_config(),
            manifest=CapabilityManifest(available=[descriptor]),
            active_skill=None,
            active_step_id=None,
            agent_id=None,
        )
        read_result = invoker._load_general_skill(
            skill.id,
            descriptor.metadata,
            {"query": "inspect"},
        )

    assert read_result["success"] is True
    assert [
        item["path"] for item in read_result["data"]["package"]["files"]
    ] == ["SKILL.md", "scripts/run.sh"]
    assert read_result["data"]["operation"] == "read"
    assert "不会执行" in read_result["data"]["notice"]


def test_harness_agent_enforces_tool_allowlist_and_keeps_an_isolated_transcript(
    monkeypatch,
) -> None:
    payloads: list[dict[str, object]] = []
    actions = iter(
        [
            {
                "action": "tool",
                "tool_name": "forbidden.tool",
                "arguments": {"secret": True},
            },
            {
                "action": "tool",
                "tool_name": "allowed.tool",
                "arguments": {"query": "ORDER-1"},
            },
            {
                "action": "finish",
                "status": "completed",
                "reply_fragment": "已完成查询。",
                "task_summary": "物流查询完成。",
            },
        ]
    )

    class FakeLLMClient:
        def __init__(self, _model_config: ModelConfig):
            pass

        def generate_json(
            self, _system_prompt: str, payload: dict[str, object]
        ) -> dict[str, object]:
            payloads.append(deepcopy(payload))
            return next(actions)

    monkeypatch.setattr(harness_agent_module, "LLMClient", FakeLLMClient)
    invoked: list[tuple[str, dict[str, object]]] = []

    def invoke_tool(name: str, arguments: dict[str, object]) -> dict[str, object]:
        invoked.append((name, arguments))
        return {
            "success": True,
            "data": {"status": "in_transit"},
            "citations": [{"source": "logistics"}],
        }

    result = HarnessTaskAgent().run(
        TaskRequirement(
            task_frame_id="task-1",
            kind="conversation",
            goal="查询物流",
            requirements=["查询 ORDER-1 的物流"],
            memory_projection=[{"kind": "preference", "content": "使用中文回复"}],
            capability_manifest=CapabilityManifest(
                available=[
                    CapabilityDescriptor(
                        capability_id="allowed",
                        name="allowed.tool",
                        kind="tool",
                    )
                ]
            ),
        ),
        _model_config(),
        invoke_tool,
        max_actions=3,
    )

    assert result.status == "completed"
    assert result.action_count == 3
    assert result.reply_fragment == "已完成查询。"
    assert result.citations == [{"source": "logistics"}]
    assert invoked == [("allowed.tool", {"query": "ORDER-1"})]

    assert set(payloads[0]) == {
        "task_requirement",
        "harness_transcript",
        "iteration",
        "remaining_actions",
    }
    assert payloads[0]["harness_transcript"] == []
    second_transcript = payloads[1]["harness_transcript"]
    assert isinstance(second_transcript, list)
    assert second_transcript[0]["tool_name"] == "forbidden.tool"
    assert second_transcript[0]["result"]["error"]["code"] == "TOOL_NOT_AVAILABLE"
    assert "OUTER_CONTEXT_MUST_NOT_LEAK" not in json.dumps(
        payloads, ensure_ascii=False
    )


def test_harness_agent_projects_only_validated_current_turn_images(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    data_url = "data:image/png;base64,aW1n"
    descriptors = materialize_task_attachments(
        [
            ChatAttachmentRead(
                id="image-current-turn",
                filename="screen.png",
                content_type="image/png",
                size=3,
                kind="image",
                data_url=data_url,
            )
        ],
        tenant_id="tenant-demo",
        session_id="session-demo",
        task_frame_id="task-image",
    )
    payloads: list[dict[str, object]] = []

    class FakeLLMClient:
        def __init__(self, _model_config: ModelConfig):
            pass

        def generate_json(
            self, _system_prompt: str, payload: dict[str, object]
        ) -> dict[str, object]:
            payloads.append(deepcopy(payload))
            return {
                "action": "finish",
                "status": "completed",
                "reply_fragment": "已读取图片。",
                "task_summary": "图片分析完成。",
            }

    monkeypatch.setattr(harness_agent_module, "LLMClient", FakeLLMClient)
    assert descriptors[0]["vision_available"] is True
    assert data_url not in json.dumps(descriptors, ensure_ascii=False)
    image_payloads = validated_task_image_payloads(
        [
            ChatAttachmentRead(
                id="image-current-turn",
                filename="screen.png",
                content_type="image/png",
                size=3,
                kind="image",
                data_url=data_url,
            )
        ]
    )
    requirement = TaskRequirement(
        task_frame_id="task-image",
        kind="conversation",
        goal="分析本轮图片",
        requirements=["说明图片内容"],
        attachments=descriptors,
    )
    requirement_dump = requirement.model_dump(mode="json")
    assert data_url not in json.dumps(requirement_dump, ensure_ascii=False)

    engine = _test_engine()
    with Session(engine) as db:
        session = _chat_session()
        row = HarnessTaskFrameRecord(
            tenant_id=session.tenant_id,
            session_id=session.id,
            source_turn_id="turn-image",
            task_id="task-image",
            kind="conversation",
            status="queued",
        )
        db.add_all([session, row])
        db.commit()
        store = TaskFrameStore(db)
        store.mark_running(row)
        store.save_requirement(row, requirement_dump)
        run = store.start_run(
            row,
            requirement=requirement_dump,
            capability_snapshot={"available": []},
        )
        db.commit()
        db.refresh(row)
        db.refresh(run)
        assert data_url not in json.dumps(
            row.task_requirement_json,
            ensure_ascii=False,
        )
        assert data_url not in json.dumps(
            run.task_requirement_json,
            ensure_ascii=False,
        )

    result = HarnessTaskAgent().run(
        requirement,
        _model_config(),
        lambda _name, _arguments: {"success": True},
        image_payloads=image_payloads,
    )

    assert result.status == "completed"
    assert payloads[0]["conversation_context"] == {
        "messages": [
            {
                "role": "user",
                "content": "当前 TaskRequirement 本轮上传的图片附件。",
                "images": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": data_url,
                            "detail": "auto",
                        },
                    }
                ],
            }
        ]
    }
    task_requirement = payloads[0]["task_requirement"]
    assert isinstance(task_requirement, dict)
    serialized_requirement = json.dumps(
        task_requirement,
        ensure_ascii=False,
    )
    assert data_url not in serialized_requirement
    assert "conversation_context" not in task_requirement


def test_harness_agent_drops_tampered_image_data_url(
    monkeypatch,
) -> None:
    payloads: list[dict[str, object]] = []

    class FakeLLMClient:
        def __init__(self, _model_config: ModelConfig):
            pass

        def generate_json(
            self, _system_prompt: str, payload: dict[str, object]
        ) -> dict[str, object]:
            payloads.append(deepcopy(payload))
            return {
                "action": "finish",
                "status": "completed",
                "reply_fragment": "无法读取图片。",
                "task_summary": "图片校验失败。",
            }

    monkeypatch.setattr(harness_agent_module, "LLMClient", FakeLLMClient)
    HarnessTaskAgent().run(
        TaskRequirement(
            task_frame_id="task-image",
            kind="conversation",
            goal="分析本轮图片",
            requirements=["说明图片内容"],
            attachments=[
                {
                    "attachment_id": "image-current-turn",
                    "filename": "screen.png",
                    "content_type": "image/png",
                    "size": 3,
                    "kind": "image",
                    "vision_available": True,
                }
            ],
        ),
        _model_config(),
        lambda _name, _arguments: {"success": True},
        image_payloads=[
            ValidatedTaskImagePayload(
                attachment_id="image-current-turn",
                filename="screen.png",
                content_type="image/png",
                size=3,
                data_url="https://example.test/image.png",
            )
        ],
    )

    assert "conversation_context" not in payloads[0]
    task_requirement = payloads[0]["task_requirement"]
    assert isinstance(task_requirement, dict)
    assert task_requirement["attachments"][0]["vision_available"] is False
    assert "https://example.test/image.png" not in json.dumps(task_requirement)


def test_task_frame_store_persists_frames_and_projects_only_active_sop_work() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        session = _chat_session(
            active_skill_id="refund",
            active_step_id="collect",
            slots_json={"order_id": "ORDER-1"},
            pending_tasks_json=[
                {
                    "task_id": "legacy-task",
                    "status": "pending",
                    "target_skill_id": "legacy-skill",
                }
            ],
        )
        db.add(session)
        db.commit()

        records = TaskFrameStore(db).persist_plan(
            session,
            "turn-1",
            TurnPlan(
                decision="continue_active",
                user_intent="退款并查询物流",
                task_frames=[
                    PlannedTaskFrame(
                        task_id="task-sop",
                        kind="sop",
                        decision="continue_active",
                        target_skill_id="refund",
                        target_step_id="ignored-for-active-frame",
                        user_intent="申请退款",
                        requirements=["完成退款申请"],
                        slot_hints={"refund_reason": "商品破损"},
                    ),
                    PlannedTaskFrame(
                        task_id="task-conversation",
                        kind="conversation",
                        decision="answer_only",
                        user_intent="查询物流",
                        requirements=["查询物流"],
                    ),
                ],
            ),
        )
        db.commit()

        assert [row.task_id for row in records] == [
            "task-sop",
            "task-conversation",
        ]
        assert records[0].step_id == "collect"
        assert records[0].slots_json == {
            "order_id": "ORDER-1",
            "refund_reason": "商品破损",
        }
        assert [item["task_id"] for item in session.pending_tasks_json] == [
            "legacy-task",
            "task-sop",
        ]

    with Session(engine) as db:
        persisted = db.exec(
            select(HarnessTaskFrameRecord).order_by(
                HarnessTaskFrameRecord.sequence
            )
        ).all()
        reloaded_session = db.get(ChatSession, "session-1")

        assert [row.task_id for row in persisted] == [
            "task-sop",
            "task-conversation",
        ]
        assert persisted[0].source_turn_id == "turn-1"
        assert persisted[0].requirements_json == ["完成退款申请"]
        assert persisted[1].kind == "conversation"
        assert reloaded_session is not None
        assert [item["task_id"] for item in reloaded_session.pending_tasks_json] == [
            "legacy-task",
            "task-sop",
        ]

        restored = planned_frame_from_record(persisted[0])
        assert restored.kind == "sop"
        assert restored.target_skill_id == "refund"
        assert restored.target_step_id == "collect"
        assert restored.slot_hints == {
            "order_id": "ORDER-1",
            "refund_reason": "商品破损",
        }

        store = TaskFrameStore(db)
        store.finish_frame(
            persisted[0],
            status="completed",
            step_id="done",
            slots=persisted[0].slots_json,
            result={"status": "completed", "task_summary": "退款已完成"},
        )
        store.project_session(reloaded_session)
        db.commit()

        assert reloaded_session.pending_tasks_json == [
            {
                "task_id": "legacy-task",
                "status": "pending",
                "target_skill_id": "legacy-skill",
            }
        ]


def test_turn_action_budget_defers_unstarted_frames_as_queued() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        session = _chat_session()
        db.add(session)
        db.commit()
        store = TaskFrameStore(db)
        rows = store.persist_plan(
            session,
            "turn-budget",
            TurnPlan(
                decision="answer_only",
                user_intent="处理两个任务",
                task_frames=[
                    PlannedTaskFrame(
                        task_id=f"task-{index}",
                        kind="conversation",
                        decision="answer_only",
                        requirements=[f"任务 {index}"],
                    )
                    for index in range(2)
                ],
            ),
        )

        store.defer_for_action_budget(rows[1:])
        db.commit()

        assert rows[0].status == "queued"
        assert rows[1].status == "queued"
        assert rows[1].result_json["status"] == "action_budget"
        assert rows[1].error_json["code"] == "TURN_ACTION_BUDGET_DEFERRED"


def test_cancellation_closes_every_frame_and_running_run_from_source_turn() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        session = _chat_session()
        db.add(session)
        db.commit()
        store = TaskFrameStore(db)
        current_rows = store.persist_plan(
            session,
            "turn-current",
            TurnPlan(
                decision="answer_only",
                user_intent="本轮复合任务",
                task_frames=[
                    PlannedTaskFrame(
                        task_id="current-running",
                        kind="conversation",
                        decision="answer_only",
                        requirements=["运行中的任务"],
                    ),
                    PlannedTaskFrame(
                        task_id="current-queued",
                        kind="conversation",
                        decision="answer_only",
                        requirements=["尚未运行的任务"],
                    ),
                ],
            ),
        )
        store.mark_running(current_rows[0])
        running_run = store.start_run(
            current_rows[0],
            requirement={"goal": "运行中的任务"},
            capability_snapshot={"available": []},
        )
        other_row = HarnessTaskFrameRecord(
            tenant_id=session.tenant_id,
            session_id=session.id,
            source_turn_id="turn-other",
            task_id="other-queued",
            kind="conversation",
            status="queued",
        )
        db.add(other_row)
        db.commit()

        harness_engine = object.__new__(HarnessV2Engine)
        harness_engine.db = db
        harness_engine.store = store
        harness_engine.session = session
        harness_engine.current_source_turn_id = "turn-current"
        harness_engine.active_run_id = None
        harness_engine.active_frame_id = None
        harness_engine.active_frame_lease_owner = None
        harness_engine.active_frame_attempt_no = None
        harness_engine.mark_cancelled()

        db.refresh(running_run)
        db.refresh(other_row)
        assert [row.status for row in current_rows] == [
            "cancelled",
            "cancelled",
        ]
        assert running_run.status == "cancelled"
        assert other_row.status == "queued"


def test_latest_awaiting_conversation_takes_focus_without_losing_sop() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        session = _chat_session(
            active_skill_id="refund",
            active_step_id="collect",
            slots_json={"order_id": "ORDER-1"},
        )
        now = utc_now()
        sop = HarnessTaskFrameRecord(
            tenant_id=session.tenant_id,
            session_id=session.id,
            source_turn_id="turn-focus",
            task_id="sop-task",
            kind="sop",
            status="queued",
            skill_id="refund",
            step_id="collect",
            slots_json={"order_id": "ORDER-1"},
            updated_at=now,
        )
        older = HarnessTaskFrameRecord(
            tenant_id=session.tenant_id,
            session_id=session.id,
            source_turn_id="turn-focus",
            task_id="conversation-old",
            kind="conversation",
            status="awaiting_user",
            requirements_json=["补充旧问题"],
            result_json={"reply_fragment": "旧问题"},
            updated_at=now - timedelta(seconds=1),
        )
        latest = HarnessTaskFrameRecord(
            tenant_id=session.tenant_id,
            session_id=session.id,
            source_turn_id="turn-focus",
            task_id="conversation-latest",
            kind="conversation",
            status="awaiting_user",
            requirements_json=["补充新问题"],
            task_requirement_json={"required_slots": ["answer"]},
            result_json={"reply_fragment": "请补充新问题"},
            updated_at=now,
        )
        db.add_all([session, sop, older, latest])
        db.commit()

        harness_engine = object.__new__(HarnessV2Engine)
        harness_engine.db = db
        harness_engine.store = TaskFrameStore(db)
        harness_engine.owner = object()
        harness_engine._restore_visible_active_frame(
            session,
            [sop, older, latest],
            {
                "active_skill_id": "refund",
                "active_step_id": "collect",
                "slots_json": {"order_id": "ORDER-1"},
            },
        )
        harness_engine.store.project_session(session)
        db.commit()

        assert session.active_skill_id is None
        assert session.awaiting_input_json["task_id"] == "conversation-latest"
        assert session.awaiting_input_json["expected_fields"] == ["answer"]
        assert (
            session.context_state_json["harness_v2"]["active_task_frame_id"]
            == "conversation-latest"
        )
        assert [item["task_id"] for item in session.pending_tasks_json] == [
            "sop-task"
        ]


def test_frame_and_run_completion_are_fenced_by_current_lease() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        session = _chat_session()
        row = HarnessTaskFrameRecord(
            tenant_id=session.tenant_id,
            session_id=session.id,
            source_turn_id="turn-fence",
            task_id="task-fence",
            kind="conversation",
            status="queued",
        )
        db.add_all([session, row])
        db.commit()
        store = TaskFrameStore(db)
        store.mark_running(row)
        run = store.start_run(
            row,
            requirement={"goal": "测试 fencing"},
            capability_snapshot={"available": []},
        )
        db.commit()
        db.refresh(row)
        db.refresh(run)
        stale_frame = HarnessTaskFrameRecord(**row.model_dump())
        stale_run = HarnessRunRecord(**run.model_dump())

        row.lease_owner = "new-frame-owner"
        row.attempt_no += 1
        row.state_version += 1
        run.lease_owner = "new-run-owner"
        run.attempt_no += 1
        db.add_all([row, run])
        db.commit()

        with pytest.raises(TaskFrameClaimConflict):
            store.save_requirement(stale_frame, {"goal": "stale"})
        with pytest.raises(TaskFrameClaimConflict):
            store.finish_frame(
                stale_frame,
                status="completed",
                step_id=None,
                slots={},
                result={"status": "completed"},
            )
        with pytest.raises(TaskFrameClaimConflict):
            store.finish_run(
                stale_run,
                status="completed",
                action_count=1,
                result={"status": "completed"},
            )

        db.refresh(row)
        db.refresh(run)
        assert row.status == "running"
        assert row.lease_owner == "new-frame-owner"
        assert run.status == "running"
        assert run.lease_owner == "new-run-owner"

        interrupted_engine = object.__new__(HarnessV2Engine)
        interrupted_engine.db = db
        interrupted_engine.store = store
        interrupted_engine.session = session
        interrupted_engine.current_source_turn_id = "turn-fence"
        interrupted_engine.active_run_id = run.id
        interrupted_engine.active_frame_id = row.id
        interrupted_engine.active_frame_lease_owner = stale_frame.lease_owner
        interrupted_engine.active_frame_attempt_no = stale_frame.attempt_no
        interrupted_engine.mark_interrupted("WORKER_CRASHED", "stale worker")

        db.refresh(row)
        db.refresh(run)
        assert row.status == "running"
        assert row.lease_owner == "new-frame-owner"
        assert run.status == "running"
        assert run.lease_owner == "new-run-owner"


def _test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _chat_session(**updates: object) -> ChatSession:
    values: dict[str, object] = {
        "id": "session-1",
        "tenant_id": "tenant-demo",
    }
    values.update(updates)
    return ChatSession(**values)


def _refund_skill() -> Skill:
    return Skill(
        id="skill-refund",
        tenant_id="tenant-demo",
        skill_id="refund",
        name="退款流程",
        status="published",
        content_json={
            "start_node_id": "collect",
            "goal": ["完成退款审核"],
            "nodes": [
                {
                    "node_id": "collect",
                    "name": "收集退款信息",
                    "instruction": "核对订单号并收集退款原因。",
                    "expected_user_info": ["order_id", "refund_reason"],
                }
            ],
            "edges": [
                {
                    "source_node_id": "collect",
                    "next_node_id": "review",
                    "condition": "slots_complete",
                }
            ],
        },
    )


def _scope_skill() -> Skill:
    return Skill(
        id="skill-scope",
        tenant_id="tenant-demo",
        skill_id="scope-demo",
        name="能力范围流程",
        status="published",
        content_json={
            "start_node_id": "first",
            "nodes": [
                {
                    "node_id": "first",
                    "name": "步骤一",
                    "capability_refs": {
                        "general_skill_ids": ["specific-first"],
                        "tool_ids": ["tool-first"],
                    },
                },
                {
                    "node_id": "second",
                    "name": "步骤二",
                    "capability_refs": {
                        "general_skill_ids": ["specific-second"],
                    },
                },
            ],
        },
    )


def _model_config() -> ModelConfig:
    return ModelConfig(
        id="model-test",
        tenant_id="tenant-demo",
        name="测试模型",
        api_key_encrypted="test",
        model="test-model",
    )
