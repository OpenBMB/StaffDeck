from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.chat import _active_skill_for_assistant_message
from app.api.skills import _skill_stats, list_skill_versions, skill_read
from app.db.models import AgentEvent, Message, Skill, SkillFeedback, Tenant
from app.db.models import ModelConfig
from app.skills.skill_distiller import SkillDistiller
from app.skills.skill_editor import SkillEditor
from app.skills.skill_schema import SkillCard, SkillDistillRequest, SkillRewriteRequest
from app.security.encryption import encrypt_secret


def test_skill_editor_only_merges_selected_step() -> None:
    current = _skill_card()
    candidate = _skill_card()
    candidate.name = "不应修改基础信息"
    candidate.steps[0].instruction = "新的收集说明"
    candidate.steps[1].instruction = "不应修改其他步骤"

    response = SkillEditor()._normalize_response(  # noqa: SLF001
        {
            "assistant_message": "已改写步骤。",
            "draft_skill": candidate.model_dump(),
            "changed_paths": ["steps.collect_info"],
        },
        SkillRewriteRequest(
            tenant_id="tenant_demo",
            current_skill=current,
            instruction="只优化第一步",
            target_path="steps.collect_info",
            target_label="步骤 1",
        ),
    )

    assert response.draft_skill.name == current.name
    assert response.draft_skill.steps[0].instruction == "新的收集说明"
    assert response.draft_skill.steps[1].instruction == current.steps[1].instruction


def test_skill_editor_merges_multiple_selected_targets() -> None:
    current = _skill_card()
    candidate = _skill_card()
    candidate.description = "新的描述"
    candidate.steps[0].instruction = "新的收集说明"
    candidate.steps[1].instruction = "不应修改第二步"

    response = SkillEditor()._normalize_response(  # noqa: SLF001
        {
            "assistant_message": "已改写多个区域。",
            "draft_skill": candidate.model_dump(),
        },
        SkillRewriteRequest(
            tenant_id="tenant_demo",
            current_skill=current,
            instruction="优化基础信息和第一步",
            target_path="basic",
            target_paths=["basic", "steps.collect_info"],
            target_label="基础信息、步骤 1",
        ),
    )

    assert response.draft_skill.description == "新的描述"
    assert response.draft_skill.steps[0].instruction == "新的收集说明"
    assert response.draft_skill.steps[1].instruction == current.steps[1].instruction


def test_skill_editor_can_target_duplicate_step_ids_by_index() -> None:
    current = _skill_card()
    current.steps[1].step_id = "collect_info"
    candidate = _skill_card()
    candidate.steps[0].instruction = "不应修改第一步"
    candidate.steps[1].step_id = "collect_info"
    candidate.steps[1].instruction = "只修改第二个重复 step"

    response = SkillEditor()._normalize_response(  # noqa: SLF001
        {
            "assistant_message": "已改写指定下标步骤。",
            "draft_skill": candidate.model_dump(),
        },
        SkillRewriteRequest(
            tenant_id="tenant_demo",
            current_skill=current,
            instruction="只改第二个重复步骤",
            target_path="steps[1]",
            target_paths=["steps[1]"],
            target_label="步骤 2",
        ),
    )

    assert response.draft_skill.steps[0].instruction == current.steps[0].instruction
    assert response.draft_skill.steps[1].instruction == "只修改第二个重复 step"


def test_skill_editor_merges_selected_step_id_change() -> None:
    current = _skill_card()
    current.steps[1].step_id = "create_order"
    candidate = _skill_card()
    candidate.steps[1].step_id = "feedback_order_result"

    response = SkillEditor()._normalize_response(  # noqa: SLF001
        {
            "assistant_message": "已修正步骤 ID。",
            "draft_skill": candidate.model_dump(),
        },
        SkillRewriteRequest(
            tenant_id="tenant_demo",
            current_skill=current,
            instruction="把反馈订单结果的 step_id 从 create_order 改成 feedback_order_result",
            target_path="steps[1]",
            target_paths=["steps[1]"],
            target_label="步骤 2：反馈结果",
        ),
    )

    assert response.draft_skill.steps[0].step_id == current.steps[0].step_id
    assert response.draft_skill.steps[1].step_id == "feedback_order_result"


