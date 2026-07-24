from types import SimpleNamespace

import pytest

from app.core.agent_loop import (
    GRAPH_PENDING_STEPS_SLOT,
    _KNOWLEDGE_RESULTS_CACHE,
    _KNOWLEDGE_STEPS_SEEN,
    AgentLoop,
)
from app.core.skill_runtime import SkillRuntime
from app.db.models import AgentEvent, ChatSession, Message, Skill, Tool
from app.session.session_schema import (
    AwaitingInput,
    KnowledgeQuery,
    PendingTask,
    RouterDecision,
    StepAgentResult,
)
from app.tools.tool_schema import ToolCall, ToolResult


class FakeEvents:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, str, dict]] = []

    def record(self, tenant_id: str, session_id: str, event_type: str, payload: dict) -> None:
        self.records.append((tenant_id, session_id, event_type, payload))


class FakeDb:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.refreshed: list[object] = []
        self.added: list[object] = []

    def add(self, row: object) -> None:
        self.added.append(row)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def refresh(self, row: object) -> None:
        self.refreshed.append(row)


class FakeExecResult:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def all(self) -> list[object]:
        return self.rows

    def first(self) -> object | None:
        return self.rows[0] if self.rows else None


def test_router_decision_only_hydrates_structured_profile_memory() -> None:
    loop = object.__new__(AgentLoop)
    session = ChatSession(id="session_test", tenant_id="tenant_demo", slots_json={})
    decision = RouterDecision(
        decision="start_new_task",
        target_skill_id="purchase",
        target_step_id="collect_user_name",
        slot_hints={"product_name": "a1", "quantity": 1},
        awaiting_input=AwaitingInput(
            skill_id="purchase",
            step_id="collect_user_name",
            expected_fields=["user_name", "product_id"],
        ),
    )

    hydrated = loop._hydrate_router_decision_from_context(
        session,
        decision,
        [_purchase_skill()],
        [{"kind": "profile", "content": "hm", "metadata": {"key": "preferred_name"}}],
    )

    assert hydrated["primary"] == {"user_name": "hm"}
    assert decision.slot_hints == {"product_name": "a1", "quantity": 1, "user_name": "hm"}
    assert decision.awaiting_input is not None
    assert decision.awaiting_input.expected_fields == ["product_id"]


class FakeMessageDb(FakeDb):
    def __init__(self, rows: list[Message]) -> None:
        super().__init__()
        self.rows = rows

    def exec(self, _statement: object) -> FakeExecResult:
        return FakeExecResult(self.rows)


class FakeEventDb(FakeDb):
    def __init__(self, tool: Tool | None, rows: list[AgentEvent]) -> None:
        super().__init__()
        self.tool = tool
        self.rows = rows
        self.exec_calls = 0

    def exec(self, _statement: object) -> FakeExecResult:
        self.exec_calls += 1
        if self.exec_calls == 1:
            return FakeExecResult([self.tool] if self.tool else [])
        return FakeExecResult(self.rows)


class FakeToolExecutor:
    def __init__(self, db: FakeDb) -> None:
        self.db = db
        self.commits_seen_before_execute: int | None = None

    def execute(
        self,
        tenant_id: str,
        tool_call: ToolCall,
        active_skill_id: str | None = None,
        agent_id: str | None = None,
    ) -> ToolResult:
        self.commits_seen_before_execute = self.db.commits
        return ToolResult(tool_name=tool_call.name, success=True, data={"ok": True})


def test_tool_call_start_event_is_committed_before_external_execute() -> None:
    db = FakeDb()
    executor = FakeToolExecutor(db)
    loop = object.__new__(AgentLoop)
    loop.db = db
    loop.events = FakeEvents()
    loop.tool_executor = executor
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="purchase",
    )

    result = loop._execute_tool_call(
        _request("下单"),
        session,
        ToolCall(name="product.purchase", arguments={"product_id": "A1"}),
    )

    assert result.success is True
    assert executor.commits_seen_before_execute == 1
    assert db.commits == 2
    assert [record[2] for record in loop.events.records] == [
        "tool_call_started",
        "tool_call_finished",
    ]


def test_side_effect_tool_call_reuses_previous_successful_result() -> None:
    tool = Tool(
        tenant_id="tenant_demo",
        name="crm.create_ticket",
        display_name="创建工单",
        method="POST",
        url="http://localhost:8000/api/mock/tickets",
        enabled=True,
    )
    event = AgentEvent(
        id="evt_existing_tool_result",
        tenant_id="tenant_demo",
        session_id="session_test",
        event_type="tool_call_finished",
        payload_json={
            "tool_name": "crm.create_ticket",
            "success": True,
            "data": {"ticket_id": "TCK-1001", "status": "created"},
            "tool_call": {
                "name": "crm.create_ticket",
                "arguments": {
                    "customer_id": "C-1",
                    "subject": "发票开具",
                    "priority": "normal",
                },
            },
        },
    )
    db = FakeEventDb(tool, [event])
    executor = FakeToolExecutor(db)
    loop = object.__new__(AgentLoop)
    loop.db = db
    loop.events = FakeEvents()
    loop.tool_executor = executor
    session = ChatSession(
        id="session_test", tenant_id="tenant_demo", active_skill_id="skill_leave_apply_001"
    )

    result = loop._execute_tool_call(
        _request("重试一下，如果办理失败需要提示我"),
        session,
        ToolCall(
            name="crm.create_ticket",
            arguments={
                "customer_id": "C-1",
                "subject": "发票开具",
                "priority": "normal",
            },
        ),
        tool_call_id="toolcall_retry",
    )

    assert result.success is True
    assert result.data["ticket_id"] == "TCK-1001"
    assert result.data["idempotent_replay"] is True
    assert executor.commits_seen_before_execute is None
    assert db.commits == 1
    assert [record[2] for record in loop.events.records] == [
        "tool_call_reused",
        "tool_call_finished",
    ]
    assert loop.events.records[-1][3]["idempotent_replay"] is True


def test_post_read_only_tool_does_not_reuse_previous_result() -> None:
    tool = Tool(
        tenant_id="tenant_demo",
        name="order.query",
        display_name="查询订单",
        method="POST",
        url="http://localhost:8000/api/mock/order/query",
        config_json={"idempotency": {"enabled": False}},
        enabled=True,
    )
    event = AgentEvent(
        id="evt_existing_query_result",
        tenant_id="tenant_demo",
        session_id="session_test",
        event_type="tool_call_finished",
        payload_json={
            "tool_name": "order.query",
            "success": True,
            "data": {"order_id": "O-1", "status": "paid"},
            "tool_call": {"name": "order.query", "arguments": {"order_id": "O-1"}},
        },
    )
    db = FakeEventDb(tool, [event])
    executor = FakeToolExecutor(db)
    loop = object.__new__(AgentLoop)
    loop.db = db
    loop.events = FakeEvents()
    loop.tool_executor = executor
    session = ChatSession(id="session_test", tenant_id="tenant_demo", active_skill_id="refund")

    result = loop._execute_tool_call(
        _request("查订单"),
        session,
        ToolCall(name="order.query", arguments={"order_id": "O-1"}),
    )

    assert result.success is True
    assert executor.commits_seen_before_execute == 1
    assert [record[2] for record in loop.events.records] == [
        "tool_call_started",
        "tool_call_finished",
    ]


@pytest.mark.parametrize("compacted_now", [False, True])
def test_stream_emits_context_status_only_when_compaction_runs(compacted_now: bool) -> None:
    db = FakeDb()
    loop = object.__new__(AgentLoop)
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        user_id="user_demo",
        agent_id="agent_demo",
        slots_json={},
        skill_stack_json=[],
        pending_tasks_json=[],
        knowledge_context_json=[],
    )
    user_message = Message(
        id="msg_user",
        tenant_id="tenant_demo",
        session_id=session.id,
        role="user",
        content="你好",
    )

    loop.db = db
    loop.events = FakeEvents()
    loop.memory = SimpleNamespace(context_memories=lambda *_args, **_kwargs: [])
    loop.runtime = SimpleNamespace(apply_decision=lambda *_args, **_kwargs: None)
    loop.router = SimpleNamespace(
        decide=lambda *_args, **_kwargs: RouterDecision(
            decision="answer_only",
            user_intent="问候",
            reason="普通问候，不需要进入业务流程。",
        )
    )
    loop._get_or_create_session = lambda _request: session
    loop._append_message = lambda *_args, **_kwargs: user_message
    loop._get_request_model = lambda *_args, **_kwargs: _model_config()
    loop._list_published_skills = lambda *_args, **_kwargs: [_purchase_skill()]
    loop._list_enabled_tools = lambda *_args, **_kwargs: []
    loop._tools_with_general_skills = lambda *_args, **_kwargs: []
    loop._get_persona_prompt = lambda *_args, **_kwargs: None
    loop._drop_unavailable_skill_state = lambda *_args, **_kwargs: False
    loop._finish_stale_completed_skill = lambda *_args, **_kwargs: None
    loop._scene_router_deferred_to_general = lambda *_args, **_kwargs: False
    loop._hydrate_router_decision_from_context = lambda *_args, **_kwargs: {}
    loop._conversation_context = lambda *_args, **_kwargs: {
        "metadata": {"compacted_now": compacted_now}
    }
    loop._get_active_skill = lambda *_args, **_kwargs: None
    loop._should_record_runtime_event_after_prune = lambda *_args, **_kwargs: False
    loop._should_run_step_agent = lambda *_args, **_kwargs: False
    loop._auto_knowledge_step_result = lambda *_args, **_kwargs: StepAgentResult()
    loop._generate_reply_stream_segment = lambda *_args, **_kwargs: iter(["收到"])
    loop._finalize_turn = lambda *_args, **_kwargs: None
    loop._recent_messages = lambda *_args, **_kwargs: []
    loop._enqueue_memory_capture = lambda *_args, **_kwargs: None

    events = list(loop.handle_turn_stream(_request("你好")))
    names = [event["event"] for event in events]
    router_index = names.index("router_decision")
    reply_index = names.index("stream_delta")

    preparing_indexes = [
        index
        for index, event in enumerate(events)
        if event["event"] == "status" and event["data"].get("phase") == "preparing"
    ]
    if compacted_now:
        assert len(preparing_indexes) == 1
        assert names.index("user_message_received") < preparing_indexes[0] < router_index
    else:
        assert preparing_indexes == []
    assert router_index < reply_index
    router_payload = events[router_index]["data"]
    assert router_payload["user_intent"] == "问候"
    assert router_payload["reason"] == "普通问候，不需要进入业务流程。"


