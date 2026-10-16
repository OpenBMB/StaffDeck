from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from zipfile import BadZipFile

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from .identity import LocalIdentity, Principal
from .capability_scope import CapabilityScope
from .models import (
    CitationSnapshot,
    KnowledgeBase,
    KnowledgeBaseVersion,
    KnowledgeBinding,
    KnowledgeBranch,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeConcept,
    KnowledgeDiscoverySuggestion,
    KnowledgeDocument,
    KnowledgeIngestJob,
    Space,
    VersionAccess,
    utc_now,
)
from .okf import (
    create_concept_evidence_rows,
    export_okf_bundle,
    lint_okf_concepts,
    normalize_concept_id,
    parse_okf_bundle,
    persist_lint_issues,
    upsert_concepts,
)
from .publication import PublicationConflict, publish_suggestion
from .full_runtime import Runtime
from .schema import KnowledgeSearchRequest
from .service import IngestPayload, KnowledgeDiscoveryConflictError
from .versions import ASSET_MODELS, clone_version
from .contracts import (SpaceCreate, BaseCreate, Upload, Edit, QueryRequest, VersionAction,
                        BindingAction, BindingEdit, BaseEdit, LayerEdit, ConceptEdit)


def serialize(row) -> dict[str, Any]:
    value = row.model_dump(mode="json")
    for key in list(value):
        if key.endswith("_json"):
            value[key.removesuffix("_json")] = value.pop(key)
    return value


