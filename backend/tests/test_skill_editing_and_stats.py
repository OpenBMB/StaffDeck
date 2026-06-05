import json
from io import BytesIO
from zipfile import ZipFile

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.chat import _active_skill_context_for_assistant_message, _active_skill_for_assistant_message
from app.api.skills import (
    _extract_uploaded_skill_file,
    _skill_stats,
    list_skill_versions,
    list_skills,
    rollback_skill_version,
    skill_read,
)
from app.db.models import AgentEvent, Message, Skill, SkillFeedback, SkillVersion, Tenant
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


def test_skill_editor_allows_selected_step_deletion() -> None:
    current = _skill_card()
    candidate_data = current.model_dump(mode="json")
    candidate_data["steps"] = candidate_data["steps"][:1]
    candidate = SkillCard.model_validate(candidate_data)

    response = SkillEditor()._normalize_response(  # noqa: SLF001
        {
            "assistant_message": "已删除反馈步骤。",
            "draft_skill": candidate.model_dump(),
        },
        SkillRewriteRequest(
            tenant_id="tenant_demo",
            current_skill=current,
            instruction="删除第二步",
            target_path="steps[1]",
            target_paths=["steps[1]"],
            target_label="步骤 2",
        ),
    )

    assert [step.step_id for step in response.draft_skill.steps] == ["collect_info"]


def test_skill_editor_allows_selected_step_insertion() -> None:
    current = _skill_card()
    candidate_data = current.model_dump(mode="json")
    candidate_data["steps"].insert(
        1,
        {
            "step_id": "confirm_purchase",
            "name": "确认购买信息",
            "instruction": "向用户确认商品和数量。",
            "expected_user_info": ["purchase_confirmed"],
            "allowed_actions": ["ask_user", "continue_flow"],
        },
    )
    candidate = SkillCard.model_validate(candidate_data)

    response = SkillEditor()._normalize_response(  # noqa: SLF001
        {
            "assistant_message": "已新增确认步骤。",
            "draft_skill": candidate.model_dump(),
        },
        SkillRewriteRequest(
            tenant_id="tenant_demo",
            current_skill=current,
            instruction="在第一步后新增确认步骤",
            target_path="steps[0]",
            target_paths=["steps[0]"],
            target_label="步骤 1",
        ),
    )

    assert [step.step_id for step in response.draft_skill.steps] == [
        "collect_info",
        "confirm_purchase",
        "reply_result",
    ]
    assert response.draft_skill.name == current.name


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


def test_skill_editor_applies_step_id_corrections_to_final_draft() -> None:
    current = _skill_card()
    current.steps[0].step_id = "collect_info"
    current.steps[1].step_id = "reply_result"
    candidate = _skill_card()
    candidate.steps[1].step_id = "collect_info"

    response = SkillEditor()._normalize_response(  # noqa: SLF001
        {
            "assistant_message": "已修正步骤 ID。",
            "draft_skill": candidate.model_dump(),
        },
        SkillRewriteRequest(
            tenant_id="tenant_demo",
            current_skill=current,
            instruction="把第二步 step_id 改成 collect_info",
            target_path="steps[1]",
            target_paths=["steps[1]"],
            target_label="步骤 2",
        ),
    )

    assert response.draft_skill.steps[0].step_id == "collect_info"
    assert response.draft_skill.steps[1].step_id == "collect_info_2"
    assert "steps[1]" in response.changed_paths
    assert any("步骤 2" in warning and "collect_info_2" in warning for warning in response.warnings)


def test_skill_editor_applies_patch_response_without_full_draft() -> None:
    current = _skill_card()
    response = SkillEditor()._normalize_response(  # noqa: SLF001
        {
            "assistant_message": "已精简回复规则。",
            "patches": [
                {
                    "path": "response_rules",
                    "value": ["信息不足时追问；工具成功后给出明确结果，不编造事实。"],
                }
            ],
            "changed_paths": ["basic"],
        },
        SkillRewriteRequest(
            tenant_id="tenant_demo",
            current_skill=current,
            instruction="回复规则太长了，精简一下",
            target_path="basic",
            target_paths=["basic", "steps[0]", "steps[1]"],
            target_label="全部区域",
        ),
    )

    assert response.draft_skill.response_rules == ["信息不足时追问；工具成功后给出明确结果，不编造事实。"]
    assert response.draft_skill.steps[0].instruction == current.steps[0].instruction


