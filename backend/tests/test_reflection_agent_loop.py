from app.core.agent_loop import AgentLoop
from app.core.reflection_agent import ReflectionDecision
from app.core.skill_runtime import SkillRuntime
from app.db.models import ChatSession, ModelConfig, Skill, Tool
from app.session.session_schema import ChatTurnRequest, RouterDecision, StepAgentResult, ToolCall
from app.tools.tool_schema import ToolResult


def test_reflection_switches_wrong_active_skill_without_suspending() -> None:
    loop = object.__new__(AgentLoop)
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="visitor_badge",
        active_step_id="collect_visitor",
    )

    decision = loop._router_decision_from_reflection(
        ReflectionDecision(
            needs_retry=True,
            reason="用户要报修，不是办理访客证。",
            target_skill_id="repair_ticket",
        ),
        session,
        [_skill("visitor_badge"), _skill("repair_ticket")],
        previous_decision=RouterDecision(decision="continue_current_skill"),
    )

    assert decision is not None
    assert decision.decision == "start_skill"
    assert decision.target_skill_id == "repair_ticket"


def test_reflection_does_not_restart_completed_skill_in_same_turn() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = _FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id=None,
        active_step_id=None,
    )

    decision = loop._router_decision_from_reflection(
        ReflectionDecision(
            needs_retry=True,
            reason="同一轮已经完成过该技能，不应再次启动。",
            target_skill_id="price_compare",
        ),
        session,
        [_skill("price_compare")],
        previous_decision=RouterDecision(decision="answer_only"),
        completed_skill_ids_this_turn={"price_compare"},
    )

    assert decision is None
    assert any(
        record[2] == "reflection_retry_skipped_completed_task"
        and record[3]["target_skill_id"] == "price_compare"
        for record in loop.events.records
    )


def test_reflection_can_continue_completed_skill_when_still_active() -> None:
    loop = object.__new__(AgentLoop)
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="price_compare",
        active_step_id="start",
    )

    decision = loop._router_decision_from_reflection(
        ReflectionDecision(
            needs_retry=True,
            reason="当前 active skill 需要继续修正。",
            target_skill_id="price_compare",
        ),
        session,
        [_skill("price_compare")],
        previous_decision=RouterDecision(decision="continue_current_skill"),
        completed_skill_ids_this_turn={"price_compare"},
    )

    assert decision is not None
    assert decision.decision == "continue_current_skill"
    assert decision.target_skill_id == "price_compare"


def test_reflection_builds_tool_call_from_slots() -> None:
    loop = object.__new__(AgentLoop)
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="repair_ticket",
        slots_json={"customer_name": "张三", "asset_id": "EQ-9", "issue": "无法启动"},
    )

    tool_call = loop._tool_call_from_reflection(
        ReflectionDecision(needs_retry=True, target_tool_name="ticket.create"),
        session,
        [_ticket_tool()],
    )

    assert tool_call is not None
    assert tool_call.name == "ticket.create"
    assert tool_call.arguments["customer_name"] == "张三"
    assert tool_call.arguments["asset_id"] == "EQ-9"
    assert tool_call.arguments["issue"] == "无法启动"


def test_reflection_builds_archive_order_tool_call_from_order_slot() -> None:
    loop = object.__new__(AgentLoop)
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="after_sales_refund",
        slots_json={"order_id": "ARCHIVE-1001"},
    )

    tool_call = loop._tool_call_from_reflection(
        ReflectionDecision(needs_retry=True, target_tool_name="order.archive_query"),
        session,
        [_archive_order_tool()],
    )

    assert tool_call is not None
    assert tool_call.name == "order.archive_query"
    assert tool_call.arguments == {"order_id": "ARCHIVE-1001"}


def test_reflection_tool_retry_is_preferred_for_current_skill_target() -> None:
    loop = object.__new__(AgentLoop)
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="after_sales_refund",
    )

    assert loop._reflection_tool_retry_targets_current_skill(
        ReflectionDecision(
            needs_retry=True,
            target_skill_id="after_sales_refund",
            target_tool_name="order.archive_query",
        ),
        session,
    )


