from app.core.response_generator import ResponseGenerator
from app.db.models import ChatSession, Skill
from app.llm.client import LLMClient
from app.session.session_schema import RouterDecision, StepAgentResult
from app.tools.tool_schema import ToolError, ToolResult


def test_response_payload_has_single_source_for_each_business_fact() -> None:
    skill = Skill(
        tenant_id="tenant_demo",
        skill_id="medical",
        name="问诊",
        status="published",
        content_json={
            "response_rules": ["给出明确建议"],
            "nodes": [
                {
                    "node_id": "collect",
                    "type": "collect_info",
                    "name": "收集症状",
                    "instruction": "收集仍然缺失的症状信息。",
                    "expected_user_info": ["symptom", "duration"],
                }
            ],
        },
    )
    decision = RouterDecision(
        decision="continue_active",
        source_message="腹泻两天",
        slot_hints={"duration": "两天"},
    )
    payload = ResponseGenerator()._payload(
        "腹泻两天",
        ChatSession(
            id="session_test",
            tenant_id="tenant_demo",
            active_skill_id="medical",
            active_step_id="collect",
            slots_json={"symptom": "腹泻", "duration": "两天"},
            awaiting_input_json={"expected_fields": ["age"]},
        ),
        skill,
        decision,
        StepAgentResult(
            reply="请补充年龄",
            slot_updates={"duration": "两天"},
            is_step_completed=False,
        ),
        None,
    )

    assert payload["current_step"]["instruction"] == "收集仍然缺失的症状信息。"
    assert payload["slots"] == {"symptom": "腹泻", "duration": "两天"}
    assert payload["step_summary"] == {
        "reply": "请补充年龄",
        "is_step_completed": False,
        "handoff": False,
    }
    assert payload["response_rules"] == ["给出明确建议"]
    assert "active_skill" not in payload
    assert "router_decision" not in payload
    assert "session" not in payload
    assert "slot_updates" not in str(payload)
    assert "source_message" not in str(payload)
    assert "awaiting_input" not in str(payload)


def test_multi_task_payload_projects_all_results_for_one_final_reply() -> None:
    generator = ResponseGenerator()
    payload = generator._payload(
        "先查询额度，再提交报销",
        ChatSession(id="session_test", tenant_id="tenant_demo"),
        None,
        RouterDecision(decision="start_new_task", user_intent="处理两个任务"),
        StepAgentResult(),
        None,
        task_results=[
            {
                "task": "查询额度",
                "slots": {"employee_id": "E-1"},
                "step_result": {
                    "action": "reply",
                    "reply": "剩余额度 1000 元",
                    "knowledge_results": [],
                    "is_step_completed": True,
                    "handoff": False,
                },
                "tool_result": {"tool_name": "quota.query", "success": True},
            },
            {
                "task": "提交报销",
                "slots": {"amount": 500},
                "step_result": {
                    "action": "ask_user",
                    "reply": "请补充发票",
                    "knowledge_results": [],
                    "is_step_completed": False,
                    "handoff": False,
                },
                "tool_result": None,
            },
        ],
    )

    assert set(payload) == {"user_message", "conversation_context", "task_results"}
    assert [item["task"] for item in payload["task_results"]] == ["查询额度", "提交报销"]
    assert payload["task_results"][0]["step_summary"]["reply"] == "剩余额度 1000 元"
    assert payload["task_results"][1]["step_summary"]["reply"] == "请补充发票"


def test_clarify_does_not_leak_internal_router_prompt(monkeypatch):
    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_text(self, system_prompt, payload):  # noqa: ANN001
        return "请提供当前用户消息、会话状态、技能进度及可用技能列表，以便进行准确的路由决策。"

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_text", fake_generate_text)
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="repair_ticket",
        active_step_id="collect_repair_info",
    )
    decision = RouterDecision(
        decision="clarify",
        clarification_question="请提供当前用户消息、会话状态、技能进度及可用技能列表，以便进行准确的路由决策。",
    )
    step_result = StepAgentResult(reply="好的，请描述一下设备问题，我会继续为您处理。")

    reply = ResponseGenerator().generate(
        message="我想报修设备",
        session=session,
        skill=None,
        router_decision=decision,
        step_result=step_result,
        tool_result=None,
        model_config=None,  # type: ignore[arg-type]
    )

    assert reply == step_result.reply
    assert "技能进度" not in reply
    assert "路由" not in reply


