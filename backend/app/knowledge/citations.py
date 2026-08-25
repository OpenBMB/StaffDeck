from __future__ import annotations

import re
from typing import Any

CITATION_EXCERPT_CHAR_LIMIT = 6000
CITATION_SUMMARY_CHAR_LIMIT = 800
CONCEPT_EXCERPT_CHAR_LIMIT = 2400
EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+(?![\w.-])",
    re.IGNORECASE,
)
TRUNCATED_EMAIL_PATTERN = re.compile(
    r"(?P<prefix>[A-Z0-9._%+-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)*)"
    r"(?P<ellipsis>\.{3}|…)",
    re.IGNORECASE,
)
SOURCE_FOOTER_PATTERN = re.compile(
    r"(?:\n|\s){0,3}(?:参考来源|参考资料|引用来源|资料来源)\s*[:：]\s*"
    r"(?:\[\d+\]\s*)+$"
)
CITATION_REFERENCE_PATTERN = re.compile(
    r"\[(\d+)\](?:\s*(?:-|–|—|至)\s*\[(\d+)\])?"
)


def compact_knowledge_citation_labels(
    content: str,
    citations: object,
) -> tuple[str, list[dict[str, Any]]]:
    """Keep cited sources and renumber them by first appearance in the reply."""
    content = SOURCE_FOOTER_PATTERN.sub("", content.rstrip()).rstrip()
    if not isinstance(citations, list) or not citations:
        # A model may emit a reference footer even when retrieval produced no
        # durable citations. Do not expose labels that cannot open a source.
        return content, []

    citations_by_label: dict[int, dict[str, Any]] = {}
    for index, citation in enumerate(citations, start=1):
        if not isinstance(citation, dict):
            continue
        label_match = re.fullmatch(r"\[(\d+)\]", str(citation.get("label") or "").strip())
        label = int(label_match.group(1)) if label_match else index
        citations_by_label.setdefault(label, citation)

    ordered_labels: list[int] = []
    for match in CITATION_REFERENCE_PATTERN.finditer(content):
        start_label = int(match.group(1))
        end_label = int(match.group(2)) if match.group(2) else start_label
        step = 1 if end_label >= start_label else -1
        for label in range(start_label, end_label + step, step):
            if label in citations_by_label and label not in ordered_labels:
                ordered_labels.append(label)

    if not ordered_labels:
        ordered_labels = list(citations_by_label)
        if not ordered_labels:
            return content, []

    label_mapping = {old_label: index for index, old_label in enumerate(ordered_labels, start=1)}

    def replace_label(match: re.Match[str]) -> str:
        old_label = int(match.group(1))
        new_label = label_mapping.get(old_label)
        # Metadata is authoritative. Unsupported labels are model-generated
        # references with no corresponding source card and must not survive.
        return f"[{new_label}]" if new_label is not None else ""

    compacted_content = re.sub(r"\[(\d+)\]", replace_label, content)
    compacted_content = re.sub(r"[ \t]+(?=\n|$)", "", compacted_content).rstrip()
    compacted_citations = [
        {**citations_by_label[old_label], "label": f"[{label_mapping[old_label]}]"}
        for old_label in ordered_labels
    ]
    return compacted_content, compacted_citations


def restore_truncated_atomic_references(content: str, citations: object) -> str:
    """Restore a uniquely identifiable email that the model abbreviated."""
    if not content or not isinstance(citations, list):
        return content
    evidence_text = "\n".join(
        str(citation.get(field) or "")
        for citation in citations
        if isinstance(citation, dict)
        for field in ("content", "excerpt", "summary", "source_path")
    )
    evidence_emails = {match.group(0) for match in EMAIL_PATTERN.finditer(evidence_text)}
    if not evidence_emails:
        return content

    def replace(match: re.Match[str]) -> str:
        prefix = match.group("prefix")
        candidates = {
            email for email in evidence_emails if email.lower().startswith(prefix.lower())
        }
        return next(iter(candidates)) if len(candidates) == 1 else match.group(0)

    return TRUNCATED_EMAIL_PATTERN.sub(replace, content)


def _compact(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 1)].rstrip()}…"


def _normalize_identity(value: str) -> str:
    return re.sub(r"[\s\W_]+", "", (value or "").lower())