def test_reflection_tool_retry_preserves_router_decision_and_streams_tool_events() -> None:
    loop = object.__new__(AgentLoop)
    loop.db = _FakeDb()
    loop.events = _FakeEvents()
    loop.tool_executor = _FakeToolExecutor()
    loop._tool_activity_payload = lambda tenant_id, name, result, *args: {  # type: ignore[method-assign]
        "toolId": name,
        "toolName": name,
        "rawToolName": name,
        "success": result.success,
        "isError": not result.success,
        "content": result.model_dump(mode="json"),
    }
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="after_sales_refund",
        active_step_id="check_refund_eligibility",
    )
    decision = RouterDecision(
        decision="continue_current_skill",
        target_skill_id="after_sales_refund",
        target_step_id="check_refund_eligibility",
        user_intent="申请退款",
    )
    stream_events: list[tuple[str, dict[str, object]]] = []

    active_skill, returned_decision, step_result, tool_result = loop._retry_with_reflection_tool_call(
        ChatTurnRequest(tenant_id="tenant_demo", message="我要退款"),
        session,
        None,
        decision,
        ToolCall(name="order.archive_query", arguments={"order_id": "ARCHIVE-1001"}),
        "主工具未命中，尝试历史订单查询",
        stream_events,
    )

    assert active_skill is None
    assert returned_decision is decision
    assert step_result.tool_call is not None
    assert step_result.tool_call.name == "order.archive_query"
    assert tool_result is not None
    assert tool_result.success
    assert stream_events[0][0] == "status"
    assert stream_events[0][1]["phase"] == "tool"
    assert stream_events[1][0] == "tool_result"


def test_zero_reflection_rounds_skips_reflection_agent() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = _FakeEvents()
    loop.reflection_agent = _RaisingReflectionAgent()
    session = ChatSession(id="session_test", tenant_id="tenant_demo")
    decision = RouterDecision(decision="continue_current_skill", user_intent="申请退款")
    step_result = StepAgentResult(is_step_completed=True)
    tool_result = ToolResult(tool_name="order.query", success=True, data={"found": False})

    returned = loop._run_reflection_rounds(
        ChatTurnRequest(tenant_id="tenant_demo", message="我要退款"),
        session,
        [],
        [],
        ModelConfig(tenant_id="tenant_demo", name="demo", api_key_encrypted="", model="demo"),
        None,
        decision,
        step_result,
        tool_result,
        0,
    )

    assert returned == (None, decision, step_result, tool_result)
    assert loop.events.records[-1][2] == "reflection_skipped"
    assert loop.events.records[-1][3]["skip_reason"] == "reflection_disabled"


def test_clarify_greeting_does_not_trigger_reflection() -> None:
    loop = object.__new__(AgentLoop)
    loop.reflection_agent = _RaisingReflectionAgent()
    session = ChatSession(id="session_test", tenant_id="tenant_demo")
    decision = RouterDecision(decision="clarify", user_intent="greeting")
    step_result = StepAgentResult(reply="您好，请问有什么可以帮您？")

    returned = loop._run_reflection_rounds(
        ChatTurnRequest(tenant_id="tenant_demo", message="你好"),
        session,
        [],
        [],
        ModelConfig(tenant_id="tenant_demo", name="demo", api_key_encrypted="", model="demo"),
        None,
        decision,
        step_result,
        None,
        1,
    )

    assert returned == (None, decision, step_result, None)