def test_stream_disconnect_does_not_persist_stop_event_without_cancel_flag() -> None:
    db = FakeDb()
    loop = object.__new__(AgentLoop)
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        user_id="user_demo",
        agent_id="agent_demo",
        slots_json={},
        skill_stack_json=[],
        pending_tasks_json=[],
        knowledge_context_json=[],
    )
    user_message = Message(
        id="msg_user",
        tenant_id="tenant_demo",
        session_id=session.id,
        role="user",
        content="你好",
    )

    loop.db = db
    loop.events = FakeEvents()
    loop.memory = SimpleNamespace(context_memories=lambda *_args, **_kwargs: [])
    loop.runtime = SimpleNamespace(apply_decision=lambda *_args, **_kwargs: None)
    loop.router = SimpleNamespace(
        decide=lambda *_args, **_kwargs: RouterDecision(
            decision="answer_only",
            user_intent="问候",
            reason="普通问候，不需要进入业务流程。",
        )
    )
    loop._get_or_create_session = lambda _request: session
    loop._append_message = lambda *_args, **_kwargs: user_message
    loop._get_request_model = lambda *_args, **_kwargs: _model_config()
    loop._list_published_skills = lambda *_args, **_kwargs: [_purchase_skill()]
    loop._list_enabled_tools = lambda *_args, **_kwargs: []
    loop._tools_with_general_skills = lambda *_args, **_kwargs: []
    loop._get_persona_prompt = lambda *_args, **_kwargs: None
    loop._drop_unavailable_skill_state = lambda *_args, **_kwargs: False
    loop._finish_stale_completed_skill = lambda *_args, **_kwargs: None
    loop._scene_router_deferred_to_general = lambda *_args, **_kwargs: False
    loop._hydrate_router_decision_from_context = lambda *_args, **_kwargs: {}
    loop._conversation_context = lambda *_args, **_kwargs: {}
    loop._get_active_skill = lambda *_args, **_kwargs: None
    loop._should_record_runtime_event_after_prune = lambda *_args, **_kwargs: False
    loop._should_run_step_agent = lambda *_args, **_kwargs: False
    loop._auto_knowledge_step_result = lambda *_args, **_kwargs: StepAgentResult()

    def disconnected_reply_stream(*_args, **_kwargs):
        raise GeneratorExit
        yield ""

    loop._generate_reply_stream_segment = disconnected_reply_stream
    loop._finalize_turn = lambda *_args, **_kwargs: None
    loop._recent_messages = lambda *_args, **_kwargs: []
    loop._enqueue_memory_capture = lambda *_args, **_kwargs: None

    with pytest.raises(GeneratorExit):
        list(loop.handle_turn_stream(_request("你好")))

    assert db.rollbacks == 1
    assert "stream_cancelled" not in [record[2] for record in loop.events.records]


def test_stream_text_events_are_persisted_for_refresh_recovery() -> None:
    db = FakeDb()
    loop = object.__new__(AgentLoop)
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        user_id="user_demo",
        agent_id="agent_demo",
        slots_json={},
        skill_stack_json=[],
        pending_tasks_json=[],
        knowledge_context_json=[],
    )

    loop.db = db
    loop.events = FakeEvents()

    payload = {"turn_id": "msg_user", "user_message_id": "msg_user", "content": "收到"}
    event = loop._stream_event("stream_delta", session, payload)

    assert event["event"] == "stream_delta"
    assert loop.events.records == [
        ("tenant_demo", "session_test", "stream_delta", payload),
    ]
    assert db.commits == 1


def test_stream_trace_events_require_turn_id_for_persistence() -> None:
    db = FakeDb()
    loop = object.__new__(AgentLoop)
    loop.db = db
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        user_id="user_demo",
        agent_id="agent_demo",
    )

    without_turn = {"toolName": "weather", "success": True}
    with_turn = {
        "turn_id": "msg_user",
        "user_message_id": "msg_user",
        "toolName": "weather",
        "success": True,
    }

    loop._stream_event("tool_result", session, without_turn)
    event = loop._stream_event("tool_result", session, with_turn)

    assert event["event"] == "tool_result"
    assert loop.events.records == [
        ("tenant_demo", "session_test", "tool_result", with_turn),
    ]
    assert db.commits == 1


def test_router_order_keeps_current_turn_followup_out_of_pending_tasks() -> None:
    loop = object.__new__(AgentLoop)
    loop.runtime = SkillRuntime()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="purchase",
        active_step_id="collect_user_name",
        slots_json={"user_name": "hm"},
    )
    router_decision = RouterDecision(
        decision="continue_active",
        target_skill_id="purchase",
        target_step_id="collect_user_name",
        confidence=0.91,
        user_intent="继续购买 A1，并比较 A1 和 A3",
        reason="用户补充购买目标，同时提出独立比价任务。",
        source_message="我买 A1 前跟 A3 比一下价格",
        slot_hints={"product_id": "A1", "quantity": 1},
        task_frames=[
            PendingTask(
                decision="continue_active",
                target_skill_id="purchase",
                target_step_id="collect_user_name",
                user_intent="继续购买 A1",
                source_message="我买 A1 前跟 A3 比一下价格",
                slot_hints={"product_id": "A1", "quantity": 1},
            ),
            PendingTask(
                task_id="task_price_compare_a1_a3",
                decision="start_new_task",
                target_skill_id="price_compare",
                target_step_id="collect_products",
                user_intent="比较 A1 和 A3 的价格",
                source_message="我买 A1 前跟 A3 比一下价格",
                slot_hints={"product_name_1": "A1", "product_name_2": "A3"},
            )
        ],
    )

    loop.runtime.apply_decision(session, router_decision)

    assert session.active_skill_id == "purchase"
    assert session.active_step_id == "collect_user_name"
    assert session.slots_json == {"user_name": "hm", "product_id": "A1", "quantity": 1}
    assert session.pending_tasks_json == []
    assert [task.target_skill_id for task in router_decision.task_frames] == [
        "purchase",
        "price_compare",
    ]


def test_router_keeps_existing_active_task_in_current_turn_plan_after_new_primary() -> None:
    loop = object.__new__(AgentLoop)
    loop.runtime = SkillRuntime()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="purchase",
        active_step_id="collect_user_name",
        slots_json={"user_name": "hm"},
    )
    router_decision = RouterDecision(
        decision="start_new_task",
        target_skill_id="price_compare",
        target_step_id="collect_products",
        confidence=0.95,
        user_intent="比较 A1 和 A3 的价格",
        reason="用户提出独立比价任务。",
        source_message="我想买一个A1,然后想跟A3比下价格",
        slot_hints={"product_name_1": "A1", "product_name_2": "A3"},
        task_frames=[
            PendingTask(
                decision="start_new_task",
                target_skill_id="price_compare",
                target_step_id="collect_products",
                user_intent="比较 A1 和 A3 的价格",
                source_message="我想买一个A1,然后想跟A3比下价格",
                slot_hints={"product_name_1": "A1", "product_name_2": "A3"},
            ),
            PendingTask(
                task_id="task_purchase_a1",
                decision="continue_active",
                target_skill_id="purchase",
                target_step_id="collect_user_name",
                user_intent="继续购买 A1",
                source_message="我想买一个A1,然后想跟A3比下价格",
                slot_hints={"user_name": "hm"},
            )
        ],
    )

    loop.runtime.apply_decision(session, router_decision)

    assert session.active_skill_id == "price_compare"
    assert session.active_step_id == "collect_products"
    assert session.slots_json == {"product_name_1": "A1", "product_name_2": "A3"}
    assert session.pending_tasks_json == []
    assert [task.target_skill_id for task in router_decision.task_frames] == [
        "price_compare",
        "purchase",
    ]


def test_current_turn_task_frames_execute_in_order_without_pending_queue() -> None:
    loop = object.__new__(AgentLoop)
    loop.runtime = SkillRuntime()
    loop.events = FakeEvents()
    loop.db = FakeDb()
    loop._get_agent_loop_max_actions = lambda _tenant_id: 4
    loop._drop_unavailable_skill_state = lambda *_args, **_kwargs: False
    loop._should_record_runtime_event_after_prune = lambda *_args, **_kwargs: False
    loop._should_run_step_agent = lambda *_args, **_kwargs: True
    loop._get_reflection_max_rounds = lambda _tenant_id: 0
    loop._run_reflection_rounds = lambda *args, **_kwargs: tuple(args[5:9])
    loop._auto_progress_skill_graph = lambda *args, **_kwargs: tuple(args[5:9])
    loop._generate_reply_segment = lambda *_args, **_kwargs: "已完成"

    skills = [_price_compare_skill(), _purchase_skill()]
    skills_by_id = {skill.skill_id: skill for skill in skills}
    executed: list[str] = []
    loop._get_active_skill = (
        lambda _tenant_id, skill_id, _agent_id: skills_by_id.get(skill_id or "")
    )

    def run_step(_request, session, active_skill, *_args, **_kwargs):
        executed.append(active_skill.skill_id)
        return StepAgentResult(reply="已完成", is_step_completed=True)

    def finalize(_tenant_id, session, active_skill, *_args, **_kwargs):
        loop.runtime.complete_current_skill(session)
        return "completed"

    loop._run_step_agent_with_context_repair = run_step
    loop._finalize_execution_after_reply = finalize
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        pending_tasks_json=[],
    )
    frames = [
        PendingTask(
            decision="start_new_task",
            target_skill_id="price_compare",
            target_step_id="collect_products",
            slot_hints={"product_name_1": "A1", "product_name_2": "A3"},
        ),
        PendingTask(
            decision="start_new_task",
            target_skill_id="purchase",
            target_step_id="collect_user_name",
            slot_hints={"product_id": "A3", "quantity": 1},
        ),
    ]

    result = loop._try_continue_pending_after_completion(
        _request("先比较 A1 和 A3，再购买 A3"),
        session,
        _model_config(),
        skills,
        [],
        None,
        [],
        {},
        "",
        turn_task_frames=frames,
    )

    assert executed == ["price_compare", "purchase"]
    assert session.pending_tasks_json == []
    assert result is not None
    assert result.reply == "已完成\n\n已完成"


