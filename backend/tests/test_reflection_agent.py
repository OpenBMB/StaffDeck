from app.core.engine.deprecated.reflection_agent import ReflectionAgent
from app.db.models import ChatSession, Skill
from app.llm import LLMClient
from app.session.session_schema import AwaitingInput, RouterDecision, StepAgentResult


def test_reflection_payload_only_contains_current_rules_and_execution_result(
    monkeypatch,
) -> None:
    captured = {}

    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_json(self, system_prompt, payload):  # noqa: ANN001
        captured["payload"] = payload
        return {"action": "pass", "needs_retry": False}

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_json", fake_generate_json)

    skill = Skill(
        tenant_id="tenant_demo",
        skill_id="refund",
        name="退款",
        status="published",
        content_json={
            "response_rules": ["必须明确说明退款结果"],
            "nodes": [
                {
                    "node_id": "process_refund",
                    "type": "tool",
                    "name": "处理退款",
                    "instruction": "确认工具结果后再完成。",
                    "allowed_actions": ["call_tool:refund.apply"],
                },
                {"node_id": "unrelated", "instruction": "不应投影"},
            ],
        },
    )
    decision = RouterDecision(
        decision="continue_active",
        target_skill_id="refund",
        target_step_id="process_refund",
        user_intent="申请退款",
        source_message="我要退款",
        awaiting_input=AwaitingInput(
            skill_id="refund", step_id="process_refund", expected_fields=["order_id"]
        ),
    )

    ReflectionAgent().review(
        "我要退款",
        ChatSession(
            id="session_test",
            tenant_id="tenant_demo",
            agent_id="agent_test",
            active_skill_id="refund",
            active_step_id="process_refund",
            slots_json={"order_id": "A001"},
            pending_tasks_json=[{"task_id": "task_1"}],
        ),
        skill,
        decision,
        StepAgentResult(reply="退款已处理", is_step_completed=True),
        None,
        [skill],
        [],
        model_config=None,  # type: ignore[arg-type]
    )

    payload = captured["payload"]
    assert payload["current_step"]["instruction"] == "确认工具结果后再完成。"
    assert payload["rules"] == {"response_rules": ["必须明确说明退款结果"]}
    assert payload["slots"] == {"order_id": "A001"}
    assert payload["step_result"]["reply"] == "退款已处理"
    assert "source_message" not in payload["router_decision"]
    assert "awaiting_input" not in payload["router_decision"]
    assert "current_session" not in payload
    assert "available_skills" not in payload
    assert "available_tools" not in payload
    assert "unrelated" not in str(payload)
    assert "session_test" not in str(payload)
    assert "agent_test" not in str(payload)
