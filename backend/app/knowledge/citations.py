from __future__ import annotations

import hashlib
import re
from typing import Any

CITATION_EXCERPT_CHAR_LIMIT = 6000
CITATION_SUMMARY_CHAR_LIMIT = 800
CONCEPT_EXCERPT_CHAR_LIMIT = 2400
# 内容指纹只取归一化后的前 N 个字符：同一条证据被不同 bucket/版本重复收录时，
# chunk_id 不同但正文一致，靠它才能判定成同一条。
CITATION_FINGERPRINT_CHAR_LIMIT = 400
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
# 正文里的数字引用编号（`[1]`、`[12]`）。`[1](url)` 是 markdown 链接、
# `[1]: url` 是链接定义，都要保留，所以后面紧跟 `(` 或 `:` 的不算编号。
INLINE_CITATION_MARKER_PATTERN = re.compile(r"[ \t]*\[(\d{1,3})\](?![(:])")
# 代码块 / 行内代码：里面的 `[1]` 是下标或示例，不能删。
CODE_SEGMENT_PATTERN = re.compile(r"(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`)")


def strip_citation_markers(content: str) -> str:
    """去掉正文里的数字引用编号。

    「展示知识来源」关掉后来源卡片不再展示，正文里孤零零的 `[1]` 就成了指向
    不存在内容的悬空编号。这里只按租户开关决定是否调用，与前端同一套口径
    （前端 `stripCitationMarkers`），这样老客户端/缓存里的旧包也不会再露出编号。
    """
    if not content or "[" not in content:
        return content
    parts = CODE_SEGMENT_PATTERN.split(content)
    return "".join(
        part if index % 2 else INLINE_CITATION_MARKER_PATTERN.sub("", part)
        for index, part in enumerate(parts)
    )


def strip_citation_section(content: str) -> str:
    """整段正文的引用清理：先删尾部「参考来源：[1] [2]」，再删正文编号。

    顺序不能反 —— 先删 `[n]` 会让 `参考资料：[1] [2]` 变成只剩标题的残留行
    （与前端 `messageVisibleContent` 同一口径）。
    """
    if not content:
        return content
    return strip_citation_markers(SOURCE_FOOTER_PATTERN.sub("", content))


# 流式分片可能把 `[1]` 切成 `[` + `1]`，尾部这几类残片要压到下一片再判定。
PARTIAL_CITATION_MARKER_TAIL_PATTERN = re.compile(r"\[(?:\d{1,3})?$")
CODE_FENCE_PATTERN = re.compile(r"(```|~~~)")


class CitationMarkerStreamStripper:
    """跨分片剥离引用编号的流式过滤器。

    与整段 `strip_citation_markers` 的差别只有两点，都是为了 SSE 分片：
    - 尾部可能是半截编号（`[`、`[1`、`[12`）时先压住，等下一片拼齐；
    - 代码围栏可能被切开，所以围栏状态要跨片保持（围栏里的 `[1]` 是数组下标，不能删）。
    """

    def __init__(self) -> None:
        self._pending = ""
        self._fence: str | None = None

    def reset(self) -> None:
        self._pending = ""
        self._fence = None

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        text = self._pending + chunk
        text, self._pending = _split_partial_tail(text)
        return self._strip(text) if text else ""

    def flush(self) -> str:
        pending, self._pending = self._pending, ""
        return self._strip(pending) if pending else ""

    def _strip(self, text: str) -> str:
        if not text:
            return ""
        parts: list[str] = []
        cursor = 0
        while cursor < len(text):
            if self._fence is not None:
                end = text.find(self._fence, cursor)
                if end < 0:
                    parts.append(text[cursor:])
                    break
                parts.append(text[cursor : end + 3])
                cursor = end + 3
                self._fence = None
                continue
            match = CODE_FENCE_PATTERN.search(text, cursor)
            if match is None:
                parts.append(strip_citation_markers(text[cursor:]))
                break
            parts.append(strip_citation_markers(text[cursor : match.start()]))
            parts.append(match.group(1))
            cursor = match.end()
            self._fence = match.group(1)
        return "".join(parts)


def _split_partial_tail(text: str) -> tuple[str, str]:
    """拆出尾部可能被后续分片补全的残片。"""
    marker = PARTIAL_CITATION_MARKER_TAIL_PATTERN.search(text)
    if marker is not None:
        start = marker.start()
        # 编号前的空格也一起压住：编号删掉后这串空白不该留在正文里
        # （整段清理时 INLINE_CITATION_MARKER_PATTERN 会连 `[ \t]*` 一起吃掉）。
        while start > 0 and text[start - 1] in " \t":
            start -= 1
        return text[:start], text[start:]
    # 结尾 1~2 个反引号/波浪号可能是下一片代码围栏的开头；完整围栏不压。
    for char in ("`", "~"):
        if text.endswith(char * 3):
            return text, ""
        for size in (2, 1):
            if text.endswith(char * size):
                return text[:-size], char * size
    return text, ""


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