def test_non_stream_followup_executes_knowledge_query_before_tool() -> None:
    loop = object.__new__(AgentLoop)
    loop.runtime = SkillRuntime()
    loop.events = FakeEvents()
    loop.db = FakeDb()
    loop._get_agent_loop_max_actions = lambda _tenant_id: 2
    loop._drop_unavailable_skill_state = lambda *_args, **_kwargs: False
    loop._should_record_runtime_event_after_prune = lambda *_args, **_kwargs: False
    loop._should_run_step_agent = lambda *_args, **_kwargs: True
    loop._get_reflection_max_rounds = lambda _tenant_id: 0
    loop._run_reflection_rounds = lambda *args, **_kwargs: tuple(args[5:9])
    loop._auto_progress_skill_graph = lambda *args, **_kwargs: tuple(args[5:9])
    loop._generate_reply_segment = lambda *_args, **_kwargs: "已完成"
    skill = _leave_policy_skill()
    loop._get_active_skill = lambda *_args, **_kwargs: skill
    calls: list[str] = []
    loop._run_step_agent_with_context_repair = lambda *_args, **_kwargs: StepAgentResult(
        knowledge_query=KnowledgeQuery(query="事假政策")
    )

    def execute_knowledge(*_args, **_kwargs):
        calls.append("knowledge")
        return StepAgentResult(
            tool_call=ToolCall(name="hr.balance_query", arguments={"employee_id": "E1"})
        )

    def execute_tool(*args, **_kwargs):
        calls.append("tool")
        return args[5], ToolResult(tool_name="hr.balance_query", success=True, data={})

    loop._execute_knowledge_query_cycle = execute_knowledge
    loop._execute_tool_action_cycle = execute_tool
    loop._finalize_execution_after_reply = lambda *_args, **_kwargs: "completed"
    session = ChatSession(id="session_test", tenant_id="tenant_demo", pending_tasks_json=[])

    result = loop._try_continue_pending_after_completion(
        _request("继续请假"),
        session,
        _model_config(),
        [skill],
        [],
        None,
        [],
        {},
        "",
        turn_task_frames=[
            PendingTask(
                decision="start_new_task",
                target_skill_id="leave",
                target_step_id="check_policy",
                slot_hints={"leave_type": "事假"},
            )
        ],
    )

    assert result is not None
    assert calls == ["knowledge", "tool"]


def test_stream_followup_executes_knowledge_query_before_tool() -> None:
    loop = object.__new__(AgentLoop)
    loop.runtime = SkillRuntime()
    loop.events = FakeEvents()
    loop.db = FakeDb()
    loop._get_agent_loop_max_actions = lambda _tenant_id: 2
    loop._drop_unavailable_skill_state = lambda *_args, **_kwargs: False
    loop._should_record_runtime_event_after_prune = lambda *_args, **_kwargs: False
    loop._should_run_step_agent = lambda *_args, **_kwargs: True
    loop._get_reflection_max_rounds = lambda _tenant_id: 0
    loop._run_reflection_rounds = lambda *args, **_kwargs: tuple(args[5:9])
    loop._auto_progress_skill_graph = lambda *args, **_kwargs: tuple(args[5:9])
    loop._skill_state_payload = lambda *_args, **_kwargs: {}
    loop._runtime_stream_context = lambda *_args, **_kwargs: {}
    skill = _leave_policy_skill()
    loop._get_active_skill = lambda *_args, **_kwargs: skill
    loop._run_step_agent_with_context_repair = lambda *_args, **_kwargs: StepAgentResult(
        knowledge_query=KnowledgeQuery(query="事假政策")
    )
    calls: list[str] = []

    def execute_knowledge(*_args, **_kwargs):
        calls.append("knowledge")
        return StepAgentResult(
            tool_call=ToolCall(name="hr.balance_query", arguments={"employee_id": "E1"})
        )

    def execute_tool(*args, **_kwargs):
        calls.append("tool")
        return args[5], ToolResult(tool_name="hr.balance_query", success=True, data={})

    loop._execute_knowledge_query_cycle = execute_knowledge
    loop._execute_tool_action_cycle = execute_tool
    loop._finalize_execution_after_reply = lambda *_args, **_kwargs: "completed"
    session = ChatSession(id="session_test", tenant_id="tenant_demo", pending_tasks_json=[])
    iterator = loop._stream_continue_pending_after_completion(
        _request("继续请假"),
        session,
        _model_config(),
        [skill],
        [],
        None,
        [],
        {},
        "",
        user_message_id="msg_user",
        turn_task_frames=[
            PendingTask(
                decision="start_new_task",
                target_skill_id="leave",
                target_step_id="check_policy",
                slot_hints={"leave_type": "事假"},
            )
        ],
    )
    list(iterator)

    assert calls == ["knowledge", "tool"]


def test_only_started_waiting_task_becomes_pending_while_later_turn_frame_still_runs() -> None:
    loop = object.__new__(AgentLoop)
    loop.runtime = SkillRuntime()
    loop.events = FakeEvents()
    loop.db = FakeDb()
    loop._get_agent_loop_max_actions = lambda _tenant_id: 4
    loop._drop_unavailable_skill_state = lambda *_args, **_kwargs: False
    loop._should_record_runtime_event_after_prune = lambda *_args, **_kwargs: False
    loop._should_run_step_agent = lambda *_args, **_kwargs: True
    loop._get_reflection_max_rounds = lambda _tenant_id: 0
    loop._run_reflection_rounds = lambda *args, **_kwargs: tuple(args[5:9])
    loop._auto_progress_skill_graph = lambda *args, **_kwargs: tuple(args[5:9])
    skills = [_price_compare_skill(), _purchase_skill()]
    skills_by_id = {skill.skill_id: skill for skill in skills}
    executed: list[str] = []
    loop._get_active_skill = (
        lambda _tenant_id, skill_id, _agent_id: skills_by_id.get(skill_id or "")
    )

    def run_step(_request, session, active_skill, *_args, **_kwargs):
        executed.append(active_skill.skill_id)
        if active_skill.skill_id == "price_compare":
            session.awaiting_input_json = {"expected_fields": ["product_name_2"]}
            return StepAgentResult(reply="请补充第二个商品")
        return StepAgentResult(reply="购买完成", is_step_completed=True)

    def finalize(_tenant_id, session, active_skill, *_args, **_kwargs):
        if active_skill.skill_id == "price_compare":
            return "continued"
        loop.runtime.complete_current_skill(session)
        return "completed"

    loop._run_step_agent_with_context_repair = run_step
    loop._finalize_execution_after_reply = finalize
    session = ChatSession(id="session_test", tenant_id="tenant_demo", pending_tasks_json=[])
    frames = [
        PendingTask(
            decision="start_new_task",
            target_skill_id="price_compare",
            target_step_id="collect_products",
            slot_hints={"product_name_1": "A1"},
        ),
        PendingTask(
            decision="start_new_task",
            target_skill_id="purchase",
            target_step_id="collect_user_name",
            slot_hints={"product_id": "A3", "quantity": 1},
        ),
    ]

    result = loop._try_continue_pending_after_completion(
        _request("先比较 A1 和另一个商品，再购买 A3"),
        session,
        _model_config(),
        skills,
        [],
        None,
        [],
        {},
        "",
        turn_task_frames=frames,
    )

    assert executed == ["price_compare", "purchase"]
    assert [frame["skill_id"] for frame in session.pending_tasks_json] == ["price_compare"]
    assert session.pending_tasks_json[0]["awaiting_input"] == {
        "expected_fields": ["product_name_2"]
    }
    assert result is not None
    assert result.reply == "请补充第二个商品\n\n购买完成"


def test_streamed_followup_tasks_collect_results_without_emitting_replies() -> None:
    loop = object.__new__(AgentLoop)
    loop.runtime = SkillRuntime()
    loop.events = FakeEvents()
    loop.db = FakeDb()
    loop._get_agent_loop_max_actions = lambda _tenant_id: 4
    loop._drop_unavailable_skill_state = lambda *_args, **_kwargs: False
    loop._should_record_runtime_event_after_prune = lambda *_args, **_kwargs: False
    loop._should_run_step_agent = lambda *_args, **_kwargs: True
    loop._get_reflection_max_rounds = lambda _tenant_id: 0
    loop._run_reflection_rounds = lambda *args, **_kwargs: tuple(args[5:9])
    loop._auto_progress_skill_graph = lambda *args, **_kwargs: tuple(args[5:9])
    loop._skill_state_payload = lambda *_args, **_kwargs: {}
    loop._runtime_stream_context = lambda *_args, **_kwargs: {}

    skills = [_price_compare_skill(), _purchase_skill()]
    skills_by_id = {skill.skill_id: skill for skill in skills}
    loop._get_active_skill = (
        lambda _tenant_id, skill_id, _agent_id: skills_by_id.get(skill_id or "")
    )

    def run_step(_request, _session, active_skill, *_args, **_kwargs):
        return StepAgentResult(
            action="ask_user",
            reply=f"{active_skill.name}需要补充信息",
        )

    loop._run_step_agent_with_context_repair = run_step
    loop._finalize_execution_after_reply = lambda *_args, **_kwargs: "continued"
    session = ChatSession(id="session_test", tenant_id="tenant_demo", pending_tasks_json=[])
    frames = [
        PendingTask(
            decision="start_new_task",
            target_skill_id="price_compare",
            target_step_id="collect_products",
        ),
        PendingTask(
            decision="start_new_task",
            target_skill_id="purchase",
            target_step_id="collect_user_name",
        ),
    ]

    iterator = loop._stream_continue_pending_after_completion(
        _request("先比价，再购买"),
        session,
        _model_config(),
        skills,
        [],
        None,
        [],
        {},
        "",
        user_message_id="msg_user",
        turn_task_frames=frames,
    )
    events: list[dict[str, object]] = []
    while True:
        try:
            events.append(next(iterator))
        except StopIteration as stop:
            result = stop.value
            break

    assert result is not None
    assert len(result.task_results) == 2
    assert [event["event"] for event in events].count("step_result") == 2
    assert not {"stream_delta", "stream_replace"}.intersection(
        event["event"] for event in events
    )


def test_drop_unavailable_skill_state_removes_disabled_sop_frames() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="archived_sop",
        active_step_id="collect_info",
        slots_json={"field": "value"},
        awaiting_input_json={"skill_id": "archived_sop", "step_id": "collect_info"},
        pending_tasks_json=[
            {"task_id": "task_archived", "target_skill_id": "archived_sop"},
            {"task_id": "task_purchase", "target_skill_id": "purchase"},
        ],
        skill_stack_json=[
            {"task_id": "stack_archived", "skill_id": "archived_sop"},
            {"task_id": "stack_purchase", "skill_id": "purchase"},
        ],
    )

    changed = loop._drop_unavailable_skill_state("tenant_demo", session, [_purchase_skill()])

    assert changed is True
    assert session.active_skill_id is None
    assert session.active_step_id is None
    assert session.slots_json == {}
    assert session.awaiting_input_json is None
    assert session.pending_tasks_json == [
        {"task_id": "task_purchase", "target_skill_id": "purchase"}
    ]
    assert session.skill_stack_json == []
    assert loop.events.records[-1][2] == "skill_state_pruned"
    assert loop.events.records[-1][3]["removed_skill_ids"] == ["archived_sop"]


