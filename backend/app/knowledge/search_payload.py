"""知识检索结果 → 模型可见内联载荷的精简投影。

背景：``knowledge_search`` 的完整结果（chunks + evidence_pack + buckets + concepts +
expanded_sections + documents）实测 80–160KB，远超 ``read_file`` 的 25KB 单次上限。
旧实现把整包 JSON 落成沙箱文件，模型只能看到 ``kind=sandbox_json_file`` 指针：读不全 →
换词重搜 → 白白多跑两轮循环（实测 35s 的问答里 13s 花在这里）。

本模块把同一份结果压成 ≤ :data:`KNOWLEDGE_PAYLOAD_MAX_CHARS` 的内联载荷，直接进
tool result：

- 删掉重复/无信息的键：``expanded_sections``（下游无人读）、``trace``（与
  ``route_trace`` 完全等价）、桶 metadata 里的 ``content`` 原文（最大头，且与 chunk
  正文重复）、evidence 里的 ``excerpt``（``content`` 的副本）、chunk metadata 里的
  ``context_window``/``source_span``/``ingest_schema_version`` 等入库元数据。
- 正文按预算裁剪，超出预算时按 ``chunk_id`` 用 ``read_knowledge_chunk`` 取全文。

下游契约刻意保持不变（避免「为了一个 case 动坏其他链路」）：

- ``evidence_pack`` 仍是引用与回答上下文的**规范条目**，键集与原来一致（只少了重复的
  ``excerpt``、正文变短）；``knowledge_citations_from_results`` /
  ``compact_knowledge_context`` / ``message_read`` 的回填链路全部照旧。
- ``chunks`` 退化为不带正文的定位索引（chunk_id / 文档 / 页码 / 标题 / 摘要），
  只有当 ``evidence_pack`` 为空（本次没有可用证据）时才补回正文——保证「引用兜底到
  chunk 层」这条链路不会因为精简而断掉。
- ``selected_concepts`` / ``okf_citations`` / ``selected_documents`` 保留，因为
  它们分别是 OKF-only 知识库的引用来源和路由证据。
"""

from __future__ import annotations

import json
from typing import Any

#: 内联载荷（``data`` 的序列化长度）上限。与 ``read_file`` 的 25KB 单次上限同量级：
#: 模型一次就能读完，不再需要「指针 → 读文件 → 读不全 → 重搜」的往返。
KNOWLEDGE_PAYLOAD_MAX_CHARS = 24_000
#: 命中条数的免费基数：≤ 这个数时预算恒为 :data:`KNOWLEDGE_PAYLOAD_MAX_CHARS`。
KNOWLEDGE_PAYLOAD_BASE_ENTRIES = 10
#: 超过基数后每条追加的预算。一篇 FAQ 被切成多个 chunk 时命中数会到 15~20 条，
#: 固定 24K 只能把正文裁到下限、甚至整包落成沙箱文件（模型得 ``read_file`` 盲读）。
KNOWLEDGE_PAYLOAD_CHARS_PER_ENTRY = 1_500
#: 内联预算硬上限：再大就不如落文件，避免把上下文窗口塞爆。
KNOWLEDGE_PAYLOAD_HARD_MAX_CHARS = 60_000
#: 单条证据正文的初始预算。8 条 × 1500 ≈ 12K，加上定位字段与其余区块仍在上限内。
KNOWLEDGE_EVIDENCE_CONTENT_CHAR_LIMIT = 1_500
#: 兜底 chunk 正文（仅 evidence_pack 为空时使用）。
KNOWLEDGE_CHUNK_CONTENT_CHAR_LIMIT = 1_500
#: OKF Wiki 页面正文预算。
KNOWLEDGE_CONCEPT_CONTENT_CHAR_LIMIT = 900
#: 摘要类字段统一上限。
KNOWLEDGE_SUMMARY_CHAR_LIMIT = 300
#: 文档卡片摘要上限。
KNOWLEDGE_DOCUMENT_SUMMARY_CHAR_LIMIT = 400
#: 预算打不下时的收缩下限与步长。
_KNOWLEDGE_CONTENT_FLOOR = 200
_KNOWLEDGE_CONTENT_SHRINK = 0.6

#: chunk 上保留的 metadata 键：定位 + 同组关系（引用分组要用），
#: 其余（context_window / source_span / ingest_schema_version / node_type / section_id /
#: related_next_chunk_id / related_previous_chunk_id）对模型与引用都无信息。
_CHUNK_METADATA_KEYS = (
    "section_path",
    "section_title",
    "bucket_title",
    "related_group_id",
    "related_chunk_ids",
    "related_chunk_index",
)
_CONCEPT_SOURCE_REF_KEYS = ("source_path", "document_id", "section_path", "page", "target")
_DOCUMENT_OUTLINE_LIMIT = 12
_BUCKET_SECTION_PATH_LIMIT = 4


