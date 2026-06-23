from app.core.response_generator import ResponseGenerator
from app.db.models import ChatSession, Skill
from app.llm.client import LLMClient
from app.session.session_schema import RouterDecision, StepAgentResult
from app.tools.tool_schema import ToolResult


def test_clarify_does_not_leak_internal_router_prompt():
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
        router_decision=RouterDecision(decision="continue_current_skill"),
        step_result=StepAgentResult(),
        tool_result=ToolResult(
            tool_name="ticket.create",
            success=True,
            data={"ticket_id": "T-100", "status": "created"},
        ),
        model_config=None,  # type: ignore[arg-type]
    )

    assert reply == "已创建报修工单 T-100，工程师会尽快联系您。"


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
        router_decision=RouterDecision(decision="continue_current_skill"),
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
        router_decision=RouterDecision(decision="continue_current_skill"),
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
                    "decision": "start_skill",
                    "target_skill_id": "skill_purchase_001",
                    "target_step_id": "collect_user_name",
                    "slot_hints": {"product_id": "iphone15", "quantity": 1},
                }
            ],
        ),
        skill=None,
        router_decision=RouterDecision(decision="continue_current_skill"),
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
        assert "last_agent_question" not in payload["session"]
        assert payload["step_result"]["reply"] == refund_reply
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
        router_decision=RouterDecision(decision="continue_current_skill"),
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
        assert "last_agent_question" not in payload["session"]
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
            router_decision=RouterDecision(decision="continue_current_skill"),
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
            router_decision=RouterDecision(decision="continue_current_skill"),
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
            router_decision=RouterDecision(decision="continue_current_skill"),
            step_result=StepAgentResult(reply="请补充完成当前步骤所需的信息。"),
            tool_result=None,
            model_config=None,  # type: ignore[arg-type]
        )
    )

    reply = "".join(chunks)
    assert reply == "好的，正在为您创建订单，请稍候..."


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
        router_decision=RouterDecision(decision="continue_current_skill"),
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
        assert payload["session"]["knowledge_context"]
        assert payload["knowledge_citation_hints"]
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
                            "excerpt": "前端规范包括目录组织、命名规范、组件编写规范。",
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