def test_drop_unavailable_skill_state_repairs_removed_active_step() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    skill = _purchase_skill()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id=skill.skill_id,
        active_step_id="removed_confirmation_step",
        slots_json={"product_id": "A1"},
        awaiting_input_json={
            "task_id": "task_purchase",
            "skill_id": skill.skill_id,
            "step_id": "removed_confirmation_step",
            "expected_fields": ["confirmation"],
        },
        last_agent_question="请确认",
    )

    changed = loop._drop_unavailable_skill_state("tenant_demo", session, [skill])

    assert changed is True
    assert session.active_skill_id == skill.skill_id
    assert session.active_step_id == "collect_user_name"
    assert session.slots_json == {"product_id": "A1"}
    assert session.awaiting_input_json == {"task_id": "task_purchase"}
    assert session.last_agent_question is None
    assert loop.events.records[-1][3]["repaired_steps"] == [
        {
            "skill_id": skill.skill_id,
            "from_step_id": "removed_confirmation_step",
            "to_step_id": "collect_user_name",
        }
    ]


def test_skill_state_payload_filters_disabled_sop_frames() -> None:
    loop = object.__new__(AgentLoop)
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="archived_sop",
        active_step_id="collect_info",
        pending_tasks_json=[
            {
                "task_id": "task_archived",
                "target_skill_id": "archived_sop",
                "target_step_id": "collect_info",
            },
            {
                "task_id": "task_purchase",
                "target_skill_id": "purchase",
                "target_step_id": "collect_user_name",
            },
        ],
        skill_stack_json=[
            {"task_id": "stack_archived", "skill_id": "archived_sop", "step_id": "collect_info"},
            {"task_id": "stack_purchase", "skill_id": "purchase", "step_id": "confirm_product"},
        ],
    )

    payload = loop._skill_state_payload(
        session,
        [_purchase_skill()],
        user_message_id="msg_current_turn",
    )

    assert payload["activeSkillId"] is None
    assert payload["activeStepId"] is None
    assert payload["user_message_id"] == "msg_current_turn"
    assert payload["turn_id"] == "msg_current_turn"
    assert payload["currentSkills"] == [
        {
            "skillId": "purchase",
            "name": "购买商品",
            "stepId": "collect_user_name",
            "state": "pending",
        },
    ]


def test_pruned_disabled_sop_runtime_event_is_not_recorded() -> None:
    loop = object.__new__(AgentLoop)
    session = ChatSession(id="session_test", tenant_id="tenant_demo")
    decision = RouterDecision(
        decision="switch_to_pending",
        target_skill_id="archived_sop",
        target_step_id="collect_info",
    )

    assert (
        loop._should_record_runtime_event_after_prune(
            decision,
            session,
            [_purchase_skill()],
            state_pruned=True,
        )
        is False
    )


def test_finalize_turn_clears_stale_last_question_for_non_question_reply() -> None:
    loop = object.__new__(AgentLoop)
    loop.db = FakeDb()
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        last_agent_question="旧的比价回复。请问您是否决定购买 A1？",
    )
    reply = "好的，已为您确认退款申请。正在为您处理订单 MOCKD57272DB0E 的退款，请您耐心等待。"

    loop._finalize_turn(session, "tenant_demo", reply)

    assert session.last_agent_question == "旧的比价回复。请问您是否决定购买 A1？"
    assert session.summary == f"最近回复：{reply[:120]}"
    assert loop.events.records[0][2] == "assistant_message_created"


def test_finalize_turn_keeps_current_question_reply() -> None:
    loop = object.__new__(AgentLoop)
    loop.db = FakeDb()
    loop.events = FakeEvents()
    session = ChatSession(id="session_test", tenant_id="tenant_demo")
    reply = "请提供您的订单号？"

    loop._finalize_turn(session, "tenant_demo", reply)

    assert session.last_agent_question is None
    assert session.summary == f"最近回复：{reply[:120]}"


def test_finalize_turn_drops_unused_knowledge_citations() -> None:
    loop = object.__new__(AgentLoop)
    loop.db = FakeDb()
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        knowledge_context_json=[
            {
                "source_message": "自动任务需要结合业务资料",
                "evidence_pack": [
                    {
                        "source_path": "service-handbook.md / 服务原则 / evidence 1",
                        "excerpt": "服务人员应先确认用户真实诉求。",
                    }
                ],
            }
        ],
    )
    reply = "本次自动任务执行完毕，已成功购买 1 个 A1 商品。"

    loop._finalize_turn(session, "tenant_demo", reply, source_message="自动任务需要结合业务资料")

    message = loop.db.added[-1]
    assert isinstance(message, Message)
    assert message.content == reply
    assert message.metadata_json == {}
    assert "knowledge_citations" not in loop.events.records[0][3]


def test_finalize_turn_keeps_only_inline_knowledge_citations() -> None:
    loop = object.__new__(AgentLoop)
    loop.db = FakeDb()
    loop.events = FakeEvents()
    session = ChatSession(id="session_test", tenant_id="tenant_demo")
    step_result = StepAgentResult(
        knowledge_results=[
            {
                "query": {"query": "前端规范有哪些？"},
                "evidence_pack": [
                    {
                        "source_path": "frontend.md / 目录规范 / evidence 1",
                        "excerpt": "前端目录规范说明。",
                    },
                    {
                        "source_path": "frontend.md / 命名规范 / evidence 1",
                        "excerpt": "前端命名规范说明。",
                    },
                ],
            }
        ],
    )
    reply = "前端规范包括目录组织和命名规范。[2]\n\n参考资料：[1][2]"

    loop._finalize_turn(
        session,
        "tenant_demo",
        reply,
        step_result=step_result,
        source_message="前端规范有哪些？",
    )

    message = loop.db.added[-1]
    assert isinstance(message, Message)
    assert message.content == "前端规范包括目录组织和命名规范。[1]"
    assert [item["label"] for item in message.metadata_json["knowledge_citations"]] == ["[1]"]


def test_merge_queued_reply_preserves_each_structured_execution_segment() -> None:
    loop = object.__new__(AgentLoop)
    refund_then_purchase = (
        "好的，已为您提交订单 MOCK7A17191FC9（商品 A1）的退款申请，退款原因为“不想要了”。\n\n"
        "接下来为您购买 A3 高阶商品，请确认以下信息：\n"
        "- 用户：hm\n"
        "- 商品：A3\n"
        "- 数量：1\n\n"
        "请问确认下单吗？"
    )
    purchase_confirmation = (
        "好的，hm。已为您确认购买 A3 高阶商品 1 件，价格 239.0 元。请问确认下单吗？"
    )

    replies, replaced = loop._merge_queued_reply_segment([], refund_then_purchase)
    replies, replaced = loop._merge_queued_reply_segment(replies, purchase_confirmation)

    assert replaced is False
    assert replies == [refund_then_purchase, purchase_confirmation]


def test_merge_queued_reply_keeps_distinct_followup_confirmations() -> None:
    loop = object.__new__(AgentLoop)
    first = "退款已处理。接下来为您购买 A1，请问确认下单吗？"
    second = "好的，hm。已为您确认购买 A3 高阶商品 1 件，价格 239.0 元。请问确认下单吗？"

    replies, replaced = loop._merge_queued_reply_segment([], first)
    replies, replaced = loop._merge_queued_reply_segment(replies, second)

    assert replaced is False
    assert replies == [first, second]


def test_apply_step_result_records_skill_context_for_step_change() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="skill_purchase_001",
        active_step_id="collect_user_name",
    )

    loop._apply_step_result(
        "tenant_demo",
        session,
        StepAgentResult(next_step_id="confirm_purchase"),
    )

    assert session.active_step_id == "confirm_purchase"
    event_type, payload = loop.events.records[0][2], loop.events.records[0][3]
    assert event_type == "skill_step_changed"
    assert payload["from_skill_id"] == "skill_purchase_001"
    assert payload["to_skill_id"] == "skill_purchase_001"
    assert payload["from_step_id"] == "collect_user_name"
    assert payload["to_step_id"] == "confirm_purchase"


def test_apply_step_result_persists_and_consumes_awaiting_confirmation() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    skill = Skill(
        tenant_id="tenant_demo",
        skill_id="meeting_room_book",
        name="会议室预订",
        content_json=_graph_content(
            "meeting_room_book",
            "会议室预订",
            [
                {
                    "node_id": "confirm_booking",
                    "name": "确认预订",
                    "expected_user_info": ["confirmation"],
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "book_room",
                    "type": "tool_call",
                    "name": "提交预订",
                    "allowed_actions": ["call_tool:admin.room_book"],
                },
            ],
        ),
        status="published",
    )
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id=skill.skill_id,
        active_step_id="confirm_booking",
        slots_json={"employee_id": "123456"},
        awaiting_input_json={"task_id": "task_booking"},
    )

    loop._apply_step_result(
        "tenant_demo",
        session,
        StepAgentResult(action="ask_user", reply="请确认是否提交预订？"),
        skill,
    )

    assert session.awaiting_input_json == {
        "task_id": "task_booking",
        "skill_id": "meeting_room_book",
        "step_id": "confirm_booking",
        "expected_fields": ["confirmation"],
        "question_summary": "请确认是否提交预订？",
    }
    assert session.last_agent_question == "请确认是否提交预订？"

    loop._apply_step_result(
        "tenant_demo",
        session,
        StepAgentResult(
            action="advance",
            slot_updates={"confirmation": True},
            next_step_id="book_room",
            is_step_completed=True,
        ),
        skill,
    )

    assert session.active_step_id == "book_room"
    assert session.awaiting_input_json == {"task_id": "task_booking"}
    assert session.last_agent_question is None


def test_apply_step_result_preserves_awaiting_input_after_invalid_next_step() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    skill = _purchase_skill()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id=skill.skill_id,
        active_step_id="collect_user_name",
        awaiting_input_json={
            "skill_id": skill.skill_id,
            "step_id": "collect_user_name",
            "expected_fields": ["user_name"],
            "question_summary": "请提供姓名",
        },
        last_agent_question="请提供姓名",
    )

    result = StepAgentResult(
        action="advance",
        next_step_id="missing_step",
        is_step_completed=True,
    )
    loop._apply_step_result("tenant_demo", session, result, skill)

    assert session.active_step_id == "collect_user_name"
    assert session.awaiting_input_json == {
        "skill_id": skill.skill_id,
        "step_id": "collect_user_name",
        "expected_fields": ["user_name"],
        "question_summary": "请提供姓名",
    }
    assert result.next_step_id is None
    assert result.is_step_completed is False