def test_successful_expected_tool_result_can_pass_reflection() -> None:
    loop = object.__new__(AgentLoop)
    loop.reflection_agent = _PassingReflectionAgent()
    loop.events = _FakeEvents()
    session = ChatSession(id="session_test", tenant_id="tenant_demo")
    decision = RouterDecision(decision="continue_current_skill", user_intent="查询订单")
    step_result = StepAgentResult(is_step_completed=True)
    tool_result = ToolResult(tool_name="order.query", success=True, data={"found": True})

    returned = loop._run_reflection_rounds(
        ChatTurnRequest(tenant_id="tenant_demo", message="查订单"),
        session,
        [],
        [],
        ModelConfig(tenant_id="tenant_demo", name="demo", api_key_encrypted="", model="demo"),
        None,
        decision,
        step_result,
        tool_result,
        1,
    )

    assert returned == (None, decision, step_result, tool_result)


def test_reflection_target_skill_is_scheduled_instead_of_skipped() -> None:
    loop = object.__new__(AgentLoop)
    loop.db = _FakeDb()
    loop.events = _FakeEvents()
    loop.runtime = SkillRuntime()
    loop.reflection_agent = _TargetSkillReflectionAgent("price_compare")
    skills = [_purchase_skill(), _price_compare_skill()]
    skills_by_id = {skill.skill_id: skill for skill in skills}
    captured: dict[str, object] = {}

    def run_step(
        request: ChatTurnRequest,
        chat_session: ChatSession,
        active_skill: Skill | None,
        tools: list[Tool],
        model_config: ModelConfig,
        router_decision: RouterDecision,
        **_: object,
    ) -> StepAgentResult:
        captured["active_skill_id"] = active_skill.skill_id if active_skill else None
        captured["router_decision"] = router_decision.model_dump(mode="json")
        captured["tool_names"] = [tool.name for tool in loop._step_agent_tools(active_skill, tools)]
        return StepAgentResult(reply="已切换到比价流程。", is_step_completed=True)

    loop._get_active_skill = lambda tenant_id, skill_id, agent_id=None: skills_by_id.get(skill_id)  # type: ignore[method-assign]
    loop._run_step_agent_with_context_repair = run_step  # type: ignore[method-assign]
    loop._skill_version = lambda tenant_id, skill_id: None  # type: ignore[method-assign]

    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="purchase",
        active_step_id="collect_purchase",
        slots_json={"product_name_1": "A1", "product_name_2": "A3"},
    )
    previous_decision = RouterDecision(
        decision="continue_current_skill",
        target_skill_id="purchase",
        user_intent="购买前比价",
    )
    previous_step = StepAgentResult(
        tool_call=ToolCall(name="product.price_query", arguments={"product_name": "A1"}),
        is_step_completed=True,
    )
    previous_tool_result = ToolResult(
        tool_name="product.price_query",
        success=False,
        error={"code": "NOT_ALLOWED", "message": "当前技能不允许调用该工具。"},
    )

    active_skill, router_decision, step_result, tool_result, retried = loop._reflect_and_retry(
        ChatTurnRequest(tenant_id="tenant_demo", message="买 A1 前跟 A3 比下价格"),
        session,
        skills,
        [_price_query_tool()],
        ModelConfig(tenant_id="tenant_demo", name="demo", api_key_encrypted="", model="demo"),
        _purchase_skill(),
        previous_decision,
        previous_step,
        previous_tool_result,
        conversation_context={},
    )

    assert retried is True
    assert active_skill is not None
    assert active_skill.skill_id == "price_compare"
    assert router_decision.decision == "start_skill"
    assert router_decision.target_skill_id == "price_compare"
    assert session.active_skill_id == "price_compare"
    assert session.active_step_id == "collect_products"
    assert step_result.reply == "已切换到比价流程。"
    assert tool_result is None
    assert captured["active_skill_id"] == "price_compare"
    assert captured["tool_names"] == ["product.price_query"]
    assert not any(record[2] == "reflection_retry_skipped" for record in loop.events.records)
    assert any(
        record[2] == "reflection_retry_started" and record[3]["mode"] == "skill"
        for record in loop.events.records
    )


class _FakeDb:
    def commit(self) -> None:
        pass

    def refresh(self, _row: object) -> None:
        pass


