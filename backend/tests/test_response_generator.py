from app.core.response_generator import ResponseGenerator
from app.db.models import ChatSession
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


def test_pending_reply_without_tool_result_falls_back_to_step_reply(monkeypatch):
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

    assert reply == "请补充完成当前步骤所需的信息。"
    assert "稍候" not in reply
    assert "正在为您" not in reply


def test_pending_step_reply_without_tool_result_falls_back_to_last_question(monkeypatch):
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

    assert reply == "请提供您的订单号。"


def test_stream_pending_reply_without_tool_result_is_not_emitted(monkeypatch):
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
    assert reply == "请补充完成当前步骤所需的信息。"
    assert "稍候" not in reply
    assert "正在为您" not in reply