def test_apply_step_result_does_not_treat_plain_reply_as_awaiting_input() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    skill = _purchase_skill()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id=skill.skill_id,
        active_step_id="collect_user_name",
    )

    loop._apply_step_result(
        "tenant_demo",
        session,
        StepAgentResult(action="reply", reply="商品信息已为您保留。"),
        skill,
    )

    assert session.awaiting_input_json is None
    assert session.last_agent_question is None


def test_record_runtime_event_skips_noop_step_change() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="skill_purchase_001",
        active_step_id="collect_user_name",
    )

    loop._record_runtime_event(
        "tenant_demo",
        session,
        "skill_purchase_001",
        "collect_user_name",
        RouterDecision(
            decision="continue_active",
            target_skill_id="skill_purchase_001",
            target_step_id="collect_user_name",
        ),
    )

    assert loop.events.records == []


def test_apply_step_result_ignores_next_step_outside_active_skill() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="refund",
        active_step_id="check_refund",
    )
    step_result = StepAgentResult(next_step_id="collect_user_name", is_step_completed=True)

    loop._apply_step_result("tenant_demo", session, step_result, _refund_skill())

    assert session.active_step_id == "check_refund"
    assert step_result.next_step_id is None
    event_type, payload = loop.events.records[0][2], loop.events.records[0][3]
    assert event_type == "step_agent_result_repaired"
    assert payload["mode"] == "invalid_next_step_ignored"
    assert payload["invalid_next_step_id"] == "collect_user_name"


def test_apply_step_result_does_not_create_step_without_active_skill() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    session = ChatSession(id="session_test", tenant_id="tenant_demo")

    loop._apply_step_result(
        "tenant_demo",
        session,
        StepAgentResult(next_step_id="confirm_purchase"),
    )

    assert session.active_skill_id is None
    assert session.active_step_id is None
    assert loop.events.records == []


def test_apply_step_result_queues_parallel_sibling_steps_and_merges() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    skill = _parallel_audit_skill(
        [
            {"source_node_id": "start", "next_node_id": "check_payee", "condition": "报文已获取"},
            {
                "source_node_id": "start",
                "next_node_id": "check_sensitive",
                "condition": "报文已获取",
            },
            {
                "source_node_id": "check_payee",
                "next_node_id": "report",
                "condition": "一致性检查完成",
            },
            {
                "source_node_id": "check_sensitive",
                "next_node_id": "report",
                "condition": "敏感词检查完成",
            },
        ]
    )
    session = ChatSession(
        id="session_parallel",
        tenant_id="tenant_demo",
        active_skill_id=skill.skill_id,
        active_step_id="start",
        slots_json={},
    )

    loop._apply_step_result(
        "tenant_demo",
        session,
        StepAgentResult(next_step_id="check_payee", is_step_completed=True),
        skill,
    )

    assert session.active_step_id == "check_payee"
    assert session.slots_json == {GRAPH_PENDING_STEPS_SLOT: ["check_sensitive"]}

    first_branch_result = StepAgentResult(next_step_id="report", is_step_completed=True)
    loop._apply_step_result("tenant_demo", session, first_branch_result, skill)

    assert session.active_step_id == "check_sensitive"
    assert first_branch_result.next_step_id == "check_sensitive"
    assert session.slots_json == {GRAPH_PENDING_STEPS_SLOT: ["report"]}

    loop._apply_step_result(
        "tenant_demo",
        session,
        StepAgentResult(next_step_id="report", is_step_completed=True),
        skill,
    )

    assert session.active_step_id == "report"
    assert session.slots_json == {}
    assert [record[2] for record in loop.events.records].count("skill_step_changed") == 3


def test_apply_step_result_does_not_queue_exclusive_sibling_conditions() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    skill = _parallel_audit_skill(
        [
            {"source_node_id": "start", "next_node_id": "approve", "condition": "审核通过"},
            {"source_node_id": "start", "next_node_id": "reject", "condition": "审核拒绝"},
        ]
    )
    session = ChatSession(
        id="session_exclusive",
        tenant_id="tenant_demo",
        active_skill_id=skill.skill_id,
        active_step_id="start",
        slots_json={},
    )

    loop._apply_step_result(
        "tenant_demo",
        session,
        StepAgentResult(next_step_id="approve", is_step_completed=True),
        skill,
    )

    assert session.active_step_id == "approve"
    assert session.slots_json == {}


def test_terminal_skill_completion_when_required_slots_are_complete() -> None:
    loop = object.__new__(AgentLoop)
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="repair_ticket",
        active_step_id="reply_ticket_result",
        slots_json={"reporter_name": "hm", "asset_id": "EQ-9", "issue_desc": "无法开机"},
    )

    assert loop._should_complete_skill(
        _repair_skill(),
        session,
        StepAgentResult(is_step_completed=True),
        None,
    )


def test_terminal_collect_step_can_complete_with_ask_user_action_when_slots_are_complete() -> None:
    loop = object.__new__(AgentLoop)
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="refund",
        active_step_id="collect_refund_reason",
        slots_json={"order_id": "A12345", "refund_reason": "不喜欢"},
    )

    assert loop._should_complete_skill(
        _refund_collect_terminal_skill(),
        session,
        StepAgentResult(is_step_completed=True, next_step_id="collect_refund_reason"),
        None,
    )


def test_stale_terminal_skill_is_cleared_before_next_route() -> None:
    loop = object.__new__(AgentLoop)
    loop.runtime = SkillRuntime()
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="repair_ticket",
        active_step_id="reply_ticket_result",
        slots_json={"reporter_name": "hm", "asset_id": "EQ-9", "issue_desc": "无法开机"},
    )

    loop._finish_stale_completed_skill("tenant_demo", session, [_repair_skill()])

    assert session.active_skill_id is None
    assert session.active_step_id is None
    assert session.slots_json == {}
    assert loop.events.records[0][2] == "skill_completed"
    assert loop.events.records[0][3]["reason"] == "stale_terminal_state"


def test_scheduled_task_followup_can_continue_after_stale_terminal_completion() -> None:
    loop = object.__new__(AgentLoop)
    loop.runtime = SkillRuntime()
    loop.events = FakeEvents()
    loop.db = FakeDb()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="repair_ticket",
        active_step_id="reply_ticket_result",
        slots_json={"reporter_name": "hm", "asset_id": "EQ-9", "issue_desc": "无法开机"},
        pending_tasks_json=[
            {
                "task_id": "task_purchase_after_compare",
                "status": "pending",
                "skill_id": "purchase",
                "target_skill_id": "purchase",
                "step_id": "collect_user_name",
                "target_step_id": "collect_user_name",
                "slots": {"user_name": "hm"},
                "slot_hints": {"user_name": "hm"},
                "intent_summary": "购买比价后更贵的商品",
            }
        ],
    )
    request = _request("自动任务唤醒：完成维修后继续处理购买任务")
    request.interaction_mode = "scheduled_task"

    should_continue = loop._should_attempt_queued_task_followup(
        request,
        session,
        [_repair_skill(), _purchase_skill()],
        "维修结果已反馈。",
        1,
    )

    assert should_continue is True
    assert session.active_skill_id is None
    assert session.active_step_id is None
    assert session.pending_tasks_json[0]["task_id"] == "task_purchase_after_compare"
    assert [record[2] for record in loop.events.records] == [
        "skill_completed",
        "scheduled_task_followup_requested",
    ]


def test_normal_chat_does_not_auto_continue_pending_after_stale_terminal_completion() -> None:
    loop = object.__new__(AgentLoop)
    loop.runtime = SkillRuntime()
    loop.events = FakeEvents()
    loop.db = FakeDb()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="repair_ticket",
        active_step_id="reply_ticket_result",
        slots_json={"reporter_name": "hm", "asset_id": "EQ-9", "issue_desc": "无法开机"},
        pending_tasks_json=[
            {
                "task_id": "task_purchase_after_compare",
                "status": "pending",
                "skill_id": "purchase",
                "target_skill_id": "purchase",
                "step_id": "collect_user_name",
                "target_step_id": "collect_user_name",
                "slots": {"user_name": "hm"},
                "slot_hints": {"user_name": "hm"},
            }
        ],
    )

    should_continue = loop._should_attempt_queued_task_followup(
        _request("普通聊天继续处理"),
        session,
        [_repair_skill(), _purchase_skill()],
        "维修结果已反馈。",
        1,
    )

    assert should_continue is False
    assert session.active_skill_id == "repair_ticket"
    assert loop.events.records == []


def test_obsolete_suspended_stack_is_cleared() -> None:
    loop = object.__new__(AgentLoop)
    loop.runtime = SkillRuntime()
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="visitor_badge",
        active_step_id="collect_visit_info",
        skill_stack_json=[
            {
                "skill_id": "repair_ticket",
                "step_id": "reply_ticket_result",
                "slots": {"reporter_name": "hm", "asset_id": "EQ-9", "issue_desc": "无法开机"},
            }
        ],
    )

    loop._finish_stale_completed_skill("tenant_demo", session, [_repair_skill()])

    assert session.active_skill_id == "visitor_badge"
    assert session.skill_stack_json == []
    assert loop.events.records == []


def test_intermediate_step_with_next_step_is_not_completed() -> None:
    loop = object.__new__(AgentLoop)
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="repair_ticket",
        active_step_id="collect_repair_info",
        slots_json={"reporter_name": "hm"},
    )

    assert not loop._should_complete_skill(
        _repair_skill(),
        session,
        StepAgentResult(is_step_completed=True, next_step_id="reply_ticket_result"),
        None,
    )


def test_model_can_complete_non_terminal_skill_when_no_next_action() -> None:
    loop = object.__new__(AgentLoop)
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="repair_ticket",
        active_step_id="collect_repair_info",
        slots_json={"reporter_name": "hm"},
    )

    assert loop._should_complete_skill(
        _repair_skill(),
        session,
        StepAgentResult(reply="好的，已取消本次报修流程。", is_step_completed=True),
        None,
    )


def test_successful_tool_call_advances_to_final_reply_step() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="refund",
        active_step_id="check_refund",
    )
    step_result = StepAgentResult(tool_call=_refund_tool_call(), is_step_completed=True)

    advanced = loop._advance_after_successful_tool(
        "tenant_demo",
        session,
        _refund_skill(),
        step_result,
        ToolResult(tool_name="order.query", success=True, data={"eligible": True}),
    )

    assert advanced
    assert session.active_step_id == "reply_result"
    assert step_result.next_step_id == "reply_result"
    assert loop.events.records[0][2] == "skill_step_changed"


