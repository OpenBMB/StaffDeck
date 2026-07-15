from app.knowledge.citations import (
    CITATION_EXCERPT_CHAR_LIMIT,
    compact_knowledge_citation_labels,
    knowledge_citations_from_results,
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


def test_knowledge_citations_prefer_wiki_concepts_over_evidence_pack() -> None:
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

    assert citations[0]["kind"] == "concept"
    assert citations[0]["title"] == "前端编码规范"
    assert citations[0]["source_path"] == "vue3-coding-standards.md"


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