def renumber_citations(citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按给定顺序重排编号，保证 label 连续、id 唯一。

    引用编号必须与卡片一一对应：跳号会让正文里的 ``[n]`` 找不到落点；id 重复
    会让前端以 ``citation.id`` 作 React key 时复用同一张卡片。
    """
    renumbered: list[dict[str, Any]] = []
    for index, citation in enumerate(citations, start=1):
        if not isinstance(citation, dict):
            continue
        renumbered.append({**citation, "id": f"kref_{index}", "label": f"[{index}]"})
    return renumbered


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


def _first_text(citation: dict[str, Any], fields: tuple[str, ...]) -> str:
    for field in fields:
        value = str(citation.get(field) or "").strip()
        if value:
            return value
    return ""


def _content_fingerprint(citation: dict[str, Any]) -> str:
    text = _first_text(citation, ("content", "excerpt", "summary"))
    normalized = _normalize_identity(text)[:CITATION_FINGERPRINT_CHAR_LIMIT]
    if not normalized:
        return ""
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()[:12]


def _citation_locator(citation: dict[str, Any]) -> str:
    """定位一段证据所在的「文档 + 位置」，缺文档信息时退回 chunk。"""
    document = _first_text(
        citation,
        ("document_id", "source_path", "source_ref", "target", "path", "uri"),
    )
    section = _first_text(citation, ("section_path",))
    chunk_id = _first_text(citation, ("chunk_id",))
    if document:
        return f"doc:{document}#{section}" if section else f"doc:{document}"
    if chunk_id:
        return f"chunk:{chunk_id}"
    return f"section:{section}" if section else ""


def citation_identity(citation: dict[str, Any]) -> str:
    """「这算不算同一份证据」的权威身份，供所有去重路径共用。

    带内容指纹：同一文档同一页被重复收录（bucket / 知识库版本不同、chunk_id
    不同但正文一致）时判为同一条；同一页里的不同片段则仍是两条，由展示层
    合并成一张卡并把编号全部列出。
    """
    if not isinstance(citation, dict):
        return ""
    concept_id = _first_text(citation, ("concept_id",))
    if concept_id:
        return f"concept:{concept_id}"
    related_group_id = _first_text(citation, ("related_group_id",))
    if related_group_id:
        return f"related:{related_group_id}"
    locator = _citation_locator(citation)
    fingerprint = _content_fingerprint(citation)
    if locator and fingerprint:
        return f"{locator}|{fingerprint}"
    if locator:
        return locator
    title = _first_text(citation, ("title",))
    if title:
        # 只有标题可依据时大小写不应影响判定（" Alpha " 与 "alpha" 是同一条）。
        return f"title:{title.lower()}|{fingerprint}"
    return fingerprint


def citation_display_key(citation: dict[str, Any]) -> str:
    """标题行的展示分组键：同一文档同一位置只占一张卡片。

    与 :func:`citation_identity` 的差别是**不含内容指纹**——同一页切出的多个
    片段应合成一张卡（片段留在详情里）。文档维度必须保留，否则不同 PDF 里
    同名的「第 3 页」会被并成一条，这正是编号对不上正文的来源之一。
    """
    if not isinstance(citation, dict):
        return ""
    concept_id = _first_text(citation, ("concept_id",))
    if concept_id:
        return f"concept:{concept_id}"
    related_group_id = _first_text(citation, ("related_group_id",))
    if related_group_id:
        return f"related:{related_group_id}"
    document = _first_text(citation, ("document_id",))
    if not document:
        document = _first_text(citation, ("source_path",))
    section = _first_text(citation, ("section_path", "title"))
    if document:
        return f"doc:{document}#{section}" if section else f"doc:{document}"
    chunk_id = _first_text(citation, ("chunk_id",))
    if chunk_id:
        return f"chunk:{chunk_id}"
    return f"title:{_first_text(citation, ('title',))}"


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
        # OKF 引用要区分「同一概念的不同落点」，沿用 concept/chunk + 文档定位；
        # 其余（含 evidence）统一走 citation_identity，避免多套身份规则打架。
        identity = (
            _semantic_identity(f"{source_id}:{source_path or content[:120]}")
            if kind == "okf"
            else ""
        )
        if not content:
            continue
        candidate: dict[str, Any] = {
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
        if not identity:
            identity = citation_identity(candidate)
        if not identity:
            continue
        candidate["_identity"] = identity
        candidate["display_key"] = citation_display_key(candidate)
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
