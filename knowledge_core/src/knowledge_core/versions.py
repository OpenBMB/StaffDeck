from __future__ import annotations

from copy import deepcopy

from sqlmodel import Session, select

from .models import (
    KnowledgeBaseVersion,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeConcept,
    KnowledgeDiscoverySuggestion,
    KnowledgeDocument,
    new_id,
    utc_now,
)

ASSET_MODELS = (
    KnowledgeDocument,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeConcept,
    KnowledgeDiscoverySuggestion,
)


def clone_version(
    db: Session,
    source: KnowledgeBaseVersion,
    label: str,
    *, preserve_metadata: bool = False,
) -> KnowledgeBaseVersion:
    target = KnowledgeBaseVersion(
        tenant_id=source.tenant_id,
        knowledge_base_id=source.knowledge_base_id,
        version=label,
        name=source.name,
        description=source.description,
        capability_scope=source.capability_scope,
        metadata_json=deepcopy(source.metadata_json),
    )
    db.add(target)
    db.flush()
    assets = [
        (
            model,
            list(
                db.exec(
                    select(model).where(
                        model.tenant_id == source.tenant_id,
                        model.knowledge_base_version_id == source.id,
                    ).order_by(model.created_at.asc())
                ).all()
            ),
        )
        for model in ASSET_MODELS
    ]
    mapping = {source.id: target.id}
    for model, rows in assets:
        for row in rows:
            mapping[row.id] = new_id(row.id.split("_", 1)[0])

    def remap(value):
        if isinstance(value, dict):
            return {key: remap(item) for key, item in value.items()}
        if isinstance(value, list):
            return [remap(item) for item in value]
        if isinstance(value, str):
            if value in mapping:
                return mapping[value]
            # Source references embed IDs alongside paragraph/section suffixes.
            for old, new in mapping.items():
                value = value.replace(old, new)
        return value

    for model, rows in assets:
        for row in rows:
            values = remap(deepcopy(row.model_dump()))
            if preserve_metadata and model in {KnowledgeDocument, KnowledgeBucket, KnowledgeChunk}:
                values["metadata_json"] = deepcopy(row.metadata_json)
            if preserve_metadata and model is KnowledgeConcept:
                for field in ("content_md", "frontmatter_json", "links_json", "citations_json"):
                    values[field] = deepcopy(getattr(row, field))
                values["source_refs_json"] = [
                    {**ref, **({"document_id": mapping[ref["document_id"]]} if ref.get("document_id") in mapping else {})}
                    for ref in deepcopy(row.source_refs_json)
                ]
            if preserve_metadata and model is KnowledgeDiscoverySuggestion:
                values["payload_json"] = deepcopy(row.payload_json)
                values["source_refs_json"] = [
                    {**ref, **({"document_id": mapping[ref["document_id"]]} if ref.get("document_id") in mapping else {})}
                    for ref in deepcopy(row.source_refs_json)
                ]
            values["created_at"] = row.created_at if preserve_metadata else utc_now()
            values["updated_at"] = utc_now()
            db.add(model(**values))
    db.flush()
    return target
