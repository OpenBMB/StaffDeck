from app.core.agent_loop import AgentLoop
from app.core.skill_runtime import SkillRuntime
from app.db.models import ChatSession, Message, Skill, Tool
from app.session.session_schema import PendingTask, RouterDecision, StepAgentResult
from app.tools.tool_schema import ToolCall, ToolResult


class FakeEvents:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, str, dict]] = []

    def record(self, tenant_id: str, session_id: str, event_type: str, payload: dict) -> None:
        self.records.append((tenant_id, session_id, event_type, payload))


class FakeDb:
    def __init__(self) -> None:
        self.commits = 0
        self.refreshed: list[object] = []
        self.added: list[object] = []

    def add(self, row: object) -> None:
        self.added.append(row)

    def commit(self) -> None:
        self.commits += 1

    def refresh(self, row: object) -> None:
        self.refreshed.append(row)


class FakeExecResult:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def all(self) -> list[object]:
        return self.rows


class FakeMessageDb(FakeDb):
    def __init__(self, rows: list[Message]) -> None:
        super().__init__()
        self.rows = rows

    def exec(self, _statement: object) -> FakeExecResult:
        return FakeExecResult(self.rows)


class FakeToolExecutor:
    def __init__(self, db: FakeDb) -> None:
        self.db = db
        self.commits_seen_before_execute: int | None = None

    def execute(
        self, tenant_id: str, tool_call: ToolCall, active_skill_id: str | None = None
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


def test_compound_turn_seeds_primary_and_created_tasks_for_scheduler() -> None:
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
        decision="continue_current_skill",
        target_skill_id="purchase",
        target_step_id="collect_user_name",
        confidence=0.91,
        user_intent="继续购买 A1，并比较 A1 和 A3",
        reason="用户补充购买目标，同时提出独立比价任务。",
        source_message="我买 A1 前跟 A3 比一下价格",
        slot_hints={"product_id": "A1", "quantity": 1},
        created_tasks=[
            PendingTask(
                task_id="task_price_compare_a1_a3",
                target_skill_id="price_compare",
                target_step_id="collect_products",
                user_intent="比较 A1 和 A3 的价格",
                source_message="我买 A1 前跟 A3 比一下价格",
                slot_hints={"product_name_1": "A1", "product_name_2": "A3"},
            )
        ],
    )

    queue_decision = loop._initial_scheduler_queue_decision(
        _request("我买 A1 前跟 A3 比一下价格"),
        session,
        router_decision,
    )

    assert queue_decision is not None
    assert queue_decision.decision == "create_pending"
    assert len(queue_decision.pending_tasks) == 2
    primary, created = queue_decision.pending_tasks
    assert primary.target_skill_id == "purchase"
    assert primary.target_step_id == "collect_user_name"
    assert primary.slot_hints == {"user_name": "hm", "product_id": "A1", "quantity": 1}
    assert created.task_id == "task_price_compare_a1_a3"
    assert created.target_skill_id == "price_compare"

    loop.runtime.apply_decision(session, queue_decision)
    loop._release_active_task_to_scheduler(session, queue_decision)

    assert session.active_skill_id is None
    assert session.active_step_id is None
    assert [frame["skill_id"] for frame in session.pending_tasks_json] == ["purchase", "price_compare"]
    assert session.pending_tasks_json[0]["slots"] == {"user_name": "hm", "product_id": "A1", "quantity": 1}
    assert session.pending_tasks_json[1]["slots"] == {"product_name_1": "A1", "product_name_2": "A3"}


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


def test_stale_terminal_skill_is_removed_from_suspended_stack() -> None:
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
    assert loop.events.records[0][2] == "skill_completed"
    assert loop.events.records[0][3]["reason"] == "stale_suspended_terminal_state"


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


def test_answer_step_can_complete_even_if_distilled_order_has_later_satisfied_collect_step() -> None:
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