def test_answer_step_can_complete_even_if_distilled_order_has_later_satisfied_collect_step() -> (
    None
):
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="refund",
        active_step_id="check_refund",
        slots_json={"order_id": "A12345", "refund_reason": "商品质量"},
    )
    step_result = StepAgentResult(tool_call=_refund_tool_call(), is_step_completed=True)

    advanced = loop._advance_after_successful_tool(
        "tenant_demo",
        session,
        _refund_skill_with_late_collect_step(),
        step_result,
        ToolResult(tool_name="order.query", success=True, data={"eligible": True}),
    )

    assert not advanced
    assert session.active_step_id == "check_refund"
    assert loop._should_complete_skill(
        _refund_skill_with_late_collect_step(),
        session,
        step_result,
        ToolResult(tool_name="order.query", success=True, data={"eligible": True}),
    )


def test_context_repair_does_not_auto_advance_satisfied_collect_step() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    loop.step_agent = _FakeStepAgent(
        [
            StepAgentResult(
                reply="您好 hm，请问您想购买的商品 ID 是什么？",
                slot_updates={"user_name": "hm"},
                next_step_id="collect_user_name",
            ),
        ]
    )
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="purchase",
        active_step_id="collect_user_name",
        slots_json={"product_id": "A3", "quantity": 1},
    )

    step_result = loop._run_step_agent_with_context_repair(
        _request("我叫hm"),
        session,
        _purchase_skill(),
        [_purchase_tool(), _order_add_tool()],
        _model_config(),
        RouterDecision(decision="continue_active", target_skill_id="purchase"),
    )

    assert session.active_step_id == "collect_user_name"
    assert loop.step_agent.calls == 1
    assert step_result.tool_call is None
    assert not any(
        event_type == "skill_step_changed" and payload.get("reason") == "expected_info_satisfied"
        for _, _, event_type, payload in loop.events.records
    )


def test_required_knowledge_step_forces_query_before_advance() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="leave",
        active_step_id="check_policy",
        slots_json={"leave_type": "事假"},
    )
    result = StepAgentResult(
        action="advance",
        reply="根据公司政策可以申请。",
        tool_call=ToolCall(name="hr.balance_query", arguments={"employee_id": "E1"}),
        knowledge_results=[{"evidence_pack": [{"content": "伪造证据"}]}],
        next_step_id="query_balance",
        is_step_completed=True,
        handoff=True,
    )

    normalized = loop._normalize_required_knowledge_step(
        _request("我要请事假"), session, _leave_policy_skill(), result
    )

    assert normalized.action == "query_knowledge"
    assert normalized.knowledge_query is not None
    assert "leave_type: 事假" in normalized.knowledge_query.query
    assert "我要请事假" not in normalized.knowledge_query.query
    assert normalized.reply is None
    assert normalized.tool_call is None
    assert normalized.next_step_id is None
    assert normalized.knowledge_results == []
    assert normalized.is_step_completed is False
    assert normalized.handoff is False
    assert any(record[2] == "knowledge_query_forced" for record in loop.events.records)


def test_required_knowledge_step_preserves_missing_field_question() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="leave",
        active_step_id="check_policy",
        slots_json={},
    )
    result = StepAgentResult(
        action="advance",
        reply="请问请假类型是什么？",
        knowledge_query=KnowledgeQuery(query="通用请假政策"),
        tool_call=ToolCall(name="hr.balance_query", arguments={}),
        next_step_id="query_balance",
        is_step_completed=True,
        handoff=True,
    )

    normalized = loop._normalize_required_knowledge_step(
        _request("我要请假"), session, _leave_policy_skill(), result
    )

    assert normalized is result
    assert normalized.knowledge_query is None
    assert normalized.reply == "请问请假类型是什么？"
    assert normalized.action == "ask_user"
    assert normalized.tool_call is None
    assert normalized.next_step_id is None
    assert normalized.is_step_completed is False
    assert normalized.handoff is False


def test_required_knowledge_step_keeps_model_query_but_removes_conflicts() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="leave",
        active_step_id="check_policy",
        slots_json={"leave_type": "年假"},
    )
    query = KnowledgeQuery(query="年假政策", query_type="policy_check")
    result = StepAgentResult(
        action="reply",
        reply="先直接回答",
        knowledge_query=query,
        next_step_id="query_balance",
    )

    normalized = loop._normalize_required_knowledge_step(
        _request("年假"), session, _leave_policy_skill(), result
    )

    assert normalized.knowledge_query is query
    assert normalized.action == "query_knowledge"
    assert normalized.reply is None
    assert normalized.next_step_id is None
    assert not any(record[2] == "knowledge_query_forced" for record in loop.events.records)


def test_repeated_knowledge_step_reuses_turn_cache_without_new_search() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    loop._run_step_agent_once = lambda *args, **kwargs: StepAgentResult(  # type: ignore[method-assign]
        action="reply", reply="已根据检索结果处理"
    )
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="leave",
        active_step_id="check_policy",
    )
    key = ("leave", "check_policy")
    cached = {"query": {"query": "事假政策"}, "evidence_pack": [{"content": "事假无薪"}]}
    _KNOWLEDGE_STEPS_SEEN.set({key})
    _KNOWLEDGE_RESULTS_CACHE.set({key: cached})

    result = loop._execute_knowledge_query_cycle(
        _request("继续"),
        session,
        _leave_policy_skill(),
        [],
        _model_config(),
        StepAgentResult(knowledge_query=KnowledgeQuery(query="事假政策")),
    )

    assert result.reply == "已根据检索结果处理"
    assert result.knowledge_results == [cached]
    assert not any(record[2] == "knowledge_query_started" for record in loop.events.records)
    assert not any(
        event_type == "step_agent_result_repaired" and payload.get("mode") == "schema_tool_call"
        for _, _, event_type, payload in loop.events.records
    )


def test_context_repair_does_not_infer_tool_when_router_is_clarifying() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    loop.step_agent = _FakeStepAgent(
        [StepAgentResult(reply="请问您想办理哪类业务？", next_step_id="confirm_product")]
    )
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="purchase",
        active_step_id="confirm_product",
        slots_json={"product_id": "A1", "quantity": 1, "user_name": "hm"},
    )

    step_result = loop._run_step_agent_with_context_repair(
        _request("我想查询订单"),
        session,
        _purchase_skill(),
        [_purchase_tool()],
        _model_config(),
        RouterDecision(decision="clarify", target_skill_id="skill_order_query"),
    )

    assert step_result.tool_call is None
    assert not any(
        event_type == "step_agent_result_repaired" and payload.get("mode") == "schema_tool_call"
        for _, _, event_type, payload in loop.events.records
    )


def test_model_slot_validation_retry_can_complete_missed_quantity() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    loop.step_agent = _FakeStepAgent(
        [
            StepAgentResult(
                reply="好的，hm。请问您想购买多少件 A1？",
                slot_updates={"user_name": "hm", "product_id": "A1"},
                next_step_id="collect_user_name",
            ),
            StepAgentResult(
                reply="正在为您创建订单，请稍候。",
                slot_updates={"quantity": 1},
                tool_call=ToolCall(
                    name="product.purchase", arguments={"product_id": "A1", "quantity": 1}
                ),
            ),
        ]
    )
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="purchase",
        active_step_id="collect_user_name",
        slots_json={},
    )

    step_result = loop._run_step_agent_with_context_repair(
        _request("我要买一个A1，我叫hm"),
        session,
        _purchase_skill(),
        [_purchase_tool()],
        _model_config(),
        RouterDecision(decision="start_new_task", target_skill_id="purchase"),
    )

    assert loop.step_agent.calls == 2
    assert session.slots_json["user_name"] == "hm"
    assert session.slots_json["product_id"] == "A1"
    assert session.slots_json["quantity"] == 1
    assert step_result.tool_call is not None
    assert step_result.tool_call.name == "product.purchase"
    assert any(
        event_type == "step_agent_result_repaired" and payload.get("mode") == "slot_validation"
        for _, _, event_type, payload in loop.events.records
    )
    assert not any(
        event_type == "skill_step_changed" and payload.get("reason") == "expected_info_satisfied"
        for _, _, event_type, payload in loop.events.records
    )


def test_step_agent_receives_full_conversation_context_within_budget() -> None:
    rows = [
        Message(
            tenant_id="tenant_demo",
            session_id="session_test",
            role="user" if index % 2 == 0 else "assistant",
            content=f"message {index}",
        )
        for index in range(16)
    ]
    loop = object.__new__(AgentLoop)
    loop.db = FakeMessageDb(rows)
    loop.events = FakeEvents()
    loop.step_agent = _FakeStepAgent([StepAgentResult(reply="ok")])
    session = ChatSession(id="session_test", tenant_id="tenant_demo")

    loop._run_step_agent_once(
        _request("message 15"),
        session,
        None,
        [],
        _model_config(),
        RouterDecision(decision="clarify"),
    )

    _args, kwargs = loop.step_agent.call_args[0]
    recent_messages = kwargs["recent_messages"]
    conversation_context = kwargs["conversation_context"]
    assert len(recent_messages) == 16
    assert recent_messages[0]["content"] == "message 0"
    assert recent_messages[-1]["content"] == "message 15"
    assert conversation_context["metadata"]["compacted"] is False
    assert conversation_context["metadata"]["total_messages"] == 16
    assert kwargs["current_knowledge"] is None


def test_all_agent_stages_share_the_same_full_conversation_context() -> None:
    rows = [
        Message(
            tenant_id="tenant_demo",
            session_id="session_test",
            role="user" if index % 2 == 0 else "assistant",
            content=f"message {index}",
        )
        for index in range(16)
    ]
    loop = object.__new__(AgentLoop)
    loop.db = FakeMessageDb(rows)
    session = ChatSession(id="session_test", tenant_id="tenant_demo")

    context = loop._conversation_context(session)

    assert len(context["messages"]) == 16
    assert context["messages"][0]["content"] == "message 0"
    assert context["messages"][-1]["content"] == "message 15"
    assert context["metadata"]["total_messages"] == 16