def test_skill_editor_stream_repairs_invalid_json_once(monkeypatch) -> None:
    def fake_stream(self, _system_prompt: str, _payload: dict):  # noqa: ANN001
        assert self.max_output_tokens == 16384
        yield '{"assistant_message": "截断的输出", "patches": ['

    def fake_text(self, _system_prompt: str, payload: dict):  # noqa: ANN001
        assert self.max_output_tokens == 16384
        assert "previous_error" in payload
        return json.dumps(
            {
                "assistant_message": "已精简回复规则。",
                "patches": [
                    {
                        "path": "response_rules",
                        "value": ["信息不足时追问；工具成功后给出明确结果，不编造事实。"],
                    }
                ],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("app.skills.skill_editor.LLMClient.generate_text_stream", fake_stream)
    monkeypatch.setattr("app.skills.skill_editor.LLMClient.generate_text", fake_text)

    events = list(
        SkillEditor().stream_text(
            SkillRewriteRequest(
                tenant_id="tenant_demo",
                current_skill=_skill_card(),
                instruction="回复规则太长了，精简一下",
                target_path="basic",
                target_paths=["basic", "steps[0]", "steps[1]"],
                target_label="全部区域",
            ),
            _model_config(),
        )
    )
    status_texts = [event["data"]["text"] for event in events if event["event"] == "status"]
    complete = next(event for event in events if event["event"] == "complete")

    assert "模型输出需要修复，正在重试一次" in status_texts
    assert complete["data"]["draft_skill"]["response_rules"] == [
        "信息不足时追问；工具成功后给出明确结果，不编造事实。"
    ]


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
            AgentEvent(
                tenant_id="tenant_demo",
                session_id="session_2",
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

    assert stats["purchase"]["call_count"] == 2
    assert stats["purchase"]["positive_feedback_count"] == 1
    assert stats["purchase"]["negative_feedback_count"] == 1
    assert stats["purchase"]["positive_rate"] == 0.5
    assert stats["purchase"]["negative_rate"] == 0.5
    assert stats["purchase@1.5.0"]["call_count"] == 2
    assert stats["purchase@1.5.0"]["positive_feedback_count"] == 1
    assert stats["purchase@1.5.0"]["negative_feedback_count"] == 1


def test_skill_stats_count_one_negative_feedback_per_flow() -> None:
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
        for message_id in ["msg_1", "msg_2"]:
            db.add(
                SkillFeedback(
                    tenant_id="tenant_demo",
                    skill_id="purchase",
                    skill_version="1.5.0",
                    session_id="session_1",
                    message_id=message_id,
                    user_id="user_1",
                    rating="down",
                )
            )
        db.commit()

        stats = _skill_stats(db, "tenant_demo")

    assert stats["purchase"]["call_count"] == 1
    assert stats["purchase"]["negative_feedback_count"] == 1
    assert stats["purchase"]["negative_rate"] == 1.0
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


def test_legacy_unversioned_stats_are_archived_to_oldest_version() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        content = _skill_card()
        old_content = content.model_copy(update={"version": "1.0.0"})
        new_content = content.model_copy(update={"version": "1.1.0"})
        db.add(
            Skill(
                tenant_id="tenant_demo",
                skill_id=content.skill_id,
                version="1.1.0",
                name=content.name,
                content_json=new_content.model_dump(),
                status="published",
            )
        )
        db.add(
            SkillVersion(
                tenant_id="tenant_demo",
                skill_id=content.skill_id,
                version="1.0.0",
                name=content.name,
                content_json=old_content.model_dump(),
                status="published",
            )
        )
        db.add(
            SkillVersion(
                tenant_id="tenant_demo",
                skill_id=content.skill_id,
                version="1.1.0",
                name=content.name,
                content_json=new_content.model_dump(),
                status="published",
            )
        )
        db.add(
            AgentEvent(
                tenant_id="tenant_demo",
                session_id="session_legacy",
                event_type="skill_started",
                payload_json={"to_skill_id": content.skill_id},
            )
        )
        db.add(
            SkillFeedback(
                tenant_id="tenant_demo",
                skill_id=content.skill_id,
                session_id="session_legacy",
                message_id="msg_legacy",
                user_id="user_legacy",
                rating="down",
            )
        )
        db.commit()

        versions = list_skill_versions(content.skill_id, "tenant_demo", db)
        stats = _skill_stats(db, "tenant_demo")
        versions_by_version = {version.version: version for version in versions}
        current_skill = db.exec(
            select(Skill).where(Skill.tenant_id == "tenant_demo", Skill.skill_id == content.skill_id)
        ).one()
        payload = skill_read(current_skill, stats)

    assert stats[content.skill_id]["call_count"] == 1
    assert stats[content.skill_id]["negative_feedback_count"] == 1
    assert versions_by_version["1.0.0"].call_count == 1
    assert versions_by_version["1.0.0"].negative_feedback_count == 1
    assert versions_by_version["1.0.0"].negative_rate == 1.0
    assert versions_by_version["1.1.0"].call_count == 0
    assert versions_by_version["1.1.0"].negative_feedback_count == 0
    assert versions_by_version["1.1.0"].negative_rate == 0.0
    assert payload.call_count == 0
    assert payload.negative_feedback_count == 0
    assert payload.negative_rate == 0.0


def test_legacy_unversioned_stats_fall_back_to_current_version_when_no_version_snapshots() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        content = _skill_card()
        db.add(
            Skill(
                tenant_id="tenant_demo",
                skill_id=content.skill_id,
                version="1.0.0",
                name=content.name,
                content_json=content.model_dump(),
                status="published",
            )
        )
        db.add(
            AgentEvent(
                tenant_id="tenant_demo",
                session_id="session_legacy",
                event_type="skill_started",
                payload_json={"to_skill_id": content.skill_id},
            )
        )
        db.add(
            SkillFeedback(
                tenant_id="tenant_demo",
                skill_id=content.skill_id,
                session_id="session_legacy",
                message_id="msg_legacy",
                user_id="user_legacy",
                rating="down",
            )
        )
        db.commit()

        current_skill = db.exec(
            select(Skill).where(Skill.tenant_id == "tenant_demo", Skill.skill_id == content.skill_id)
        ).one()
        payload = skill_read(current_skill, _skill_stats(db, "tenant_demo"))

    assert payload.call_count == 1
    assert payload.negative_feedback_count == 1
    assert payload.negative_rate == 1.0


def test_rollback_skill_version_restores_content_without_copying_stats() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        old_content = _skill_card()
        old_content.version = "1.0.0"
        old_content.name = "旧版购买"
        new_content = _skill_card()
        new_content.version = "1.1.0"
        new_content.name = "新版购买"
        db.add(
            Skill(
                tenant_id="tenant_demo",
                skill_id=old_content.skill_id,
                version="1.1.0",
                name=new_content.name,
                content_json=new_content.model_dump(),
                status="published",
            )
        )
        db.add(
            SkillVersion(
                tenant_id="tenant_demo",
                skill_id=old_content.skill_id,
                version="1.0.0",
                name=old_content.name,
                content_json=old_content.model_dump(),
                status="published",
            )
        )
        db.add(
            SkillVersion(
                tenant_id="tenant_demo",
                skill_id=old_content.skill_id,
                version="1.1.0",
                name=new_content.name,
                content_json=new_content.model_dump(),
                status="published",
            )
        )
        db.commit()

        payload = rollback_skill_version(old_content.skill_id, "1.0.0", "tenant_demo", db)
        row = db.exec(
            select(Skill).where(
                Skill.tenant_id == "tenant_demo",
                Skill.skill_id == old_content.skill_id,
            )
        ).one()

    assert payload.version == "1.0.0"
    assert payload.name == "旧版购买"
    assert payload.status == "published"
    assert row.content_json["version"] == "1.0.0"
    assert row.content_json["name"] == "旧版购买"


def test_skill_read_uses_current_version_stats_for_skill_list() -> None:
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

    assert payload.call_count == 1
    assert payload.positive_feedback_count == 0
    assert payload.negative_feedback_count == 0


def test_skill_read_includes_total_and_recent_version_ranking_stats() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        content = _skill_card()
        current = content.model_copy(update={"version": "1.3.0"})
        db.add(
            Skill(
                tenant_id="tenant_demo",
                skill_id=content.skill_id,
                version="1.3.0",
                name=content.name,
                content_json=current.model_dump(),
                status="published",
            )
        )
        for version in ["1.0.0", "1.1.0", "1.2.0", "1.3.0"]:
            version_content = content.model_copy(update={"version": version})
            db.add(
                SkillVersion(
                    tenant_id="tenant_demo",
                    skill_id=content.skill_id,
                    version=version,
                    name=content.name,
                    content_json=version_content.model_dump(),
                    status="published",
                )
            )
            db.add(
                AgentEvent(
                    tenant_id="tenant_demo",
                    session_id=f"session_{version}",
                    event_type="skill_started",
                    payload_json={"to_skill_id": content.skill_id, "to_skill_version": version},
                )
            )
        db.add(
            SkillFeedback(
                tenant_id="tenant_demo",
                skill_id=content.skill_id,
                skill_version="1.0.0",
                session_id="session_1.0.0",
                message_id="msg_old",
                user_id="user_old",
                rating="down",
            )
        )
        db.add(
            SkillFeedback(
                tenant_id="tenant_demo",
                skill_id=content.skill_id,
                skill_version="1.2.0",
                session_id="session_1.2.0",
                message_id="msg_recent_up",
                user_id="user_recent_up",
                rating="up",
            )
        )
        db.add(
            SkillFeedback(
                tenant_id="tenant_demo",
                skill_id=content.skill_id,
                skill_version="1.3.0",
                session_id="session_1.3.0",
                message_id="msg_recent_down",
                user_id="user_recent_down",
                rating="down",
            )
        )
        db.commit()

        rows = list_skills("tenant_demo", db)

    payload = rows[0]
    assert payload.call_count == 1
    assert payload.total_call_count == 4
    assert payload.total_negative_feedback_count == 2
    assert payload.recent_versions == ["1.3.0", "1.2.0", "1.1.0"]
    assert payload.recent_call_count == 3
    assert payload.recent_positive_feedback_count == 1
    assert payload.recent_negative_feedback_count == 1
    assert payload.recent_positive_rate == 0.3333
    assert payload.recent_negative_rate == 0.3333


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
                payload_json={"to_skill_id": "refund", "to_step_id": "collect_order"},
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
        context = _active_skill_context_for_assistant_message(db, "tenant_demo", assistant)

    assert skill_id == "refund"
    assert context == {"skill_id": "refund", "skill_version": None, "step_id": "collect_order"}


def test_message_feedback_attribution_uses_router_skill_hint_for_legacy_step_event() -> None:
    with _test_session() as db:
        db.add(Message(id="msg_user", tenant_id="tenant_demo", session_id="session_1", role="user", content="继续下单"))
        assistant = Message(
            id="msg_assistant",
            tenant_id="tenant_demo",
            session_id="session_1",
            role="assistant",
            content="已为您下单。",
        )
        db.add(assistant)
        db.add(
            AgentEvent(
                tenant_id="tenant_demo",
                session_id="session_1",
                event_type="user_message_received",
                payload_json={"message": "继续下单"},
            )
        )
        db.add(
            AgentEvent(
                tenant_id="tenant_demo",
                session_id="session_1",
                event_type="router_decision_created",
                payload_json={
                    "decision": "continue_current_skill",
                    "target_skill_id": "purchase",
                    "target_step_id": "create_order",
                },
            )
        )
        db.add(
            AgentEvent(
                tenant_id="tenant_demo",
                session_id="session_1",
                event_type="skill_step_changed",
                payload_json={"from_step_id": "confirm_purchase", "to_step_id": "create_order"},
            )
        )
        db.add(
            AgentEvent(
                tenant_id="tenant_demo",
                session_id="session_1",
                event_type="assistant_message_created",
                payload_json={"reply": "已为您下单。"},
            )
        )
        db.commit()

        skill_id = _active_skill_for_assistant_message(db, "tenant_demo", assistant)
        context = _active_skill_context_for_assistant_message(db, "tenant_demo", assistant)

    assert skill_id == "purchase"
    assert context == {"skill_id": "purchase", "skill_version": None, "step_id": "create_order"}


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
        assert self.max_output_tokens == 16384
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


def test_skill_distiller_stream_repairs_invalid_json_with_model(monkeypatch) -> None:
    def fake_stream(self, _system_prompt: str, _payload: dict):  # noqa: ANN001
        assert self.max_output_tokens == 16384
        yield '{"draft_skill": {"name": "截断"'

    def fake_text(self, _system_prompt: str, payload: dict):  # noqa: ANN001
        assert self.max_output_tokens == 16384
        assert payload["repair_attempt"] == 1
        return json.dumps(
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
                        "enabled": True,
                        "multi_slot_per_turn": True,
                        "extract_scope": "all_skill_expected_user_info",
                        "skip_satisfied_steps": True,
                        "target_info": ["product_name_1", "product_name_2"],
                    },
                    "response_rules": ["不要编造价格。"],
                    "steps": [
                        {
                            "step_id": "collect_names",
                            "name": "收集商品名称",
                            "instruction": "收集两个商品名称。",
                            "expected_user_info": ["product_name_1", "product_name_2"],
                            "allowed_actions": ["ask_user"],
                        },
                        {
                            "step_id": "reply_result",
                            "name": "反馈结果",
                            "instruction": "反馈明确结果。",
                            "expected_user_info": [],
                            "allowed_actions": ["answer_user"],
                        },
                    ],
                    "interruption_policy": {},
                },
                "warnings": [],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("app.skills.skill_distiller.LLMClient.generate_text_stream", fake_stream)
    monkeypatch.setattr("app.skills.skill_distiller.LLMClient.generate_text", fake_text)

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
    complete = next(event for event in events if event["event"] == "complete")

    assert "模型输出需要修复，正在重试" in status_texts
    assert any(event["event"] == "chunk_reset" for event in events)
    assert complete["data"]["draft_skill"]["name"] == "商品比价"


def test_skill_distiller_stream_uses_staged_generation_after_repair_failure(monkeypatch) -> None:
    def fake_stream(self, _system_prompt: str, _payload: dict):  # noqa: ANN001
        assert self.max_output_tokens == 16384
        yield '{"draft_skill": {"name": "截断"'

    def fake_text(self, _system_prompt: str, payload: dict):  # noqa: ANN001
        assert self.max_output_tokens == 16384
        if "repair_instruction" in payload:
            return "still invalid"
        mode = payload.get("generation_mode")
        if mode == "outline_only":
            return json.dumps(
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
                            "enabled": True,
                            "multi_slot_per_turn": True,
                            "extract_scope": "all_skill_expected_user_info",
                            "skip_satisfied_steps": True,
                            "target_info": ["product_name_1", "product_name_2"],
                        },
                        "response_rules": ["不要编造价格。"],
                        "steps": [
                            {
                                "step_id": "collect_names",
                                "name": "收集商品名称",
                                "instruction": "收集两个商品名称。",
                                "expected_user_info": ["product_name_1", "product_name_2"],
                                "allowed_actions": ["ask_user"],
                            },
                            {
                                "step_id": "reply_result",
                                "name": "反馈结果",
                                "instruction": "反馈结果。",
                                "expected_user_info": [],
                                "allowed_actions": ["answer_user"],
                            },
                        ],
                        "interruption_policy": {},
                    },
                    "warnings": [],
                },
                ensure_ascii=False,
            )
        if mode == "expand_step":
            step = dict(payload["target_step"])
            step["instruction"] = f"扩写步骤 {payload['target_step_index'] + 1}，支持自适应推进。"
            return json.dumps({"step": step, "warnings": [], "tool_suggestions": []}, ensure_ascii=False)
        if mode == "final_review":
            return json.dumps({"draft_skill": payload["current_draft"], "warnings": []}, ensure_ascii=False)
        raise AssertionError(f"unexpected payload: {payload}")

    monkeypatch.setattr("app.skills.skill_distiller.LLMClient.generate_text_stream", fake_stream)
    monkeypatch.setattr("app.skills.skill_distiller.LLMClient.generate_text", fake_text)

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
    complete = next(event for event in events if event["event"] == "complete")
    instructions = [step["instruction"] for step in complete["data"]["draft_skill"]["steps"]]

    assert "模型修复失败，改用分段生成" in status_texts
    assert any("扩写步骤 1" in instruction for instruction in instructions)
    assert any("扩写步骤 2" in instruction for instruction in instructions)


def test_extract_uploaded_skill_file_reads_docx_text() -> None:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml",
            """
            <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
              <w:body>
                <w:p><w:r><w:t>标题：商品比价</w:t></w:r></w:p>
                <w:p><w:r><w:t>用户提供两个商品名称后进行比价。</w:t></w:r></w:p>
              </w:body>
            </w:document>
            """,
        )

    text = _extract_uploaded_skill_file("skill.docx", buffer.getvalue())

    assert "标题：商品比价" in text
    assert "用户提供两个商品名称后进行比价。" in text


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
