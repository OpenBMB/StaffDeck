from app.core.router import Router
from app.db.models import ChatSession, Skill
from app.llm import LLMClient


def test_router_payload_exposes_step_details_and_allows_compound_interrupt(monkeypatch):
    captured = {}

    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_json(self, system_prompt, payload):  # noqa: ANN001
        captured["system_prompt"] = system_prompt
        captured["payload"] = payload
        purchase = next(item for item in payload["available_skills"] if item["skill_id"] == "purchase")
        assert purchase["required_info"] == ["user_name", "product_id", "quantity"]
        assert purchase["steps"][0]["instruction"] == "收集姓名、商品和数量。"
        assert purchase["steps"][0]["expected_user_info"] == ["user_name", "product_id", "quantity"]
        assert purchase["steps"][0]["allowed_actions"] == ["ask_user", "continue_flow"]
        assert "不要让原则10吞掉复合意图" in system_prompt
        assert "不要输出纯 clarify" in system_prompt
        assert "应放入 slot_hints" in system_prompt
        return {
            "decision": "answer_related_question_then_resume",
            "target_skill_id": "price_compare",
            "target_step_id": "collect_products",
            "confidence": 0.92,
            "user_intent": "购买前比价",
            "reason": "用户同时补充购买信息并提出购买前比价，应先回答比价再恢复购买流程。",
            "should_resume_after_answer": True,
            "clarification_question": "",
        }

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_json", fake_generate_json)

    decision = Router().decide(
        "我叫hm，我想买A1，但买之前我想先跟A3比个价",
        ChatSession(
            id="session_test",
            tenant_id="tenant_demo",
            active_skill_id="purchase",
            active_step_id="collect_user_name",
            slots_json={},
        ),
        [_purchase_skill(), _price_compare_skill()],
        model_config=None,  # type: ignore[arg-type]
        memory_context=[{"kind": "profile", "content": "用户姓名/称呼：hm"}],
    )

    assert decision.decision == "answer_related_question_then_resume"
    assert decision.target_skill_id == "price_compare"
    assert decision.target_step_id == "collect_products"
    assert captured["payload"]["current_session"]["active_skill_id"] == "purchase"
    assert captured["payload"]["memory_context"] == [{"kind": "profile", "content": "用户姓名/称呼：hm"}]


def test_router_accepts_ordered_pending_tasks(monkeypatch):
    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_json(self, system_prompt, payload):  # noqa: ANN001
        assert "pending_tasks" in system_prompt
        assert payload["current_session"]["active_skill_id"] == "refund"
        return {
            "decision": "continue_current_skill",
            "target_skill_id": "refund",
            "target_step_id": "confirm_refund_order",
            "confidence": 0.93,
            "user_intent": "确认当前退货，并在完成后购买 A3",
            "reason": "用户先确认当前退货，再提出后续购买任务。",
            "should_resume_after_answer": False,
            "clarification_question": "",
            "pending_tasks": [
                {
                    "decision": "start_skill",
                    "target_skill_id": "purchase",
                    "target_step_id": "",
                    "confidence": 0.9,
                    "user_intent": "购买 A3",
                    "reason": "用户说退完后想买一个 A3。",
                    "source_message": "退了吧，退完我想买一个a3",
                    "slot_hints": {"product_id": "A3", "quantity": 1},
                }
            ],
        }

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_json", fake_generate_json)

    decision = Router().decide(
        "退了吧，退完我想买一个a3",
        ChatSession(
            id="session_test",
            tenant_id="tenant_demo",
            active_skill_id="refund",
            active_step_id="confirm_refund_order",
            slots_json={"order_id": "O1", "refund_type": "退货"},
        ),
        [_refund_skill(), _purchase_skill()],
        model_config=None,  # type: ignore[arg-type]
    )

    assert decision.decision == "continue_current_skill"
    assert decision.target_skill_id == "refund"
    assert decision.pending_tasks[0].target_skill_id == "purchase"
    assert decision.pending_tasks[0].target_step_id == "collect_user_name"