def test_model_slot_validation_retry_does_not_fill_without_model_progress() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    loop.step_agent = _FakeStepAgent(
        [
            StepAgentResult(reply="请问您想购买多少件 A1？", next_step_id="collect_user_name"),
            StepAgentResult(reply="请问您想购买多少件 A1？", next_step_id="collect_user_name"),
        ]
    )
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="purchase",
        active_step_id="collect_user_name",
        slots_json={"product_id": "A1", "user_name": "hm"},
    )

    step_result = loop._run_step_agent_with_context_repair(
        _request("随便看看"),
        session,
        _purchase_skill(),
        [_purchase_tool()],
        _model_config(),
        RouterDecision(decision="continue_active", target_skill_id="purchase"),
    )

    assert loop.step_agent.calls == 2
    assert "quantity" not in session.slots_json
    assert step_result.tool_call is None
    assert not any(
        event_type == "step_agent_result_repaired" and payload.get("mode") == "slot_validation"
        for _, _, event_type, payload in loop.events.records
    )


def test_start_new_task_slot_validation_accepts_reply_repair() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    loop.step_agent = _FakeStepAgent(
        [
            StepAgentResult(
                reply="好的，hm！请问您想购买什么商品？另外，请提供您的姓名以便我们为您下单。",
                slot_updates={"user_name": "hm"},
                next_step_id="collect_user_name",
            ),
            StepAgentResult(
                reply="好的，hm！请问您想购买什么商品？需要购买多少件？",
                next_step_id="collect_user_name",
            ),
        ]
    )
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="purchase",
        active_step_id="collect_user_name",
        slots_json={},
    )

    step_result = loop._run_step_agent_with_context_repair(
        _request("我想买东西"),
        session,
        _purchase_skill(),
        [_purchase_tool()],
        _model_config(),
        RouterDecision(decision="start_new_task", target_skill_id="purchase"),
        memory_context=[
            {
                "kind": "profile",
                "content": "hm",
                "metadata": {"key": "preferred_name"},
            }
        ],
        conversation_context={"messages": [{"role": "user", "content": "我想买东西"}]},
    )

    assert loop.step_agent.calls == 2
    assert session.slots_json["user_name"] == "hm"
    assert step_result.reply == "好的，hm！请问您想购买什么商品？需要购买多少件？"
    assert any(
        event_type == "step_agent_result_repaired" and payload.get("mode") == "slot_validation"
        for _, _, event_type, payload in loop.events.records
    )


def test_tool_step_self_loop_advances_to_reply_and_completes_after_success() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="purchase",
        active_step_id="confirm_product",
        slots_json={"product_id": "A1", "quantity": 1, "user_name": "hm"},
    )
    step_result = StepAgentResult(
        tool_call=ToolCall(name="product.purchase", arguments={"product_id": "A1", "quantity": 1}),
        next_step_id="confirm_product",
        is_step_completed=True,
    )

    advanced = loop._advance_after_successful_tool(
        "tenant_demo",
        session,
        _purchase_skill_with_incomplete_required_info(),
        step_result,
        ToolResult(tool_name="product.purchase", success=True, data={"order_id": "MOCK-1"}),
    )

    assert advanced
    assert session.active_step_id == "reply_result"
    assert step_result.next_step_id == "reply_result"
    assert loop._should_complete_skill(
        _purchase_skill_with_incomplete_required_info(),
        session,
        step_result,
        ToolResult(tool_name="product.purchase", success=True, data={"order_id": "MOCK-1"}),
    )


def test_tool_continuation_is_model_driven_and_accumulates_results() -> None:
    loop = object.__new__(AgentLoop)
    loop.db = FakeDb()
    loop.events = FakeEvents()
    loop.tool_executor = _RecordingPriceToolExecutor()
    loop.step_agent = _FakeStepAgent(
        [
            StepAgentResult(
                tool_call=ToolCall(name="product.price_query", arguments={"product_name": "A3"}),
                is_step_completed=True,
            ),
            StepAgentResult(
                reply="A1 和 A3 均已查到，可以给出比价结果。",
                next_step_id="reply_result",
                is_step_completed=True,
            ),
        ]
    )
    loop._recent_messages = lambda session: []  # type: ignore[method-assign]
    loop._tool_activity_payload = lambda tenant_id, name, result, *args: {  # type: ignore[method-assign]
        "toolName": name,
        "toolCallId": args[1] if len(args) > 1 else "",
        "content": result.model_dump(mode="json"),
        "success": result.success,
        "isError": not result.success,
    }
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="price_compare",
        active_step_id="query_price",
        slots_json={"product_name_1": "A1", "product_name_2": "A3"},
    )

    stream_events: list[tuple[str, dict[str, object]]] = []
    step_result, tool_result = loop._execute_tool_action_cycle(
        _request("我想比下 A1 和 A3 的价格"),
        session,
        _price_compare_skill(),
        [_price_query_tool()],
        _model_config(),
        StepAgentResult(
            tool_call=ToolCall(name="product.price_query", arguments={"product_name": "A1"}),
            is_step_completed=True,
        ),
        stream_events,
    )

    assert [call.arguments["product_name"] for call in loop.tool_executor.calls] == ["A1", "A3"]
    assert loop.step_agent.calls == 2
    assert tool_result is not None
    assert tool_result.data["product_name"] == "A3"
    assert step_result.tool_call is None
    assert session.active_step_id == "reply_result"
    assert len(session.slots_json["_tool_results"]) == 2
    tool_result_events = [payload for event, payload in stream_events if event == "tool_result"]
    assert len(tool_result_events) == 2
    assert tool_result_events[0]["toolCallId"] != tool_result_events[1]["toolCallId"]
    assert any(event == "agent_loop_continued" for event, _ in stream_events)


def test_tool_continuation_respects_configured_action_limit() -> None:
    loop = object.__new__(AgentLoop)
    loop.db = FakeDb()
    loop.events = FakeEvents()
    loop.tool_executor = _RecordingPriceToolExecutor()
    loop.step_agent = _FakeStepAgent(
        [
            StepAgentResult(
                tool_call=ToolCall(name="product.price_query", arguments={"product_name": "A3"}),
                is_step_completed=True,
            )
        ]
    )
    loop._recent_messages = lambda session: []  # type: ignore[method-assign]
    loop._tool_activity_payload = lambda tenant_id, name, result, *args: {  # type: ignore[method-assign]
        "toolName": name,
        "toolCallId": args[1] if len(args) > 1 else "",
        "content": result.model_dump(mode="json"),
        "success": result.success,
        "isError": not result.success,
    }
    loop._get_agent_loop_max_actions = lambda tenant_id: 1  # type: ignore[method-assign]
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="price_compare",
        active_step_id="query_price",
        slots_json={"product_name_1": "A1", "product_name_2": "A3"},
    )

    loop._execute_tool_action_cycle(
        _request("我想比下 A1 和 A3 的价格"),
        session,
        _price_compare_skill(),
        [_price_query_tool()],
        _model_config(),
        StepAgentResult(
            tool_call=ToolCall(name="product.price_query", arguments={"product_name": "A1"}),
            is_step_completed=True,
        ),
        [],
    )

    assert [call.arguments["product_name"] for call in loop.tool_executor.calls] == ["A1"]
    assert loop.step_agent.calls == 1
    assert len(session.slots_json["_tool_results"]) == 1


def test_duplicate_tool_call_with_reply_completes_from_existing_tool_result() -> None:
    loop = object.__new__(AgentLoop)
    loop.db = FakeDb()
    loop.events = FakeEvents()
    loop.tool_executor = _RecordingPriceToolExecutor()
    loop.step_agent = _FakeStepAgent(
        [
            StepAgentResult(
                reply="A1 的价格已查到，可以继续。",
                tool_call=ToolCall(name="product.price_query", arguments={"product_name": "A1"}),
                is_step_completed=False,
            )
        ]
    )
    loop._recent_messages = lambda session: []  # type: ignore[method-assign]
    loop._tool_activity_payload = lambda tenant_id, name, result, *args: {  # type: ignore[method-assign]
        "toolName": name,
        "toolCallId": args[1] if len(args) > 1 else "",
        "content": result.model_dump(mode="json"),
        "success": result.success,
        "isError": not result.success,
    }
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="price_compare",
        active_step_id="step_query_price_1",
        slots_json={"product_name_1": "A1"},
    )

    step_result, tool_result = loop._execute_tool_action_cycle(
        _request("查 A1 价格"),
        session,
        _price_compare_skill(),
        [_price_query_tool()],
        _model_config(),
        StepAgentResult(
            tool_call=ToolCall(name="product.price_query", arguments={"product_name": "A1"}),
            is_step_completed=True,
        ),
        [],
    )

    assert [call.arguments["product_name"] for call in loop.tool_executor.calls] == ["A1"]
    assert tool_result is not None and tool_result.success is True
    assert step_result.tool_call is None
    assert step_result.is_step_completed is True
    assert step_result.reply == "A1 的价格已查到，可以继续。"
    assert not any(record[2] == "agent_loop_stopped" for record in loop.events.records)
    assert any(
        record[2] == "agent_loop_completed" and record[3]["mode"] == "respond_after_duplicate"
        for record in loop.events.records
    )


def _repair_skill() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="repair_ticket",
        name="设备报修",
        content_json=_graph_content(
            "repair_ticket",
            "设备报修",
            [
                {
                    "node_id": "collect_repair_info",
                    "name": "收集报修信息",
                    "expected_user_info": ["reporter_name", "asset_id", "issue_desc"],
                    "allowed_actions": ["ask_user"],
                },
                {
                    "node_id": "reply_ticket_result",
                    "name": "反馈工单结果",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user", "handoff_human"],
                },
            ],
            required_info=["reporter_name", "asset_id", "issue_desc"],
        ),
        status="published",
    )


def _refund_skill() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="refund",
        name="售后退款流程",
        content_json=_graph_content(
            "refund",
            "售后退款流程",
            [
                {
                    "node_id": "check_refund",
                    "type": "tool_call",
                    "name": "核实退款条件",
                    "expected_user_info": ["order_id", "refund_reason"],
                    "allowed_actions": ["continue_flow", "call_tool:order.query"],
                },
                {
                    "node_id": "reply_result",
                    "name": "反馈结果",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user", "handoff_human"],
                },
            ],
            required_info=["order_id", "refund_reason"],
        ),
        status="published",
    )