def test_skill_stats_counts_skill_entry_and_feedback() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        content = _skill_card()
        db.add(
            Skill(
                tenant_id="tenant_demo",
                skill_id="purchase",
                version="1.5.0",
                name="购买商品",
                content_json=content.model_dump(),
                status="published",
            )
        )
        db.add(
            AgentEvent(
                tenant_id="tenant_demo",
                session_id="session_1",
                event_type="skill_started",
                payload_json={"to_skill_id": "purchase", "to_skill_version": "1.5.0"},
            )
        )
        db.add(
            SkillFeedback(
                tenant_id="tenant_demo",
                skill_id="purchase",
                skill_version="1.5.0",
                session_id="session_1",
                message_id="msg_1",
                user_id="user_1",
                rating="up",
            )
        )
        db.add(
            SkillFeedback(
                tenant_id="tenant_demo",
                skill_id="purchase",
                skill_version="1.5.0",
                session_id="session_2",
                message_id="msg_2",
                user_id="user_2",
                rating="down",
            )
        )
        db.commit()

        stats = _skill_stats(db, "tenant_demo")

    assert stats["purchase"]["call_count"] == 1
    assert stats["purchase"]["positive_feedback_count"] == 1
    assert stats["purchase"]["negative_feedback_count"] == 1
    assert stats["purchase"]["positive_rate"] == 0.5
    assert stats["purchase"]["negative_rate"] == 0.5
    assert stats["purchase@1.5.0"]["call_count"] == 1
    assert stats["purchase@1.5.0"]["positive_feedback_count"] == 1
    assert stats["purchase@1.5.0"]["negative_feedback_count"] == 1


def test_skill_versions_are_snapshotted_with_version_stats() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        content = _skill_card()
        row = Skill(
            tenant_id="tenant_demo",
            skill_id=content.skill_id,
            version="1.5.0",
            name=content.name,
            content_json=content.model_dump(),
            status="draft",
        )
        db.add(row)
        db.add(
            AgentEvent(
                tenant_id="tenant_demo",
                session_id="session_1",
                event_type="skill_started",
                payload_json={"to_skill_id": content.skill_id, "to_skill_version": "1.5.0"},
            )
        )
        db.commit()

        versions = list_skill_versions(content.skill_id, "tenant_demo", db)

    assert versions[0].version == "1.5.0"
    assert versions[0].call_count == 1


def test_skill_read_uses_aggregate_stats_for_skill_list() -> None:
    content = _skill_card()
    row = Skill(
        tenant_id="tenant_demo",
        skill_id="purchase",
        version="1.5.0",
        name="购买商品",
        content_json=content.model_dump(),
        status="published",
    )
    payload = skill_read(
        row,
        {
            "purchase": {
                "call_count": 3,
                "positive_feedback_count": 2,
                "negative_feedback_count": 1,
                "positive_rate": 0.6667,
                "negative_rate": 0.3333,
            },
            "purchase@1.5.0": {
                "call_count": 1,
                "positive_feedback_count": 0,
                "negative_feedback_count": 0,
                "positive_rate": 0.0,
                "negative_rate": 0.0,
            },
        },
    )

    assert payload.call_count == 3
    assert payload.positive_feedback_count == 2
    assert payload.negative_feedback_count == 1


def test_message_feedback_attribution_uses_turn_active_skill() -> None:
    with _test_session() as db:
        db.add(Message(id="msg_user", tenant_id="tenant_demo", session_id="session_1", role="user", content="我要退款"))
        assistant = Message(
            id="msg_assistant",
            tenant_id="tenant_demo",
            session_id="session_1",
            role="assistant",
            content="请提供订单号。",
        )
        db.add(assistant)
        db.add(
            AgentEvent(
                tenant_id="tenant_demo",
                session_id="session_1",
                event_type="user_message_received",
                payload_json={"message": "我要退款"},
            )
        )
        db.add(
            AgentEvent(
                tenant_id="tenant_demo",
                session_id="session_1",
                event_type="skill_started",
                payload_json={"to_skill_id": "refund"},
            )
        )
        db.add(
            AgentEvent(
                tenant_id="tenant_demo",
                session_id="session_1",
                event_type="assistant_message_created",
                payload_json={"reply": "请提供订单号。"},
            )
        )
        db.commit()

        skill_id = _active_skill_for_assistant_message(db, "tenant_demo", assistant)

    assert skill_id == "refund"


