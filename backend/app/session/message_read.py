from __future__ import annotations

import re

from sqlalchemy import or_
from sqlmodel import Session, select

from app.db.models import KnowledgeChunk, KnowledgeConcept, Message
from app.knowledge.citations import CITATION_EXCERPT_CHAR_LIMIT, compact_knowledge_citation_labels
from app.session.session_schema import MessageRead


def message_read(
    row: Message,
    feedback_rating: str | None = None,
    turn_id: str | None = None,
    db: Session | None = None,
    content_override: str | None = None,
) -> MessageRead:
    """Serialize every chat surface through the same metadata and citation path."""
    metadata = _message_metadata_read(row, db)
    content = row.content if content_override is None else content_override
    if row.role == "assistant":
        content, compacted_citations = compact_knowledge_citation_labels(
            content,
            metadata.get("knowledge_citations"),
        )
        metadata = dict(metadata)
        if compacted_citations:
            metadata["knowledge_citations"] = compacted_citations
        else:
            metadata.pop("knowledge_citations", None)
            metadata.pop("knowledge_query", None)
    metadata_turn_id = str(metadata.get("turn_id") or metadata.get("user_message_id") or "").strip()
    return MessageRead(
        id=row.id,
        tenant_id=row.tenant_id,
        session_id=row.session_id,
        role=row.role,
        content=content,
        metadata=metadata,
        turn_id=turn_id or metadata_turn_id or None,
        created_at=row.created_at.isoformat(),
        feedback_rating=feedback_rating,
    )


def _message_metadata_read(row: Message, db: Session | None = None) -> dict:
    metadata = dict(row.metadata_json or {})
    if db is None:
        return metadata
    citations = metadata.get("knowledge_citations")
    if not isinstance(citations, list) or not citations:
        return metadata
    hydrated: list[object] = []
    changed = False
    for citation in citations:
        if not isinstance(citation, dict):
            hydrated.append(citation)
            continue
        content = _citation_content_from_db(db, row.tenant_id, citation)
        if content:
            next_citation = dict(citation)
            next_citation["content"] = content[:CITATION_EXCERPT_CHAR_LIMIT]
            next_citation["excerpt"] = content[:CITATION_EXCERPT_CHAR_LIMIT]
            hydrated.append(next_citation)
            changed = True
        else:
            hydrated.append(citation)
    if changed:
        metadata["knowledge_citations"] = hydrated
    return metadata


def _citation_content_from_db(db: Session, tenant_id: str, citation: dict) -> str:
    concept_id = str(citation.get("concept_id") or "").strip()
    if concept_id:
        concept = db.exec(
            select(KnowledgeConcept).where(
                KnowledgeConcept.tenant_id == tenant_id,
                or_(KnowledgeConcept.concept_id == concept_id, KnowledgeConcept.id == concept_id),
            )
        ).first()
        if concept:
            content = _strip_okf_frontmatter(concept.content_md or "")
            if content:
                return content
    chunk_id = str(citation.get("chunk_id") or "").strip()
    if chunk_id:
        chunk = db.get(KnowledgeChunk, chunk_id)
        if chunk and chunk.tenant_id == tenant_id and chunk.content:
            return chunk.content
    return ""


def _strip_okf_frontmatter(value: str) -> str:
    return re.sub(r"^---[\s\S]*?---\s*", "", value or "", count=1).strip()