def test_tool_result_reply_is_model_driven(monkeypatch):
    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_text(self, system_prompt, payload):  # noqa: ANN001
        assert payload["tool_result"]["tool_name"] == "ticket.create"
        return "已创建报修工单 T-100，工程师会尽快联系您。"

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_text", fake_generate_text)

    reply = ResponseGenerator().generate(
        message="设备坏了",
        session=ChatSession(id="session_test", tenant_id="tenant_demo"),
        skill=None,
        router_decision=RouterDecision(decision="continue_active"),
        step_result=StepAgentResult(),
        tool_result=ToolResult(
            tool_name="ticket.create",
            success=True,
            data={"ticket_id": "T-100", "status": "created"},
        ),
        model_config=None,  # type: ignore[arg-type]
    )

    assert reply == "已创建报修工单 T-100，工程师会尽快联系您。"


def test_failed_tool_result_returns_explicit_failure_without_model_call(monkeypatch):
    def forbidden_generate_text(self, system_prompt, payload):  # noqa: ANN001
        raise AssertionError("failed tool replies should not rely on model generation")

    monkeypatch.setattr(LLMClient, "generate_text", forbidden_generate_text)

    reply = ResponseGenerator().generate(
        message="查一下订单",
        session=ChatSession(id="session_test", tenant_id="tenant_demo"),
        skill=None,
        router_decision=RouterDecision(decision="continue_active"),
        step_result=StepAgentResult(),
        tool_result=ToolResult(
            tool_name="order.query",
            success=False,
            error=ToolError(code="HTTP_ERROR", message="工具返回异常状态码：502"),
        ),
        model_config=None,  # type: ignore[arg-type]
    )

    assert reply == "工具调用失败：order.query（HTTP_ERROR）：工具返回异常状态码：502。请检查工具配置、调用参数或外部服务状态后重试。"


def test_model_failure_returns_explicit_reason(monkeypatch):
    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_text(self, system_prompt, payload):  # noqa: ANN001
        raise RuntimeError("upstream timeout")

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_text", fake_generate_text)

    reply = ResponseGenerator().generate(
        message="你好",
        session=ChatSession(id="session_test", tenant_id="tenant_demo"),
        skill=None,
        router_decision=RouterDecision(decision="answer_only"),
        step_result=StepAgentResult(reply="你好"),
        tool_result=None,
        model_config=None,  # type: ignore[arg-type]
    )

    assert reply == "模型调用失败（LLM_ERROR）：upstream timeout。请检查模型配置、API Key、网络或模型服务状态后重试。"


def test_pending_reply_without_tool_result_uses_model_reply(monkeypatch):
    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_text(self, system_prompt, payload):  # noqa: ANN001
        return "好的，正在为您创建订单，请稍候..."

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_text", fake_generate_text)

    reply = ResponseGenerator().generate(
        message="一个",
        session=ChatSession(
            id="session_test",
            tenant_id="tenant_demo",
            last_agent_question="请问您想购买多少件？",
        ),
        skill=None,
        router_decision=RouterDecision(decision="continue_active"),
        step_result=StepAgentResult(reply="请补充完成当前步骤所需的信息。"),
        tool_result=None,
        model_config=None,  # type: ignore[arg-type]
    )

    assert reply == "好的，正在为您创建订单，请稍候..."


def test_pending_step_reply_without_tool_result_does_not_fall_back_to_last_question(monkeypatch):
    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_text(self, system_prompt, payload):  # noqa: ANN001
        return "正在处理，请稍等。"

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_text", fake_generate_text)

    reply = ResponseGenerator().generate(
        message="hm",
        session=ChatSession(
            id="session_test",
            tenant_id="tenant_demo",
            last_agent_question="请提供您的订单号。",
        ),
        skill=None,
        router_decision=RouterDecision(decision="continue_active"),
        step_result=StepAgentResult(reply="正在为您提交，请稍候。"),
        tool_result=None,
        model_config=None,  # type: ignore[arg-type]
    )

    assert reply == "正在处理，请稍等。"
    assert reply != "请提供您的订单号。"


