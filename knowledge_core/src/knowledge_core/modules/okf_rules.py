from __future__ import annotations
import json
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import ZipFile
from ..citations import CITATION_EXCERPT_CHAR_LIMIT

OKF_VERSION = "0.1"

RESERVED_FILENAMES = {"index.md", "log.md"}

CONCEPT_TYPES = {
    "Source Document",
    "Source Section",
    "Topic",
    "Playbook",
    "Business Rule",
    "Query Analysis",
}

MIN_CONCEPT_SEARCH_SCORE = 4.0

def search_concepts(query, concepts, limit=6):
    if not query.strip(): return []
    scored = []
    tokens = _query_tokens(query)
    for row in concepts:
        heading = ' '.join([row.concept_id, row.concept_type, row.title, row.description or '']).lower()
        body = _strip_frontmatter(row.content_md).lower()[:2400]
        score, matched = 0.0, False
        for token in tokens:
            if not token: continue
            if token in heading:
                score += 6.0 + min(len(token), 6); matched = True
            elif token in body:
                score += 3.0 + min(len(token), 4) * .5; matched = True
        if query.lower() in f'{heading} {body}': score += 10; matched = True
        if row.concept_type == 'Source Document' and matched: score -= 2
        if score >= MIN_CONCEPT_SEARCH_SCORE and matched: scored.append((score, str(row.updated_at), row))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [row for _, _, row in scored[:limit]]

@dataclass
class ParsedOkfDocument:
    concept_id: str
    frontmatter: dict[str, Any]
    body: str
    content_md: str

def build_okf_for_document(
    document: KnowledgeDocument,
    section_nodes: list[dict[str, Any]],
    buckets: list[KnowledgeBucket],
) -> list[dict[str, Any]]:
    document_slug = safe_path_segment(document.title or Path(document.filename).stem or document.id)
    document_concept_id = f"sources/{document_slug}"
    document_card = (
        document.metadata_json.get("document_card")
        if isinstance(document.metadata_json, dict)
        else {}
    )
    if not isinstance(document_card, dict):
        document_card = {}
    concepts: list[dict[str, Any]] = [
        _source_document_concept(
            document, document_concept_id, section_nodes, buckets, document_card
        )
    ]
    for section in section_nodes[:80]:
        concepts.append(_source_section_concept(document, document_concept_id, section))
    for bucket in buckets:
        concepts.append(_bucket_concept(document, document_concept_id, bucket))
    return concepts

def selected_concept_cards(concepts: list[KnowledgeConcept]) -> list[dict[str, Any]]:
    return [
        {
            "id": row.id,
            "concept_id": row.concept_id,
            "type": row.concept_type,
            "title": row.title,
            "description": row.description,
            "links": row.links_json or [],
            "citations": row.citations_json or [],
            "source_refs": row.source_refs_json or [],
            "content": _strip_frontmatter(row.content_md).strip()[:CITATION_EXCERPT_CHAR_LIMIT],
        }
        for row in concepts
    ]

def okf_citations_for_concepts(concepts: list[KnowledgeConcept]) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in concepts:
        for citation in row.citations_json or []:
            target = str(citation.get("target") or "")
            key = (row.concept_id, target)
            if not target or key in seen:
                continue
            seen.add(key)
            citations.append(
                {
                    "concept_id": row.concept_id,
                    "title": row.title,
                    "label": citation.get("label") or citation.get("text") or target,
                    "target": target,
                }
            )
    return citations