class _FakeEvents:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, str, dict]] = []

    def record(self, tenant_id: str, session_id: str, event_type: str, payload: dict) -> None:
        self.records.append((tenant_id, session_id, event_type, payload))


class _FakeToolExecutor:
    def execute(self, tenant_id: str, tool_call: ToolCall, active_skill_id: str | None) -> ToolResult:
        return ToolResult(
            tool_name=tool_call.name,
            success=True,
            data={"source": "archive_order_center", "found": True},
        )


class _RaisingReflectionAgent:
    def review(self, *args: object, **kwargs: object) -> ReflectionDecision:
        raise AssertionError("reflection agent should not be called")


class _PassingReflectionAgent:
    def review(self, *args: object, **kwargs: object) -> ReflectionDecision:
        return ReflectionDecision(action="pass", needs_retry=False)


class _TargetSkillReflectionAgent:
    def __init__(self, target_skill_id: str) -> None:
        self.target_skill_id = target_skill_id

    def review(self, *args: object, **kwargs: object) -> ReflectionDecision:
        return ReflectionDecision(
            action="try_other_tool",
            needs_retry=True,
            reason="当前技能不能调用目标工具，切到可执行技能。",
            target_skill_id=self.target_skill_id,
        )


def _skill(skill_id: str) -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id=skill_id,
        name=skill_id,
        content_json={
            "skill_id": skill_id,
            "name": skill_id,
            "nodes": [{"node_id": "start", "type": "collect_info", "name": "开始", "allowed_actions": ["ask_user"]}],
            "edges": [],
            "start_node_id": "start",
            "terminal_node_ids": ["start"],
        },
        status="published",
    )


def _purchase_skill() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="purchase",
        name="购买商品",
        content_json={
            "skill_id": "purchase",
            "name": "购买商品",
            "nodes": [
                {
                    "node_id": "collect_purchase",
                    "type": "collect_info",
                    "name": "收集购买信息",
                    "allowed_actions": ["ask_user", "continue_flow"],
                }
            ],
            "edges": [],
            "start_node_id": "collect_purchase",
            "terminal_node_ids": ["collect_purchase"],
        },
        status="published",
    )


def _price_compare_skill() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="price_compare",
        name="商品比价",
        content_json={
            "skill_id": "price_compare",
            "name": "商品比价",
            "nodes": [
                {
                    "node_id": "collect_products",
                    "type": "collect_info",
                    "name": "收集待比价商品",
                    "allowed_actions": ["ask_user", "continue_flow"],
                },
                {
                    "node_id": "query_prices",
                    "type": "tool_call",
                    "name": "查询商品价格",
                    "allowed_actions": ["call_tool:product.price_query", "continue_flow"],
                },
            ],
            "edges": [
                {
                    "source_node_id": "collect_products",
                    "next_node_id": "query_prices",
                    "priority": 0,
                    "label": "默认推进",
                }
            ],
            "start_node_id": "collect_products",
            "terminal_node_ids": ["query_prices"],
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
        allowed_skills_json=["price_compare"],
        enabled=True,
    )


def _ticket_tool() -> Tool:
    return Tool(
        tenant_id="tenant_demo",
        name="ticket.create",
        display_name="创建工单",
        method="POST",
        url="http://localhost:8000/api/mock/ticket/create",
        input_schema={
            "type": "object",
            "properties": {
                "customer_name": {"type": "string"},
                "asset_id": {"type": "string"},
                "issue": {"type": "string"},
            },
            "required": ["customer_name", "asset_id", "issue"],
        },
        allowed_skills_json=["repair_ticket"],
        enabled=True,
    )


def _archive_order_tool() -> Tool:
    return Tool(
        tenant_id="tenant_demo",
        name="order.archive_query",
        display_name="历史订单查询",
        method="POST",
        url="http://localhost:8000/api/mock/order/archive-query",
        input_schema={
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
        allowed_skills_json=["after_sales_refund", "after_sales_exchange"],
        enabled=True,
    )