def test_skill_read_normalizes_duplicate_step_ids() -> None:
    content = _skill_card()
    content.steps[1].step_id = content.steps[0].step_id
    row = Skill(
        tenant_id="tenant_demo",
        skill_id=content.skill_id,
        name=content.name,
        content_json=content.model_dump(),
        status="draft",
    )

    payload = skill_read(row)
    step_ids = [step.step_id for step in payload.content.steps]

    assert step_ids == ["collect_info", "collect_info_2"]


def test_skill_distiller_stream_uses_generation_status(monkeypatch) -> None:
    def fake_stream(self, _system_prompt: str, _payload: dict):  # noqa: ANN001
        yield """
        {
          "draft_skill": {
            "skill_id": "skill_compare_price",
            "name": "商品比价",
            "version": "1.0.0",
            "business_domain": "ecommerce",
            "description": "比较两个商品价格。",
            "trigger_intents": ["compare_price"],
            "user_utterance_examples": ["比较 A 和 B"],
            "goal": ["收集两个商品名称", "反馈比价结果"],
            "required_info": ["product_name_1", "product_name_2"],
            "slot_filling_policy": {
              "enabled": true,
              "multi_slot_per_turn": true,
              "extract_scope": "all_skill_expected_user_info",
              "skip_satisfied_steps": true,
              "target_info": ["product_name_1", "product_name_2"]
            },
            "steps": [
              {
                "step_id": "collect_names",
                "name": "收集商品名称",
                "instruction": "收集两个商品名称。",
                "expected_user_info": ["product_name_1", "product_name_2"],
                "allowed_actions": ["ask_user"]
              },
              {
                "step_id": "reply_result",
                "name": "反馈结果",
                "instruction": "反馈明确结果。",
                "expected_user_info": [],
                "allowed_actions": ["answer_user"]
              }
            ],
            "interruption_policy": {},
            "response_rules": []
          },
          "warnings": []
        }
        """

    monkeypatch.setattr("app.skills.skill_distiller.LLMClient.generate_text_stream", fake_stream)
    events = list(
        SkillDistiller().stream_text(
            SkillDistillRequest(
                tenant_id="tenant_demo",
                title="商品比价",
                raw_content="用户提供两个商品的名称，系统根据商品价格进行比价",
            ),
            _model_config(),
        )
    )
    status_texts = [event["data"]["text"] for event in events if event["event"] == "status"]

    assert "正在改写技能" not in status_texts
    assert "模型正在规划技能结构" in status_texts
    assert "正在校验模型输出结构" in status_texts
    assert "已完成 Skill Card 结构化" in status_texts


def _skill_card() -> SkillCard:
    return SkillCard(
        skill_id="purchase",
        name="购买商品",
        version="1.0.0",
        business_domain="commerce",
        description="购买流程",
        trigger_intents=["购买"],
        user_utterance_examples=["我要买 A1"],
        goal=["完成下单"],
        required_info=["product_id"],
        steps=[
            {
                "step_id": "collect_info",
                "name": "收集信息",
                "instruction": "收集商品信息",
                "expected_user_info": ["product_id"],
                "allowed_actions": ["ask_user", "continue_flow"],
            },
            {
                "step_id": "reply_result",
                "name": "反馈结果",
                "instruction": "反馈订单结果",
                "expected_user_info": [],
                "allowed_actions": ["answer_user"],
            },
        ],
        interruption_policy={},
        response_rules=[],
    )


def _model_config() -> ModelConfig:
    return ModelConfig(
        tenant_id="tenant_demo",
        name="mock",
        api_key_encrypted=encrypt_secret("mock"),
        model="mock-model",
    )


def _test_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
