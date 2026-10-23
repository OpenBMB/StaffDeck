"""知识检索结果精简投影（app/knowledge/search_payload.py）的行为契约。

重点不是「压到多少字符」，而是**下游看到的引用与回答上下文必须与改动前一致**：
``evidence_pack`` 键集不变（只少了与 content 重复的 excerpt），正文按预算裁剪；
``chunks`` 退化为定位索引（页码/标题/摘要），只有 evidence_pack 为空时才带正文。
"""

from __future__ import annotations

import json

from app.core.base.context_projection import compact_knowledge_context
from app.knowledge.citations import knowledge_citations_from_results
from app.knowledge.search_payload import (
    KNOWLEDGE_PAYLOAD_HARD_MAX_CHARS,
    KNOWLEDGE_PAYLOAD_MAX_CHARS,
    compact_knowledge_search_payload,
    knowledge_payload_budget,
    serialized_chars,
)


def _chunk(index: int, *, content: str | None = None, with_content: bool = True) -> dict:
    chunk_id = f"kchunk_{index:04d}"
    metadata = {
        "section_path": f"PDF 文档 / 第 {index} 页",
        "section_title": f"第 {index} 页",
        "bucket_title": "PDF 文档",
        "related_group_id": None,
        "related_chunk_index": 0,
        "related_chunk_ids": [],
        # 入库元数据：对模型与引用都无信息，必须被精简掉
        "context_window": "上下文窗口" * 60,
        "source_span": {"start_paragraph": 1, "end_paragraph": 2},
        "ingest_schema_version": 2,
        "node_type": "evidence_chunk",
        "section_id": f"sec_{index}",
        "related_next_chunk_id": None,
        "related_previous_chunk_id": None,
    }
    item = {
        "id": chunk_id,
        "tenant_id": "tenant-demo",
        "knowledge_base_id": "kb-1",
        "document_id": "kdoc_1",
        "bucket_id": f"kbucket_{index}",
        "chunk_index": index,
        "content": content if content is not None else f"第 {index} 页正文" * 40,
        "summary": f"第 {index} 页摘要",
        "source_ref": f"手册.pdf / PDF 文档 / 第 {index} 页 / evidence {index}",
        "metadata": metadata,
        "created_at": "2026-09-17T00:00:00",
        "updated_at": "2026-09-17T00:00:00",
    }
    if not with_content:
        item.pop("content")
    return item


def _evidence(index: int, *, content: str | None = None) -> dict:
    text = content if content is not None else f"第 {index} 页正文" * 40
    return {
        "chunk_id": f"kchunk_{index:04d}",
        "related_group_id": None,
        "related_chunk_ids": [],
        "related_chunk_index": 0,
        "document_id": "kdoc_1",
        "bucket_id": f"kbucket_{index}",
        "source_path": f"手册.pdf / PDF 文档 / 第 {index} 页 / evidence {index}",
        "section_path": f"PDF 文档 / 第 {index} 页",
        "summary": f"第 {index} 页摘要",
        "content": text,
        "excerpt": text,
        "relevance_score": 3.2,
        "confidence_reason": "章节标题、路径、摘要或正文与查询相关",
    }


def _bucket(index: int) -> dict:
    return {
        "id": f"kbucket_{index}",
        "tenant_id": "tenant-demo",
        "knowledge_base_id": "kb-1",
        "document_id": "kdoc_1",
        "bucket_key": f"bucket-{index}",
        "title": "PDF 文档",
        "summary": "整份文档的结构索引",
        "token_estimate": 1_500,
        "chunk_count": 23,
        "status": "ready",
        "metadata": {
            "bucket_type": "structure",
            # 体积大头：整桶原文，与 chunk 正文重复
            "content": "整桶原文" * 2_000,
            "document_card": {"title": "手册", "summary": "文档摘要" * 50},
            "section_paths": [f"PDF 文档 / 第 {i} 页" for i in range(11)],
            "section_ids": [f"sec_{i}" for i in range(11)],
            "representative_chunk_ids": ["kchunk_0001", "kchunk_0002"],
            "quality": {"status": "ready", "warnings": []},
            "applicable_query_types": ["answer"],
            "ingest_schema_version": 2,
        },
        "created_at": "2026-09-17T00:00:00",
        "updated_at": "2026-09-17T00:00:00",
    }


