from app.knowledge.citations import knowledge_citations_from_results


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
                        "excerpt": "UltraRAG4 引用测试规则。",
                    }
                ],
            }
        ]
    )

    assert citations[0]["kind"] == "concept"
    assert citations[0]["title"] == "前端编码规范"
    assert citations[0]["source_path"] == "vue3-coding-standards.md"
