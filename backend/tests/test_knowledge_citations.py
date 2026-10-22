from app.knowledge.citations import (
    CITATION_EXCERPT_CHAR_LIMIT,
    citation_display_key,
    citation_identity,
    compact_knowledge_citation_labels,
    knowledge_citations_from_results,
    renumber_citations,
    restore_truncated_atomic_references,
)


def test_compact_knowledge_citation_labels_renumbers_by_first_appearance() -> None:
    content, citations = compact_knowledge_citation_labels(
        "先参考手册。[4] 再确认规范。[1] 最后仍参考手册。[4]",
        [
            {"id": "kref_1", "label": "[1]", "title": "规范"},
            {"id": "kref_2", "label": "[2]", "title": "无关来源"},
            {"id": "kref_3", "label": "[3]", "title": "另一无关来源"},
            {"id": "kref_4", "label": "[4]", "title": "手册"},
        ],
    )

    assert content == "先参考手册。[1] 再确认规范。[2] 最后仍参考手册。[1]"
    assert [(item["label"], item["title"]) for item in citations] == [
        ("[1]", "手册"),
        ("[2]", "规范"),
    ]


def test_compact_knowledge_citation_labels_supports_historical_filtered_metadata() -> None:
    content, citations = compact_knowledge_citation_labels(
        "排查步骤来自手册。[1] 区域故障需要报修。[4]",
        [
            {"id": "kref_1", "label": "[1]", "title": "排查手册"},
            {"id": "kref_4", "label": "[4]", "title": "网络故障"},
        ],
    )

    assert content == "排查步骤来自手册。[1] 区域故障需要报修。[2]"
    assert [item["label"] for item in citations] == ["[1]", "[2]"]


def test_compact_knowledge_citation_labels_preserves_range_order() -> None:
    content, citations = compact_knowledge_citation_labels(
        "请假制度依据员工手册。[1]-[4]\n\n参考来源：[1] [2] [3] [4] [5]",
        [
            {"id": f"kref_{index}", "label": f"[{index}]", "title": f"来源 {index}"}
            for index in range(1, 6)
        ],
    )

    assert content == "请假制度依据员工手册。[1]-[4]"
    assert [item["label"] for item in citations] == ["[1]", "[2]", "[3]", "[4]"]


def test_compact_knowledge_citation_labels_removes_unsupported_model_labels() -> None:
    content, citations = compact_knowledge_citation_labels(
        "制度正文。[1]\n\n参考来源：[1] [2] [3] [4]",
        [{"id": "kref_1", "label": "[1]", "title": "付款制度"}],
    )

    assert content == "制度正文。[1]"
    assert citations == [{"id": "kref_1", "label": "[1]", "title": "付款制度"}]


def test_compact_knowledge_citation_labels_removes_footer_without_sources() -> None:
    content, citations = compact_knowledge_citation_labels(
        "没有检索依据的回答。\n\n参考来源：[1] [2]",
        [],
    )

    assert content == "没有检索依据的回答。"
    assert citations == []


def test_compact_knowledge_citation_labels_keeps_sources_without_adding_a_footer() -> None:
    content, citations = compact_knowledge_citation_labels(
        "制度规定七天内可以申请退款。",
        [
            {"id": "kref_1", "label": "[1]", "title": "退款政策"},
            {"id": "kref_4", "label": "[4]", "title": "退款流程"},
        ],
    )

    assert content == "制度规定七天内可以申请退款。"
    assert [(item["label"], item["title"]) for item in citations] == [
        ("[1]", "退款政策"),
        ("[2]", "退款流程"),
    ]


def test_restore_truncated_email_from_unique_cited_evidence() -> None:
    reply = "请将材料发送至 ops@example... [1]"
    citations = [
        {
            "label": "[1]",
            "source_path": "employee-guide.md",
            "excerpt": "材料准备完成后发送至 ops@example.test。",
        }
    ]

    assert restore_truncated_atomic_references(reply, citations) == (
        "请将材料发送至 ops@example.test [1]"
    )


def test_restore_truncated_email_keeps_ambiguous_prefix_unchanged() -> None:
    reply = "联系 ops@example... [1]"
    citations = [
        {
            "label": "[1]",
            "excerpt": "可联系 ops@example.test 或 ops@example.team。",
        }
    ]

    assert restore_truncated_atomic_references(reply, citations) == reply