def _payload(*, evidence: bool = True, chunk_count: int = 8) -> dict:
    return {
        "selected_buckets": [_bucket(index) for index in range(1, 5)],
        "chunks": [_chunk(index) for index in range(1, chunk_count + 1)],
        "trace": [{"phase": "read_chunks", "message": "读取引用来源"}],
        "route_trace": [{"phase": "read_chunks", "message": "读取引用来源"}],
        "selected_documents": [
            {
                "id": "kdoc_1",
                "knowledge_base_id": "kb-1",
                "title": "Hi Sphere 手册",
                "filename": "手册.pdf",
                "file_type": "pdf",
                "summary": "文档摘要" * 60,
                "outline": [f"第 {i} 章" for i in range(20)],
                "key_entities": ["Hi Sphere", "复星旅文"],
                "section_count": 24,
                "chunk_count": 23,
                "updated_at": "2026-09-17T00:00:00",
            }
        ],
        "selected_concepts": [
            {
                "id": "kconcept_1",
                "concept_id": "concept-project",
                "type": "Topic",
                "title": "项目概况",
                "description": "项目定位",
                "links": [],
                "citations": [{"target": "手册.pdf", "label": "1"}],
                "source_refs": [{"source_path": "手册.pdf", "document_id": "kdoc_1"}],
                "content": "Wiki 正文" * 900,
            }
        ],
        "okf_citations": [
            {"concept_id": "concept-project", "title": "项目概况", "label": "1", "target": "手册.pdf"}
        ],
        # 下游无人读取，纯体积
        "expanded_sections": [
            {"path": f"PDF 文档 / 第 {i} 页", "content": "章节正文" * 600} for i in range(24)
        ],
        "evidence_pack": [_evidence(index) for index in range(1, 6)] if evidence else [],
    }


def _citation_fingerprint(citations: list[dict]) -> list[tuple]:
    return [
        (
            item.get("id"),
            item.get("label"),
            item.get("kind"),
            item.get("title"),
            item.get("section_path"),
            item.get("source_path"),
            item.get("chunk_id"),
            item.get("document_id"),
            str(item.get("content") or ""),
        )
        for item in citations
    ]


def test_compaction_drops_duplicate_and_unread_keys() -> None:
    payload = _payload()
    compacted = compact_knowledge_search_payload(payload)

    assert serialized_chars(payload) > 100_000
    assert serialized_chars(compacted) <= KNOWLEDGE_PAYLOAD_MAX_CHARS
    # 与 route_trace 完全等价的 trace、下游无人读取的 expanded_sections 都不再下发
    assert "trace" not in compacted
    assert "expanded_sections" not in compacted
    # 桶只留定位与摘要：整桶原文（metadata.content）是最大头
    assert all("metadata" not in bucket for bucket in compacted["selected_buckets"])
    assert all(
        "content" not in bucket.get("metadata", {})
        for bucket in compacted["selected_buckets"]
    )
    # chunk metadata 只留定位字段
    chunk_metadata = compacted["chunks"][0]["metadata"]
    assert set(chunk_metadata) == {
        "section_path",
        "section_title",
        "bucket_title",
        "related_chunk_index",
    }
    assert "context_window" not in chunk_metadata
    # evidence 里 excerpt 是 content 的副本
    assert all("excerpt" not in item for item in compacted["evidence_pack"])
    assert all(item.get("content") for item in compacted["evidence_pack"])


def test_compaction_keeps_citation_contract_identical() -> None:
    payload = _payload()

    compacted = compact_knowledge_search_payload(payload)

    raw_citations = knowledge_citations_from_results([payload])
    slim_citations = knowledge_citations_from_results([compacted])
    assert raw_citations
    assert _citation_fingerprint(raw_citations) == _citation_fingerprint(slim_citations)


def test_compaction_keeps_answer_context_identical() -> None:
    payload = _payload()

    compacted = compact_knowledge_search_payload(payload)

    assert compact_knowledge_context([payload]) == compact_knowledge_context([compacted])