def test_pending_phrase_in_confirmation_question_is_not_rejected(monkeypatch):
    step_reply = (
        "好的，已为您记录购买 1 个 A1 的意向。"
        "稍后我会为您处理 iPhone 15 的购买需求。"
        "请问确认为您创建 1 个 A1 的订单吗？"
    )

    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_text(self, system_prompt, payload):  # noqa: ANN001
        return step_reply

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_text", fake_generate_text)

    reply = ResponseGenerator().generate(
        message="嗯，我买一个A1吧，然后我还想再买一个iphone15",
        session=ChatSession(
            id="session_test",
            tenant_id="tenant_demo",
            active_skill_id="skill_purchase_001",
            active_step_id="confirm_purchase",
            slots_json={"user_name": "哈", "product_id": "A1", "quantity": 1},
            pending_tasks_json=[
                {
                    "decision": "start_new_task",
                    "target_skill_id": "skill_purchase_001",
                    "target_step_id": "collect_user_name",
                    "slot_hints": {"product_id": "iphone15", "quantity": 1},
                }
            ],
        ),
        skill=None,
        router_decision=RouterDecision(decision="continue_active"),
        step_result=StepAgentResult(reply=step_reply, next_step_id="confirm_purchase"),
        tool_result=None,
        model_config=None,  # type: ignore[arg-type]
    )

    assert reply == step_reply
    assert "具体诉求" not in reply


def test_response_payload_does_not_include_stale_last_question(monkeypatch):
    stale_price_reply = (
        "您好，已为您查询到 A1 和 A3 的价格信息：\n\n"
        "1. **A1 标准商品**：价格 **129.0 元**\n"
        "2. **A3 高阶商品**：价格 **239.0 元**\n\n"
        "请问您是否决定购买 A1？"
    )
    refund_reply = "好的，已为您记录退款申请。为了继续处理，请提供您的订单号。"

    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_text(self, system_prompt, payload):  # noqa: ANN001
        assert "session" not in payload
        assert payload["step_summary"]["reply"] == refund_reply
        return stale_price_reply

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_text", fake_generate_text)

    reply = ResponseGenerator().generate(
        message="确认退款",
        session=ChatSession(
            id="session_test",
            tenant_id="tenant_demo",
            active_skill_id="after_sales_refund",
            active_step_id="process_refund",
            last_agent_question=stale_price_reply,
        ),
        skill=None,
        router_decision=RouterDecision(decision="continue_active"),
        step_result=StepAgentResult(reply=refund_reply, is_step_completed=True),
        tool_result=None,
        model_config=None,  # type: ignore[arg-type]
    )

    assert reply == stale_price_reply


def test_stream_payload_does_not_include_stale_last_question(monkeypatch):
    stale_price_reply = "A1 和 A3 的比价结果如下。请问您是否决定购买 A1？"
    refund_reply = "好的，已为您记录退款申请。为了继续处理，请提供您的订单号。"

    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_text_stream(self, system_prompt, payload):  # noqa: ANN001
        assert "session" not in payload
        yield stale_price_reply[:12]
        yield stale_price_reply[12:]

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_text_stream", fake_generate_text_stream)

    chunks = list(
        ResponseGenerator().generate_stream(
            message="确认退款",
            session=ChatSession(
                id="session_test",
                tenant_id="tenant_demo",
                active_skill_id="after_sales_refund",
                active_step_id="process_refund",
                last_agent_question=stale_price_reply,
            ),
            skill=None,
            router_decision=RouterDecision(decision="continue_active"),
            step_result=StepAgentResult(reply=refund_reply, is_step_completed=True),
            tool_result=None,
            model_config=None,  # type: ignore[arg-type]
        )
    )

    reply = "".join(chunks)
    assert reply == stale_price_reply


def test_stream_reply_with_tool_result_is_model_driven(monkeypatch):
    stale_price_reply = "A1 和 A3 的比价结果如下。请问您是否决定购买 A1？"
    refund_reply = "订单 MOCKD57272DB0E 的退款申请已提交，当前状态为处理中。"

    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_text_stream(self, system_prompt, payload):  # noqa: ANN001
        assert payload["tool_result"]["tool_name"] == "order.refund"
        yield stale_price_reply[:12]
        yield stale_price_reply[12:]

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_text_stream", fake_generate_text_stream)

    chunks = list(
        ResponseGenerator().generate_stream(
            message="确认退款",
            session=ChatSession(
                id="session_test",
                tenant_id="tenant_demo",
                active_skill_id="after_sales_refund",
                active_step_id="process_refund",
                last_agent_question=stale_price_reply,
            ),
            skill=None,
            router_decision=RouterDecision(decision="continue_active"),
            step_result=StepAgentResult(reply=refund_reply, is_step_completed=True),
            tool_result=ToolResult(
                tool_name="order.refund",
                success=True,
                data={"order_id": "MOCKD57272DB0E", "refund_status": "processing"},
            ),
            model_config=None,  # type: ignore[arg-type]
        )
    )

    reply = "".join(chunks)
    assert reply == stale_price_reply