def _parallel_audit_skill(edges: list[dict[str, object]]) -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="skill_parallel_audit",
        name="并行审核",
        content_json={
            "skill_id": "skill_parallel_audit",
            "name": "并行审核",
            "required_info": ["message_content"],
            "nodes": [
                {
                    "node_id": "start",
                    "type": "collect_info",
                    "name": "收集信息",
                    "instruction": "收集用户报文。",
                    "expected_user_info": ["message_content"],
                    "allowed_actions": ["ask_user"],
                },
                {
                    "node_id": "check_payee",
                    "type": "condition",
                    "name": "收款方一致性检查",
                    "instruction": "检查收款方是否一致。",
                    "expected_user_info": [],
                    "allowed_actions": ["continue_flow"],
                },
                {
                    "node_id": "check_sensitive",
                    "type": "condition",
                    "name": "敏感词检查",
                    "instruction": "检查敏感词。",
                    "expected_user_info": [],
                    "allowed_actions": ["continue_flow"],
                },
                {
                    "node_id": "approve",
                    "type": "response",
                    "name": "通过",
                    "instruction": "反馈通过。",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "reject",
                    "type": "response",
                    "name": "拒绝",
                    "instruction": "反馈拒绝。",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "report",
                    "type": "response",
                    "name": "生成报告",
                    "instruction": "汇总检查结果。",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user"],
                },
            ],
            "edges": [
                {
                    "source_node_id": str(edge["source_node_id"]),
                    "next_node_id": str(edge["next_node_id"]),
                    "condition": str(edge.get("condition") or ""),
                    "priority": index,
                }
                for index, edge in enumerate(edges)
            ],
            "start_node_id": "start",
            "terminal_node_ids": ["report", "approve", "reject"],
        },
        status="published",
    )


def _refund_collect_terminal_skill() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="refund",
        name="售后退款流程",
        content_json=_graph_content(
            "refund",
            "售后退款流程",
            [
                {
                    "node_id": "collect_order",
                    "name": "收集订单号",
                    "expected_user_info": ["order_id"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                },
                {
                    "node_id": "collect_refund_reason",
                    "name": "收集退款原因",
                    "expected_user_info": ["refund_reason"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                },
            ],
            required_info=["order_id", "refund_reason"],
        ),
        status="published",
    )


def _refund_tool() -> Tool:
    return Tool(
        tenant_id="tenant_demo",
        name="order.query",
        display_name="订单查询",
        method="POST",
        url="http://localhost:8000/api/mock/order/query",
        input_schema={
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "refund_reason": {"type": "string"},
            },
            "required": ["order_id", "refund_reason"],
        },
        allowed_skills_json=["refund"],
        enabled=True,
    )


def _refund_skill_with_late_collect_step() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="refund",
        name="售后退款流程",
        content_json=_graph_content(
            "refund",
            "售后退款流程",
            [
                {
                    "node_id": "collect_order",
                    "type": "tool_call",
                    "name": "收集订单",
                    "expected_user_info": ["order_id"],
                    "allowed_actions": ["ask_user", "call_tool:order.query"],
                },
                {
                    "node_id": "check_refund",
                    "name": "查询退款资格",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user", "handoff_human"],
                },
                {
                    "node_id": "collect_refund_reason",
                    "name": "收集退款原因",
                    "expected_user_info": ["refund_reason"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                },
            ],
            required_info=["order_id", "refund_reason"],
        ),
        status="published",
    )


def _refund_tool_call():
    return ToolCall(
        name="order.query",
        arguments={"order_id": "A12345", "refund_reason": "商品质量"},
    )


class _FakeStepAgent:
    def __init__(self, results: list[StepAgentResult]) -> None:
        self.results = results
        self.calls = 0
        self.call_args: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def run(self, *args: object, **kwargs: object) -> StepAgentResult:
        self.call_args.append((args, kwargs))
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return result


class _RecordingPriceToolExecutor:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    def execute(
        self,
        tenant_id: str,
        tool_call: ToolCall,
        active_skill_id: str | None = None,
        agent_id: str | None = None,
    ) -> ToolResult:
        self.calls.append(tool_call)
        product_name = str(tool_call.arguments.get("product_name") or "")
        return ToolResult(
            tool_name=tool_call.name,
            success=True,
            data={
                "product_name": product_name,
                "found": True,
                "price": 129 if product_name == "A1" else 239,
            },
        )


def _request(message: str):
    from app.session.session_schema import ChatTurnRequest

    return ChatTurnRequest(tenant_id="tenant_demo", session_id="session_test", message=message)


def _model_config():
    from app.db.models import ModelConfig

    return ModelConfig(tenant_id="tenant_demo", name="demo", api_key_encrypted="", model="demo")


def _graph_content(
    skill_id: str,
    name: str,
    nodes: list[dict[str, object]],
    *,
    required_info: list[str] | None = None,
) -> dict[str, object]:
    normalized_nodes = [
        {
            "node_id": str(node["node_id"]),
            "type": node.get("type")
            or ("collect_info" if node.get("expected_user_info") else "response"),
            "name": str(node.get("name") or node["node_id"]),
            "instruction": str(node.get("instruction") or ""),
            "expected_user_info": list(node.get("expected_user_info") or []),
            "allowed_actions": list(node.get("allowed_actions") or []),
            "knowledge_scope": dict(node.get("knowledge_scope") or {}),
            "metadata": dict(node.get("metadata") or {}),
        }
        for node in nodes
    ]
    return {
        "skill_id": skill_id,
        "name": name,
        "required_info": required_info or [],
        "nodes": normalized_nodes,
        "edges": [
            {
                "source_node_id": normalized_nodes[index]["node_id"],
                "next_node_id": normalized_nodes[index + 1]["node_id"],
                "priority": index,
                "label": "默认推进",
            }
            for index in range(len(normalized_nodes) - 1)
        ],
        "start_node_id": normalized_nodes[0]["node_id"],
        "terminal_node_ids": [normalized_nodes[-1]["node_id"]],
    }


def _purchase_skill() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="purchase",
        name="购买商品",
        content_json=_graph_content(
            "purchase",
            "购买商品",
            [
                {
                    "node_id": "collect_user_name",
                    "name": "收集用户与商品",
                    "expected_user_info": ["user_name", "product_id", "quantity"],
                    "allowed_actions": ["ask_user"],
                },
                {
                    "node_id": "confirm_product",
                    "type": "tool_call",
                    "name": "创建订单",
                    "expected_user_info": ["product_id"],
                    "allowed_actions": ["call_tool:product.purchase", "call_tool:order.add"],
                },
                {
                    "node_id": "reply_result",
                    "name": "反馈订单",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user"],
                },
            ],
            required_info=["user_name", "product_id", "quantity"],
        ),
        status="published",
    )


def _leave_policy_skill() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="leave",
        name="请假申请",
        content_json=_graph_content(
            "leave",
            "请假申请",
            [
                {
                    "node_id": "check_policy",
                    "type": "knowledge_query",
                    "name": "检索假期政策",
                    "instruction": "根据请假类型检索假期政策。",
                    "expected_user_info": ["leave_type"],
                    "allowed_actions": ["answer_user"],
                    "knowledge_scope": {"query_fields": ["leave_type"]},
                },
                {
                    "node_id": "query_balance",
                    "type": "tool_call",
                    "name": "查询余额",
                    "allowed_actions": ["call_tool:hr.balance_query"],
                },
            ],
            required_info=["leave_type"],
        ),
        status="published",
    )


def _purchase_skill_with_incomplete_required_info() -> Skill:
    skill = _purchase_skill()
    skill.content_json = {
        **(skill.content_json or {}),
        "required_info": ["user_id", "product_id", "quantity"],
    }
    return skill


def _purchase_tool() -> Tool:
    return Tool(
        tenant_id="tenant_demo",
        name="product.purchase",
        display_name="购买商品",
        method="POST",
        url="http://localhost:8000/api/mock/product/purchase",
        input_schema={
            "type": "object",
            "properties": {
                "product_id": {"type": "string"},
                "quantity": {"type": "integer"},
                "user_id": {"type": "string"},
            },
            "required": ["product_id"],
        },
        enabled=True,
    )


def _order_add_tool() -> Tool:
    return Tool(
        tenant_id="tenant_demo",
        name="order.add",
        display_name="订单添加",
        method="POST",
        url="http://localhost:8000/api/mock/order/add",
        input_schema={
            "type": "object",
            "properties": {"product_id": {"type": "string"}},
            "required": ["product_id"],
        },
        enabled=True,
    )


def _price_compare_skill() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="price_compare",
        name="商品比价",
        content_json=_graph_content(
            "price_compare",
            "商品比价",
            [
                {
                    "node_id": "collect_products",
                    "name": "收集商品",
                    "expected_user_info": ["product_name_1", "product_name_2"],
                    "allowed_actions": ["ask_user"],
                },
                {
                    "node_id": "query_price",
                    "type": "tool_call",
                    "name": "查询价格",
                    "expected_user_info": [],
                    "allowed_actions": ["call_tool:product.price_query"],
                },
                {
                    "node_id": "reply_result",
                    "name": "反馈结果",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user"],
                },
            ],
            required_info=["product_name_1", "product_name_2"],
        ),
        status="published",
    )


def _price_query_tool() -> Tool:
    return Tool(
        tenant_id="tenant_demo",
        name="product.price_query",
        display_name="商品价格查询",
        method="POST",
        url="http://localhost:8000/api/mock/product/price-query",
        input_schema={
            "type": "object",
            "properties": {"product_name": {"type": "string"}},
            "required": ["product_name"],
        },
        enabled=True,
    )


def test_step_agent_tools_are_scoped_to_active_skill() -> None:
    loop = object.__new__(AgentLoop)
    purchase_skill = _purchase_skill()
    price_skill = _price_compare_skill()
    price_tool = _price_query_tool()
    price_tool.allowed_skills_json = [price_skill.skill_id]
    global_tool = _order_add_tool()

    purchase_tool_names = {
        tool.name
        for tool in loop._step_agent_tools(
            purchase_skill,
            [price_tool, global_tool],
            active_step_id="confirm_product",
            slots={"product_id": "A1"},
        )
    }
    price_tool_names = {
        tool.name
        for tool in loop._step_agent_tools(
            price_skill,
            [price_tool, global_tool],
            active_step_id="query_price",
        )
    }

    assert purchase_tool_names == {"order.add"}
    assert price_tool_names == {"product.price_query"}
    assert (
        loop._step_agent_tools(
            purchase_skill,
            [price_tool, global_tool],
            active_step_id="collect_user_name",
            slots={},
        )
        == []
    )
    assert loop._step_agent_tools(None, [price_tool, global_tool]) == []


def _refund_skill_with_tool_collect_step() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="refund",
        name="退款",
        content_json=_graph_content(
            "refund",
            "退款",
            [
                {
                    "node_id": "collect_order",
                    "type": "tool_call",
                    "name": "收集订单",
                    "expected_user_info": ["order_id"],
                    "allowed_actions": ["ask_user", "call_tool:order.query"],
                },
                {
                    "node_id": "reply_result",
                    "name": "反馈结果",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user"],
                },
            ],
            required_info=["order_id"],
        ),
        status="published",
    )