def test_context_repair_skips_satisfied_collect_step_and_uses_model_tool_call() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    loop.step_agent = _FakeStepAgent(
        [
            StepAgentResult(
                reply="您好 hm，请问您想购买的商品 ID 是什么？",
                slot_updates={"user_name": "hm"},
                next_step_id="collect_user_name",
            ),
            StepAgentResult(
                reply="正在为您创建订单，请稍候。",
                tool_call=ToolCall(name="product.purchase", arguments={"product_id": "A3", "quantity": 1}),
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
        RouterDecision(decision="continue_current_skill", target_skill_id="purchase"),
    )

    assert session.active_step_id == "confirm_product"
    assert loop.step_agent.calls == 2
    assert step_result.tool_call is not None
    assert step_result.tool_call.name == "product.purchase"
    assert step_result.tool_call.arguments["product_id"] == "A3"
    assert step_result.tool_call.arguments["quantity"] == 1
    assert any(
        event_type == "skill_step_changed"
        and payload.get("reason") == "expected_info_satisfied"
        for _, _, event_type, payload in loop.events.records
    )
    assert not any(
        event_type == "step_agent_result_repaired"
        and payload.get("mode") == "schema_tool_call"
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
        event_type == "step_agent_result_repaired"
        and payload.get("mode") == "schema_tool_call"
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
                next_step_id="collect_user_name",
            ),
            StepAgentResult(
                reply="正在为您创建订单，请稍候。",
                tool_call=ToolCall(name="product.purchase", arguments={"product_id": "A1", "quantity": 1}),
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
        RouterDecision(decision="suspend_current_and_start_new_skill", target_skill_id="purchase"),
    )

    assert loop.step_agent.calls == 3
    assert session.slots_json["user_name"] == "hm"
    assert session.slots_json["product_id"] == "A1"
    assert session.slots_json["quantity"] == 1
    assert step_result.tool_call is not None
    assert step_result.tool_call.name == "product.purchase"
    assert any(
        event_type == "step_agent_result_repaired"
        and payload.get("mode") == "slot_validation"
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

    args, _kwargs = loop.step_agent.call_args[0]
    recent_messages = args[7]
    conversation_context = args[9]
    assert len(recent_messages) == 16
    assert recent_messages[0]["content"] == "message 0"
    assert recent_messages[-1]["content"] == "message 15"
    assert conversation_context["metadata"]["compacted"] is False
    assert conversation_context["metadata"]["total_messages"] == 16


def test_router_context_uses_recent_messages_only() -> None:
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

    context = loop._conversation_context(session, max_messages=8)

    assert len(context["messages"]) == 8
    assert context["messages"][0]["content"] == "message 8"
    assert context["messages"][-1]["content"] == "message 15"
    assert context["metadata"]["total_messages"] == 8


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
        RouterDecision(decision="continue_current_skill", target_skill_id="purchase"),
    )

    assert loop.step_agent.calls == 2
    assert "quantity" not in session.slots_json
    assert step_result.tool_call is None
    assert not any(
        event_type == "step_agent_result_repaired"
        and payload.get("mode") == "slot_validation"
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
                "content": "用户姓名/称呼：hm",
                "metadata": {"key": "preferred_name"},
            }
        ],
        conversation_context={"messages": [{"role": "user", "content": "我想买东西"}]},
    )

    assert loop.step_agent.calls == 2
    assert session.slots_json["user_name"] == "hm"
    assert step_result.reply == "好的，hm！请问您想购买什么商品？需要购买多少件？"
    assert any(
        event_type == "step_agent_result_repaired"
        and payload.get("mode") == "slot_validation"
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


def test_context_repair_does_not_skip_satisfied_tool_step() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="refund",
        active_step_id="collect_order",
        slots_json={"order_id": "A12345"},
    )

    advanced = loop._advance_past_satisfied_collection_steps(
        "tenant_demo", session, _refund_skill_with_tool_collect_step()
    )

    assert not advanced
    assert session.active_step_id == "collect_order"


def _repair_skill() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="repair_ticket",
        name="设备报修",
        content_json={
            "skill_id": "repair_ticket",
            "name": "设备报修",
            "required_info": ["reporter_name", "asset_id", "issue_desc"],
            "steps": [
                {
                    "step_id": "collect_repair_info",
                    "name": "收集报修信息",
                    "expected_user_info": ["reporter_name", "asset_id", "issue_desc"],
                    "allowed_actions": ["ask_user"],
                },
                {
                    "step_id": "reply_ticket_result",
                    "name": "反馈工单结果",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user", "handoff_human"],
                },
            ],
        },
        status="published",
    )


def _refund_skill() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="refund",
        name="售后退款流程",
        content_json={
            "skill_id": "refund",
            "name": "售后退款流程",
            "required_info": ["order_id", "refund_reason"],
            "steps": [
                {
                    "step_id": "check_refund",
                    "name": "核实退款条件",
                    "expected_user_info": ["order_id", "refund_reason"],
                    "allowed_actions": ["continue_flow", "call_tool:order.query"],
                },
                {
                    "step_id": "reply_result",
                    "name": "反馈结果",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user", "handoff_human"],
                },
            ],
        },
        status="published",
    )