def test_task_scheduler_selects_multiple_existing_task_frames(monkeypatch):
    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_json(self, system_prompt, payload):  # noqa: ANN001
        assert "task scheduler" in system_prompt
        assert payload["candidate_task_frames"][0]["task_id"] == "task_purchase_a3"
        assert payload["candidate_task_frames"][1]["task_id"] == "task_purchase_a1"
        return {
            "action": "run_tasks",
            "selected_task_ids": ["task_purchase_a3", "missing_task", "task_purchase_a1"],
            "confidence": 0.91,
            "reason": "当前任务完成后，用户原始消息包含后续购买 A3 和 A1。",
        }

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_json", fake_generate_json)

    decision = Router().schedule_tasks_after_completion(
        "退完帮我买一个 A3，再买一个 A1",
        ChatSession(
            id="session_test",
            tenant_id="tenant_demo",
            pending_tasks_json=[
                {
                    "task_id": "task_purchase_a3",
                    "skill_id": "purchase",
                    "target_skill_id": "purchase",
                    "step_id": "collect_user_name",
                    "target_step_id": "collect_user_name",
                    "slots": {"product_id": "A3", "quantity": 1},
                },
                {
                    "task_id": "task_purchase_a1",
                    "skill_id": "purchase",
                    "target_skill_id": "purchase",
                    "step_id": "collect_user_name",
                    "target_step_id": "collect_user_name",
                    "slots": {"product_id": "A1", "quantity": 1},
                }
            ],
        ),
        [_purchase_skill()],
        model_config=None,  # type: ignore[arg-type]
        completed_reply="退款已完成。",
    )

    assert decision.action == "run_tasks"
    assert decision.selected_task_ids == ["task_purchase_a3", "task_purchase_a1"]


def test_router_coerces_answer_alias_before_schema_validation(monkeypatch):
    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_json(self, system_prompt, payload):  # noqa: ANN001
        return {
            "decision": "answer",
            "confidence": 0.8,
            "user_intent": "闲聊问候",
            "reason": "用户只是问候。",
        }

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_json", fake_generate_json)

    decision = Router().decide(
        "你好啊",
        ChatSession(id="session_test", tenant_id="tenant_demo"),
        [_purchase_skill()],
        model_config=None,  # type: ignore[arg-type]
    )

    assert decision.decision == "answer_only"


def test_router_downgrades_unknown_decision_to_clarify(monkeypatch):
    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_json(self, system_prompt, payload):  # noqa: ANN001
        return {
            "decision": "respond_to_user",
            "confidence": 0.2,
            "user_intent": "未知",
        }

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_json", fake_generate_json)

    decision = Router().decide(
        "你好啊",
        ChatSession(id="session_test", tenant_id="tenant_demo"),
        [_purchase_skill()],
        model_config=None,  # type: ignore[arg-type]
    )

    assert decision.decision == "clarify"
    assert decision.clarification_question


def test_router_removes_hallucinated_target_skill_from_non_matching_flow(monkeypatch):
    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_json(self, system_prompt, payload):  # noqa: ANN001
        assert "不要编造 target_skill_id" in system_prompt
        return {
            "decision": "clarify",
            "target_skill_id": "skill_weather_query",
            "target_step_id": "step_query_weather",
            "confidence": 0.85,
            "user_intent": "查询海淀区天气",
            "reason": "模型错误地假设存在天气流程。",
        }

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_json", fake_generate_json)

    decision = Router().decide(
        "我想看下海淀区的天气",
        ChatSession(id="session_test", tenant_id="tenant_demo"),
        [_purchase_skill()],
        model_config=None,  # type: ignore[arg-type]
    )

    assert decision.decision == "clarify"
    assert decision.target_skill_id is None
    assert decision.target_step_id is None


def _purchase_skill() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="purchase",
        name="购买商品流程",
        description="帮助用户购买商品。",
        status="published",
        content_json={
            "business_domain": "commerce",
            "trigger_intents": ["购买", "下单"],
            "required_info": ["user_name", "product_id", "quantity"],
            "steps": [
                {
                    "step_id": "collect_user_name",
                    "name": "收集用户信息与商品详情",
                    "instruction": "收集姓名、商品和数量。",
                    "expected_user_info": ["user_name", "product_id", "quantity"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                }
            ],
        },
    )


def _price_compare_skill() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="price_compare",
        name="商品比价服务",
        description="比较两个商品的价格。",
        status="published",
        content_json={
            "business_domain": "commerce",
            "trigger_intents": ["比价", "价格对比"],
            "required_info": ["product_name_1", "product_name_2"],
            "steps": [
                {
                    "step_id": "collect_products",
                    "name": "收集待比价商品",
                    "instruction": "收集两个商品名。",
                    "expected_user_info": ["product_name_1", "product_name_2"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                }
            ],
        },
    )


def _refund_skill() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="refund",
        name="售后退款流程",
        description="处理退货退款。",
        status="published",
        content_json={
            "business_domain": "after_sales",
            "trigger_intents": ["退货", "退款"],
            "required_info": ["order_id"],
            "steps": [
                {
                    "step_id": "confirm_refund_order",
                    "name": "确认售后订单",
                    "instruction": "确认订单后继续。",
                    "expected_user_info": ["order_confirmed"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                }
            ],
        },
    )