def _semantic_identity(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def _display_title(value: str) -> str:
    return _compact(value, 72)


def knowledge_source_candidates(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one normalized source stream for both answer context and citations."""
    tiers = (
        ("evidence", result.get("evidence_pack")),
        ("evidence", result.get("chunks")),
        ("concept", result.get("selected_concepts")),
        ("okf", result.get("okf_citations")),
    )
    for kind, raw_items in tiers:
        if not isinstance(raw_items, list):
            continue
        items = [item for item in raw_items if isinstance(item, dict)]
        candidates = _normalize_source_items(kind, items)
        if candidates:
            return candidates
    return []


def _normalize_source_items(
    kind: str,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: list[list[dict[str, Any]]] = []
    group_indexes: dict[str, int] = {}
    for index, item in enumerate(items):
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        group_id = str(
            item.get("related_group_id") or metadata.get("related_group_id") or ""
        ).strip()
        key = f"related:{group_id}" if kind == "evidence" and group_id else f"single:{index}"
        if key not in group_indexes:
            group_indexes[key] = len(groups)
            groups.append([])
        groups[group_indexes[key]].append(item)

    candidates: list[dict[str, Any]] = []
    for group in groups:
        item = group[0]
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        source_refs = item.get("source_refs") if isinstance(item.get("source_refs"), list) else []
        source_ref = source_refs[0] if source_refs and isinstance(source_refs[0], dict) else {}
        content = "\n\n".join(
            str(
                part.get("content")
                or part.get("excerpt")
                or part.get("content_excerpt")
                or part.get("content_md")
                or ""
            ).strip()
            for part in group
            if str(
                part.get("content")
                or part.get("excerpt")
                or part.get("content_excerpt")
                or part.get("content_md")
                or ""
            ).strip()
        )
        summary = str(
            item.get("description") or item.get("summary") or item.get("label") or ""
        ).strip()
        if not content:
            content = summary
        section_path = str(
            item.get("section_path") or metadata.get("section_path") or ""
        ).strip()
        source_path = str(
            item.get("source_path")
            or item.get("source_ref")
            or item.get("target")
            or item.get("path")
            or item.get("uri")
            or metadata.get("source_path")
            or source_ref.get("source_path")
            or source_ref.get("document_id")
            or ""
        ).strip()
        source_id = str(
            item.get("chunk_id") or item.get("concept_id") or item.get("id") or ""
        ).strip()
        title = _display_title(
            str(item.get("title") or item.get("name") or "").strip()
            or section_path
            or source_path
            or source_id
            or "知识来源"
        )
        group_id = str(
            item.get("related_group_id") or metadata.get("related_group_id") or ""
        ).strip()
        if kind == "evidence":
            raw_identity = (
                f"related:{group_id}"
                if group_id
                else source_id or section_path or source_path or content[:120]
            )
        elif kind == "okf":
            raw_identity = f"{source_id}:{source_path or content[:120]}"
        else:
            raw_identity = source_id or source_path or content[:120]
        identity = _semantic_identity(raw_identity)
        if not identity or not content:
            continue
        candidate = {
            "_identity": identity,
            "kind": kind,
            "title": title,
            "source_path": source_path,
            "content": content[:CITATION_EXCERPT_CHAR_LIMIT],
            "excerpt": content[:CITATION_EXCERPT_CHAR_LIMIT],
            "summary": summary[:CITATION_SUMMARY_CHAR_LIMIT],
        }
        if kind == "evidence":
            candidate.update(
                {
                    "section_path": section_path,
                    "confidence_reason": str(item.get("confidence_reason") or ""),
                    "document_id": item.get("document_id"),
                    "bucket_id": item.get("bucket_id"),
                    "chunk_id": str(item.get("chunk_id") or item.get("id") or "").strip(),
                    "related_group_id": group_id or None,
                    "related_chunk_ids": [
                        str(part.get("chunk_id") or part.get("id") or "").strip()
                        for part in group
                        if str(part.get("chunk_id") or part.get("id") or "").strip()
                    ],
                }
            )
        else:
            candidate["concept_id"] = str(item.get("concept_id") or item.get("id") or "")
            if kind == "concept":
                candidate["concept_type"] = item.get("type")
        candidates.append(candidate)
    return candidates


def knowledge_citations_from_results(
    knowledge_results: list[dict[str, Any]],
    limit: int = 4,
    *,
    max_results: int | None = 1,
) -> list[dict[str, Any]]:
    if limit <= 0 or max_results == 0:
        return []
    citations: list[dict[str, Any]] = []
    seen_identities: set[str] = set()
    results = [item for item in knowledge_results if isinstance(item, dict)]
    if max_results is not None:
        results = results[-max(1, max_results) :]
    for result in results:
        for candidate in knowledge_source_candidates(result):
            identity = _normalize_identity(str(candidate.get("_identity") or ""))
            if not identity or identity in seen_identities:
                continue
            seen_identities.add(identity)
            payload = {key: value for key, value in candidate.items() if key != "_identity"}
            citations.append(
                {
                    "id": f"kref_{len(citations) + 1}",
                    "label": f"[{len(citations) + 1}]",
                    **payload,
                }
            )
            if len(citations) >= limit:
                return citations
    return citations
