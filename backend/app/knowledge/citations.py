from __future__ import annotations

import re
from typing import Any


def _compact(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 1)].rstrip()}…"


def _normalize_identity(value: str) -> str:
    return re.sub(r"[\s\W_]+", "", (value or "").lower())


def _semantic_identity(value: str) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    text = re.split(r"在第\s*\d+\s*章第\s*\d+\s*节", text, maxsplit=1)[0]
    text = re.split(r"第\s*\d+(?:\.\d+)*\s+", text, maxsplit=1)[0] or text
    text = text.split("。", 1)[0]
    return text or value


def _display_title(value: str) -> str:
    title = re.sub(r"\s+", " ", (value or "").strip())
    if " / evidence" in title:
        title = title.split(" / evidence", 1)[0].strip()
    if "用于统一" in title:
        title = title.split("用于统一", 1)[0].strip()
    if "。服务人员" in title:
        title = title.split("。", 1)[0].strip()
    return _compact(title, 72)


def knowledge_citations_from_results(
    knowledge_results: list[dict[str, Any]],
    limit: int = 4,
) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    seen_identities: set[str] = set()

    def add(kind: str, identity: str, payload: dict[str, Any]) -> None:
        if len(citations) >= limit:
            return
        normalized = _normalize_identity(identity)
        title_identity = _normalize_identity(str(payload.get("title") or ""))
        if not normalized or normalized in seen_identities or (title_identity and title_identity in seen_identities):
            return
        seen_identities.add(normalized)
        if title_identity:
            seen_identities.add(title_identity)
        citations.append(
            {
                "id": f"kref_{len(citations) + 1}",
                "label": f"[{len(citations) + 1}]",
                "kind": kind,
                **payload,
            }
        )

    for result in knowledge_results:
        for concept in result.get("selected_concepts") or []:
            if not isinstance(concept, dict):
                continue
            concept_id = str(concept.get("concept_id") or concept.get("id") or "").strip()
            title = _display_title(str(concept.get("title") or concept_id or "Wiki 概念"))
            description = str(concept.get("description") or "").strip()
            source_refs = concept.get("source_refs") if isinstance(concept.get("source_refs"), list) else []
            source_path = ""
            if source_refs and isinstance(source_refs[0], dict):
                source_path = str(source_refs[0].get("source_path") or source_refs[0].get("document_id") or "")
            add(
                "concept",
                concept_id or title,
                {
                    "title": title,
                    "source_path": source_path,
                    "excerpt": description[:820],
                    "concept_id": concept_id,
                    "concept_type": concept.get("type"),
                },
            )

        for item in result.get("evidence_pack") or []:
            if not isinstance(item, dict):
                continue
            excerpt = str(item.get("excerpt") or "").strip()
            summary = str(item.get("summary") or "").strip()
            section_path = str(item.get("section_path") or "").strip()
            source_path = str(item.get("source_path") or "").strip()
            chunk_id = str(item.get("chunk_id") or "").strip()
            title = _display_title(section_path or source_path or summary or "知识片段")
            identity = _semantic_identity(section_path or summary or f"{source_path}:{excerpt[:120]}" or chunk_id)
            add(
                "evidence",
                identity,
                {
                    "title": title,
                    "source_path": source_path,
                    "section_path": section_path,
                    "excerpt": excerpt[:1600],
                    "summary": summary[:420],
                    "confidence_reason": str(item.get("confidence_reason") or ""),
                    "document_id": item.get("document_id"),
                    "bucket_id": item.get("bucket_id"),
                    "chunk_id": chunk_id,
                },
            )

        if not citations:
            for item in result.get("okf_citations") or []:
                if not isinstance(item, dict):
                    continue
                concept_id = str(item.get("concept_id") or "").strip()
                target = str(item.get("target") or "").strip()
                label = str(item.get("label") or "").strip()
                title = _display_title(str(item.get("title") or concept_id or "OKF 引用"))
                add(
                    "okf",
                    f"{concept_id}:{target or label}",
                    {
                        "title": title,
                        "source_path": target,
                        "excerpt": label,
                        "concept_id": concept_id,
                    },
                )
    return citations
