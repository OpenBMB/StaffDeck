from __future__ import annotations
import json
import re
from datetime import timedelta
from typing import Any
from ..citations import CITATION_EXCERPT_CHAR_LIMIT

SECTION_TARGET_CHARS = 1400

EVIDENCE_CHUNK_CHARS = 900

BUCKET_SECTION_CHARS = 6000

PARAGRAPH_GROUP_CHARS = 4200

SEARCH_DOCUMENT_ROUTE_LIMIT = 120

SEARCH_BUCKET_ROUTE_LIMIT = 160

TERMINAL_INGEST_STATUSES = {"succeeded", "failed", "cancelled"}

CANCELLING_INGEST_STATUSES = {"cancel_requested", "cancelled"}

CANCEL_REQUEST_STALE_AFTER = timedelta(seconds=15)

SEARCH_MIN_DOCUMENT_SCORE = 2.0

SEARCH_MIN_BUCKET_SCORE = 2.0

SEARCH_MIN_CHUNK_SCORE = 2.0

SEARCH_MIN_EVIDENCE_SCORE = 2.0

RELATED_CHUNK_MAX_COUNT = 6

RELATED_CHUNK_MAX_CHARS = 4800

KNOWLEDGE_INGEST_SCHEMA_VERSION = 2

QUERY_NOISE_PHRASES = (
    "我想知道",
    "麻烦帮我",
    "什么时候",
    "告诉我",
    "请问",
    "怎么",
    "如何",
    "什么",
    "哪些",
    "哪个",
    "一下",
)

INGEST_STAGES: list[dict[str, Any]] = [
    {"key": "queued", "label": "排队中", "progress": 0.0},
    {"key": "parsing", "label": "解析原始资料", "progress": 0.08},
    {"key": "normalizing", "label": "规范化 Source", "progress": 0.16},
    {"key": "documenting", "label": "写入 Source Document", "progress": 0.24},
    {"key": "bucketing", "label": "规划 Wiki 页面", "progress": 0.36},
    {"key": "bucket_writing", "label": "写入 OKF Wiki", "progress": 0.48},
    {"key": "chunking", "label": "生成引用来源", "progress": 0.62},
    {"key": "summarizing", "label": "刷新 PageIndex", "progress": 0.74},
    {"key": "discovering", "label": "发现 SOP/工具", "progress": 0.88},
    {"key": "done", "label": "完成入库", "progress": 1.0},
]

INGEST_STAGE_BY_KEY = {stage["key"]: stage for stage in INGEST_STAGES}