def test_knowledge_citations_use_evidence_pack_as_canonical_source() -> None:
    citations = knowledge_citations_from_results(
        [
            {
                "selected_concepts": [
                    {
                        "concept_id": f"sources/vue3-coding-standards-{index}",
                        "type": "Source Document",
                        "title": f"前端编码规范 {index}",
                        "description": "Vue 3、Vite、TypeScript、组件编写和命名规范。",
                        "source_refs": [{"source_path": "vue3-coding-standards.md"}],
                    }
                    for index in range(4)
                ],
                "evidence_pack": [
                    {
                        "chunk_id": "kchunk_citation_demo",
                        "document_id": "kdoc_citation_demo",
                        "bucket_id": "kbucket_citation_demo",
                        "source_path": "citation-demo.md",
                        "section_path": "知识引用测试说明 / 引用规则",
                        "summary": "回答基于业务资料时必须展示可点击知识引用。",
                        "excerpt": "StaffDeck 引用测试规则。",
                    }
                ],
            }
        ]
    )

    assert len(citations) == 1
    assert citations[0]["kind"] == "evidence"
    assert citations[0]["title"] == "知识引用测试说明 / 引用规则"
    assert citations[0]["source_path"] == "citation-demo.md"


def test_knowledge_citations_fall_back_to_wiki_concepts_without_evidence() -> None:
    citations = knowledge_citations_from_results(
        [
            {
                "selected_concepts": [
                    {
                        "concept_id": "sources/vue3-coding-standards",
                        "type": "Source Document",
                        "title": "前端编码规范",
                        "description": "Vue 3、Vite、TypeScript、组件编写和命名规范。",
                        "source_refs": [{"source_path": "vue3-coding-standards.md"}],
                    }
                ]
            }
        ]
    )

    assert citations[0]["kind"] == "concept"
    assert citations[0]["title"] == "前端编码规范"
    assert citations[0]["source_path"] == "vue3-coding-standards.md"


def test_empty_evidence_does_not_block_concept_fallback() -> None:
    citations = knowledge_citations_from_results(
        [
            {
                "evidence_pack": [{}],
                "selected_concepts": [
                    {
                        "concept_id": "sources/refund-policy",
                        "title": "退款政策",
                        "content": "七天内可申请退款。",
                        "source_refs": [{"source_path": "refund-policy.md"}],
                    }
                ],
            }
        ]
    )

    assert len(citations) == 1
    assert citations[0]["kind"] == "concept"
    assert citations[0]["source_path"] == "refund-policy.md"


def test_knowledge_citations_support_chunks_only_results() -> None:
    citations = knowledge_citations_from_results(
        [
            {
                "chunks": [
                    {
                        "id": "chunk_historical",
                        "source_ref": "historical-guide.md",
                        "content": "历史知识结果中的有效证据。",
                    }
                ]
            }
        ]
    )

    assert len(citations) == 1
    assert citations[0]["kind"] == "evidence"
    assert citations[0]["source_path"] == "historical-guide.md"
    assert citations[0]["content"] == "历史知识结果中的有效证据。"


def test_knowledge_citations_fall_back_to_okf_without_evidence_or_concepts() -> None:
    citations = knowledge_citations_from_results(
        [
            {
                "okf_citations": [
                    {
                        "concept_id": "sources/refund-policy",
                        "title": "退款政策",
                        "target": "refund-policy.md",
                        "label": "七天内可申请退款",
                    }
                ]
            }
        ]
    )

    assert citations[0]["kind"] == "okf"
    assert citations[0]["title"] == "退款政策"
    assert citations[0]["source_path"] == "refund-policy.md"


def test_knowledge_citations_apply_source_fallback_per_result() -> None:
    citations = knowledge_citations_from_results(
        [
            {
                "evidence_pack": [
                    {
                        "chunk_id": "chunk_policy",
                        "source_path": "policy.pdf",
                        "section_path": "报销制度",
                        "content": "制度证据",
                    }
                ]
            },
            {
                "okf_citations": [
                    {
                        "concept_id": "sources/login-guide",
                        "title": "登录指南",
                        "target": "login.md",
                        "label": "登录步骤",
                    }
                ]
            },
        ],
        max_results=None,
    )

    assert [(item["kind"], item["source_path"]) for item in citations] == [
        ("evidence", "policy.pdf"),
        ("okf", "login.md"),
    ]


def test_knowledge_citations_default_to_latest_result_like_answer_context() -> None:
    citations = knowledge_citations_from_results(
        [
            {
                "evidence_pack": [
                    {
                        "chunk_id": "old_chunk",
                        "source_path": "old-policy.pdf",
                        "content": "旧检索结果",
                    }
                ]
            },
            {
                "evidence_pack": [
                    {
                        "chunk_id": "latest_chunk",
                        "source_path": "latest-policy.pdf",
                        "content": "最新检索结果",
                    }
                ]
            },
        ]
    )

    assert len(citations) == 1
    assert citations[0]["source_path"] == "latest-policy.pdf"