def knowledge_payload_budget(payload: dict[str, Any]) -> int:
    """本次检索允许直连内联的载荷预算（按命中条数自适应）。

    固定 24K 在「命中 10 条左右」时刚好够；命中 15~20 条时，即使正文压到下限
    也塞不进去，整包被换成沙箱文件指针——模型拿不到证据只能 `read_file` 盲读
    JSON（单次 25KB，还要接力），再 `read_knowledge_chunk` 取原文，一次问答白白
    多两次模型往返。这里按条数线性放大预算，并设硬上限。

    ``_persist_large_json_result`` 必须用同一个返回值做落盘判定，否则「压缩放宽、
    落盘阈值不变」等于白改。
    """

    if not isinstance(payload, dict):
        return KNOWLEDGE_PAYLOAD_MAX_CHARS
    entries = 0
    for key in ("evidence_pack", "chunks"):
        value = payload.get(key)
        if isinstance(value, list):
            entries = max(entries, len(value))
    extra_entries = max(0, entries - KNOWLEDGE_PAYLOAD_BASE_ENTRIES)
    return min(
        KNOWLEDGE_PAYLOAD_HARD_MAX_CHARS,
        KNOWLEDGE_PAYLOAD_MAX_CHARS + extra_entries * KNOWLEDGE_PAYLOAD_CHARS_PER_ENTRY,
    )


def compact_knowledge_search_payload(
    payload: dict[str, Any],
    *,
    max_chars: int = KNOWLEDGE_PAYLOAD_MAX_CHARS,
) -> dict[str, Any]:
    """把完整检索结果投影成可内联进 tool result 的精简载荷。

    返回的 dict 结构与 ``KnowledgeSearchResponse.model_dump()`` 对齐（下游按同一
    组键读取），只是在预算内。仍然超限时由调用方按既有逻辑落沙箱文件兜底——
    本函数不会抛异常，也绝不返回比输入更大的结果。
    """

    if not isinstance(payload, dict):
        return {}
    evidence = [
        item for item in payload.get("evidence_pack") or [] if isinstance(item, dict)
    ]
    content_limit = KNOWLEDGE_EVIDENCE_CONTENT_CHAR_LIMIT
    concept_limit = KNOWLEDGE_CONCEPT_CONTENT_CHAR_LIMIT
    while True:
        compacted = _project(
            payload, evidence, content_limit=content_limit, concept_limit=concept_limit
        )
        if serialized_chars(compacted) <= max(1, max_chars):
            return compacted
        if content_limit <= _KNOWLEDGE_CONTENT_FLOOR and concept_limit <= 120:
            return compacted
        content_limit = max(
            _KNOWLEDGE_CONTENT_FLOOR, int(content_limit * _KNOWLEDGE_CONTENT_SHRINK)
        )
        concept_limit = max(120, int(concept_limit * _KNOWLEDGE_CONTENT_SHRINK))


def serialized_chars(value: Any) -> int:
    """与工具结果落盘/内联判定口径一致的序列化长度。"""

    try:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                default=str,
            )
        )
    except (TypeError, ValueError):
        return KNOWLEDGE_PAYLOAD_MAX_CHARS + 1


def _project(
    payload: dict[str, Any],
    evidence: list[dict[str, Any]],
    *,
    content_limit: int,
    concept_limit: int,
) -> dict[str, Any]:
    titles = _document_titles(payload)
    has_evidence = bool(evidence)
    # evidence_pack 为空时 chunk 层是唯一的引用来源，必须带上正文。
    chunk_content_limit = 0 if has_evidence else content_limit
    return {
        "chunk_count": len(payload.get("chunks") or []),
        "chunks": [
            entry
            for item in payload.get("chunks") or []
            if isinstance(item, dict)
            for entry in [_chunk_entry(item, titles, content_limit=chunk_content_limit)]
            if entry
        ],
        "evidence_pack": [
            entry
            for item in evidence
            for entry in [_evidence_entry(item, content_limit=content_limit)]
            if entry
        ],
        "selected_documents": [
            entry
            for item in payload.get("selected_documents") or []
            if isinstance(item, dict)
            for entry in [_document_entry(item)]
            if entry
        ],
        "selected_buckets": [
            entry
            for item in payload.get("selected_buckets") or []
            if isinstance(item, dict)
            for entry in [_bucket_entry(item)]
            if entry
        ],
        "selected_concepts": [
            entry
            for item in payload.get("selected_concepts") or []
            if isinstance(item, dict)
            for entry in [_concept_entry(item, content_limit=concept_limit)]
            if entry
        ],
        "okf_citations": [
            dict(item)
            for item in payload.get("okf_citations") or []
            if isinstance(item, dict)
        ],
        "route_trace": [
            entry
            for item in payload.get("route_trace") or []
            if isinstance(item, dict)
            for entry in [_trace_entry(item)]
            if entry
        ],
    }