def test_compaction_exposes_page_title_and_score_for_each_chunk() -> None:
    compacted = compact_knowledge_search_payload(_payload())
    first = compacted["chunks"][0]

    assert first["chunk_id"] == "kchunk_0001"
    assert first["title"] == "Hi Sphere 手册"
    assert first["page"] == "第 1 页"
    assert first["section_path"] == "PDF 文档 / 第 1 页"
    assert first["summary"]
    # 有 evidence_pack 时 chunk 层不重复正文
    assert "content" not in first
    assert compacted["evidence_pack"][0]["relevance_score"] == 3.2


def test_compaction_keeps_chunk_content_when_evidence_pack_is_empty() -> None:
    compacted = compact_knowledge_search_payload(_payload(evidence=False))

    assert compacted["evidence_pack"] == []
    assert compacted["chunks"][0]["content"]
    # evidence_pack 为空时 chunk 层是唯一引用来源，引用必须仍能成立
    assert len(knowledge_citations_from_results([compacted])) == 4


def test_compaction_shrinks_content_to_meet_budget() -> None:
    payload = _payload(evidence=True)
    for item in payload["evidence_pack"]:
        item["content"] = "很长的正文" * 4_000
        item["excerpt"] = item["content"]

    compacted = compact_knowledge_search_payload(payload, max_chars=12_000)

    assert serialized_chars(compacted) <= 12_000
    assert all(item.get("content", "").endswith("…") for item in compacted["evidence_pack"])


def test_compaction_stops_at_floor_when_budget_is_unreachable() -> None:
    payload = _payload(evidence=True)
    for item in payload["evidence_pack"]:
        item["content"] = "很长的正文" * 4_000

    # 结构性字段本身已超预算：必须终止并返回最小形态，交由落沙箱文件兜底，而不是死循环
    compacted = compact_knowledge_search_payload(payload, max_chars=200)

    assert serialized_chars(compacted) > 200
    assert all(len(item.get("content", "")) <= 220 for item in compacted["evidence_pack"])


def test_compaction_is_defensive_for_unexpected_payloads() -> None:
    assert compact_knowledge_search_payload({}) == {
        "chunk_count": 0,
        "chunks": [],
        "evidence_pack": [],
        "selected_documents": [],
        "selected_buckets": [],
        "selected_concepts": [],
        "okf_citations": [],
        "route_trace": [],
    }
    assert compact_knowledge_search_payload(None) == {}  # type: ignore[arg-type]
    payload = {"chunks": ["not-a-dict", None], "evidence_pack": [{"chunk_id": "x"}]}
    compacted = compact_knowledge_search_payload(payload)
    assert compacted["chunks"] == []
    assert compacted["evidence_pack"] == [{"chunk_id": "x"}]


def test_serialized_chars_matches_json_dump_used_by_persistence() -> None:
    value = {"a": "中文", "b": [1, 2, 3]}
    assert serialized_chars(value) == len(
        json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    )


def test_budget_scales_with_hit_count() -> None:
    """内联预算按命中条数自适应：≤10 条维持 24K，再多按条追加，封顶 60K。"""

    assert knowledge_payload_budget({"evidence_pack": [_evidence(1)] * 10}) == (
        KNOWLEDGE_PAYLOAD_MAX_CHARS
    )
    assert knowledge_payload_budget({"evidence_pack": [_evidence(1)] * 19}) == 37_500
    assert knowledge_payload_budget({"chunks": [_chunk(1)] * 100}) == (
        KNOWLEDGE_PAYLOAD_HARD_MAX_CHARS
    )


def test_large_hit_sets_fit_the_scaled_budget() -> None:
    """19 条命中的真实场景：固定 24K 压到下限仍超预算（会落沙箱文件），
    自适应预算下必须能完整内联，模型不必 read_file 盲读。"""

    payload = _payload(chunk_count=19)
    payload["evidence_pack"] = [_evidence(index) for index in range(1, 20)]
    budget = knowledge_payload_budget(payload)

    compacted = compact_knowledge_search_payload(payload, max_chars=budget)

    assert budget > KNOWLEDGE_PAYLOAD_MAX_CHARS
    assert serialized_chars(compacted) <= budget
    assert len(compacted["evidence_pack"]) == 19