def create_app(runtime: Runtime, *, ui_directory: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        runtime.initialize()
        try:
            yield
        finally:
            runtime.close()

    app = FastAPI(title="Knowledge Core", version="0.1.0", lifespan=lifespan)
    app.state.runtime = runtime
    prefix = "/api/km/v1"

    @app.get("/api/km/config")
    def configuration():
        from .capability_catalog import FULL, describe
        return {**runtime.configuration.as_dict(), "api_protocol": "knowledge-core/v1",
                "identity_required": not isinstance(runtime.identity, LocalIdentity),
                "model_mode": "model" if runtime.model_config or runtime.capabilities else "lexical",
                "operations": describe({"identity": ("GET", "/identity"), **{name: operation for name, operation in FULL.items() if name != "publish_discovery" or runtime.publisher is not None}})}

    @app.middleware("http")
    async def maintenance(request, call_next):
        if runtime.read_only and request.url.path.startswith(prefix) and request.method not in {"GET", "HEAD"}:
            if request.url.path != prefix + "/query" and not request.url.path.endswith(("/okf/lint", "/export")):
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "Knowledge writes are frozen for maintenance"}, status_code=503)
        return await call_next(request)

    def principal(authorization: str | None = Header(default=None)) -> Principal:
        try:
            return runtime.identity.authenticate(authorization)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, "Identity provider unavailable") from exc

    def session():
        with runtime.session() as db:
            yield db

    @app.get(prefix + "/identity")
    def identity(actor=Depends(principal)):
        return {"id": actor.id, "permissions": sorted(actor.permissions)}

    def get_row(db, model, resource_id, actor):
        row = db.get(model, resource_id)
        if row is None or row.tenant_id != actor.tenant_id:
            raise HTTPException(404, "Resource not found")
        return row

    def space(db, actor, space_id, action):
        runtime.authorization.authorize(actor, space_id, action)
        row = db.get(Space, space_id)
        if row is None and space_id == "local" and actor.id == "local":
            row = Space(id="local", tenant_id=actor.tenant_id, name="Local")
            if not runtime.read_only:
                db.add(row)
                db.commit()
        if row is None or row.tenant_id != actor.tenant_id or row.status != "active":
            raise HTTPException(404, "Space not found")
        return row

    def binding(db, actor, space_id, kb_id, action="read"):
        space(db, actor, space_id, action)
        get_row(db, KnowledgeBase, kb_id, actor)
        row = db.exec(
            select(KnowledgeBinding).where(
                KnowledgeBinding.tenant_id == actor.tenant_id,
                KnowledgeBinding.space_id == space_id,
                KnowledgeBinding.knowledge_base_id == kb_id,
            )
        ).first()
        if row is None or row.hidden:
            raise HTTPException(404, "Knowledge binding not found")
        branch = db.exec(
            select(KnowledgeBranch).where(
                KnowledgeBranch.tenant_id == actor.tenant_id,
                KnowledgeBranch.space_id == space_id,
                KnowledgeBranch.knowledge_base_id == kb_id,
            )
        ).first()
        if branch is None:
            raise HTTPException(409, "Knowledge branch missing")
        return branch

    def writable_branch(db, branch):
        shared = db.exec(
            select(KnowledgeBranch).where(
                KnowledgeBranch.tenant_id == branch.tenant_id,
                KnowledgeBranch.head_version_id == branch.head_version_id,
                KnowledgeBranch.id != branch.id,
            )
        ).first()
        kb = db.get(KnowledgeBase, branch.knowledge_base_id)
        published = (kb.metadata_json or {}).get("published_version_id")
        if shared is not None or published == branch.head_version_id:
            from uuid import uuid4

            source = db.get(KnowledgeBaseVersion, branch.head_version_id)
            target = clone_version(db, source, "draft-" + uuid4().hex)
            grant_version(db, branch.tenant_id, branch.space_id, branch.knowledge_base_id, target.id)
            branch.head_version_id = target.id
            branch.sync_state = "modified"
            branch.updated_at = utc_now()
            db.add(branch)
            db.commit()
            db.refresh(branch)
        return branch

    def grant_version(db, tenant_id, space_id, kb_id, version_id):
        access_id = f"{space_id}:{version_id}"
        if db.get(VersionAccess, access_id) is None:
            db.add(VersionAccess(
                id=access_id, tenant_id=tenant_id, space_id=space_id,
                knowledge_base_id=kb_id, version_id=version_id,
            ))

    def visible_version(db, actor, space_id, kb_id, version_id):
        branch = binding(db, actor, space_id, kb_id)
        kb = db.get(KnowledgeBase, kb_id)
        if version_id in {
            branch.head_version_id, branch.base_version_id,
            (kb.metadata_json or {}).get("published_version_id"),
        }:
            return
        access = db.get(VersionAccess, f"{space_id}:{version_id}")
        if access is None or access.tenant_id != actor.tenant_id:
            raise HTTPException(404, "Version not visible in this space")

    def asset(db, actor, model, resource_id, space_id, action="read"):
        row = get_row(db, model, resource_id, actor)
        branch = binding(db, actor, space_id, row.knowledge_base_id, action)
        version = get_row(db, KnowledgeBaseVersion, row.knowledge_base_version_id, actor)
        if version.knowledge_base_id != row.knowledge_base_id:
            raise HTTPException(409, "Version ownership mismatch")
        visible_version(db, actor, space_id, row.knowledge_base_id, row.knowledge_base_version_id)
        historical_write = "history:write" in actor.permissions
        if action == "write" and not historical_write and row.knowledge_base_version_id != branch.head_version_id:
            raise HTTPException(409, "Historical versions are immutable")
        kb = db.get(KnowledgeBase, row.knowledge_base_id)
        if action == "write" and not historical_write and (
            (kb.metadata_json or {}).get("published_version_id") == row.knowledge_base_version_id
            or db.exec(
                select(KnowledgeBranch).where(
                    KnowledgeBranch.tenant_id == actor.tenant_id,
                    KnowledgeBranch.head_version_id == row.knowledge_base_version_id,
                    KnowledgeBranch.id != branch.id,
                )
            ).first()
            is not None
        ):
            raise HTTPException(409, "Copy the published or shared version before editing")
        return row

    def check_revision(db, model, row, expected):
        if row.updated_at.isoformat() != expected:
            raise HTTPException(409, "Resource changed; reload before saving")
        changed = db.exec(
            update(model)
            .where(
                model.id == row.id,
                model.updated_at == row.updated_at,
            )
            .values(updated_at=utc_now())
        )
        if changed.rowcount != 1:
            db.rollback()
            raise HTTPException(409, "Resource changed; reload before saving")

    def snapshot_chunks(db, actor, document_id):
        for chunk in db.exec(
            select(KnowledgeChunk).where(
                KnowledgeChunk.tenant_id == actor.tenant_id,
                KnowledgeChunk.document_id == document_id,
            )
        ).all():
            if db.get(CitationSnapshot, chunk.id) is None:
                db.add(
                    CitationSnapshot(
                        id=chunk.id,
                        tenant_id=actor.tenant_id,
                        knowledge_base_id=chunk.knowledge_base_id,
                        knowledge_base_version_id=chunk.knowledge_base_version_id,
                        payload_json=serialize(chunk),
                    )
                )

    def materialize_missing_concepts(db, tenant_id, kb_id, version_id):
        from .okf import build_okf_for_document
        documents = db.exec(select(KnowledgeDocument).where(
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeDocument.knowledge_base_id == kb_id,
            KnowledgeDocument.knowledge_base_version_id == version_id,
            KnowledgeDocument.status == "ready",
        )).all()
        for document in documents:
            if db.exec(select(KnowledgeConcept.id).where(
                KnowledgeConcept.document_id == document.id,
                KnowledgeConcept.knowledge_base_version_id == version_id,
            )).first():
                continue
            buckets = db.exec(select(KnowledgeBucket).where(
                KnowledgeBucket.document_id == document.id,
            ).order_by(KnowledgeBucket.created_at)).all()
            sections = document.metadata_json.get("section_tree", [])
            if sections or buckets:
                upsert_concepts(db, tenant_id, kb_id, version_id,
                                build_okf_for_document(document, sections, buckets))

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready():
        if not runtime.ready():
            raise HTTPException(503, "Database unavailable")
        return {"status": "ready"}

    @app.get(prefix + "/spaces")
    def list_spaces(actor=Depends(principal), db=Depends(session)):
        local = None
        if actor.id == "local":
            local = space(db, actor, "local", "read")
        rows = db.exec(select(Space).where(Space.tenant_id == actor.tenant_id)).all()
        if local is not None and all(row.id != local.id for row in rows):
            rows = [local, *rows]
        result = []
        for row in rows:
            if "*" not in actor.spaces and row.id not in actor.spaces:
                continue
            try:
                runtime.authorization.authorize(actor, row.id, "write")
                can_write = True
            except HTTPException as exc:
                if exc.status_code != 403:
                    raise
                can_write = False
            result.append({**serialize(row), "can_write": can_write})
        return result

    @app.post(prefix + "/spaces", status_code=201)
    def create_space(body: SpaceCreate, actor=Depends(principal), db=Depends(session)):
        runtime.authorization.authorize(actor, "*", "write")
        row = Space(
            **({"id": body.id} if body.id else {}),
            tenant_id=actor.tenant_id,
            name=body.name,
            owner_id=actor.id,
            owner_name=actor.display_name,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return serialize(row)

    @app.get(prefix + "/knowledge-bases")
    def list_bases(space_id="local", actor=Depends(principal), db=Depends(session)):
        space(db, actor, space_id, "read")
        bindings = db.exec(
            select(KnowledgeBinding).where(
                KnowledgeBinding.tenant_id == actor.tenant_id,
                KnowledgeBinding.space_id == space_id,
                KnowledgeBinding.hidden == False,
            )
        ).all()
        return [
            serialize(get_row(db, KnowledgeBase, row.knowledge_base_id, actor)) for row in bindings
        ]

    @app.post(prefix + "/knowledge-bases", status_code=201)
    def create_base(body: BaseCreate, actor=Depends(principal), db=Depends(session)):
        space(db, actor, body.space_id, "write")
        body.name = body.name.strip()
        if not body.name:
            raise HTTPException(400, "Knowledge base name cannot be empty")
        row = KnowledgeBase(
            tenant_id=actor.tenant_id,
            name=body.name,
            description=body.description,
            capability_scope=body.capability_scope,
            metadata_json={**body.metadata, "owner_id": actor.id, "owner_name": actor.display_name},
        )
        db.add(row)
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(409, "Knowledge base name already exists") from exc
        version = KnowledgeBaseVersion(
            id=f"kbver_{row.id}_1_0_0",
            tenant_id=actor.tenant_id,
            knowledge_base_id=row.id,
            name=row.name,
            capability_scope=row.capability_scope,
        )
        db.add(version)
        grant_version(db, actor.tenant_id, body.space_id, row.id, version.id)
        db.add(
            KnowledgeBinding(
                tenant_id=actor.tenant_id,
                space_id=body.space_id,
                knowledge_base_id=row.id,
            )
        )
        db.add(
            KnowledgeBranch(
                tenant_id=actor.tenant_id,
                space_id=body.space_id,
                knowledge_base_id=row.id,
                base_version_id=version.id,
                head_version_id=version.id,
            )
        )
        row.metadata_json = {**row.metadata_json, "published_version_id": version.id}
        db.add(row)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(409, "Knowledge base name already exists") from exc
        db.refresh(row)
        return serialize(row)

    @app.get(prefix + "/knowledge-bases/{kb_id}")
    def get_base(kb_id, space_id="local", actor=Depends(principal), db=Depends(session)):
        branch = binding(db, actor, space_id, kb_id)
        return {**serialize(get_row(db, KnowledgeBase, kb_id, actor)), "branch": serialize(branch)}

    @app.put(prefix + "/knowledge-bases/{kb_id}")
    def edit_base(
        kb_id, body: BaseEdit, space_id="local", actor=Depends(principal), db=Depends(session)
    ):
        binding(db, actor, space_id, kb_id, "write")
        row = get_row(db, KnowledgeBase, kb_id, actor)
        check_revision(db, KnowledgeBase, row, body.expected_updated_at)
        if body.status is not None and body.status not in {"active", "archived"}:
            raise HTTPException(422, "Invalid status")
        for field in body.model_fields_set - {"expected_updated_at"}:
            if field == "metadata":
                existing = row.metadata_json or {}
                row.metadata_json = {**(body.metadata or {}), **{
                    key: existing[key] for key in ("owner_id", "owner_name", "published_version_id")
                    if key in existing
                }}
            else:
                setattr(row, field, getattr(body, field))
        row.updated_at = utc_now()
        db.add(row)
        db.commit()
        db.refresh(row)
        return serialize(row)

    @app.put(prefix + "/knowledge-bases/{kb_id}/branch")
    def edit_branch(kb_id, body: BaseEdit, space_id="local", actor=Depends(principal), db=Depends(session)):
        branch = binding(db, actor, space_id, kb_id, "write")
        version = get_row(db, KnowledgeBaseVersion, branch.head_version_id, actor)
        check_revision(db, KnowledgeBaseVersion, version, body.expected_updated_at)
        db.commit()
        branch = writable_branch(db, branch)
        version = get_row(db, KnowledgeBaseVersion, branch.head_version_id, actor)
        for field in ("name", "description", "capability_scope"):
            if field in body.model_fields_set and getattr(body, field) is not None:
                value = getattr(body, field)
                if field == "name":
                    value = value.strip()
                    if not value:
                        raise HTTPException(400, "Knowledge base name cannot be empty")
                setattr(version, field, value)
        if body.metadata is not None:
            version.metadata_json = {**version.metadata_json, **body.metadata}
        if body.status is not None:
            if body.status not in {"active", "archived"}:
                raise HTTPException(422, "Invalid status")
            branch.status = "active" if body.status == "active" else "inactive"
        version.updated_at, branch.updated_at = utc_now(), utc_now()
        branch.sync_state = "modified"
        db.add(version)
        db.add(branch)
        db.commit()
        db.refresh(version)
        return serialize(version)

    @app.delete(prefix + "/knowledge-bases/{kb_id}")
    def delete_base(
        kb_id,
        space_id="local",
        permanent: bool = False,
        actor=Depends(principal),
        db=Depends(session),
    ):
        binding(db, actor, space_id, kb_id, "write")
        if not permanent:
            row = db.exec(
                select(KnowledgeBinding).where(
                    KnowledgeBinding.tenant_id == actor.tenant_id,
                    KnowledgeBinding.space_id == space_id,
                    KnowledgeBinding.knowledge_base_id == kb_id,
                )
            ).first()
            row.hidden = True
            db.add(row)
        else:
            bindings = db.exec(
                select(KnowledgeBinding).where(
                    KnowledgeBinding.tenant_id == actor.tenant_id,
                    KnowledgeBinding.knowledge_base_id == kb_id,
                )
            ).all()
            for row in bindings:
                space(db, actor, row.space_id, "write")
            active = db.exec(
                select(KnowledgeIngestJob).where(
                    KnowledgeIngestJob.knowledge_base_id == kb_id,
                    KnowledgeIngestJob.status.in_(["queued", "running", "cancel_requested"]),
                )
            ).first()
            if active:
                raise HTTPException(409, "Cancel active tasks before deleting")
            for model in (
                *ASSET_MODELS,
                KnowledgeIngestJob,
                CitationSnapshot,
                KnowledgeBranch,
                KnowledgeBinding,
                KnowledgeBaseVersion,
                VersionAccess,
            ):
                db.exec(
                    delete(model).where(
                        model.tenant_id == actor.tenant_id,
                        model.knowledge_base_id == kb_id,
                    )
                )
            db.delete(get_row(db, KnowledgeBase, kb_id, actor))
        db.commit()
        return {"status": "deleted" if permanent else "hidden"}

    @app.post(prefix + "/knowledge-bases/{kb_id}/bindings")
    def bind_base(
        kb_id,
        body: BindingAction,
        source_space_id="local",
        actor=Depends(principal),
        db=Depends(session),
    ):
        source = binding(db, actor, source_space_id, kb_id)
        space(db, actor, body.space_id, "write")
        row = db.exec(
            select(KnowledgeBinding).where(
                KnowledgeBinding.tenant_id == actor.tenant_id,
                KnowledgeBinding.space_id == body.space_id,
                KnowledgeBinding.knowledge_base_id == kb_id,
            )
        ).first()
        if row is None:
            row = KnowledgeBinding(
                tenant_id=actor.tenant_id,
                space_id=body.space_id,
                knowledge_base_id=kb_id,
            )
            db.add(
                KnowledgeBranch(
                    tenant_id=actor.tenant_id,
                    space_id=body.space_id,
                    knowledge_base_id=kb_id,
                    base_version_id=source.base_version_id,
                    head_version_id=source.head_version_id,
                )
            )
        row.hidden = body.hidden
        row.updated_at = utc_now()
        grant_version(db, actor.tenant_id, body.space_id, kb_id, source.head_version_id)
        grant_version(db, actor.tenant_id, body.space_id, kb_id, source.base_version_id)
        db.add(row)
        db.commit()
        db.refresh(row)
        return serialize(row)

    @app.get(prefix + "/bindings")
    def list_bindings(space_id="local", actor=Depends(principal), db=Depends(session)):
        space(db, actor, space_id, "read")
        rows = db.exec(select(KnowledgeBinding).where(
            KnowledgeBinding.tenant_id == actor.tenant_id,
            KnowledgeBinding.space_id == space_id,
        )).all()
        result = []
        for row in rows:
            branch = db.exec(select(KnowledgeBranch).where(
                KnowledgeBranch.tenant_id == actor.tenant_id,
                KnowledgeBranch.space_id == space_id,
                KnowledgeBranch.knowledge_base_id == row.knowledge_base_id,
            )).first()
            if branch is None:
                raise HTTPException(409, "Knowledge branch missing")
            base = get_row(db, KnowledgeBase, row.knowledge_base_id, actor)
            head = get_row(db, KnowledgeBaseVersion, branch.head_version_id, actor)
            result.append({**serialize(row), "branch": serialize(branch),
                           "metadata": {**row.metadata_json, "name": head.name if branch.sync_state == "modified" else base.name}})
        return result

    @app.put(prefix + "/knowledge-bases/{kb_id}/binding")
    def edit_binding(kb_id, body: BindingEdit, space_id="local",
                     actor=Depends(principal), db=Depends(session)):
        space(db, actor, space_id, "write")
        get_row(db, KnowledgeBase, kb_id, actor)
        row = db.exec(select(KnowledgeBinding).where(
            KnowledgeBinding.tenant_id == actor.tenant_id,
            KnowledgeBinding.space_id == space_id,
            KnowledgeBinding.knowledge_base_id == kb_id,
        )).first()
        if row is None:
            raise HTTPException(404, "Knowledge binding not found")
        branch = db.exec(select(KnowledgeBranch).where(
            KnowledgeBranch.tenant_id == actor.tenant_id,
            KnowledgeBranch.space_id == space_id,
            KnowledgeBranch.knowledge_base_id == kb_id,
        )).first()
        if branch is None:
            raise HTTPException(409, "Knowledge branch missing")
        if body.status is not None:
            if body.status not in {"active", "inactive", "deleted"}:
                raise HTTPException(422, "Invalid binding status")
            branch.status = body.status
        if body.hidden is not None:
            row.hidden = body.hidden
        if body.metadata is not None:
            row.metadata_json = body.metadata
        row.updated_at = utc_now()
        branch.updated_at = utc_now()
        db.add(row)
        db.add(branch)
        db.commit()
        db.refresh(row)
        db.refresh(branch)
        return {**serialize(row), "branch": serialize(branch)}

    @app.get(prefix + "/knowledge-bases/{kb_id}/versions")
    def versions(kb_id, space_id="local", actor=Depends(principal), db=Depends(session)):
        branch = binding(db, actor, space_id, kb_id)
        kb = get_row(db, KnowledgeBase, kb_id, actor)
        allowed = {branch.head_version_id, branch.base_version_id,
                   (kb.metadata_json or {}).get("published_version_id")}
        allowed.update(row.version_id for row in db.exec(select(VersionAccess).where(
            VersionAccess.tenant_id == actor.tenant_id, VersionAccess.space_id == space_id,
            VersionAccess.knowledge_base_id == kb_id,
        )).all())
        return [
            serialize(row)
            for row in db.exec(
                select(KnowledgeBaseVersion).where(
                    KnowledgeBaseVersion.tenant_id == actor.tenant_id,
                    KnowledgeBaseVersion.knowledge_base_id == kb_id,
                    KnowledgeBaseVersion.id.in_(allowed),
                )
            ).all()
        ]

    @app.post(prefix + "/knowledge-bases/{kb_id}/versions/{operation}")
    def change_version(
        kb_id,
        operation,
        body: VersionAction,
        space_id="local",
        actor=Depends(principal),
        db=Depends(session),
    ):
        branch = binding(db, actor, space_id, kb_id, "write")
        kb = get_row(db, KnowledgeBase, kb_id, actor)
        if operation == "copy":
            source = get_row(
                db, KnowledgeBaseVersion, body.version_id or branch.head_version_id, actor
            )
            if source.knowledge_base_id != kb_id:
                raise HTTPException(409, "Version belongs to another knowledge base")
            visible_version(db, actor, space_id, kb_id, source.id)
            from uuid import uuid4

            if body.empty:
                target = KnowledgeBaseVersion(tenant_id=actor.tenant_id, knowledge_base_id=kb_id,
                    version=body.version_label or "draft-" + uuid4().hex, name=body.name or source.name,
                    description=body.description, capability_scope=source.capability_scope)
                db.add(target)
                db.flush()
            else:
                target = clone_version(db, source, body.version_label or "draft-" + uuid4().hex,
                                       preserve_metadata=body.preserve_metadata)
                if body.name is not None: target.name = body.name
                if "description" in body.model_fields_set: target.description = body.description
                db.add(target)
            branch.head_version_id, branch.sync_state = target.id, "modified"
        elif operation == "publish":
            kb.metadata_json = {**kb.metadata_json, "published_version_id": branch.head_version_id}
            branch.base_version_id = branch.head_version_id
            branch.sync_state = "synced"
            db.add(kb)
        elif operation in {"sync", "rollback"}:
            target_id = (
                (kb.metadata_json or {}).get("published_version_id")
                if operation == "sync"
                else body.version_id
            )
            target = get_row(db, KnowledgeBaseVersion, target_id, actor)
            if target.knowledge_base_id != kb_id:
                raise HTTPException(409, "Version belongs to another knowledge base")
            visible_version(db, actor, space_id, kb_id, target.id)
            branch.head_version_id = target.id
            if operation == "sync": branch.base_version_id = target.id
            branch.sync_state = "synced" if target.id == branch.base_version_id else "modified"
        else:
            raise HTTPException(404, "Version operation not found")
        branch.updated_at = utc_now()
        grant_version(db, actor.tenant_id, space_id, kb_id, branch.head_version_id)
        db.add(branch)
        db.commit()
        db.refresh(branch)
        return serialize(branch)

    @app.post(prefix + "/knowledge-bases/{kb_id}/documents", status_code=202)
    def upload(kb_id, body: Upload, actor=Depends(principal), db=Depends(session)):
        branch = binding(db, actor, body.space_id, kb_id, "write")
        try:
            base64.b64decode(body.content_base64, validate=True)
        except ValueError as exc:
            raise HTTPException(422, "Invalid base64") from exc
        branch = writable_branch(db, branch)
        job = runtime.service(db).create_ingest_job(
            IngestPayload(
                tenant_id=actor.tenant_id,
                knowledge_base_id=kb_id,
                knowledge_base_version_id=branch.head_version_id,
                filename=body.filename,
                content_base64=body.content_base64,
                title=body.title,
                metadata={**body.metadata, "capability_scope": body.capability_scope,
                          "km_model_id": body.model_id,
                          "creator_id": actor.id, "creator_name": body.metadata.get("creator_name") or actor.display_name},
            )
        )
        result = serialize(job)
        runtime.enqueue(job.id, actor, body.space_id)
        return result

    @app.get(prefix + "/knowledge-bases/{kb_id}/documents")
    def documents(kb_id, space_id="local", actor=Depends(principal), db=Depends(session)):
        branch = binding(db, actor, space_id, kb_id)
        return [
            serialize(row)
            for row in db.exec(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.tenant_id == actor.tenant_id,
                    KnowledgeDocument.knowledge_base_id == kb_id,
                    KnowledgeDocument.knowledge_base_version_id == branch.head_version_id,
                )
            ).all()
        ]

    @app.get(prefix + "/jobs/{job_id}")
    def job(job_id, space_id="local", actor=Depends(principal), db=Depends(session)):
        row = get_row(db, KnowledgeIngestJob, job_id, actor)
        binding(db, actor, space_id, row.knowledge_base_id)
        visible_version(db, actor, space_id, row.knowledge_base_id, row.knowledge_base_version_id)
        return serialize(row)

    @app.post(prefix + "/jobs/{job_id}/cancel")
    def cancel(job_id, space_id="local", actor=Depends(principal), db=Depends(session)):
        row = get_row(db, KnowledgeIngestJob, job_id, actor)
        binding(db, actor, space_id, row.knowledge_base_id, "write")
        return serialize(runtime.service(db).cancel_ingest_job(job_id, actor.tenant_id))

    @app.get(prefix + "/documents/{document_id}")
    def document(document_id, space_id="local", actor=Depends(principal), db=Depends(session)):
        row = asset(db, actor, KnowledgeDocument, document_id, space_id)
        return serialize(row)

    @app.put(prefix + "/documents/{document_id}")
    def edit(
        document_id, body: Edit, space_id="local", actor=Depends(principal), db=Depends(session)
    ):
        row = asset(db, actor, KnowledgeDocument, document_id, space_id, "write")
        check_revision(db, KnowledgeDocument, row, body.expected_updated_at)
        if body.content_md is not None:
            snapshot_chunks(db, actor, document_id)
        db.flush()
        if body.content_md is not None:
            row = runtime.service(db, actor.tenant_id, space_id).replace_document_content(row, body.content_md, title=body.title)
        elif body.title is not None or body.status is not None or body.metadata is not None:
            if body.title is not None: row.title = body.title
            metadata = dict(row.metadata_json or {})
            metadata["document_card"] = {**metadata.get("document_card", {}), "title": row.title}
            row.metadata_json = metadata
            buckets = db.exec(select(KnowledgeBucket).where(KnowledgeBucket.document_id == row.id)).all()
            db.exec(delete(KnowledgeConcept).where(
                KnowledgeConcept.document_id == row.id,
                KnowledgeConcept.concept_type.in_(["Source Document", "Source Section"]),
            ))
            from .okf import build_okf_for_document
            upsert_concepts(db, row.tenant_id, row.knowledge_base_id, row.knowledge_base_version_id,
                            build_okf_for_document(row, metadata.get("section_tree", []), buckets))
        if body.status is not None:
            row.status = body.status
        if body.metadata is not None:
            preserved = {key: value for key, value in row.metadata_json.items()
                         if key in {"raw_text", "creator_id", "creator_name"}}
            row.metadata_json = {**body.metadata, **preserved}
        row.updated_at = utc_now()
        db.add(row)
        db.commit()
        db.refresh(row)
        return serialize(row)

    @app.delete(prefix + "/documents/{document_id}")
    def archive_document(
        document_id, space_id="local", actor=Depends(principal), db=Depends(session)
    ):
        row = asset(db, actor, KnowledgeDocument, document_id, space_id, "write")
        row.status, row.updated_at = "archived", utc_now()
        db.add(row)
        db.commit()
        return {"status": "archived"}

    @app.get(prefix + "/documents/{document_id}/{layer}")
    def layers(document_id, layer, space_id="local", actor=Depends(principal), db=Depends(session)):
        asset(db, actor, KnowledgeDocument, document_id, space_id)
        model = {
            "buckets": KnowledgeBucket,
            "chunks": KnowledgeChunk,
            "concepts": KnowledgeConcept,
            "suggestions": KnowledgeDiscoverySuggestion,
        }.get(layer)
        if model is None:
            raise HTTPException(404, "Layer not found")
        return [
            serialize(row)
            for row in db.exec(
                select(model).where(
                    model.tenant_id == actor.tenant_id,
                    model.document_id == document_id,
                )
            ).all()
        ]

    @app.post(prefix + "/query")
    def query(body: QueryRequest, actor=Depends(principal), db=Depends(session)):
        space(db, actor, body.space_id, "read")
        bindings = db.exec(
            select(KnowledgeBinding).where(
                KnowledgeBinding.tenant_id == actor.tenant_id,
                KnowledgeBinding.space_id == body.space_id,
                KnowledgeBinding.hidden == False,
            )
        ).all()
        visible = {row.knowledge_base_id for row in bindings}
        requested = set(body.knowledge_base_ids) if body.knowledge_base_ids else visible
        if not requested <= visible:
            raise HTTPException(403, "Knowledge base outside space")
        branches = [binding(db, actor, body.space_id, kb_id) for kb_id in requested]
        active = {branch.knowledge_base_id for branch in branches if branch.status == "active"
                  and db.get(KnowledgeBase, branch.knowledge_base_id).status == "active"}
        if body.knowledge_base_ids and active != requested:
            raise HTTPException(409, "Knowledge binding is inactive")
        requested = active
        branches = [branch for branch in branches if branch.knowledge_base_id in active]
        if body.version_ids:
            branches = [branch for branch in branches if branch.head_version_id in body.version_ids]
            requested = {branch.knowledge_base_id for branch in branches}
        request = KnowledgeSearchRequest(
            **body.model_dump(exclude={"space_id", "knowledge_base_ids", "version_ids", "query", "max_chunks", "max_buckets", "use_model", "model_id"}),
            tenant_id=actor.tenant_id,
            space_id=body.space_id,
            query=body.query,
            knowledge_base_ids=list(requested),
            knowledge_base_version_ids=[branch.head_version_id for branch in branches
                if not body.version_ids or branch.head_version_id in body.version_ids],
            max_chunks=body.max_chunks,
            max_buckets=body.max_buckets,
        )
        service = runtime.service(db, actor.tenant_id, body.space_id, body.model_id)
        return (
            service
            .search(
                request,
                service.model_config if body.use_model else None,
            )
            .model_dump(mode="json")
        )

    @app.get(prefix + "/citations/{chunk_id}")
    def citation(chunk_id, space_id="local", actor=Depends(principal), db=Depends(session)):
        snapshot = db.get(CitationSnapshot, chunk_id)
        if snapshot and snapshot.tenant_id == actor.tenant_id:
            binding(db, actor, space_id, snapshot.knowledge_base_id)
            visible_version(db, actor, space_id, snapshot.knowledge_base_id,
                            snapshot.knowledge_base_version_id)
            return snapshot.payload_json
        return serialize(asset(db, actor, KnowledgeChunk, chunk_id, space_id))

    @app.get(prefix + "/jobs")
    def list_jobs(space_id="local", status: str | None = None, limit: int = 20,
                  actor=Depends(principal), db=Depends(session)):
        space(db, actor, space_id, "read")
        branches = db.exec(select(KnowledgeBranch).where(
            KnowledgeBranch.tenant_id == actor.tenant_id, KnowledgeBranch.space_id == space_id,
        )).all()
        visible_ids = []
        for branch in branches:
            try:
                binding(db, actor, space_id, branch.knowledge_base_id)
                visible_ids.append(branch.head_version_id)
            except HTTPException:
                continue
        stmt = select(KnowledgeIngestJob).where(
            KnowledgeIngestJob.tenant_id == actor.tenant_id,
            KnowledgeIngestJob.knowledge_base_version_id.in_(visible_ids),
        )
        if status:
            stmt = stmt.where(KnowledgeIngestJob.status == status)
        return [serialize(row) for row in db.exec(
            stmt.order_by(KnowledgeIngestJob.created_at.desc()).limit(min(max(limit, 1), 100))
        ).all()]

    @app.get(prefix + "/knowledge-bases/{kb_id}/okf")
    def export_okf(kb_id, space_id="local", actor=Depends(principal), db=Depends(session)):
        branch = binding(db, actor, space_id, kb_id)
        kb = get_row(db, KnowledgeBase, kb_id, actor)
        concepts = db.exec(
            select(KnowledgeConcept).where(
                KnowledgeConcept.tenant_id == actor.tenant_id,
                KnowledgeConcept.knowledge_base_version_id == branch.head_version_id,
            )
        ).all()
        return Response(
            export_okf_bundle(kb, branch.head_version_id, concepts),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="knowledge.okf.zip"'},
        )

    @app.post(prefix + "/knowledge-bases/{kb_id}/okf")
    def import_okf(kb_id, body: Upload, actor=Depends(principal), db=Depends(session)):
        branch = binding(db, actor, body.space_id, kb_id, "write")
        try:
            parsed = parse_okf_bundle(
                body.filename, base64.b64decode(body.content_base64, validate=True)
            )
        except (ValueError, BadZipFile) as exc:
            raise HTTPException(422, str(exc)) from exc
        branch = writable_branch(db, branch)
        document = KnowledgeDocument(
            tenant_id=actor.tenant_id,
            knowledge_base_id=kb_id,
            knowledge_base_version_id=branch.head_version_id,
            filename=body.filename,
            file_type="okf",
            title=body.title or body.filename,
            status="ready",
        )
        db.add(document)
        db.commit()
        db.refresh(document)
        concepts = upsert_concepts(
            db,
            actor.tenant_id,
            kb_id,
            branch.head_version_id,
            [
                {
                    "concept_id": item.concept_id,
                    "content_md": item.content_md,
                    "document_id": document.id,
                }
                for item in parsed
            ],
        )
        create_concept_evidence_rows(
            db, actor.tenant_id, kb_id, branch.head_version_id, document, concepts
        )
        return {"document_id": document.id, "concept_count": len(concepts)}

    @app.get(prefix + "/knowledge-bases/{kb_id}/concepts")
    def list_concepts(kb_id, space_id="local", materialize: bool = False, actor=Depends(principal), db=Depends(session)):
        branch = binding(db, actor, space_id, kb_id)
        if materialize:
            binding(db, actor, space_id, kb_id, "write")
            materialize_missing_concepts(db, actor.tenant_id, kb_id, branch.head_version_id)
        return [
            serialize(row)
            for row in db.exec(
                select(KnowledgeConcept).where(
                    KnowledgeConcept.tenant_id == actor.tenant_id,
                    KnowledgeConcept.knowledge_base_version_id == branch.head_version_id,
                    KnowledgeConcept.status == "active",
                )
            ).all()
        ]

    @app.post(prefix + "/knowledge-bases/{kb_id}/okf/lint")
    def lint(kb_id, space_id="local", materialize: bool = False, actor=Depends(principal), db=Depends(session)):
        branch = binding(db, actor, space_id, kb_id)
        if materialize:
            binding(db, actor, space_id, kb_id, "write")
            materialize_missing_concepts(db, actor.tenant_id, kb_id, branch.head_version_id)
        issues = lint_okf_concepts(db, actor.tenant_id, kb_id, branch.head_version_id)
        persist_lint_issues(db, actor.tenant_id, kb_id, branch.head_version_id, issues)
        return {"issue_count": len(issues), "issues": issues}

    @app.get(prefix + "/knowledge-bases/{kb_id}/concepts/{concept_id:path}")
    def get_okf_concept(kb_id, concept_id, space_id="local", actor=Depends(principal), db=Depends(session)):
        branch = binding(db, actor, space_id, kb_id)
        row = db.exec(select(KnowledgeConcept).where(
            KnowledgeConcept.tenant_id == actor.tenant_id,
            KnowledgeConcept.knowledge_base_version_id == branch.head_version_id,
            KnowledgeConcept.concept_id == normalize_concept_id(concept_id),
            KnowledgeConcept.status == "active",
        )).first()
        if row is None:
            raise HTTPException(404, "Concept not found")
        return serialize(row)

    @app.put(prefix + "/knowledge-bases/{kb_id}/concepts/{concept_id:path}")
    def put_concept(kb_id, concept_id, body: ConceptEdit, space_id="local",
                    actor=Depends(principal), db=Depends(session)):
        if not normalize_concept_id(concept_id) or not body.content_md.strip():
            raise HTTPException(422, "Concept ID and content cannot be empty")
        branch = binding(db, actor, space_id, kb_id, "write")
        before = db.exec(select(KnowledgeConcept).where(
            KnowledgeConcept.tenant_id == actor.tenant_id,
            KnowledgeConcept.knowledge_base_version_id == branch.head_version_id,
            KnowledgeConcept.concept_id == normalize_concept_id(concept_id),
        )).first()
        if before:
            if not body.expected_updated_at:
                raise HTTPException(422, "expected_updated_at is required")
            check_revision(db, KnowledgeConcept, before, body.expected_updated_at)
        elif body.expected_updated_at:
            raise HTTPException(409, "Concept no longer exists at the expected revision")
        branch = writable_branch(db, branch)
        if body.document_id:
            document = asset(db, actor, KnowledgeDocument, body.document_id, space_id, "write")
            if document.knowledge_base_id != kb_id:
                raise HTTPException(404, "Document not found in this knowledge base")
        existing = db.exec(select(KnowledgeConcept).where(
            KnowledgeConcept.tenant_id == actor.tenant_id,
            KnowledgeConcept.knowledge_base_version_id == branch.head_version_id,
            KnowledgeConcept.concept_id == normalize_concept_id(concept_id),
        )).first()
        item = {"concept_id": concept_id, "content_md": body.content_md, "status": body.status}
        if "document_id" in body.model_fields_set:
            item["document_id"] = body.document_id
        elif existing:
            item["document_id"] = existing.document_id
        if existing:
            item["source_refs"] = existing.source_refs_json
        rows = upsert_concepts(db, actor.tenant_id, kb_id, branch.head_version_id, [item])
        return serialize(rows[0])

    @app.get(prefix + "/{layer}/{resource_id}")
    def get_layer(
        layer, resource_id, space_id="local", actor=Depends(principal), db=Depends(session)
    ):
        model = {
            "buckets": KnowledgeBucket,
            "chunks": KnowledgeChunk,
            "concepts": KnowledgeConcept,
            "suggestions": KnowledgeDiscoverySuggestion,
        }.get(layer)
        if model is None:
            raise HTTPException(404, "Layer not found")
        return serialize(asset(db, actor, model, resource_id, space_id))

    @app.get(prefix + "/buckets/{bucket_id}/chunks")
    def bucket_chunks(bucket_id, space_id="local", actor=Depends(principal), db=Depends(session)):
        asset(db, actor, KnowledgeBucket, bucket_id, space_id)
        return [serialize(row) for row in db.exec(select(KnowledgeChunk).where(
            KnowledgeChunk.tenant_id == actor.tenant_id, KnowledgeChunk.bucket_id == bucket_id,
        ).order_by(KnowledgeChunk.chunk_index)).all()]

    @app.put(prefix + "/{layer}/{resource_id}")
    def edit_layer(
        layer,
        resource_id,
        body: LayerEdit,
        space_id="local",
        actor=Depends(principal),
        db=Depends(session),
    ):
        model = {
            "buckets": KnowledgeBucket,
            "chunks": KnowledgeChunk,
            "concepts": KnowledgeConcept,
        }.get(layer)
        if model is None:
            raise HTTPException(404, "Layer not found")
        row = asset(db, actor, model, resource_id, space_id, "write")
        if body.refresh_sources and "history:write" not in actor.permissions:
            raise HTTPException(403, "Historical source refresh requires host permission")
        check_revision(db, model, row, body.expected_updated_at)
        if body.title is not None and hasattr(row, "title"):
            row.title = body.title
        if body.summary is not None and hasattr(row, "summary"):
            row.summary = body.summary
        if body.metadata is not None and hasattr(row, "metadata_json"):
            row.metadata_json = body.metadata
        if body.content_md is not None:
            if model is KnowledgeConcept:
                if not body.content_md.strip():
                    raise HTTPException(422, "Concept content cannot be empty")
                snapshot_chunks(db, actor, row.document_id)
                concepts = upsert_concepts(
                    db,
                    actor.tenant_id,
                    row.knowledge_base_id,
                    row.knowledge_base_version_id,
                    [
                        {
                            "concept_id": row.concept_id,
                            "content_md": body.content_md,
                            "document_id": row.document_id,
                            "source_refs": row.source_refs_json,
                        }
                    ],
                )
                if row.document_id:
                    document = db.get(KnowledgeDocument, row.document_id)
                    all_concepts = db.exec(
                        select(KnowledgeConcept).where(
                            KnowledgeConcept.document_id == row.document_id,
                        )
                    ).all()
                    create_concept_evidence_rows(
                        db,
                        actor.tenant_id,
                        row.knowledge_base_id,
                        row.knowledge_base_version_id,
                        document,
                        all_concepts,
                    )
                db.refresh(concepts[0])
                return serialize(concepts[0])
            if model is KnowledgeChunk:
                row.content = body.content_md
            else:
                row.metadata_json = {**row.metadata_json, "content": body.content_md}
        row.updated_at = utc_now()
        db.add(row)
        db.commit()
        db.refresh(row)
        if body.refresh_sources:
            document = db.get(KnowledgeDocument, row.document_id)
            if model is KnowledgeChunk:
                bucket = db.get(KnowledgeBucket, row.bucket_id)
                chunks = db.exec(select(KnowledgeChunk).where(KnowledgeChunk.bucket_id == row.bucket_id).order_by(KnowledgeChunk.chunk_index)).all()
                content = "\n\n".join(chunk.content for chunk in chunks if chunk.content.strip()).strip()
                bucket.metadata_json = {**bucket.metadata_json, "content": content[:6000], "chunk_count": len(chunks), "representative_chunk_ids": [chunk.id for chunk in chunks[:3]]}
                bucket.token_estimate = max(1, len(content) // 2) if content else bucket.token_estimate
                bucket.updated_at = utc_now()
                db.add(bucket)
                db.commit()
            if document:
                from .okf import build_okf_for_document
                buckets = db.exec(select(KnowledgeBucket).where(KnowledgeBucket.document_id == document.id).order_by(KnowledgeBucket.created_at)).all()
                db.exec(delete(KnowledgeConcept).where(KnowledgeConcept.document_id == document.id, KnowledgeConcept.concept_type.in_(["Source Document", "Source Section"])))
                db.flush()
                upsert_concepts(db, actor.tenant_id, document.knowledge_base_id, document.knowledge_base_version_id, build_okf_for_document(document, document.metadata_json.get("section_tree", []), buckets))
            db.refresh(row)
        return serialize(row)

    @app.get(prefix + "/knowledge-bases/{kb_id}/suggestions")
    def suggestions(kb_id, space_id="local", actor=Depends(principal), db=Depends(session)):
        branch = binding(db, actor, space_id, kb_id)
        return [
            serialize(row)
            for row in db.exec(
                select(KnowledgeDiscoverySuggestion).where(
                    KnowledgeDiscoverySuggestion.tenant_id == actor.tenant_id,
                    KnowledgeDiscoverySuggestion.knowledge_base_version_id
                    == branch.head_version_id,
                )
            ).all()
        ]

    @app.post(prefix + "/suggestions/{suggestion_id}/{operation}")
    def review(
        suggestion_id, operation, space_id="local", actor=Depends(principal), db=Depends(session)
    ):
        row = asset(db, actor, KnowledgeDiscoverySuggestion, suggestion_id, space_id)
        if operation != "export":
            binding(db, actor, space_id, row.knowledge_base_id, "write")
        service = runtime.service(db)
        try:
            if operation == "review":
                return service.confirm_discovery(row)
            if operation == "reject":
                service.reject_discovery(row)
                return serialize(row)
            if operation == "export":
                return serialize(row)
            if operation == "publish":
                if runtime.publisher is None:
                    raise HTTPException(409, "No publication adapter configured")
                return publish_suggestion(db, row, runtime.publisher)
        except (KnowledgeDiscoveryConflictError, PublicationConflict) as exc:
            raise HTTPException(409, str(exc)) from exc
        raise HTTPException(404, "Suggestion operation not found")

    ui = ui_directory or Path(__file__).parent / "ui"
    if ui_directory is not None:
        import json
        try:
            version = json.loads((ui / "version.json").read_text())
            if version.get("protocol") != "knowledge-frame/v1" or not (ui / "index.html").is_file():
                raise ValueError("incompatible UI artifact")
        except (OSError, ValueError) as exc:
            raise ValueError("Invalid Knowledge UI directory") from exc
    if (ui / "index.html").is_file():
        app.mount("/ui/v1", StaticFiles(directory=ui, html=True), name="knowledge-ui-v1")
        app.mount("/", StaticFiles(directory=ui, html=True), name="knowledge-ui")
    return app
