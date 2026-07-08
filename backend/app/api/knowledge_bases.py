from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func
from sqlmodel import Session, select

from app.db import get_session
from app.agents.branching import (
    ensure_agent_private_knowledge_branch,
    ensure_knowledge_base_version,
    ensure_open_gallery_binding,
    get_agent,
    hide_open_gallery_binding,
    is_open_gallery_resource,
    knowledge_version_for_upload,
    mark_resource_open_gallery,
    mark_resource_private_for_agent,
    promote_knowledge_branch_to_overall,
    rollback_knowledge_branch,
    sync_knowledge_branch_from_overall,
)
from app.db.models import (
    AgentKnowledgeBranch,
    AgentResourceBinding,
    KnowledgeBase,
    KnowledgeBaseVersion,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeConcept,
    KnowledgeDiscoverySuggestion,
    KnowledgeDocument,
    KnowledgeIngestJob,
    utc_now,
)
from app.knowledge.schema import (
    KnowledgeBaseCreateRequest,
    KnowledgeConceptRead,
    KnowledgeConceptUpdateRequest,
    KnowledgeBaseRead,
    KnowledgeBaseRollbackRequest,
    KnowledgeBaseUpdateRequest,
)
from app.knowledge.okf import (
    build_okf_for_document,
    export_okf_bundle,
    lint_okf_concepts,
    normalize_concept_id,
    parse_okf_markdown,
    persist_lint_issues,
    upsert_concepts,
)
from app.security.tenant import ensure_tenant

router = APIRouter(prefix="/api/enterprise/knowledge-bases", tags=["enterprise:knowledge-bases"])


@router.get("", response_model=list[KnowledgeBaseRead])
def list_knowledge_bases(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> list[KnowledgeBaseRead]:
    ensure_tenant(db, tenant_id)
    agent = get_agent(db, tenant_id, agent_id)
    if agent and not agent.is_overall:
        branches = db.exec(
            select(AgentKnowledgeBranch)
            .where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == agent.id,
                AgentKnowledgeBranch.status != "deleted",
            )
            .order_by(AgentKnowledgeBranch.updated_at.desc())
        ).all()
        if not branches:
            return []
        knowledge_base_ids = [branch.knowledge_base_id for branch in branches]
        rows_by_id = {
            row.id: row
            for row in db.exec(
                select(KnowledgeBase).where(
                    KnowledgeBase.tenant_id == tenant_id,
                    KnowledgeBase.id.in_(knowledge_base_ids),
                )
            ).all()
        }
        versions: dict[str, KnowledgeBaseVersion] = {}
        for branch in branches:
            kb = rows_by_id.get(branch.knowledge_base_id)
            if kb:
                versions[kb.id] = ensure_knowledge_base_version(db, kb, branch.head_version)
        stats = _knowledge_base_stats(db, tenant_id, [version.id for version in versions.values()])
        branch_meta = _knowledge_branch_meta(db, tenant_id, agent_id)
        return [
            knowledge_base_read(
                rows_by_id[branch.knowledge_base_id],
                stats.get(branch.knowledge_base_id, {}),
                version_row=versions.get(branch.knowledge_base_id),
                branch_meta=branch_meta.get(branch.knowledge_base_id),
            )
            for branch in branches
            if branch.knowledge_base_id in rows_by_id
        ]
    visible_versions = _management_knowledge_base_versions(db, tenant_id, agent_id)
    visible_ids = list(visible_versions.keys())
    rows = db.exec(
        select(KnowledgeBase)
        .where(KnowledgeBase.tenant_id == tenant_id, KnowledgeBase.id.in_(visible_ids) if visible_ids else KnowledgeBase.id == "__none__")
        .order_by(KnowledgeBase.updated_at.desc())
    ).all()
    stats = _knowledge_base_stats(db, tenant_id, [version.id for version in visible_versions.values()])
    branch_meta = _knowledge_branch_meta(db, tenant_id, agent_id)
    return [
        knowledge_base_read(
            row,
            stats.get(row.id, {}),
            version_row=visible_versions.get(row.id),
            branch_meta=branch_meta.get(row.id),
        )
        for row in rows
    ]