def _normalize_text(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    compact: list[str] = []
    blank = False
    for line in lines:
        if not line.strip():
            if not blank:
                compact.append("")
            blank = True
            continue
        compact.append(line)
        blank = False
    return "\n".join(compact).strip()

def _split_sections(text: str) -> list[str]:
    paragraphs = _paragraph_blocks(text)
    if not paragraphs:
        return [text[:BUCKET_SECTION_CHARS]]

    sections: list[str] = []
    current: list[str] = []
    current_len = 0

    for paragraph in paragraphs:
        parts = _split_large_paragraph(paragraph, BUCKET_SECTION_CHARS)
        for part in parts:
            is_heading = _looks_like_heading(part)
            projected = current_len + len(part) + (2 if current else 0)
            if current and (is_heading or projected > PARAGRAPH_GROUP_CHARS):
                sections.append("\n\n".join(current).strip())
                current = []
                current_len = 0
            current.append(part)
            current_len += len(part) + (2 if current_len else 0)

    if current:
        sections.append("\n\n".join(current).strip())
    return [section for section in sections if section.strip()] or [text[:BUCKET_SECTION_CHARS]]

def _chunk_text(text: str, max_chars: int) -> list[str]:
    paragraphs = _paragraph_blocks(text)
    if paragraphs:
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for paragraph in paragraphs:
            for part in _split_large_paragraph(paragraph, max_chars):
                projected = current_len + len(part) + (2 if current else 0)
                if current and projected > max_chars:
                    chunks.append("\n\n".join(current).strip())
                    current = []
                    current_len = 0
                current.append(part)
                current_len += len(part) + (2 if current_len else 0)
        if current:
            chunks.append("\n\n".join(current).strip())
        return [chunk for chunk in chunks if chunk.strip()]

    chunks: list[str] = []
    cursor = 0
    while cursor < len(text):
        end = min(len(text), cursor + max_chars)
        if end < len(text):
            boundary = max(text.rfind("\n\n", cursor, end), text.rfind("\n", cursor, end))
            if boundary > cursor + max_chars // 2:
                end = boundary
        chunk = text[cursor:end].strip()
        if chunk:
            chunks.append(chunk)
        cursor = max(end, cursor + 1)
    return chunks or [text.strip()]

def _chunk_text_related_groups(text: str, max_chars: int) -> list[list[str]]:
    paragraphs = _paragraph_blocks(text)
    if not paragraphs:
        chunks = _chunk_text(text, max_chars)
        return [chunks] if chunks else []

    groups: list[list[str]] = []
    current: list[str] = []
    current_len = 0

    def flush_current() -> None:
        nonlocal current, current_len
        if current:
            groups.append(["\n\n".join(current).strip()])
            current = []
            current_len = 0

    for paragraph in paragraphs:
        if _heading_info(paragraph):
            flush_current()
            continue
        parts = _split_large_paragraph(paragraph, max_chars)
        if len(parts) > 1:
            flush_current()
            groups.append(parts)
            continue
        part = parts[0]
        projected = current_len + len(part) + (2 if current else 0)
        if current and projected > max_chars:
            flush_current()
        current.append(part)
        current_len += len(part) + (2 if current_len else 0)
    flush_current()
    return [group for group in groups if any(part.strip() for part in group)]

def _paragraph_blocks(text: str) -> list[str]:
    normalized = _normalize_text(text)
    if not normalized:
        return []
    blocks = [block.strip() for block in re.split(r"\n\s*\n+", normalized) if block.strip()]
    if len(blocks) > 1:
        return blocks
    # Plain exported documents often lose blank lines. Fall back to single lines
    # so long documents still get incremental discovery instead of one huge block.
    return [line.strip() for line in normalized.splitlines() if line.strip()]

def _looks_like_heading(text: str) -> bool:
    stripped = text.strip()
    return bool(
        re.match(r"^#{1,6}\s+\S", stripped)
        or re.match(r"^第[一二三四五六七八九十百千万0-9]+[章节篇部分]\b", stripped)
    )

def _split_large_paragraph(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    sentences = [item.strip() for item in re.split(r"(?<=[。！？.!?；;])\s*", text) if item.strip()]
    if len(sentences) <= 1:
        return _hard_split_text(text, max_chars)
    parts: list[str] = []
    current: list[str] = []
    current_len = 0
    for sentence in sentences:
        if len(sentence) > max_chars:
            if current:
                parts.append("".join(current).strip())
                current = []
                current_len = 0
            parts.extend(_hard_split_text(sentence, max_chars))
            continue
        if current and current_len + len(sentence) > max_chars:
            parts.append("".join(current).strip())
            current = []
            current_len = 0
        current.append(sentence)
        current_len += len(sentence)
    if current:
        parts.append("".join(current).strip())
    return [part for part in parts if part.strip()]

def _hard_split_text(text: str, max_chars: int) -> list[str]:
    return [
        text[index : index + max_chars].strip()
        for index in range(0, len(text), max_chars)
        if text[index : index + max_chars].strip()
    ]

def _ingest_steps_for(stage: str, progress: float, status: str) -> list[dict[str, Any]]:
    if stage == "failed" or status in {"failed", "cancelled"}:
        return [
            {
                **item,
                "status": "done" if float(item["progress"]) < progress else "pending",
            }
            for item in INGEST_STAGES
        ]
    return [
        {
            **item,
            "status": (
                "running"
                if item["key"] == stage
                else "done"
                if float(item["progress"]) < progress or (stage == "done" and item["key"] == "done")
                else "pending"
            ),
        }
        for item in INGEST_STAGES
    ]

def _build_section_nodes(text: str) -> list[dict[str, Any]]:
    paragraphs = _paragraph_blocks(text)
    if not paragraphs:
        return [
            {
                "section_id": "sec_1",
                "related_section_id": "sec_1",
                "node_type": "section",
                "level": 1,
                "title": "全文",
                "parent_id": None,
                "path": "全文",
                "section_order": 1,
                "summary": _summarize_text(text, 260),
                "content": text,
                "source_span": {"start_paragraph": 0, "end_paragraph": 0},
                "anchor_entities": _extract_anchor_entities(text),
            }
        ]

    nodes: list[dict[str, Any]] = []
    stack: dict[int, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    current_parts: list[str] = []
    current_start = 0

    def flush(end_paragraph: int) -> None:
        nonlocal current, current_parts, current_start
        if current is None:
            return
        content = "\n\n".join(part for part in current_parts if part.strip()).strip()
        if not content:
            content = str(current.get("title") or "")
        current["content"] = content
        current["summary"] = _section_summary(current.get("title"), content)
        current["anchor_entities"] = _extract_anchor_entities(content)
        current["source_span"] = {"start_paragraph": current_start, "end_paragraph": end_paragraph}
        nodes.append(current)
        current = None
        current_parts = []

    def start_section(
        title: str,
        level: int,
        paragraph_index: int,
        parent_id: str | None = None,
        related_section_id: str | None = None,
    ) -> dict[str, Any]:
        section_id = f"sec_{len(nodes) + 1}"
        parent = next((stack[item] for item in sorted(stack, reverse=True) if item < level), None)
        resolved_parent_id = (
            parent_id if parent_id is not None else (parent.get("section_id") if parent else None)
        )
        parent_path = str(parent.get("path") or "") if parent else ""
        path = f"{parent_path} / {title}" if parent_path else title
        return {
            "section_id": section_id,
            "related_section_id": related_section_id or section_id,
            "node_type": "section",
            "level": level,
            "title": title,
            "parent_id": resolved_parent_id,
            "path": path,
            "section_order": len(nodes) + 1,
            "summary": "",
            "content": "",
            "source_span": {"start_paragraph": paragraph_index, "end_paragraph": paragraph_index},
            "anchor_entities": [],
        }

    for index, paragraph in enumerate(paragraphs):
        heading = _heading_info(paragraph)
        if heading:
            flush(index - 1)
            level, title = heading
            current = start_section(title, level, index)
            current_parts = [paragraph]
            current_start = index
            stack = {key: value for key, value in stack.items() if key < level}
            stack[level] = current
            continue

        if current is None:
            current = start_section(f"段落组 {len(nodes) + 1}", 1, index)
            current_parts = []
            current_start = index

        projected = sum(len(part) for part in current_parts) + len(paragraph)
        if current_parts and projected > SECTION_TARGET_CHARS:
            parent_id = str(current.get("parent_id") or "")
            related_section_id = str(
                current.get("related_section_id") or current.get("section_id") or ""
            )
            title = str(current.get("title") or f"段落组 {len(nodes) + 1}")
            level = int(current.get("level") or 1)
            flush(index - 1)
            current = start_section(
                title,
                level,
                index,
                parent_id or None,
                related_section_id or None,
            )
            current_parts = []
            current_start = index
        current_parts.append(paragraph)

    flush(len(paragraphs) - 1)
    if not nodes:
        return _build_section_nodes(text[:SECTION_TARGET_CHARS])
    return nodes

def _build_document_card(
    title: str,
    filename: str,
    file_type: str,
    text: str,
    section_nodes: list[dict[str, Any]],
) -> dict[str, Any]:
    outline = [
        {
            "section_id": node.get("section_id"),
            "title": node.get("title"),
            "path": node.get("path"),
            "level": node.get("level"),
            "summary": node.get("summary"),
        }
        for node in section_nodes[:60]
    ]
    summary_source = "\n".join(str(node.get("summary") or "") for node in section_nodes[:12])
    entities = _extract_anchor_entities(text[:12000])
    use_cases = [str(node.get("title")) for node in section_nodes if node.get("title")][:8]
    return {
        "title": title,
        "filename": filename,
        "file_type": file_type,
        "summary": _summarize_text(summary_source or text, 520),
        "outline": outline,
        "applicable_scenarios": use_cases,
        "key_entities": entities[:24],
        "char_count": len(text),
        "section_count": len(section_nodes),
    }

def _heading_info(text: str) -> tuple[int, str] | None:
    stripped = text.strip()
    if not stripped:
        return None
    markdown = re.match(r"^(#{1,6})\s+(.+)$", stripped)
    if markdown:
        return len(markdown.group(1)), markdown.group(2).strip()
    chapter = re.match(
        r"^第[一二三四五六七八九十百千万0-9]+[章节篇部分]\s*[：:\-、]?\s*(.+)?$", stripped
    )
    if chapter:
        return 1, (chapter.group(1) or stripped).strip()
    return None

def _section_summary(title: Any, content: str) -> str:
    prefix = str(title or "").strip()
    summary = _summarize_text(content, 260)
    if prefix and prefix not in summary:
        return f"{prefix}：{summary}"[:320]
    return summary

def _summarize_text(text: str, max_chars: int) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    if len(compact) <= max_chars:
        return compact
    end = compact.rfind("。", 0, max_chars)
    if end < max_chars // 2:
        end = compact.rfind("；", 0, max_chars)
    if end < max_chars // 2:
        end = max_chars
    return compact[:end].strip() + "..."

def _extract_anchor_entities(text: str) -> list[str]:
    candidates: list[str] = []
    patterns = [
        r"[A-Za-z][A-Za-z0-9_.:/-]{2,}",
        r"[\u4e00-\u9fff]{2,12}",
        r"\d+(?:\.\d+)+",
    ]
    for pattern in patterns:
        candidates.extend(re.findall(pattern, text or ""))
    seen: set[str] = set()
    result: list[str] = []
    for item in candidates:
        value = str(item).strip(".,，。；;：:()（）[]【】")
        if len(value) < 2 or value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= 40:
            break
    return result

def _structure_bucket_specs(section_nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not section_nodes:
        return []
    top_groups: dict[str, list[dict[str, Any]]] = {}
    for node in section_nodes:
        path = str(node.get("path") or node.get("title") or "未命名章节")
        top = path.split(" / ")[0].strip() or "未命名章节"
        top_groups.setdefault(top, []).append(node)
    specs: list[dict[str, Any]] = []
    for index, (title, nodes) in enumerate(top_groups.items()):
        content = "\n\n".join(str(node.get("content") or "") for node in nodes)
        section_paths = [str(node.get("path") or node.get("title") or "") for node in nodes]
        specs.append(
            {
                "bucket_key": _safe_key(title, fallback=f"structure_{index + 1}"),
                "title": title,
                "summary": _summarize_text(
                    "\n".join(str(node.get("summary") or "") for node in nodes), 420
                ),
                "content": content[:BUCKET_SECTION_CHARS],
                "section_ids": [
                    str(node.get("section_id")) for node in nodes if node.get("section_id")
                ],
                "section_paths": section_paths,
                "bucket_type": "structure",
                "concept_type": "Topic",
                "applicable_query_types": ["answer", "policy_check"],
            }
        )
    return specs

def _normalize_llm_bucket_specs(
    buckets: list[dict[str, Any]],
    section_nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    section_by_index = {index: node for index, node in enumerate(section_nodes)}
    section_by_id = {str(node.get("section_id")): node for node in section_nodes}
    for index, item in enumerate(buckets):
        section_ids = [str(value) for value in item.get("section_ids", []) if value]
        if not section_ids and isinstance(item.get("section_indexes"), list):
            section_ids = [
                str(section_by_index[int(value)].get("section_id"))
                for value in item.get("section_indexes", [])
                if isinstance(value, int) and value in section_by_index
            ]
        nodes = [
            section_by_id[section_id] for section_id in section_ids if section_id in section_by_id
        ]
        content = "\n\n".join(str(node.get("content") or "") for node in nodes)
        title = str(item.get("title") or f"任务桶 {index + 1}")
        specs.append(
            {
                "bucket_key": str(item.get("bucket_key") or _safe_key(title, f"task_{index + 1}")),
                "title": title,
                "summary": str(item.get("summary") or _summarize_text(content, 420)),
                "content": content[:BUCKET_SECTION_CHARS],
                "section_ids": section_ids,
                "section_paths": [
                    str(node.get("path") or node.get("title") or "") for node in nodes
                ],
                "bucket_type": str(item.get("bucket_type") or "task"),
                "concept_type": str(item.get("concept_type") or "Topic"),
                "applicable_query_types": item.get("applicable_query_types")
                if isinstance(item.get("applicable_query_types"), list)
                else ["answer", "policy_check", "interface_discovery", "procedure_discovery"],
            }
        )
    return specs

def _unique_bucket_specs(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_sections: set[tuple[str, ...]] = set()
    for item in specs:
        key = str(item.get("bucket_key") or "").strip()
        section_ids = tuple(sorted(str(value) for value in item.get("section_ids", []) if value))
        signature = section_ids or (key,)
        if key in seen_keys or signature in seen_sections:
            continue
        seen_keys.add(key)
        seen_sections.add(signature)
        result.append(item)
    return result

def _unique_strings(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result

def _bucket_quality(spec: dict[str, Any], section_ids: list[str], content: str) -> dict[str, Any]:
    warnings: list[str] = []
    if not section_ids:
        warnings.append("missing_source_section")
    if not str(spec.get("summary") or "").strip():
        warnings.append("missing_summary")
    if len(content.strip()) < 40:
        warnings.append("content_too_short")
    return {
        "status": "warning" if warnings else "ready",
        "warnings": warnings,
        "has_source_sections": bool(section_ids),
        "has_summary": bool(str(spec.get("summary") or "").strip()),
        "content_chars": len(content or ""),
    }

def _fallback_bucket_specs(
    sections: list[str], section_nodes: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    if section_nodes:
        return _structure_bucket_specs(section_nodes)
    return [
        {
            "bucket_key": f"bucket_{index + 1}",
            "title": _guess_title(section, index),
            "summary": section[:360],
            "content": section,
            "bucket_type": "structure",
            "concept_type": "Topic",
            "section_ids": [],
            "section_paths": [],
        }
        for index, section in enumerate(sections)
    ]

def _document_card_for_search(row: KnowledgeDocument) -> dict[str, Any]:
    metadata = row.metadata_json or {}
    card = metadata.get("document_card") if isinstance(metadata.get("document_card"), dict) else {}
    return {
        "id": row.id,
        "title": card.get("title") or row.title or row.filename,
        "filename": row.filename,
        "file_type": row.file_type,
        "summary": card.get("summary") or "",
        "outline": card.get("outline") if isinstance(card.get("outline"), list) else [],
        "key_entities": card.get("key_entities")
        if isinstance(card.get("key_entities"), list)
        else [],
        "section_count": card.get("section_count"),
        "chunk_count": row.chunk_count,
        "updated_at": row.updated_at.isoformat(),
    }

def _document_card_for_route(row: KnowledgeDocument) -> dict[str, Any]:
    card = _document_card_for_search(row)
    return {
        "id": card["id"],
        "title": _summarize_text(str(card.get("title") or ""), 120),
        "filename": _summarize_text(str(card.get("filename") or ""), 120),
        "file_type": card.get("file_type"),
        "summary": _summarize_text(str(card.get("summary") or ""), 160),
        "outline": _route_labels(card.get("outline"), 16, 80),
        "key_entities": _route_labels(card.get("key_entities"), 12, 40),
        "section_count": card.get("section_count"),
        "chunk_count": card.get("chunk_count"),
    }

def _route_candidates(ranked: list[Any], candidates: list[Any], limit: int) -> list[Any]:
    selected = list(ranked[:limit])
    selected_ids = {str(getattr(item, "id", "")) for item in selected}
    for item in candidates:
        item_id = str(getattr(item, "id", ""))
        if item_id and item_id not in selected_ids:
            selected.append(item)
            selected_ids.add(item_id)
        if len(selected) >= limit:
            break
    return selected

def _route_labels(value: object, limit: int, char_limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    labels: list[str] = []
    for item in value[:limit]:
        if isinstance(item, dict):
            label = next(
                (
                    str(item.get(key) or "").strip()
                    for key in ("path", "title", "name", "heading", "label")
                    if item.get(key)
                ),
                "",
            )
            if not label:
                label = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        else:
            label = str(item or "").strip()
        if label:
            labels.append(_summarize_text(label, char_limit))
    return labels

def _score_documents(query: str, documents: list[KnowledgeDocument]) -> list[KnowledgeDocument]:
    scored: list[tuple[float, KnowledgeDocument]] = []
    for row in documents:
        card = (row.metadata_json or {}).get("document_card", {})
        score = _score_weighted_text(
            query,
            (row.title or "", 3.0),
            (row.filename, 2.0),
            (json.dumps(card, ensure_ascii=False), 1.0),
        )
        if score >= SEARCH_MIN_DOCUMENT_SCORE:
            scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [row for _score, row in scored]

def _score_buckets(
    query: str,
    buckets: list[KnowledgeBucket],
    query_type: str = "answer",
) -> list[KnowledgeBucket]:
    scored: list[tuple[float, KnowledgeBucket]] = []
    for row in buckets:
        metadata = row.metadata_json or {}
        score = _score_weighted_text(
            query,
            (row.title, 3.0),
            (row.summary, 1.8),
            (" ".join(str(item) for item in metadata.get("section_paths", [])), 1.4),
            (str(metadata.get("content") or ""), 0.6),
        )
        applicable_query_types = metadata.get("applicable_query_types", [])
        if isinstance(applicable_query_types, list) and query_type in applicable_query_types:
            score += 2.0
        if score >= SEARCH_MIN_BUCKET_SCORE:
            scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [row for _score, row in scored]

def _rank_chunks(
    query: str,
    chunks: list[KnowledgeChunk],
    selected_buckets: list[KnowledgeBucket],
    expanded_sections: list[dict[str, Any]],
) -> list[KnowledgeChunk]:
    bucket_rank = {bucket.id: index for index, bucket in enumerate(selected_buckets)}
    section_scores = {
        str(item.get("section_id")): _score_weighted_text(
            query,
            (str(item.get("title") or ""), 2.0),
            (str(item.get("path") or ""), 1.5),
            (str(item.get("summary") or ""), 1.0),
        )
        for item in expanded_sections
        if item.get("section_id")
    }

    scored: list[tuple[tuple[float, int, int], KnowledgeChunk]] = []
    for chunk in chunks:
        metadata = chunk.metadata_json or {}
        section_bonus = min(3.0, section_scores.get(str(metadata.get("section_id")), 0.0) * 0.2)
        text_score = _score_weighted_text(
            query,
            (str(metadata.get("section_title") or ""), 2.0),
            (str(metadata.get("section_path") or ""), 1.5),
            (chunk.summary or "", 1.4),
            (chunk.content, 1.0),
        )
        if text_score < SEARCH_MIN_CHUNK_SCORE:
            continue
        scored.append(
            (
                (
                    text_score + section_bonus,
                    -bucket_rank.get(chunk.bucket_id, 999),
                    -chunk.chunk_index,
                ),
                chunk,
            )
        )
    scored.sort(key=lambda item: item[0], reverse=True)
    return [chunk for _score, chunk in scored]

def _select_diverse_chunk_hits(
    ranked_chunks: list[KnowledgeChunk],
    selected_buckets: list[KnowledgeBucket],
    max_chunks: int,
) -> list[KnowledgeChunk]:
    if max_chunks <= 0:
        return []
    selected_ids: set[str] = set()
    diversity_slots = min(len(selected_buckets), max(1, max_chunks - 1))
    for bucket in selected_buckets[:diversity_slots]:
        match = next(
            (
                chunk
                for chunk in ranked_chunks
                if chunk.bucket_id == bucket.id and chunk.id not in selected_ids
            ),
            None,
        )
        if match is not None:
            selected_ids.add(match.id)
        if len(selected_ids) >= max_chunks:
            break
    for chunk in ranked_chunks:
        if len(selected_ids) >= max_chunks:
            break
        selected_ids.add(chunk.id)
    return [chunk for chunk in ranked_chunks if chunk.id in selected_ids]

def _expand_related_chunks(
    ranked_hits: list[KnowledgeChunk],
    candidates: list[KnowledgeChunk],
) -> list[KnowledgeChunk]:
    """Return each hit and every chunk from its contiguous source section."""
    if not ranked_hits:
        return []
    candidates_by_id = {chunk.id: chunk for chunk in candidates}
    candidates_by_group: dict[str, list[KnowledgeChunk]] = {}
    for chunk in candidates:
        group_id = str((chunk.metadata_json or {}).get("related_group_id") or "").strip()
        if group_id:
            candidates_by_group.setdefault(group_id, []).append(chunk)
    for group in candidates_by_group.values():
        group.sort(key=lambda row: (row.chunk_index, row.id))

    expanded: list[KnowledgeChunk] = []
    seen: set[str] = set()
    seen_groups: set[str] = set()
    for hit in ranked_hits:
        metadata = hit.metadata_json or {}
        group_id = str(metadata.get("related_group_id") or "").strip()
        related_ids = [
            str(item)
            for item in metadata.get("related_chunk_ids", [])
            if str(item) in candidates_by_id
        ]
        group = [candidates_by_id[item] for item in related_ids]
        if not group and group_id:
            full_group = candidates_by_group.get(group_id, [])
            hit_index = next(
                (index for index, chunk in enumerate(full_group) if chunk.id == hit.id),
                0,
            )
            group = _related_chunk_window(full_group, hit_index)
        if group:
            if group_id in seen_groups:
                if hit.id not in seen:
                    expanded.append(hit)
                    seen.add(hit.id)
                continue
            seen_groups.add(group_id)
            for chunk in sorted(group, key=lambda row: (row.chunk_index, row.id)):
                if chunk.id not in seen:
                    expanded.append(chunk)
                    seen.add(chunk.id)
            continue
        if hit.id not in seen:
            expanded.append(hit)
            seen.add(hit.id)
    # Each expanded group is emitted in source order; hit groups retain relevance priority.
    return expanded

def _related_chunk_window(
    chunks: list[KnowledgeChunk],
    center_index: int,
) -> list[KnowledgeChunk]:
    if not chunks:
        return []
    center_index = min(max(center_index, 0), len(chunks) - 1)
    selected = [center_index]
    total_chars = len(chunks[center_index].content)
    distance = 1
    while len(selected) < RELATED_CHUNK_MAX_COUNT:
        added = False
        for index in (center_index - distance, center_index + distance):
            if index < 0 or index >= len(chunks):
                continue
            chunk_chars = len(chunks[index].content)
            if total_chars + chunk_chars > RELATED_CHUNK_MAX_CHARS:
                continue
            selected.append(index)
            total_chars += chunk_chars
            added = True
            if len(selected) >= RELATED_CHUNK_MAX_COUNT:
                break
        if not added and center_index - distance < 0 and center_index + distance >= len(chunks):
            break
        distance += 1
        if distance > len(chunks):
            break
    return [chunks[index] for index in sorted(selected)]

def _build_evidence_pack(
    query: str,
    chunks: list[KnowledgeChunk],
    direct_hit_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    direct_hit_ids = direct_hit_ids or set()
    for chunk in chunks:
        metadata = chunk.metadata_json or {}
        score = _score_text(query, f"{chunk.summary or ''} {chunk.content}")
        related_group_id = str(metadata.get("related_group_id") or "").strip()
        is_direct_hit = chunk.id in direct_hit_ids or not direct_hit_ids
        if score < SEARCH_MIN_EVIDENCE_SCORE and not related_group_id and not is_direct_hit:
            continue
        evidence.append(
            {
                "chunk_id": chunk.id,
                "related_group_id": related_group_id or None,
                "related_chunk_ids": metadata.get("related_chunk_ids") or [],
                "related_chunk_index": metadata.get("related_chunk_index"),
                "document_id": chunk.document_id,
                "bucket_id": chunk.bucket_id,
                "source_path": chunk.source_ref,
                "section_path": metadata.get("section_path"),
                "summary": chunk.summary,
                "content": chunk.content[:CITATION_EXCERPT_CHAR_LIMIT],
                "excerpt": chunk.content[:CITATION_EXCERPT_CHAR_LIMIT],
                "relevance_score": round(score, 2),
                "confidence_reason": (
                    "与命中片段属于同一连续章节"
                    if not is_direct_hit
                    else "章节标题、路径、摘要或正文与查询相关"
                    if score < SEARCH_MIN_EVIDENCE_SCORE
                    else "引用来源摘要、章节路径或正文与查询相关"
                ),
            }
        )
    return evidence

def _expand_sections(
    documents: list[KnowledgeDocument],
    buckets: list[KnowledgeBucket],
    max_depth: int,
) -> list[dict[str, Any]]:
    nodes_by_doc: dict[str, dict[str, dict[str, Any]]] = {}
    for document in documents:
        tree = (document.metadata_json or {}).get("section_tree")
        if isinstance(tree, list):
            nodes_by_doc[document.id] = {
                str(node.get("section_id")): node
                for node in tree
                if isinstance(node, dict) and node.get("section_id")
            }
    wanted: dict[tuple[str, str], dict[str, Any]] = {}
    for bucket in buckets:
        metadata = bucket.metadata_json or {}
        section_ids = [str(value) for value in metadata.get("section_ids", []) if value]
        doc_nodes = nodes_by_doc.get(bucket.document_id, {})
        for section_id in section_ids:
            node = doc_nodes.get(section_id)
            if node:
                _collect_section_with_children(
                    bucket.document_id, node, doc_nodes, max_depth, wanted, bucket.title
                )
    return list(wanted.values())

def _collect_section_with_children(
    document_id: str,
    node: dict[str, Any],
    all_nodes: dict[str, dict[str, Any]],
    max_depth: int,
    result: dict[tuple[str, str], dict[str, Any]],
    reason: str,
) -> None:
    section_id = str(node.get("section_id") or "")
    if not section_id:
        return
    result[(document_id, section_id)] = {
        "document_id": document_id,
        "section_id": section_id,
        "title": node.get("title"),
        "path": node.get("path"),
        "summary": node.get("summary"),
        "level": node.get("level"),
        "source_span": node.get("source_span") or {},
        "reason": f"命中内部索引：{reason}",
    }
    if max_depth <= 0:
        return
    children = [child for child in all_nodes.values() if child.get("parent_id") == section_id]
    for child in children:
        _collect_section_with_children(document_id, child, all_nodes, max_depth - 1, result, reason)

def _safe_key(text: str, fallback: str) -> str:
    ascii_words = re.findall(r"[A-Za-z0-9]+", text)
    if ascii_words:
        key = "_".join(ascii_words).lower()
    else:
        key = "bucket_" + str(abs(hash(text)) % 100000)
    key = re.sub(r"_+", "_", key).strip("_")
    return key[:64] or fallback

def _query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for term in re.findall(r"[A-Za-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,}", query or ""):
        normalized = term.lower()
        if re.fullmatch(r"[\u4e00-\u9fff]+", normalized):
            for phrase in QUERY_NOISE_PHRASES:
                normalized = normalized.replace(phrase, "")
        normalized = normalized.strip()
        if len(normalized) < 2:
            continue
        terms.append(normalized)
        if re.fullmatch(r"[\u4e00-\u9fff]{3,}", normalized):
            for size in (4, 3, 2):
                if len(normalized) <= size:
                    continue
                terms.extend(
                    normalized[index : index + size] for index in range(len(normalized) - size + 1)
                )
    result: list[str] = []
    seen: set[str] = set()
    for term in terms:
        normalized = term.lower().strip()
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result[:96]

def _score_text(query: str, text: str) -> float:
    variants = _query_variants(query)
    if not variants:
        return 0.0
    scores = sorted((_score_single_query(variant, text) for variant in variants), reverse=True)
    return scores[0] + sum(scores[1:]) * 0.15

def _score_single_query(query: str, text: str) -> float:
    haystack = (text or "").lower()
    score = 0.0
    if query and query.lower() in haystack:
        score += 5.0
    for term in _query_terms(query):
        count = haystack.count(term)
        if count:
            term_weight = 1.2
            if re.fullmatch(r"[\u4e00-\u9fff]{3,}", term):
                term_weight = 2.0
            if re.fullmatch(r"[\u4e00-\u9fff]{4,}", term):
                term_weight = 2.7
            if len(term) >= 5:
                term_weight = 3.2
            score += term_weight * min(1.5, 1.0 + (count - 1) * 0.15)
    return score

def _score_weighted_text(query: str, *fields: tuple[str, float]) -> float:
    return sum(_score_text(query, text) * weight for text, weight in fields if text)

def _query_variants(query: str) -> list[str]:
    variants: list[str] = []
    seen: set[str] = set()
    for value in (query or "").splitlines():
        normalized = re.sub(r"\s+", " ", value).strip()
        identity = normalized.lower()
        if normalized and identity not in seen:
            seen.add(identity)
            variants.append(normalized)
    return variants

def _guess_title(section: str, index: int) -> str:
    first_line = next(
        (line.strip("# ").strip() for line in section.splitlines() if line.strip()), ""
    )
    return first_line[:60] if first_line else f"知识主题 {index + 1}"

def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