def test_knowledge_citations_keep_distinct_chunks_in_the_same_section() -> None:
    citations = knowledge_citations_from_results(
        [
            {
                "evidence_pack": [
                    {
                        "chunk_id": "chunk_1",
                        "section_path": "报销制度",
                        "content": "内容一",
                    },
                    {
                        "chunk_id": "chunk_2",
                        "section_path": "报销制度",
                        "content": "内容二",
                    },
                ]
            }
        ]
    )

    assert [item["chunk_id"] for item in citations] == ["chunk_1", "chunk_2"]


def test_knowledge_citations_keep_distinct_okf_targets_for_one_concept() -> None:
    citations = knowledge_citations_from_results(
        [
            {
                "okf_citations": [
                    {
                        "concept_id": "sources/refund",
                        "target": "refund-a.md",
                        "label": "退款条件 A",
                    },
                    {
                        "concept_id": "sources/refund",
                        "target": "refund-b.md",
                        "label": "退款条件 B",
                    },
                ]
            }
        ]
    )

    assert [item["source_path"] for item in citations] == [
        "refund-a.md",
        "refund-b.md",
    ]


def test_knowledge_citations_use_concept_content_instead_of_summary() -> None:
    content = "完整 Content 段落。" * 120
    citations = knowledge_citations_from_results(
        [
            {
                "selected_concepts": [
                    {
                        "concept_id": "sources/chatgpt-memory/sections/sec-4",
                        "type": "Source Section",
                        "title": "段落组 1",
                        "description": "段落组 1 摘要，不完整。",
                        "content": content,
                        "source_refs": [{"source_path": "memory.md"}],
                    }
                ],
            }
        ]
    )

    assert citations[0]["content"] == content
    assert citations[0]["excerpt"] == content
    assert citations[0]["summary"] == "段落组 1 摘要，不完整。"


def test_knowledge_citations_keep_long_evidence_excerpt_until_display_limit() -> None:
    excerpt = "引用片段" * 900
    citations = knowledge_citations_from_results(
        [
            {
                "evidence_pack": [
                    {
                        "chunk_id": "kchunk_long_excerpt",
                        "document_id": "kdoc_long_excerpt",
                        "bucket_id": "kbucket_long_excerpt",
                        "source_path": "long-citation.md",
                        "section_path": "长引用测试",
                        "summary": "长引用摘要",
                        "excerpt": excerpt,
                    }
                ],
            }
        ]
    )

    assert citations[0]["excerpt"] == excerpt


def test_knowledge_citations_cap_evidence_excerpt_at_display_limit() -> None:
    excerpt = "x" * (CITATION_EXCERPT_CHAR_LIMIT + 16)
    citations = knowledge_citations_from_results(
        [
            {
                "evidence_pack": [
                    {
                        "chunk_id": "kchunk_capped_excerpt",
                        "document_id": "kdoc_capped_excerpt",
                        "bucket_id": "kbucket_capped_excerpt",
                        "source_path": "capped-citation.md",
                        "section_path": "引用上限测试",
                        "summary": "引用上限摘要",
                        "excerpt": excerpt,
                    }
                ],
            }
        ]
    )

    assert citations[0]["excerpt"] == excerpt[:CITATION_EXCERPT_CHAR_LIMIT]


# ---------------------------------------------------------------------------
# 引用编号一致性：正文里的 [n] 必须能在卡片列表里找到同号卡片
# ---------------------------------------------------------------------------


def test_citation_identity_collapses_duplicate_copies_of_one_chunk() -> None:
    """同一文档同一页被两个 bucket 重复收录：chunk_id 不同但正文一致。"""
    original = {
        "kind": "evidence",
        "document_id": "kdoc_project",
        "section_path": "PDF 文档 / 第 4 页",
        "chunk_id": "kchunk_a",
        "bucket_id": "kbucket_a",
        "excerpt": "年客流 2000 万人次。",
    }
    duplicated = {**original, "chunk_id": "kchunk_b", "bucket_id": "kbucket_b"}

    assert citation_identity(original) == citation_identity(duplicated)


def test_citation_identity_keeps_distinct_passages_on_the_same_page() -> None:
    shared = {
        "kind": "evidence",
        "document_id": "kdoc_project",
        "section_path": "PDF 文档 / 第 4 页",
    }
    first = {**shared, "chunk_id": "kchunk_a", "excerpt": "第一段。"}
    second = {**shared, "chunk_id": "kchunk_b", "excerpt": "第二段。"}

    assert citation_identity(first) != citation_identity(second)
    # 但展示上仍算同一张卡：位置相同，片段留在详情弹窗里。
    assert citation_display_key(first) == citation_display_key(second)