@router.post("", response_model=KnowledgeBaseRead)
def create_knowledge_base(
    request: KnowledgeBaseCreateRequest,
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> KnowledgeBaseRead:
    ensure_tenant(db, request.tenant_id)
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Knowledge base name cannot be empty")
    existing = db.exec(
        select(KnowledgeBase).where(KnowledgeBase.tenant_id == request.tenant_id, KnowledgeBase.name == name)
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Knowledge base name already exists")
    row = KnowledgeBase(
        tenant_id=request.tenant_id,
        name=name,
        description=request.description,
        metadata_json=request.metadata,
        status="active",
    )
    db.add(row)
    db.flush()
    agent = get_agent(db, request.tenant_id, agent_id)
    if agent and not agent.is_overall:
        mark_resource_private_for_agent(row, agent.id)
        ensure_agent_private_knowledge_branch(db, request.tenant_id, agent.id, row)
    else:
        mark_resource_open_gallery(row)
        ensure_open_gallery_binding(db, request.tenant_id, "knowledge_base", row.id, "active")
    db.commit()
    db.refresh(row)
    return knowledge_base_read(row, {}, version_row=ensure_knowledge_base_version(db, row))


@router.get("/{knowledge_base_id}", response_model=KnowledgeBaseRead)
def get_knowledge_base(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> KnowledgeBaseRead:
    row = _get_knowledge_base(db, tenant_id, knowledge_base_id)
    visible_versions = _management_knowledge_base_versions(db, tenant_id, agent_id)
    stats = _knowledge_base_stats(
        db,
        tenant_id,
        [visible_versions[row.id].id] if row.id in visible_versions else None,
    )
    branch_meta = _knowledge_branch_meta(db, tenant_id, agent_id).get(row.id)
    return knowledge_base_read(
        row,
        stats.get(row.id, {}),
        version_row=visible_versions.get(row.id),
        branch_meta=branch_meta,
    )


@router.put("/{knowledge_base_id}", response_model=KnowledgeBaseRead)
def update_knowledge_base(
    knowledge_base_id: str,
    request: KnowledgeBaseUpdateRequest,
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> KnowledgeBaseRead:
    row = _get_knowledge_base(db, request.tenant_id, knowledge_base_id)
    agent = get_agent(db, request.tenant_id, agent_id)
    if agent and not agent.is_overall:
        branch = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == request.tenant_id,
                AgentKnowledgeBranch.agent_id == agent.id,
                AgentKnowledgeBranch.knowledge_base_id == knowledge_base_id,
            )
        ).first()
        if not branch:
            branch = sync_knowledge_branch_from_overall(db, request.tenant_id, agent.id, knowledge_base_id)
        version = ensure_knowledge_base_version(db, row, branch.head_version)
        if request.name is not None:
            name = request.name.strip()
            if not name:
                raise HTTPException(status_code=400, detail="Knowledge base name cannot be empty")
            version.name = name
        if request.description is not None:
            version.description = request.description
        if request.metadata is not None:
            version.metadata_json = request.metadata
        if request.status is not None:
            branch.status = "active" if request.status == "active" else "inactive"
            binding = db.exec(
                select(AgentResourceBinding).where(
                    AgentResourceBinding.tenant_id == request.tenant_id,
                    AgentResourceBinding.agent_id == agent.id,
                    AgentResourceBinding.resource_type == "knowledge_base",
                    AgentResourceBinding.resource_id == knowledge_base_id,
                )
            ).first()
            if binding:
                binding.status = branch.status
                binding.updated_at = utc_now()
                db.add(binding)
        if request.name is not None or request.description is not None or request.metadata is not None:
            branch.sync_state = "diverged"
        version.updated_at = utc_now()
        branch.updated_at = utc_now()
        db.add(version)
        db.add(branch)
        db.commit()
        db.refresh(row)
        stats = _knowledge_base_stats(db, request.tenant_id, [version.id]).get(row.id, {})
        return knowledge_base_read(
            row,
            stats,
            version_row=version,
            branch_meta={
                "base_version": branch.base_version,
                "head_version": branch.head_version,
                "sync_state": branch.sync_state,
                "status": branch.status,
            },
        )
    if request.name is not None:
        name = request.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Knowledge base name cannot be empty")
        conflict = db.exec(
            select(KnowledgeBase).where(
                KnowledgeBase.tenant_id == request.tenant_id,
                KnowledgeBase.name == name,
                KnowledgeBase.id != row.id,
            )
        ).first()
        if conflict:
            raise HTTPException(status_code=409, detail="Knowledge base name already exists")
        row.name = name
    if request.description is not None:
        row.description = request.description
    if request.status is not None:
        row.status = request.status
    if request.metadata is not None:
        row.metadata_json = request.metadata
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return knowledge_base_read(
        row,
        _knowledge_base_stats(db, request.tenant_id).get(row.id, {}),
        version_row=ensure_knowledge_base_version(db, row),
    )


@router.get("/{knowledge_base_id}/versions")
def list_knowledge_base_versions(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> list[dict[str, object]]:
    row = _get_knowledge_base(db, tenant_id, knowledge_base_id)
    agent = get_agent(db, tenant_id, agent_id)
    branch = None
    if agent and not agent.is_overall:
        branch = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == agent.id,
                AgentKnowledgeBranch.knowledge_base_id == knowledge_base_id,
            )
        ).first()
    rows = db.exec(
        select(KnowledgeBaseVersion)
        .where(KnowledgeBaseVersion.tenant_id == tenant_id, KnowledgeBaseVersion.knowledge_base_id == row.id)
        .order_by(KnowledgeBaseVersion.updated_at.desc())
    ).all()
    return [
        {
            "id": version.id,
            "version": version.version,
            "name": version.name,
            "description": version.description,
            "status": version.status,
            "is_head": bool(branch and branch.head_version == version.version),
            "is_base": bool(branch and branch.base_version == version.version),
            "updated_at": version.updated_at.isoformat(),
            "created_at": version.created_at.isoformat(),
        }
        for version in rows
    ]


@router.get("/{knowledge_base_id}/okf/concepts", response_model=list[KnowledgeConceptRead])
def list_okf_concepts(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    concept_type: str | None = Query(None),
    db: Session = Depends(get_session),
) -> list[KnowledgeConceptRead]:
    version = _visible_knowledge_version(db, tenant_id, knowledge_base_id, agent_id)
    _ensure_okf_concepts_for_version(db, tenant_id, knowledge_base_id, version.id)
    stmt = select(KnowledgeConcept).where(
        KnowledgeConcept.tenant_id == tenant_id,
        KnowledgeConcept.knowledge_base_id == knowledge_base_id,
        KnowledgeConcept.knowledge_base_version_id == version.id,
        KnowledgeConcept.status != "deleted",
    )
    if concept_type:
        stmt = stmt.where(KnowledgeConcept.concept_type == concept_type)
    rows = db.exec(stmt.order_by(KnowledgeConcept.concept_type, KnowledgeConcept.concept_id)).all()
    return [concept_read(row) for row in rows]


@router.get("/{knowledge_base_id}/okf/concepts/{concept_id:path}", response_model=KnowledgeConceptRead)
def get_okf_concept(
    knowledge_base_id: str,
    concept_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> KnowledgeConceptRead:
    version = _visible_knowledge_version(db, tenant_id, knowledge_base_id, agent_id)
    _ensure_okf_concepts_for_version(db, tenant_id, knowledge_base_id, version.id)
    row = _get_concept(db, tenant_id, knowledge_base_id, version.id, concept_id)
    return concept_read(row)


@router.put("/{knowledge_base_id}/okf/concepts/{concept_id:path}", response_model=KnowledgeConceptRead)
def upsert_okf_concept(
    knowledge_base_id: str,
    concept_id: str,
    request: KnowledgeConceptUpdateRequest,
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> KnowledgeConceptRead:
    version = _writable_knowledge_version(db, request.tenant_id, knowledge_base_id, agent_id)
    document_id = _document_id_for_version(db, request.tenant_id, knowledge_base_id, version.id, request.document_id)
    parsed = parse_okf_markdown(concept_id, request.content_md)
    rows = upsert_concepts(
        db,
        request.tenant_id,
        knowledge_base_id,
        version.id,
        [
            {
                "concept_id": parsed.concept_id,
                "content_md": parsed.content_md,
                "document_id": document_id,
                "status": request.status,
            }
        ],
    )
    return concept_read(rows[0])


@router.get("/{knowledge_base_id}/okf/export")
def export_okf(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> Response:
    kb = _get_knowledge_base(db, tenant_id, knowledge_base_id)
    version = _visible_knowledge_version(db, tenant_id, knowledge_base_id, agent_id)
    _ensure_okf_concepts_for_version(db, tenant_id, knowledge_base_id, version.id)
    rows = db.exec(
        select(KnowledgeConcept)
        .where(
            KnowledgeConcept.tenant_id == tenant_id,
            KnowledgeConcept.knowledge_base_id == knowledge_base_id,
            KnowledgeConcept.knowledge_base_version_id == version.id,
            KnowledgeConcept.status == "active",
        )
        .order_by(KnowledgeConcept.concept_id)
    ).all()
    archive = export_okf_bundle(kb, version.id, rows)
    filename = f"{kb.name or knowledge_base_id}-okf-{version.version}.zip"
    fallback_filename = f"{knowledge_base_id}-okf-{version.version}.zip"
    return Response(
        content=archive,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{fallback_filename}"; filename*=UTF-8\'\'{quote(filename)}'
            )
        },
    )


@router.post("/{knowledge_base_id}/okf/lint")
def lint_okf(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> dict[str, object]:
    version = _visible_knowledge_version(db, tenant_id, knowledge_base_id, agent_id)
    _ensure_okf_concepts_for_version(db, tenant_id, knowledge_base_id, version.id)
    issues = lint_okf_concepts(db, tenant_id, knowledge_base_id, version.id)
    persist_lint_issues(db, tenant_id, knowledge_base_id, version.id, issues)
    return {"status": "ok", "issue_count": len(issues), "issues": issues}


@router.delete("/{knowledge_base_id}")
def delete_knowledge_base(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> dict[str, str]:
    agent = get_agent(db, tenant_id, agent_id)
    if agent and not agent.is_overall:
        row = _get_knowledge_base(db, tenant_id, knowledge_base_id)
        branch = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == agent.id,
                AgentKnowledgeBranch.knowledge_base_id == knowledge_base_id,
            )
        ).first()
        if not branch:
            branch = sync_knowledge_branch_from_overall(db, tenant_id, agent.id, knowledge_base_id)
        branch.status = "deleted"
        branch.updated_at = utc_now()
        binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == tenant_id,
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "knowledge_base",
                AgentResourceBinding.resource_id == row.id,
            )
        ).first()
        if not binding:
            binding = AgentResourceBinding(
                tenant_id=tenant_id,
                agent_id=agent.id,
                resource_type="knowledge_base",
                resource_id=row.id,
                status="deleted",
            )
        else:
            binding.status = "deleted"
            binding.updated_at = utc_now()
        db.add(branch)
        db.add(binding)
        db.commit()
        return {"status": "hidden"}
    row = _get_knowledge_base(db, tenant_id, knowledge_base_id)
    if agent and agent.is_overall:
        if not is_open_gallery_resource(db, tenant_id, "knowledge_base", row):
            raise HTTPException(status_code=404, detail="Knowledge base not visible in open gallery")
        hide_open_gallery_binding(db, tenant_id, "knowledge_base", row.id)
        db.commit()
        return {"status": "hidden"}
    for model in (
        KnowledgeDiscoverySuggestion,
        KnowledgeIngestJob,
        KnowledgeConcept,
        KnowledgeChunk,
        KnowledgeBucket,
        KnowledgeDocument,
        KnowledgeBaseVersion,
        AgentKnowledgeBranch,
    ):
        children = db.exec(
            select(model).where(
                model.tenant_id == tenant_id,
                model.knowledge_base_id == row.id,
            )
        ).all()
        for child in children:
            db.delete(child)
    bindings = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.resource_type == "knowledge_base",
            AgentResourceBinding.resource_id == row.id,
        )
    ).all()
    for binding in bindings:
        db.delete(binding)
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@router.post("/{knowledge_base_id}/sync-from-overall")
def sync_knowledge_base_from_overall(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str = Query(...),
    db: Session = Depends(get_session),
) -> dict[str, object]:
    agent = get_agent(db, tenant_id, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.is_overall:
        raise HTTPException(status_code=400, detail="Overall agent is already the trunk")
    branch = sync_knowledge_branch_from_overall(db, tenant_id, agent_id, knowledge_base_id)
    db.commit()
    return {"status": "synced", "knowledge_base_id": knowledge_base_id, "head_version": branch.head_version}


@router.post("/{knowledge_base_id}/promote-to-overall")
def promote_knowledge_base_to_overall(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str = Query(...),
    db: Session = Depends(get_session),
) -> dict[str, object]:
    agent = get_agent(db, tenant_id, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.is_overall:
        raise HTTPException(status_code=400, detail="Overall agent does not have a branch to promote")
    version = promote_knowledge_branch_to_overall(db, tenant_id, agent_id, knowledge_base_id)
    db.commit()
    return {"status": "promoted", "knowledge_base_id": knowledge_base_id, "version": version.version}


@router.post("/{knowledge_base_id}/rollback")
def rollback_knowledge_base(
    knowledge_base_id: str,
    request: KnowledgeBaseRollbackRequest,
    db: Session = Depends(get_session),
) -> dict[str, object]:
    agent = get_agent(db, request.tenant_id, request.agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.is_overall:
        raise HTTPException(status_code=400, detail="Use overall version management for trunk knowledge base")
    branch = rollback_knowledge_branch(db, request.tenant_id, request.agent_id, knowledge_base_id, request.version)
    db.commit()
    return {"status": "rolled_back", "knowledge_base_id": knowledge_base_id, "head_version": branch.head_version}


def knowledge_base_read(
    row: KnowledgeBase,
    stats: dict[str, int],
    version_row: KnowledgeBaseVersion | None = None,
    branch_meta: dict[str, str] | None = None,
) -> KnowledgeBaseRead:
    branch_status = (branch_meta or {}).get("status")
    if branch_status == "inactive":
        effective_status = "archived"
    elif branch_status == "active":
        effective_status = "active"
    elif branch_status:
        effective_status = branch_status
    else:
        effective_status = row.status
    return KnowledgeBaseRead(
        id=row.id,
        tenant_id=row.tenant_id,
        name=version_row.name if version_row else row.name,
        description=version_row.description if version_row else row.description,
        status=effective_status,
        version=version_row.version if version_row else None,
        branch_sync_state=(branch_meta or {}).get("sync_state"),
        branch_base_version=(branch_meta or {}).get("base_version"),
        branch_head_version=(branch_meta or {}).get("head_version"),
        metadata=(version_row.metadata_json if version_row else row.metadata_json) or {},
        document_count=int(stats.get("document_count", 0)),
        bucket_count=int(stats.get("bucket_count", 0)),
        chunk_count=int(stats.get("chunk_count", 0)),
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def concept_read(row: KnowledgeConcept) -> KnowledgeConceptRead:
    return KnowledgeConceptRead(
        id=row.id,
        tenant_id=row.tenant_id,
        knowledge_base_id=row.knowledge_base_id,
        knowledge_base_version_id=row.knowledge_base_version_id,
        document_id=row.document_id,
        concept_id=row.concept_id,
        concept_type=row.concept_type,
        title=row.title,
        description=row.description,
        content_md=row.content_md,
        frontmatter=row.frontmatter_json or {},
        links=row.links_json or [],
        citations=row.citations_json or [],
        source_refs=row.source_refs_json or [],
        status=row.status,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _visible_knowledge_version(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    agent_id: str | None,
) -> KnowledgeBaseVersion:
    _get_knowledge_base(db, tenant_id, knowledge_base_id)
    versions = _management_knowledge_base_versions(db, tenant_id, agent_id)
    version = versions.get(knowledge_base_id)
    if not version:
        raise HTTPException(status_code=404, detail="Knowledge base version not visible")
    return version


def _writable_knowledge_version(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    agent_id: str | None,
) -> KnowledgeBaseVersion:
    _get_knowledge_base(db, tenant_id, knowledge_base_id)
    agent = get_agent(db, tenant_id, agent_id)
    if agent and not agent.is_overall:
        version = knowledge_version_for_upload(db, tenant_id, knowledge_base_id, agent.id)
        db.commit()
        return version
    return _visible_knowledge_version(db, tenant_id, knowledge_base_id, agent_id)


def _ensure_okf_concepts_for_version(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    version_id: str,
) -> None:
    documents = db.exec(
        select(KnowledgeDocument).where(
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeDocument.knowledge_base_id == knowledge_base_id,
            KnowledgeDocument.knowledge_base_version_id == version_id,
            KnowledgeDocument.status == "ready",
        )
    ).all()
    for document in documents:
        existing = db.exec(
            select(KnowledgeConcept.id).where(
                KnowledgeConcept.tenant_id == tenant_id,
                KnowledgeConcept.knowledge_base_id == knowledge_base_id,
                KnowledgeConcept.knowledge_base_version_id == version_id,
                KnowledgeConcept.document_id == document.id,
            )
        ).first()
        if existing:
            continue
        metadata = document.metadata_json or {}
        section_nodes = metadata.get("section_tree") if isinstance(metadata.get("section_tree"), list) else []
        buckets = db.exec(
            select(KnowledgeBucket)
            .where(
                KnowledgeBucket.tenant_id == tenant_id,
                KnowledgeBucket.knowledge_base_id == knowledge_base_id,
                KnowledgeBucket.knowledge_base_version_id == version_id,
                KnowledgeBucket.document_id == document.id,
            )
            .order_by(KnowledgeBucket.created_at.asc())
        ).all()
        if not section_nodes and not buckets:
            continue
        upsert_concepts(
            db,
            tenant_id,
            knowledge_base_id,
            version_id,
            build_okf_for_document(document, section_nodes, buckets),
        )


def _get_concept(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    knowledge_base_version_id: str,
    concept_id: str,
) -> KnowledgeConcept:
    normalized = normalize_concept_id(concept_id)
    row = db.exec(
        select(KnowledgeConcept).where(
            KnowledgeConcept.tenant_id == tenant_id,
            KnowledgeConcept.knowledge_base_id == knowledge_base_id,
            KnowledgeConcept.knowledge_base_version_id == knowledge_base_version_id,
            KnowledgeConcept.concept_id == normalized,
            KnowledgeConcept.status != "deleted",
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="OKF concept not found")
    return row


def _document_id_for_version(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    knowledge_base_version_id: str,
    document_id: str | None,
) -> str | None:
    if not document_id:
        return None
    current = db.get(KnowledgeDocument, document_id)
    if (
        current
        and current.tenant_id == tenant_id
        and current.knowledge_base_id == knowledge_base_id
        and current.knowledge_base_version_id == knowledge_base_version_id
    ):
        return current.id
    if not current or current.tenant_id != tenant_id or current.knowledge_base_id != knowledge_base_id:
        return document_id
    cloned = db.exec(
        select(KnowledgeDocument)
        .where(
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeDocument.knowledge_base_id == knowledge_base_id,
            KnowledgeDocument.knowledge_base_version_id == knowledge_base_version_id,
            KnowledgeDocument.filename == current.filename,
            KnowledgeDocument.file_type == current.file_type,
        )
        .order_by(KnowledgeDocument.created_at.asc())
    ).first()
    return cloned.id if cloned else document_id


def _management_knowledge_base_versions(
    db: Session,
    tenant_id: str,
    agent_id: str | None,
) -> dict[str, KnowledgeBaseVersion]:
    agent = get_agent(db, tenant_id, agent_id)
    if agent and not agent.is_overall:
        branches = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == agent.id,
                AgentKnowledgeBranch.status != "deleted",
            )
        ).all()
        result: dict[str, KnowledgeBaseVersion] = {}
        for branch in branches:
            kb = db.get(KnowledgeBase, branch.knowledge_base_id)
            if kb and kb.tenant_id == tenant_id:
                result[kb.id] = ensure_knowledge_base_version(db, kb, branch.head_version)
        return result
    rows = db.exec(select(KnowledgeBase).where(KnowledgeBase.tenant_id == tenant_id)).all()
    rows = [row for row in rows if is_open_gallery_resource(db, tenant_id, "knowledge_base", row)]
    return {row.id: ensure_knowledge_base_version(db, row) for row in rows}


def _get_knowledge_base(db: Session, tenant_id: str, knowledge_base_id: str) -> KnowledgeBase:
    ensure_tenant(db, tenant_id)
    row = db.get(KnowledgeBase, knowledge_base_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    return row


def _knowledge_base_stats(
    db: Session,
    tenant_id: str,
    version_ids: list[str] | None = None,
) -> dict[str, dict[str, int]]:
    stats: dict[str, dict[str, int]] = {}
    doc_stmt = select(KnowledgeDocument.knowledge_base_id, func.count(KnowledgeDocument.id)).where(
        KnowledgeDocument.tenant_id == tenant_id
    )
    bucket_stmt = select(KnowledgeBucket.knowledge_base_id, func.count(KnowledgeBucket.id)).where(
        KnowledgeBucket.tenant_id == tenant_id
    )
    chunk_stmt = select(KnowledgeChunk.knowledge_base_id, func.count(KnowledgeChunk.id)).where(
        KnowledgeChunk.tenant_id == tenant_id
    )
    if version_ids is not None:
        doc_stmt = doc_stmt.where(KnowledgeDocument.knowledge_base_version_id.in_(version_ids))
        bucket_stmt = bucket_stmt.where(KnowledgeBucket.knowledge_base_version_id.in_(version_ids))
        chunk_stmt = chunk_stmt.where(KnowledgeChunk.knowledge_base_version_id.in_(version_ids))
    for knowledge_base_id, count in db.exec(doc_stmt.group_by(KnowledgeDocument.knowledge_base_id)).all():
        stats.setdefault(knowledge_base_id, {})["document_count"] = int(count or 0)
    for knowledge_base_id, count in db.exec(bucket_stmt.group_by(KnowledgeBucket.knowledge_base_id)).all():
        stats.setdefault(knowledge_base_id, {})["bucket_count"] = int(count or 0)
    for knowledge_base_id, count in db.exec(chunk_stmt.group_by(KnowledgeChunk.knowledge_base_id)).all():
        stats.setdefault(knowledge_base_id, {})["chunk_count"] = int(count or 0)
    return stats


def _knowledge_branch_meta(db: Session, tenant_id: str, agent_id: str | None) -> dict[str, dict[str, str]]:
    agent = get_agent(db, tenant_id, agent_id)
    if not agent or agent.is_overall:
        return {}
    rows = db.exec(
        select(AgentKnowledgeBranch).where(
            AgentKnowledgeBranch.tenant_id == tenant_id,
            AgentKnowledgeBranch.agent_id == agent.id,
            AgentKnowledgeBranch.status != "deleted",
        )
    ).all()
    return {
        row.knowledge_base_id: {
            "base_version": row.base_version,
            "head_version": row.head_version,
            "sync_state": row.sync_state,
            "status": row.status,
        }
        for row in rows
    }