def _refund_collect_terminal_skill() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="refund",
        name="售后退款流程",
        content_json={
            "skill_id": "refund",
            "name": "售后退款流程",
            "required_info": ["order_id", "refund_reason"],
            "steps": [
                {
                    "step_id": "collect_order",
                    "name": "收集订单号",
                    "expected_user_info": ["order_id"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                },
                {
                    "step_id": "collect_refund_reason",
                    "name": "收集退款原因",
                    "expected_user_info": ["refund_reason"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                },
            ],
        },
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
        content_json={
            "skill_id": "refund",
            "name": "售后退款流程",
            "required_info": ["order_id", "refund_reason"],
            "steps": [
                {
                    "step_id": "collect_order",
                    "name": "收集订单",
                    "expected_user_info": ["order_id"],
                    "allowed_actions": ["ask_user", "call_tool:order.query"],
                },
                {
                    "step_id": "check_refund",
                    "name": "查询退款资格",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user", "handoff_human"],
                },
                {
                    "step_id": "collect_refund_reason",
                    "name": "收集退款原因",
                    "expected_user_info": ["refund_reason"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                },
            ],
        },
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
        self, tenant_id: str, tool_call: ToolCall, active_skill_id: str | None = None
    ) -> ToolResult:
        self.calls.append(tool_call)
        product_name = str(tool_call.arguments.get("product_name") or "")
        return ToolResult(
            tool_name=tool_call.name,
            success=True,
            data={"product_name": product_name, "found": True, "price": 129 if product_name == "A1" else 239},
        )


def _request(message: str):
    from app.session.session_schema import ChatTurnRequest

    return ChatTurnRequest(tenant_id="tenant_demo", session_id="session_test", message=message)


def _model_config():
    from app.db.models import ModelConfig

    return ModelConfig(tenant_id="tenant_demo", name="demo", api_key_encrypted="", model="demo")


def _purchase_skill() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="purchase",
        name="购买商品",
        content_json={
            "skill_id": "purchase",
            "name": "购买商品",
            "required_info": ["user_name", "product_id", "quantity"],
            "steps": [
                {
                    "step_id": "collect_user_name",
                    "name": "收集用户与商品",
                    "expected_user_info": ["user_name", "product_id", "quantity"],
                    "allowed_actions": ["ask_user"],
                },
                {
                    "step_id": "confirm_product",
                    "name": "创建订单",
                    "expected_user_info": ["product_id"],
                    "allowed_actions": ["call_tool:product.purchase", "call_tool:order.add"],
                },
                {
                    "step_id": "reply_result",
                    "name": "反馈订单",
                    "expected_user_info": [],
                    "allowed_actions": ["reply"],
                },
            ],
        },
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
        content_json={
            "skill_id": "price_compare",
            "name": "商品比价",
            "required_info": ["product_name_1", "product_name_2"],
            "steps": [
                {
                    "step_id": "collect_products",
                    "name": "收集商品",
                    "expected_user_info": ["product_name_1", "product_name_2"],
                    "allowed_actions": ["ask_user"],
                },
                {
                    "step_id": "query_price",
                    "name": "查询价格",
                    "expected_user_info": [],
                    "allowed_actions": ["call_tool:product.price_query"],
                },
                {
                    "step_id": "reply_result",
                    "name": "反馈结果",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user"],
                },
            ],
        },
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
        tool.name for tool in loop._step_agent_tools(purchase_skill, [price_tool, global_tool])
    }
    price_tool_names = {
        tool.name for tool in loop._step_agent_tools(price_skill, [price_tool, global_tool])
    }

    assert purchase_tool_names == {"order.add"}
    assert price_tool_names == {"product.price_query", "order.add"}
    assert loop._step_agent_tools(None, [price_tool, global_tool]) == []


def _refund_skill_with_tool_collect_step() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="refund",
        name="退款",
        content_json={
            "skill_id": "refund",
            "name": "退款",
            "required_info": ["order_id"],
            "steps": [
                {
                    "step_id": "collect_order",
                    "name": "收集订单",
                    "expected_user_info": ["order_id"],
                    "allowed_actions": ["ask_user", "call_tool:order.query"],
                },
                {
                    "step_id": "reply_result",
                    "name": "反馈结果",
                    "expected_user_info": [],
                    "allowed_actions": ["reply"],
                },
            ],
        },
        status="published",
    )
