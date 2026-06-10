from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.general_skills import import_general_skill, list_general_skills
from app.core import AgentLoop
from app.db.models import AgentEvent, GeneralSkill, ModelConfig, Skill, Tenant, User
from app.general_skills.runner import GeneralSkillRunner
from app.general_skills.schema import GeneralSkillImportRequest
from app.llm import LLMClient, LLMError
from app.security.auth import hash_password
from app.security.encryption import encrypt_secret
from app.session.session_schema import ChatTurnRequest


WEATHER_SKILL_MD = """# 中国城市天气查询工具

python weather.py -json -today <地区名称>
"""


def test_import_general_skill_uses_user_supplied_metadata() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)

        first = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="用户填写天气技能",
                slug="weather-zh",
                description="用户填写描述",
                homepage="https://example.com/weather",
                markdown=WEATHER_SKILL_MD,
            ),
            db,
        )
        second = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="用户改名天气技能",
                slug="weather-cn",
                description="用户改写描述",
                homepage="https://example.com/weather-cn",
                original_slug="weather-zh",
                markdown=WEATHER_SKILL_MD.replace("中国城市天气查询工具", "天气 demo"),
            ),
            db,
        )

        rows = list_general_skills("tenant_demo", db)
        assert first.id == second.id
        assert len(rows) == 1
        assert rows[0].slug == "weather-cn"
        assert rows[0].name == "用户改名天气技能"
        assert rows[0].description == "用户改写描述"
        assert rows[0].homepage == "https://example.com/weather-cn"
        assert rows[0].skill_markdown.startswith("# 天气 demo")


def test_chat_turn_treats_unmatched_scene_request_as_chat_not_general_skill(monkeypatch) -> None:
    calls: list[str] = []

    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_json(self, system_prompt, payload):  # noqa: ANN001
        prompt_text = str(system_prompt)
        if "企业技能路由器" in prompt_text:
            calls.append("router")
            return {
                "decision": "clarify",
                "target_skill_id": "skill_weather_query",
                "target_step_id": "step_query_weather",
                "confidence": 0.85,
                "user_intent": "查询海淀区天气",
                "reason": "模型错误地假设存在天气流程。",
            }
        if "通用技能选择器" in prompt_text:
            raise AssertionError("general skill selector should not run after unmatched scene routing")
        if "通用技能执行器" in prompt_text or "通用技能结果回复器" in prompt_text:
            raise AssertionError("general skill runner should not run after unmatched scene routing")
        if "企业技能执行助手" in prompt_text:
            raise AssertionError("step agent should not run without an active scene skill")
        raise AssertionError("unexpected JSON prompt")

    def fake_generate_text(self, system_prompt, payload):  # noqa: ANN001
        calls.append("response")
        assert payload["active_skill"] is None
        assert payload["router_decision"]["decision"] == "clarify"
        assert payload["router_decision"]["target_skill_id"] is None
        assert payload["tool_result"] is None
        return "我这里没有天气查询流程，无法为你实时查询天气。"

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_json", fake_generate_json)
    monkeypatch.setattr(LLMClient, "generate_text", fake_generate_text)

    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(_purchase_scene_skill())
        db.add(
            GeneralSkill(
                tenant_id="tenant_demo",
                slug="weather-zh",
                name="中国城市天气",
                description="中国城市天气查询工具",
                homepage="https://www.weather.com.cn/",
                skill_markdown=WEATHER_SKILL_MD,
                status="published",
            )
        )
        db.commit()

        response = AgentLoop(db).handle_turn(
            ChatTurnRequest(
                tenant_id="tenant_demo",
                user_id="user_demo",
                message="我想看下海淀区的天气",
            )
        )

        assert response.reply == "我这里没有天气查询流程，无法为你实时查询天气。"
        assert calls == ["router", "response"]
        assert response.router_decision is not None
        assert response.router_decision.target_skill_id is None
        events = db.exec(select(AgentEvent).where(AgentEvent.session_id == response.session_id)).all()
        event_types = {event.event_type for event in events}
        assert "general_skill_selected" not in event_types
        assert "tool_call_started" not in event_types
        assert "step_agent_result_created" not in event_types


def test_general_skill_runner_repairs_failed_code(monkeypatch) -> None:
    calls: list[str] = []

    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_json(self, system_prompt, payload):  # noqa: ANN001
        prompt_text = str(system_prompt)
        if "代码修复器" in prompt_text:
            calls.append("repair")
            return {
                "code": (
                    "import json\n"
                    "payload=json.loads(input())\n"
                    "print(json.dumps({'success': True, 'city': '北京', 'weather': '晴', 'query': payload['query']}, ensure_ascii=False))\n"
                ),
                "rationale": "修复失败输出",
            }
        if "通用技能执行器" in prompt_text:
            calls.append("runner")
            return {
                "code": "import json\nprint(json.dumps({'success': False, 'error': 'first_fail'}, ensure_ascii=False))\n",
                "rationale": "首次尝试失败",
            }
        if "通用技能结果回复器" in prompt_text:
            calls.append("reply")
            assert payload["structured_result"]["success"] is True
            return {"reply": "北京今天晴。"}
        raise AssertionError("unexpected prompt")

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_json", fake_generate_json)

    skill = GeneralSkill(
        tenant_id="tenant_demo",
        slug="weather-zh",
        name="中国城市天气",
        description="中国城市天气查询工具",
        homepage="https://www.weather.com.cn/",
        skill_markdown=WEATHER_SKILL_MD,
        status="published",
    )
    model_config = ModelConfig(
        tenant_id="tenant_demo",
        name="Fake model",
        api_key_encrypted=encrypt_secret("test-key"),
        model="fake",
        is_default=True,
        enabled=True,
    )

    events: list[dict] = []

    response = GeneralSkillRunner().run(skill, "北京今天天气怎么样", model_config, max_attempts=2, event_sink=events.append)

    assert response.reply == "北京今天晴。"
    assert response.structured_result["success"] is True
    assert calls == ["runner", "repair", "reply"]
    assert any(item["phase"] == "reflection_retrying" for item in response.execution_trace)
    assert any(item["phase"] == "stdout_chunk" and "first_fail" in item["text"] for item in events)