def test_stream_failed_tool_result_returns_explicit_failure_without_model_call(monkeypatch):
    def forbidden_generate_text_stream(self, system_prompt, payload):  # noqa: ANN001
        raise AssertionError("failed tool replies should not rely on model generation")

    monkeypatch.setattr(LLMClient, "generate_text_stream", forbidden_generate_text_stream)

    chunks = list(
        ResponseGenerator().generate_stream(
            message="查一下订单",
            session=ChatSession(id="session_test", tenant_id="tenant_demo"),
            skill=None,
            router_decision=RouterDecision(decision="continue_active"),
            step_result=StepAgentResult(),
            tool_result=ToolResult(
                tool_name="order.query",
                success=False,
                error=ToolError(code="TIMEOUT", message="工具调用超时。"),
            ),
            model_config=None,  # type: ignore[arg-type]
        )
    )

    assert "".join(chunks) == "工具调用失败：order.query（TIMEOUT）：工具调用超时。请检查工具配置、调用参数或外部服务状态后重试。"


def test_stream_model_failure_returns_explicit_reason(monkeypatch):
    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_text_stream(self, system_prompt, payload):  # noqa: ANN001
        raise RuntimeError("connection refused")
        yield ""  # pragma: no cover

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_text_stream", fake_generate_text_stream)

    chunks = list(
        ResponseGenerator().generate_stream(
            message="你好",
            session=ChatSession(id="session_test", tenant_id="tenant_demo"),
            skill=None,
            router_decision=RouterDecision(decision="answer_only"),
            step_result=StepAgentResult(reply="你好"),
            tool_result=None,
            model_config=None,  # type: ignore[arg-type]
        )
    )

    assert "".join(chunks) == "模型调用失败（LLM_ERROR）：connection refused。请检查模型配置、API Key、网络或模型服务状态后重试。"


def test_stream_pending_reply_without_tool_result_is_model_driven(monkeypatch):
    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_text_stream(self, system_prompt, payload):  # noqa: ANN001
        yield "好的，"
        yield "正在为您创建订单，请稍候..."

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_text_stream", fake_generate_text_stream)

    chunks = list(
        ResponseGenerator().generate_stream(
            message="一个",
            session=ChatSession(
                id="session_test",
                tenant_id="tenant_demo",
                last_agent_question="请问您想购买多少件？",
            ),
            skill=None,
            router_decision=RouterDecision(decision="continue_active"),
            step_result=StepAgentResult(reply="请补充完成当前步骤所需的信息。"),
            tool_result=None,
            model_config=None,  # type: ignore[arg-type]
        )
    )

    reply = "".join(chunks)
    assert reply == "好的，正在为您创建订单，请稍候..."


def test_stream_reply_yields_provider_chunks_without_collecting_first(monkeypatch):
    emitted: list[str] = []

    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_text_stream(self, system_prompt, payload):  # noqa: ANN001
        emitted.append("provider_started")
        yield "第一段"
        emitted.append("after_first_chunk")
        yield "第二段"

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_text_stream", fake_generate_text_stream)

    stream = ResponseGenerator().generate_stream(
        message="继续",
        session=ChatSession(id="session_test", tenant_id="tenant_demo"),
        skill=None,
        router_decision=RouterDecision(decision="answer_only"),
        step_result=StepAgentResult(),
        tool_result=None,
        model_config=None,  # type: ignore[arg-type]
    )

    assert next(stream) == "第一段"
    assert emitted == ["provider_started"]
    assert next(stream) == "第二段"
    assert emitted == ["provider_started", "after_first_chunk"]


def test_completed_step_reply_is_model_driven(monkeypatch):
    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_text(self, system_prompt, payload):  # noqa: ANN001
        assert payload["progress"]["missing_current_step_info"] == []
        assert payload["progress"]["missing_required_info"] == []
        assert payload["progress"]["skill_completion_ready"] is True
        return "请问您的退货原因是什么？"

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_text", fake_generate_text)

    reply = ResponseGenerator().generate(
        message="不喜欢",
        session=ChatSession(
            id="session_test",
            tenant_id="tenant_demo",
            active_skill_id="refund",
            active_step_id="collect_refund_reason",
            slots_json={"order_id": "A12345", "refund_reason": "不喜欢"},
            last_agent_question="请问您的退货原因是什么？",
        ),
        skill=Skill(
            tenant_id="tenant_demo",
            skill_id="refund",
            name="退款",
            status="published",
            content_json={
                "required_info": ["order_id", "refund_reason"],
                "steps": [
                    {
                        "step_id": "collect_refund_reason",
                        "expected_user_info": ["refund_reason"],
                        "allowed_actions": ["ask_user", "continue_flow"],
                    }
                ],
            },
        ),
        router_decision=RouterDecision(decision="continue_active"),
        step_result=StepAgentResult(
            reply="已记录退货原因，正在为您提交退货申请，请稍候。",
            is_step_completed=True,
            next_step_id="collect_refund_reason",
        ),
        tool_result=None,
        model_config=None,  # type: ignore[arg-type]
    )

    assert reply == "请问您的退货原因是什么？"