def _chunk_entry(
    item: dict[str, Any],
    titles: dict[str, str],
    *,
    content_limit: int,
) -> dict[str, Any]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    document_id = str(item.get("document_id") or "").strip()
    entry: dict[str, Any] = {
        "chunk_id": str(item.get("id") or item.get("chunk_id") or "").strip(),
        "document_id": document_id or None,
        "bucket_id": item.get("bucket_id"),
        "chunk_index": item.get("chunk_index"),
        "title": _clip(
            titles.get(document_id)
            or str(metadata.get("bucket_title") or "").strip()
            or str(item.get("source_ref") or "").strip(),
            KNOWLEDGE_SUMMARY_CHAR_LIMIT,
        ),
        "section_path": metadata.get("section_path"),
        "page": _page_label(metadata),
        "summary": _clip(item.get("summary"), KNOWLEDGE_SUMMARY_CHAR_LIMIT),
        "source_ref": item.get("source_ref"),
        "metadata": {
            key: metadata[key] for key in _CHUNK_METADATA_KEYS if metadata.get(key) not in (None, "", [], {})
        },
    }
    content = _clip(item.get("content"), content_limit)
    if content:
        entry["content"] = content
    return _drop_empty(entry)


def _evidence_entry(item: dict[str, Any], *, content_limit: int) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "chunk_id": str(item.get("chunk_id") or item.get("id") or "").strip(),
        "document_id": item.get("document_id"),
        "bucket_id": item.get("bucket_id"),
        "source_path": item.get("source_path"),
        "section_path": item.get("section_path"),
        "summary": _clip(item.get("summary"), KNOWLEDGE_SUMMARY_CHAR_LIMIT),
        "content": _clip(item.get("content") or item.get("excerpt"), content_limit),
        "relevance_score": item.get("relevance_score"),
        "confidence_reason": item.get("confidence_reason"),
        "related_group_id": item.get("related_group_id"),
        "related_chunk_ids": [
            str(value)
            for value in (item.get("related_chunk_ids") or [])
            if str(value).strip()
        ],
    }
    return _drop_empty(entry)


def _document_entry(item: dict[str, Any]) -> dict[str, Any]:
    return _drop_empty(
        {
            "id": item.get("id"),
            "knowledge_base_id": item.get("knowledge_base_id"),
            "title": item.get("title"),
            "filename": item.get("filename"),
            "file_type": item.get("file_type"),
            "summary": _clip(item.get("summary"), KNOWLEDGE_DOCUMENT_SUMMARY_CHAR_LIMIT),
            "outline": _labels(item.get("outline"), _DOCUMENT_OUTLINE_LIMIT, 60),
            "section_count": item.get("section_count"),
            "chunk_count": item.get("chunk_count"),
        }
    )


def _bucket_entry(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    return _drop_empty(
        {
            "id": item.get("id"),
            "document_id": item.get("document_id"),
            "bucket_key": item.get("bucket_key"),
            "title": item.get("title"),
            "summary": _clip(item.get("summary"), KNOWLEDGE_SUMMARY_CHAR_LIMIT),
            "chunk_count": item.get("chunk_count") or metadata.get("chunk_count"),
            "token_estimate": item.get("token_estimate"),
            "bucket_type": metadata.get("bucket_type"),
            "section_paths": _labels(
                metadata.get("section_paths"), _BUCKET_SECTION_PATH_LIMIT, 60
            ),
        }
    )


def _concept_entry(item: dict[str, Any], *, content_limit: int) -> dict[str, Any]:
    source_refs = item.get("source_refs") if isinstance(item.get("source_refs"), list) else []
    return _drop_empty(
        {
            "concept_id": str(item.get("concept_id") or item.get("id") or "").strip(),
            "type": item.get("type"),
            "title": item.get("title"),
            "description": _clip(item.get("description"), KNOWLEDGE_SUMMARY_CHAR_LIMIT),
            "content": _clip(item.get("content") or item.get("content_md"), content_limit),
            "source_refs": [
                {
                    key: ref[key]
                    for key in _CONCEPT_SOURCE_REF_KEYS
                    if isinstance(ref, dict) and ref.get(key) not in (None, "")
                }
                for ref in source_refs
                if isinstance(ref, dict)
            ],
        }
    )


def _trace_entry(item: dict[str, Any]) -> dict[str, Any]:
    return _drop_empty(
        {
            "phase": item.get("phase"),
            "message": _clip(item.get("message"), 120),
        }
    )


def _document_titles(payload: dict[str, Any]) -> dict[str, str]:
    titles: dict[str, str] = {}
    for item in payload.get("selected_documents") or []:
        if not isinstance(item, dict):
            continue
        document_id = str(item.get("id") or "").strip()
        if not document_id:
            continue
        titles[document_id] = str(item.get("title") or item.get("filename") or "").strip()
    return titles


def _page_label(metadata: dict[str, Any]) -> str:
    section_title = str(metadata.get("section_title") or "").strip()
    if section_title:
        return section_title
    section_path = str(metadata.get("section_path") or "").strip()
    if not section_path:
        return ""
    return section_path.rsplit("/", 1)[-1].strip()


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}…"


def _labels(value: Any, limit: int, item_limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        text
        for item in value[:limit]
        for text in [_clip(item, item_limit)]
        if text
    ]


def _drop_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if item is not None and item != "" and item != [] and item != {}
    }