def test_general_skill_runner_reflects_failed_initial_plan(monkeypatch) -> None:
    calls: list[str] = []

    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_json(self, system_prompt, payload):  # noqa: ANN001
        prompt_text = str(system_prompt)
        if "代码修复器" in prompt_text:
            calls.append("repair")
            assert payload["previous_attempts"][0]["structured_result"]["error"] == "plan_generation_failed"
            return {
                "code": (
                    "import json\n"
                    "payload=json.loads(input())\n"
                    "print(json.dumps({'success': True, 'city': '廊坊', 'weather': '多云', 'query': payload['query']}, ensure_ascii=False))\n"
                ),
                "rationale": "重新输出合法 runner JSON",
            }
        if "通用技能执行器" in prompt_text:
            calls.append("runner_failed")
            raise LLMError("Model did not return valid JSON after retry")
        if "通用技能结果回复器" in prompt_text:
            calls.append("reply")
            assert payload["structured_result"]["success"] is True
            return {"reply": "廊坊今天多云。"}
        raise AssertionError("unexpected prompt")

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_json", fake_generate_json)

    skill = GeneralSkill(
        tenant_id="tenant_demo",
        slug="weather-zh",
        name="中国城市天气",
        description="中国城市天气查询工具",
        homepage="https://www.weather.com.cn/",
        skill_markdown=WEATHER_SKILL_MD,
        status="published",
    )
    model_config = ModelConfig(
        tenant_id="tenant_demo",
        name="Fake model",
        api_key_encrypted=encrypt_secret("test-key"),
        model="fake",
        is_default=True,
        enabled=True,
    )

    response = GeneralSkillRunner().run(skill, "廊坊天气", model_config, max_attempts=2)

    assert response.reply == "廊坊今天多云。"
    assert response.structured_result["success"] is True
    assert calls == ["runner_failed", "repair", "reply"]
    assert any(item["phase"] == "plan_failed" for item in response.execution_trace)
    assert any(item["phase"] == "reflection_retrying" for item in response.execution_trace)


def test_general_skill_runner_stops_on_non_retryable_failure(monkeypatch) -> None:
    calls: list[str] = []

    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_json(self, system_prompt, payload):  # noqa: ANN001
        prompt_text = str(system_prompt)
        if "通用技能执行器" in prompt_text:
            calls.append("runner")
            return {
                "code": (
                    "import json\n"
                    "print(json.dumps({"
                    "'success': False, "
                    "'error': 'source_unavailable', "
                    "'message': '天气源不可用', "
                    "'attempted_urls': ['https://example.invalid/weather'], "
                    "'exception_type': 'TimeoutError', "
                    "'exception_message': 'timed out', "
                    "'retryable': False"
                    "}, ensure_ascii=False))\n"
                ),
                "rationale": "返回不可自动修复的失败",
            }
        if "代码修复器" in prompt_text:
            calls.append("repair")
            raise AssertionError("non-retryable failure should not call repair")
        if "通用技能结果回复器" in prompt_text:
            calls.append("reply")
            assert payload["structured_result"]["retryable"] is False
            return {"reply": "当前天气源不可用，建议稍后再试。"}
        raise AssertionError("unexpected prompt")

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_json", fake_generate_json)

    skill = GeneralSkill(
        tenant_id="tenant_demo",
        slug="weather-zh",
        name="中国城市天气",
        description="中国城市天气查询工具",
        homepage="https://www.weather.com.cn/",
        skill_markdown=WEATHER_SKILL_MD,
        status="published",
    )
    model_config = ModelConfig(
        tenant_id="tenant_demo",
        name="Fake model",
        api_key_encrypted=encrypt_secret("test-key"),
        model="fake",
        is_default=True,
        enabled=True,
    )

    response = GeneralSkillRunner().run(skill, "北京今天天气怎么样", model_config, max_attempts=10)

    assert response.reply == "当前天气源不可用，建议稍后再试。"
    assert calls == ["runner", "reply"]
    assert any(item["phase"] == "reflection_stopped" for item in response.execution_trace)


def _seed_minimal_tenant(db: Session) -> None:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(
        User(
            id="user_demo",
            tenant_id="tenant_demo",
            username="user_demo",
            password_hash=hash_password("demo"),
        )
    )
    db.add(
        ModelConfig(
            tenant_id="tenant_demo",
            name="Fake model",
            api_key_encrypted=encrypt_secret("test-key"),
            model="fake",
            is_default=True,
            enabled=True,
        )
    )
    db.commit()


def _purchase_scene_skill() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="purchase",
        name="购买商品流程",
        description="帮助用户购买商品。",
        status="published",
        content_json={
            "business_domain": "commerce",
            "trigger_intents": ["购买", "下单"],
            "required_info": ["product_id"],
            "steps": [
                {
                    "step_id": "collect_product",
                    "name": "收集商品信息",
                    "instruction": "收集用户想购买的商品。",
                    "expected_user_info": ["product_id"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                }
            ],
        },
    )


def _test_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