def test_knowledge_result_does_not_prefer_generic_step_reply(monkeypatch):
    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_text(self, system_prompt, payload):  # noqa: ANN001
        assert payload["retrieved_knowledge"]
        assert payload["knowledge_citation_hints"]
        assert "knowledge_results" not in payload["step_summary"]
        assert "slot_updates" not in payload["step_summary"]
        assert "active_skill" not in payload
        assert "router_decision" not in payload
        assert "session" not in payload
        assert "content" not in payload["knowledge_citation_hints"][0]
        assert len(
            payload["retrieved_knowledge"][0]["retrieved_knowledge"][0]["content"]
        ) <= 803
        return "前端规范包括目录组织、命名规范和组件编写规范。[1]"

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_text", fake_generate_text)

    reply = ResponseGenerator().generate(
        message="前端规范有哪些？",
        session=ChatSession(id="session_test", tenant_id="tenant_demo"),
        skill=None,
        router_decision=RouterDecision(decision="answer_only", user_intent="了解前端编码规范"),
        step_result=StepAgentResult(
            reply="请您再补充一下具体诉求，我会继续帮您处理。",
            knowledge_results=[
                {
                    "source_message": "前端规范有哪些？",
                    "evidence_pack": [
                        {
                            "source_path": "vue3-coding-standards.md / 前端编码规范 / evidence 1",
                            "excerpt": "前端规范包括目录组织、命名规范、组件编写规范。" * 200,
                            "reason": "命中前端规范问题",
                        }
                    ],
                }
            ],
        ),
        tool_result=None,
        model_config=None,  # type: ignore[arg-type]
    )

    assert reply == "前端规范包括目录组织、命名规范和组件编写规范。[1]"


def test_response_generator_skips_model_for_simple_step_question(monkeypatch) -> None:
    def fail_generate_text(*_args, **_kwargs):
        raise AssertionError("simple ask_user reply must not call the response model")

    monkeypatch.setattr("app.core.response_generator.LLMClient.generate_text", fail_generate_text)
    reply = ResponseGenerator().generate(
        "我要报销",
        ChatSession(id="session_test", tenant_id="tenant_demo"),
        None,
        RouterDecision(decision="continue_active"),
        StepAgentResult(action="ask_user", reply="请补充报销金额。"),
        None,
        model_config=None,  # type: ignore[arg-type]
    )

    assert reply == "请补充报销金额。"


def test_response_generator_stream_skips_model_for_simple_clarification(monkeypatch) -> None:
    def fail_generate_text_stream(*_args, **_kwargs):
        raise AssertionError("simple clarification must not call the response model")

    monkeypatch.setattr(
        "app.core.response_generator.LLMClient.generate_text_stream",
        fail_generate_text_stream,
    )
    chunks = list(
        ResponseGenerator().generate_stream(
            "我要办理业务",
            ChatSession(id="session_test", tenant_id="tenant_demo"),
            None,
            RouterDecision(decision="continue_active"),
            StepAgentResult(action="clarify", reply="请说明具体业务类型。"),
            None,
            model_config=None,  # type: ignore[arg-type]
        )
    )

    assert "".join(chunks) == "请说明具体业务类型。"


def test_response_prompt_preserves_previous_reply_for_format_followup() -> None:
    prompt = ResponseGenerator()._stage_payload(
        {
            "user_message": "你按照 markdown 语法返回",
            "conversation_context": {
                "messages": [
                    {"role": "user", "content": "返回随便一个网站"},
                    {"role": "assistant", "content": "随便给你一个网站：www.baidu.com"},
                    {"role": "user", "content": "你按照 markdown 语法返回"},
                ]
            },
        },
        None,
    )

    instructions = prompt["_agent_stage"]["instructions"]
    assert "最近一条 assistant 回复作为待处理内容" in instructions
    assert "[label](https://example.com)" in instructions
