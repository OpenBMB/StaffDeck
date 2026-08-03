from app.core.legacy_conversation_projection import LegacyConversationProjection
from app.db.models import ChatSession, Skill
from app.session.session_schema import StepAgentResult


def test_dedupe_citations_preserves_first_four_and_relabels() -> None:
    citations = [
        {"title": " Alpha "},
        {"title": "alpha"},
        {"section_path": "Beta"},
        {"summary": "Gamma"},
        {"excerpt": "Delta"},
        {"title": "Epsilon"},
    ]

    assert LegacyConversationProjection.dedupe_knowledge_citations(citations) == [
        {"title": " Alpha ", "label": "[1]"},
        {"section_path": "Beta", "label": "[2]"},
        {"summary": "Gamma", "label": "[3]"},
        {"excerpt": "Delta", "label": "[4]"},
    ]


def test_skill_state_hides_unavailable_active_and_pending_skills() -> None:
    visible = Skill(
        tenant_id="tenant_test",
        skill_id="visible",
        version="1.0.0",
        name="Visible",
    )
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_test",
        active_skill_id="hidden",
        active_step_id="step_hidden",
        pending_tasks_json=[
            {"target_skill_id": "visible", "target_step_id": "step_visible"},
            {"target_skill_id": "hidden"},
        ],
    )

    assert LegacyConversationProjection.skill_state_payload(session, [visible]) == {
        "activeSkillId": None,
        "activeStepId": None,
        "currentSkills": [
            {
                "skillId": "visible",
                "name": "Visible",
                "stepId": "step_visible",
                "state": "pending",
            }
        ],
    }


def test_reply_citation_and_title_projection_preserves_legacy_rules() -> None:
    assert LegacyConversationProjection.fallback_session_title("  请问 A？ ") == "请问 A"
    assert LegacyConversationProjection.normalize_reply_citation_labels(
        "有效[1] 无效[9]", [{}, {}]
    ) == "有效[1] 无效[2]"
    assert LegacyConversationProjection.strip_trailing_citation_summary(
        "正文\n参考资料：[1] [2]"
    ) == "正文"


def test_assistant_metadata_uses_injected_citation_deduper() -> None:
    calls: list[list[dict[str, object]]] = []
    metadata = LegacyConversationProjection.assistant_message_metadata(
        StepAgentResult(
            knowledge_results=[
                {
                    "query": {"query": "Q"},
                    "chunks": [{"title": "T", "content": "C"}],
                }
            ]
        ),
        citation_deduper=lambda citations: calls.append(citations)
        or [{"title": "injected", "label": "[1]"}],
    )

    assert calls
    assert metadata["knowledge_citations"] == [
        {"title": "injected", "label": "[1]"}
    ]


def test_assistant_metadata_uses_latest_knowledge_result_window() -> None:
    metadata = LegacyConversationProjection.assistant_message_metadata(
        StepAgentResult(
            knowledge_results=[
                {
                    "query": {"query": "旧问题"},
                    "chunks": [
                        {"id": "old", "content": "旧答案", "source_ref": "old.md"}
                    ],
                },
                {
                    "query": {"query": "新问题"},
                    "chunks": [
                        {"id": "new", "content": "新答案", "source_ref": "new.md"}
                    ],
                },
            ]
        )
    )

    assert metadata["knowledge_query"] == {"query": "新问题"}
    assert metadata["knowledge_citations"][0]["source_path"] == "new.md"