def test_citation_display_key_separates_same_page_of_different_documents() -> None:
    first = {
        "title": "PDF 文档 / 第 3 页",
        "document_id": "kdoc_a",
        "section_path": "PDF 文档 / 第 3 页",
    }
    second = {**first, "document_id": "kdoc_b"}

    assert citation_display_key(first) != citation_display_key(second)


def test_knowledge_citations_dedupe_redundant_copies_with_contiguous_labels() -> None:
    citations = knowledge_citations_from_results(
        [
            {
                "evidence_pack": [
                    {
                        "chunk_id": "kchunk_a",
                        "document_id": "kdoc_project",
                        "bucket_id": "kbucket_a",
                        "source_path": "project.pdf",
                        "section_path": "PDF 文档 / 第 4 页",
                        "excerpt": "年客流 2000 万人次。",
                    },
                    {
                        # 重复收录：同文档、同页、同内容，只有 chunk/bucket 不同。
                        "chunk_id": "kchunk_b",
                        "document_id": "kdoc_project",
                        "bucket_id": "kbucket_b",
                        "source_path": "project.pdf",
                        "section_path": "PDF 文档 / 第 4 页",
                        "excerpt": "年客流 2000 万人次。",
                    },
                    {
                        "chunk_id": "kchunk_c",
                        "document_id": "kdoc_project",
                        "bucket_id": "kbucket_a",
                        "source_path": "project.pdf",
                        "section_path": "PDF 文档 / 第 5 页",
                        "excerpt": "乐园全年龄段适配。",
                    },
                ]
            }
        ]
    )

    assert [item["chunk_id"] for item in citations] == ["kchunk_a", "kchunk_c"]
    assert [item["label"] for item in citations] == ["[1]", "[2]"]
    assert [item["id"] for item in citations] == ["kref_1", "kref_2"]
    assert citations[0]["display_key"]


def test_renumber_citations_keeps_labels_contiguous_and_ids_unique() -> None:
    renumbered = renumber_citations(
        [
            {"id": "kref_1", "label": "[1]", "chunk_id": "chunk-a"},
            {"id": "kref_1", "label": "[1]", "chunk_id": "chunk-b"},
            {"id": "kref_4", "label": "[4]", "chunk_id": "chunk-c"},
        ]
    )

    assert [item["label"] for item in renumbered] == ["[1]", "[2]", "[3]"]
    assert [item["id"] for item in renumbered] == ["kref_1", "kref_2", "kref_3"]
    assert len({item["id"] for item in renumbered}) == 3


def test_globalize_citations_renumbers_across_frames_and_drops_duplicates() -> None:
    """各任务帧各自从 kref_1 起编号，全局化后不能撞 id，也不能跳号。"""
    from app.core.engine.turn import _globalize_citations
    from app.core.base.request_compiler import TaskExecutionResult

    shared_excerpt = "年客流 2000 万人次。"
    first = TaskExecutionResult(
        task_frame_id="task-1",
        status="completed",
        citations=[
            {
                "id": "kref_1",
                "label": "[1]",
                "kind": "evidence",
                "document_id": "kdoc_project",
                "section_path": "PDF 文档 / 第 4 页",
                "chunk_id": "kchunk_a",
                "excerpt": shared_excerpt,
            }
        ],
    )
    second = TaskExecutionResult(
        task_frame_id="task-2",
        status="completed",
        citations=[
            {
                "id": "kref_1",
                "label": "[1]",
                "kind": "evidence",
                "document_id": "kdoc_project",
                "section_path": "PDF 文档 / 第 4 页",
                "chunk_id": "kchunk_b",
                "excerpt": shared_excerpt,
            },
            {
                "id": "kref_2",
                "label": "[2]",
                "kind": "evidence",
                "document_id": "kdoc_project",
                "section_path": "PDF 文档 / 第 5 页",
                "chunk_id": "kchunk_c",
                "excerpt": "乐园全年龄段适配。",
            },
        ],
    )

    citations = _globalize_citations([first, second])

    assert [item["label"] for item in citations] == ["[1]", "[2]"]
    assert [item["id"] for item in citations] == ["kref_1", "kref_2"]
    # 第二帧里重复的那条被折叠，两帧共用同一条编号。
    assert [item["label"] for item in first.citations] == ["[1]"]
    assert [item["label"] for item in second.citations] == ["[1]", "[2]"]
