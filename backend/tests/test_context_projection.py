from app.core.context_projection import (
    compact_awaiting_input,
    compact_conversation_context,
    compact_knowledge_context,
    compact_memory_context,
    compact_pending_tasks,
    compact_step_result,
    compact_step_skill_context,
)
from app.knowledge.citations import knowledge_citations_from_results


def test_compact_knowledge_context_keeps_evidence_without_duplicate_payloads() -> None:
    result = {
        "query": {"query": "退款规则"},
        "source_message": "请查询退款规则",
        "selected_documents": [
            {
                "id": "doc_1",
                "title": "退款规则",
                "summary": "文档摘要" * 500,
                "outline": ["不应进入控制模型输入"] * 100,
            }
        ],
        "selected_concepts": [
            {
                "concept_id": "concept_wrong_source",
                "title": "不应混入的路由概念",
                "content": "不应进入回答证据",
            }
        ],
        "okf_citations": [
            {
                "concept_id": "okf_wrong_source",
                "title": "不应混入的 OKF 引用",
                "label": "不应进入回答证据",
            }
        ],
        "chunks": [{"id": "chunk_1", "content": "重复切片" * 2_000}],
        "expanded_sections": [{"content": "完整目录树" * 2_000}],
        "evidence_pack": [
            {
                "chunk_id": "chunk_1",
                "source_path": "退款规则/时限",
                "summary": "七天内可申请退款",
                "content": "有效证据" * 2_000,
                "excerpt": "不应重复发送" * 2_000,
            }
        ],
    }

    compacted = compact_knowledge_context([result])

    assert len(compacted) == 1
    assert "chunks" not in compacted[0]
    assert "expanded_sections" not in compacted[0]
    assert "selected_documents" not in compacted[0]
    knowledge = compacted[0]["retrieved_knowledge"]
    assert len(knowledge) == 1
    assert knowledge[0]["label"] == "检索到的知识 1"
    assert "chunk_id" not in knowledge[0]
    assert len(knowledge[0]["content"]) <= 803
    assert "excerpt" not in knowledge[0]
    assert "不应进入回答证据" not in str(knowledge)
    assert result["chunks"][0]["content"].startswith("重复切片")


def test_compact_step_result_only_projects_knowledge_results() -> None:
    payload = {
        "reply": "已找到规则",
        "slot_updates": {"order_id": "A001"},
        "knowledge_results": [
            {
                "evidence_pack": [
                    {"chunk_id": "chunk_1", "content": "证据" * 2_000}
                ]
            }
        ],
    }

    compacted = compact_step_result(payload)

    assert compacted["reply"] == "已找到规则"
    assert compacted["slot_updates"] == {"order_id": "A001"}
    assert "knowledge_results" not in compacted
    assert len(
        compacted["retrieved_knowledge"][0]["retrieved_knowledge"][0]["content"]
    ) <= 803


def test_compact_knowledge_context_keeps_complete_related_group() -> None:
    evidence = [
        {
            "chunk_id": f"chunk_{index}",
            "related_group_id": "faq_group",
            "section_path": "示例流程 / 办理步骤",
            "content": f"第 {index} 段内容",
        }
        for index in range(1, 9)
    ]

    compacted = compact_knowledge_context([{"evidence_pack": evidence}])

    knowledge = compacted[0]["retrieved_knowledge"]
    assert len(knowledge) == 1
    assert "第 1 段内容" in knowledge[0]["content"]
    assert "第 8 段内容" in knowledge[0]["content"]


def test_compact_knowledge_context_does_not_split_email_at_limit() -> None:
    content = f"{'x' * 790} ops@example.test 后续说明"
    compacted = compact_knowledge_context(
        [{"evidence_pack": [{"content": content, "source_path": "guide.md"}]}]
    )

    projected = compacted[0]["retrieved_knowledge"][0]["content"]
    assert "ops@example.test" in projected
    assert "ops@example..." not in projected


def test_compact_knowledge_context_and_citations_share_normalized_source() -> None:
    result = {
        "selected_concepts": [
            {
                "concept_id": "sources/demo",
                "title": "统一来源",
                "content": "统一处理后的知识内容。",
                "source_refs": [{"source_path": "demo.md"}],
            }
        ]
    }

    compacted = compact_knowledge_context([result])
    citations = knowledge_citations_from_results([result])

    assert compacted[0]["retrieved_knowledge"][0]["source"] == "demo.md"
    assert citations[0]["source_path"] == "demo.md"


def test_document_route_metadata_without_evidence_is_not_answer_context() -> None:
    result = {
        "selected_documents": [
            {
                "id": "doc_route_only",
                "filename": "route-only.pdf",
                "summary": "仅用于路由、没有读取正文的文档摘要。",
            }
        ]
    }

    assert compact_knowledge_context([result]) == []
    assert knowledge_citations_from_results([result]) == []


def test_compact_conversation_context_keeps_recent_history_within_control_budget() -> None:
    context = {
        "messages": [
            {"role": "user" if index % 2 == 0 else "assistant", "content": str(index) * 1_000}
            for index in range(20)
        ]
    }

    compacted = compact_conversation_context(context, token_budget=4_000)

    assert compacted["metadata"]["estimated_tokens"] <= 4_000
    assert compacted["messages"][-1]["content"].startswith("19")
    assert compacted["metadata"]["compacted"] is True