def build_index_markdown(kb: KnowledgeBase, concepts: list[KnowledgeConcept]) -> str:
    groups: dict[str, list[KnowledgeConcept]] = {}
    for concept in concepts:
        groups.setdefault(concept.concept_type or "Topic", []).append(concept)
    lines = [
        "---",
        f'okf_version: "{OKF_VERSION}"',
        f"title: {json.dumps(kb.name, ensure_ascii=False)}",
        f"description: {json.dumps(kb.description or '', ensure_ascii=False)}",
        "---",
        "",
    ]
    for concept_type in sorted(groups):
        lines.append(f"# {concept_type}")
        lines.append("")
        for concept in sorted(groups[concept_type], key=lambda item: item.concept_id):
            description = concept.description or concept.frontmatter_json.get("description") or ""
            lines.append(f"* [{concept.title}]({concept.concept_id}.md) - {description}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"

def build_log_markdown(log_entries: list[dict[str, Any]], concepts: list[KnowledgeConcept]) -> str:
    by_date: dict[str, list[str]] = {}
    for entry in log_entries:
        date = str(entry.get("date") or "")[:10] or utc_now().date().isoformat()
        by_date.setdefault(date, []).append(str(entry.get("message") or "Update"))
    if not by_date:
        for concept in concepts[:20]:
            date = concept.updated_at.date().isoformat()
            by_date.setdefault(date, []).append(
                f"**Update**: Maintained [{concept.title}]({concept.concept_id}.md)."
            )
    lines = ["# Knowledge Bundle Update Log", ""]
    for date in sorted(by_date, reverse=True):
        lines.append(f"## {date}")
        for message in by_date[date]:
            lines.append(f"* {message}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"

def parse_okf_bundle(filename: str, content: bytes) -> list[ParsedOkfDocument]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".zip":
        return _parse_okf_zip(content)
    if suffix in {".md", ".markdown"}:
        concept_id = normalize_concept_id(Path(filename).with_suffix("").as_posix())
        return [parse_okf_markdown(concept_id, _decode_text(content))]
    raise ValueError("OKF 导入仅支持 .zip 或 .md 文件。")

def parse_okf_markdown(concept_id: str, content_md: str) -> ParsedOkfDocument:
    text = content_md.replace("\r\n", "\n").replace("\r", "\n").strip()
    frontmatter: dict[str, Any] = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end >= 0:
            raw = text[4:end].strip()
            body = text[end + 4 :].lstrip("\n")
            frontmatter = _parse_frontmatter(raw)
    if not frontmatter.get("type"):
        frontmatter["type"] = "Topic"
    if not frontmatter.get("title"):
        frontmatter["title"] = Path(concept_id).name.replace("-", " ").strip().title()
    normalized = render_okf_markdown(frontmatter, body)
    return ParsedOkfDocument(normalize_concept_id(concept_id), frontmatter, body, normalized)

def render_okf_markdown(frontmatter: dict[str, Any], body: str) -> str:
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    lines.append("")
    lines.append(body.strip())
    return "\n".join(lines).strip() + "\n"

def normalize_concept_id(value: str) -> str:
    text = value.strip().replace("\\", "/").strip("/")
    text = text.removesuffix(".md")
    parts = [safe_path_segment(part) for part in text.split("/") if part.strip()]
    return "/".join(part for part in parts if part)

def safe_path_segment(value: Any, fallback: str = "concept") -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\.md$", "", text)
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", text).strip("-")
    return text[:80] or fallback

def extract_links(markdown: str) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    for match in re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", markdown):
        label = match.group(1).strip()
        target = match.group(2).strip()
        if target:
            links.append({"label": label, "target": target})
    return links

def extract_citations(markdown: str) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    section = _citations_section(markdown)
    for index, match in enumerate(
        re.finditer(r"(?:\[(\d+)\]\s*)?\[([^\]]+)\]\(([^)]+)\)", section), start=1
    ):
        citations.append(
            {
                "index": int(match.group(1) or index),
                "label": match.group(2).strip(),
                "target": match.group(3).strip(),
            }
        )
    return citations

def _source_document_concept(
    document: KnowledgeDocument,
    concept_id: str,
    section_nodes: list[dict[str, Any]],
    buckets: list[KnowledgeBucket],
    document_card: dict[str, Any],
) -> dict[str, Any]:
    title = document.title or Path(document.filename).stem or document.filename
    document_metadata = document.metadata_json or {}
    summary = str(document_card.get("summary") or document_metadata.get("summary") or "")
    outline_lines = [
        f"- [{node.get('title') or node.get('path')}](/{concept_id}/sections/{safe_path_segment(node.get('section_id'))}.md) - {node.get('summary') or ''}"
        for node in section_nodes[:40]
    ]
    bucket_lines = [
        f"- [{bucket.title}](/topics/{safe_path_segment(bucket.bucket_key or bucket.title)}.md) - {bucket.summary}"
        for bucket in buckets[:30]
    ]
    body = "\n".join(
        [
            "# Summary",
            "",
            summary or "由原始资料生成的 OKF 文档入口。",
            "",
            "# Outline",
            "",
            *(outline_lines or ["- 暂无章节结构"]),
            "",
            "# Knowledge Buckets",
            "",
            *(bucket_lines or ["- 暂无知识桶"]),
            "",
            "# Citations",
            "",
            f"[1] [Original document](ultrarag://knowledge/documents/{document.id})",
        ]
    )
    frontmatter = {
        "type": "Source Document",
        "title": title,
        "description": summary or f"从 {document.filename} 生成的原始资料入口页。",
        "resource": f"ultrarag://knowledge/documents/{document.id}",
        "tags": ["source", "business-knowledge"],
        "timestamp": document.updated_at.isoformat(),
    }
    return {
        "concept_id": concept_id,
        "concept_type": "Source Document",
        "title": title,
        "description": frontmatter["description"],
        "document_id": document.id,
        "content_md": render_okf_markdown(frontmatter, body),
        "source_refs": [{"document_id": document.id, "filename": document.filename}],
    }

def _source_section_concept(
    document: KnowledgeDocument, document_concept_id: str, section: dict[str, Any]
) -> dict[str, Any]:
    section_id = str(
        section.get("section_id") or safe_path_segment(section.get("title"), "section")
    )
    concept_id = f"{document_concept_id}/sections/{safe_path_segment(section_id)}"
    title = str(section.get("path") or section.get("title") or "未命名章节")
    summary = str(section.get("summary") or "")
    content = str(section.get("content") or "")
    body = "\n".join(
        [
            "# Summary",
            "",
            summary or _summarize(content, 260),
            "",
            "# Content",
            "",
            content,
            "",
            "# Citations",
            "",
            f"[1] [Original document](ultrarag://knowledge/documents/{document.id})",
        ]
    )
    frontmatter = {
        "type": "Source Section",
        "title": title,
        "description": summary or _summarize(content, 180),
        "resource": f"ultrarag://knowledge/documents/{document.id}#section={section_id}",
        "tags": ["source-section"],
        "timestamp": document.updated_at.isoformat(),
        "source_document": document_concept_id,
        "section_path": title,
    }
    return {
        "concept_id": concept_id,
        "concept_type": "Source Section",
        "title": title,
        "description": frontmatter["description"],
        "document_id": document.id,
        "content_md": render_okf_markdown(frontmatter, body),
        "source_refs": [
            {"document_id": document.id, "section_id": section_id, "section_path": title}
        ],
    }

def _bucket_concept(
    document: KnowledgeDocument, document_concept_id: str, bucket: KnowledgeBucket
) -> dict[str, Any]:
    metadata = bucket.metadata_json or {}
    concept_type = _concept_type_for_bucket(bucket)
    folder = (
        "playbooks"
        if concept_type == "Playbook"
        else "rules"
        if concept_type == "Business Rule"
        else "topics"
    )
    concept_id = f"{folder}/{safe_path_segment(bucket.bucket_key or bucket.title)}"
    content = str(metadata.get("content") or bucket.summary)
    section_links = [
        f"- [{path}](/{document_concept_id}/sections/{safe_path_segment(str(section_id))}.md)"
        for path, section_id in zip(
            metadata.get("section_paths") or [], metadata.get("section_ids") or []
        )
        if path and section_id
    ]
    body = "\n".join(
        [
            "# Summary",
            "",
            bucket.summary,
            "",
            "# Source Sections",
            "",
            *(section_links or [f"- [Source document](/{document_concept_id}.md)"]),
            "",
            "# Notes",
            "",
            content,
            "",
            "# Citations",
            "",
            f"[1] [Original document](ultrarag://knowledge/documents/{document.id})",
        ]
    )
    frontmatter = {
        "type": concept_type,
        "title": bucket.title,
        "description": bucket.summary,
        "resource": f"ultrarag://knowledge/buckets/{bucket.id}",
        "tags": ["knowledge-bucket", str(metadata.get("bucket_type") or "structure")],
        "timestamp": bucket.updated_at.isoformat(),
        "source_document": document_concept_id,
    }
    return {
        "concept_id": concept_id,
        "concept_type": concept_type,
        "title": bucket.title,
        "description": bucket.summary,
        "document_id": document.id,
        "content_md": render_okf_markdown(frontmatter, body),
        "source_refs": [
            {"document_id": document.id, "bucket_id": bucket.id, "bucket_key": bucket.bucket_key}
        ],
    }

def _concept_type_for_bucket(bucket: KnowledgeBucket) -> str:
    metadata = bucket.metadata_json if isinstance(bucket.metadata_json, dict) else {}
    concept_type = str(metadata.get("concept_type") or "Topic").strip()
    return concept_type if concept_type in {"Topic", "Playbook", "Business Rule"} else "Topic"

def _parse_okf_zip(content: bytes) -> list[ParsedOkfDocument]:
    rows: list[ParsedOkfDocument] = []
    with ZipFile(BytesIO(content)) as archive:
        for name in sorted(archive.namelist()):
            if name.endswith("/") or not name.lower().endswith(".md"):
                continue
            path = Path(name)
            if path.name in RESERVED_FILENAMES:
                continue
            concept_id = normalize_concept_id(path.with_suffix("").as_posix())
            rows.append(parse_okf_markdown(concept_id, _decode_text(archive.read(name))))
    return rows

def _parse_frontmatter(raw: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore

        parsed = yaml.safe_load(raw)
        if isinstance(parsed, dict):
            return dict(parsed)
    except Exception:
        pass
    result: dict[str, Any] = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        result[key] = _parse_scalar(value)
    return result

def _parse_scalar(value: str) -> Any:
    if not value:
        return ""
    if value[0] in {'"', "'", "[", "{"}:
        try:
            return json.loads(value.replace("'", '"') if value[0] == "'" else value)
        except Exception:
            return value.strip("\"'")
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return value

def _yaml_scalar(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)

def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="ignore")

def _citations_section(markdown: str) -> str:
    match = re.search(r"(?im)^#\s+citations\s*$", markdown)
    if not match:
        match = re.search(r"(?im)^#\s+引用\s*$", markdown)
    if not match:
        return ""
    return markdown[match.end() :]

def _strip_frontmatter(markdown: str) -> str:
    text = markdown.replace("\r\n", "\n").replace("\r", "\n").strip()
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end >= 0:
            return text[end + 4 :].strip()
    return text

def _chunk_text(text: str, max_chars: int) -> list[str]:
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n+", text) if item.strip()]
    if not paragraphs:
        paragraphs = [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if current:
                chunks.append("\n\n".join(current).strip())
                current = []
                current_len = 0
            chunks.extend(
                paragraph[index : index + max_chars].strip()
                for index in range(0, len(paragraph), max_chars)
            )
            continue
        projected = current_len + len(paragraph) + (2 if current else 0)
        if current and projected > max_chars:
            chunks.append("\n\n".join(current).strip())
            current = []
            current_len = 0
        current.append(paragraph)
        current_len += len(paragraph) + (2 if current_len else 0)
    if current:
        chunks.append("\n\n".join(current).strip())
    return [chunk for chunk in chunks if chunk.strip()]

def _summarize(text: str, max_chars: int) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    if len(compact) <= max_chars:
        return compact
    return compact[:max_chars].rstrip() + "..."

def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None

def _query_tokens(query: str) -> list[str]:
    tokens: list[str] = []
    for token in re.findall(r"[A-Za-z0-9_.:/-]{2,}|[\u4e00-\u9fff]{2,}", query):
        text = token.lower().strip()
        if not text:
            continue
        tokens.append(text)
        if re.fullmatch(r"[\u4e00-\u9fff]{3,}", text):
            # Chinese queries usually have no whitespace. Add short n-grams so
            # "刚创建订单想取消" can match concept pages containing
            # "取消刚创建的订单" or "订单处理".
            for size in (4, 3, 2):
                if len(text) <= size:
                    continue
                tokens.extend(text[index : index + size] for index in range(len(text) - size + 1))
    seen: set[str] = set()
    unique_tokens: list[str] = []
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        unique_tokens.append(token)
    return unique_tokens[:96]

def lint_okf_concepts(concepts):
    concept_ids = {row.concept_id for row in concepts}
    issues: list[dict[str, Any]] = []
    inbound: dict[str, int] = {concept_id: 0 for concept_id in concept_ids}
    for row in concepts:
        if not row.frontmatter_json.get('type'):
            issues.append(_lint_issue(row, 'missing_type', '概念缺少 OKF 必需 type 字段。'))
        if not row.citations_json and row.concept_type not in {'Topic', 'Query Analysis'}:
            issues.append(_lint_issue(row, 'missing_citation', '概念没有 # Citations 或外部来源引用。'))
        for link in row.links_json or []:
            target = normalize_concept_id(str(link.get('target') or '').removeprefix('/').removesuffix('.md'))
            if not target:
                continue
            if target in inbound:
                inbound[target] += 1
            elif not str(link.get('target') or '').startswith(('http://', 'https://', 'ultrarag://')):
                issues.append(_lint_issue(row, 'broken_link', f'链接目标不存在：{link.get('target')}'))
    for row in concepts:
        if row.concept_type not in {'Source Document'} and inbound.get(row.concept_id, 0) == 0:
            issues.append(_lint_issue(row, 'orphan_concept', '概念没有入站链接，可能难以被渐进发现。'))
    title_groups: dict[str, list[KnowledgeConcept]] = {}
    for row in concepts:
        title_groups.setdefault(row.title.strip().lower(), []).append(row)
    for rows in title_groups.values():
        if len(rows) > 1:
            for row in rows:
                issues.append(_lint_issue(row, 'duplicate_title', f'存在重复标题：{row.title}'))
    return issues

def _lint_issue(row, issue_type, message):
    return {"issue_type": issue_type, "title": row.title, "message": message, "concept_id": row.concept_id, "concept_type": row.concept_type, "document_id": row.document_id}
