from app.core.agent_loop import AgentLoop
from app.core.skill_runtime import SkillRuntime
from app.db.models import ChatSession, Skill, Tool
from app.session.session_schema import RouterDecision, StepAgentResult
from app.tools.tool_schema import ToolCall, ToolResult


class FakeEvents:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, str, dict]] = []

    def record(self, tenant_id: str, session_id: str, event_type: str, payload: dict) -> None:
        self.records.append((tenant_id, session_id, event_type, payload))


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


def test_context_repair_skips_satisfied_collect_step_and_uses_schema_tool() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    loop.step_agent = _FakeStepAgent(
        [
            StepAgentResult(
                reply="您好 hm，请问您想购买的商品 ID 是什么？",
                slot_updates={"user_name": "hm"},
                next_step_id="collect_user_name",
            ),
            StepAgentResult(reply="正在为您创建订单，请稍候。"),
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
    assert any(
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
            StepAgentResult(reply="正在为您创建订单，请稍候。"),
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

    def run(self, *args: object, **kwargs: object) -> StepAgentResult:
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return result


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