def test_step_skill_context_keeps_only_local_graph_and_complete_instructions() -> None:
    current_instruction = "当前节点业务说明" * 1_000
    target_instruction = "目标节点业务说明" * 1_000
    compacted = compact_step_skill_context(
        {
            "skill_id": "refund",
            "required_info": ["order_id"],
            "response_rules": ["展示退款状态"],
            "nodes": [
                {
                    "node_id": "collect_order",
                    "type": "collect_info",
                    "instruction": current_instruction,
                    "expected_user_info": ["order_id"],
                    "allowed_actions": ["ask_user"],
                },
                {
                    "node_id": "query_order",
                    "type": "tool",
                    "instruction": target_instruction,
                },
                {"node_id": "unrelated", "instruction": "不得投影"},
            ],
            "edges": [
                {"source_node_id": "collect_order", "next_node_id": "query_order"},
                {"source_node_id": "query_order", "next_node_id": "unrelated"},
            ],
        },
        "collect_order",
    )

    assert compacted is not None
    assert "skill_id" not in compacted
    assert "response_rules" not in compacted
    assert "required_info" not in compacted
    assert compacted["current_step"]["instruction"] == current_instruction
    assert compacted["next_steps"] == [
        {
            "node_id": "query_order",
            "type": "tool",
            "instruction": target_instruction,
        }
    ]
    assert "nodes" not in compacted
    assert "edges" not in compacted
    assert "adjacent_edges" not in compacted
    assert "target_steps" not in compacted
    assert "name" not in str(compacted)
    assert "optional" not in str(compacted)
    assert "retry_policy" not in str(compacted)
    assert "unrelated" not in str(compacted)


def test_step_skill_context_embeds_only_direct_transition_metadata() -> None:
    compacted = compact_step_skill_context(
        {
            "nodes": [
                {"node_id": "current", "instruction": "当前"},
                {"node_id": "approved", "instruction": "通过"},
                {"node_id": "rejected", "instruction": "拒绝"},
                {"node_id": "after_approved", "instruction": "后续节点不得传入"},
            ],
            "edges": [
                {
                    "source_node_id": "current",
                    "next_node_id": "approved",
                    "condition": "amount <= limit",
                    "label": "未超标",
                    "priority": 1,
                },
                {
                    "source_node_id": "current",
                    "next_node_id": "rejected",
                    "condition": "amount > limit",
                    "label": "超标",
                    "priority": 2,
                },
                {
                    "source_node_id": "approved",
                    "next_node_id": "after_approved",
                    "condition": "always",
                },
            ],
        },
        "current",
    )

    assert compacted is not None
    assert [step["node_id"] for step in compacted["next_steps"]] == [
        "approved",
        "rejected",
    ]
    assert compacted["next_steps"][0]["transition"] == {
        "condition": "amount <= limit",
        "label": "未超标",
    }
    assert compacted["next_steps"][1]["transition"] == {
        "condition": "amount > limit",
        "label": "超标",
    }
    assert "after_approved" not in str(compacted)


def test_compact_memory_context_only_returns_deduplicated_content_text() -> None:
    memory = compact_memory_context(
        [
            {
                "id": "mem_1",
                "tenant_id": "tenant_demo",
                "kind": "profile",
                "content": "32",
                "importance": 0.9,
                "metadata": {"key": "age", "reason": "用户提供了年龄"},
                "updated_at": "2026-07-13T00:00:00",
            },
            {"id": "mem_2", "kind": "profile", "content": "32"},
            {"id": "mem_3", "kind": "fact", "content": "近期存在肠道不适"},
        ]
    )

    assert memory == "- 32\n- 近期存在肠道不适"
    assert "mem_" not in memory
    assert "updated_at" not in memory


def test_compact_runtime_state_drops_storage_and_audit_metadata() -> None:
    tasks = compact_pending_tasks(
        [
            {
                "task_id": "task_1",
                "tenant_id": "tenant_demo",
                "agent_id": "agent_1",
                "user_id": "user_1",
                "session_id": "session_1",
                "skill_id": "medical_consultation_v1",
                "step_id": "collect_symptoms",
                "slots": {"duration": "两天"},
                "created_at": "2026-07-13T00:00:00",
                "updated_at": "2026-07-13T00:01:00",
                "source_turn_id": "turn_1",
            }
        ]
    )
    awaiting = compact_awaiting_input(
        {
            "task_id": "task_1",
            "turn_id": "turn_1",
            "skill_id": "medical_consultation_v1",
            "step_id": "collect_symptoms",
            "expected_fields": ["duration"],
            "created_at": "2026-07-13T00:00:00",
        }
    )

    assert tasks == [
        {
            "task_id": "task_1",
            "skill_id": "medical_consultation_v1",
            "step_id": "collect_symptoms",
            "slots": {"duration": "两天"},
        }
    ]
    assert awaiting == {
        "skill_id": "medical_consultation_v1",
        "step_id": "collect_symptoms",
        "expected_fields": ["duration"],
    }
    serialized = str((tasks, awaiting))
    for field in (
        "tenant_id",
        "agent_id",
        "user_id",
        "session_id",
        "created_at",
        "updated_at",
        "source_turn_id",
    ):
        assert field not in serialized
