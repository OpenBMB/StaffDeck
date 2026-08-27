from __future__ import annotations

import hashlib
from typing import Any

from sqlmodel import Session, delete, select, update

from app.audit_cases.chunking import chunk_text
from app.audit_cases.schema import (
    AuditCaseAccessDenied,
    AuditCaseCreate,
    AuditCaseEventRead,
    AuditCaseKnowledgeVersionOption,
    AuditCaseManagementOptions,
    AuditCaseManagementPage,
    AuditCaseManagementRead,
    AuditCaseMemberUpdate,
    AuditCaseNotFound,
    AuditCaseReadOnly,
    AuditCaseUpdate,
    AuditMaterialProcessingError,
    audit_case_read,
)
from app.audit_cases.storage import delete_case_storage, read_case_blob, write_case_blob
from app.db.models import (
    AuditCase,
    AuditCaseEvent,
    AuditCaseMaterial,
    AuditCaseMaterialChunk,
    ChatSession,
    KnowledgeBaseVersion,
    User,
    new_id,
    utc_now,
)
from app.knowledge.parser import KnowledgeParseError, extract_text

_CASE_EVENT_METADATA_KEYS = {
    "filename",
    "sha256",
    "version",
    "status",
    "error_code",
    "knowledge_base_version_ids",
    "report_version_id",
    "count",
}

SUPPORTED_AUDIT_MATERIAL_EXTENSIONS = (
    ".pdf",
    ".docx",
    ".txt",
    ".md",
    ".markdown",
    ".html",
    ".htm",
)
MAX_AUDIT_MATERIAL_BYTES = 50 * 1024 * 1024


def record_case_event(
    db: Session,
    *,
    case: AuditCase,
    actor_user_id: str,
    event_type: str,
    resource_type: str,
    resource_id: str,
    metadata: dict[str, Any],
) -> None:
    if set(metadata) - _CASE_EVENT_METADATA_KEYS:
        raise ValueError("unsafe audit event metadata")
    db.add(
        AuditCaseEvent(
            tenant_id=case.tenant_id,
            audit_case_id=case.id,
            actor_user_id=actor_user_id,
            event_type=event_type,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata_json=dict(metadata),
        )
    )


class AuditCaseService:
    def __init__(self, db: Session):
        self.db = db

    def can_access(self, case: AuditCase, user: User) -> bool:
        return (
            case.tenant_id == user.tenant_id
            and (
                user.role == "admin"
                or case.owner_user_id == user.id
                or user.id in set(case.member_user_ids_json or [])
            )
        )

    def get_case_for_user(self, tenant_id: str, case_id: str, user: User) -> AuditCase:
        row = self.db.get(AuditCase, case_id)
        if row is None or row.tenant_id != tenant_id or not self.can_access(row, user):
            raise AuditCaseNotFound(case_id)
        return row

    def create_case(self, owner: User, request: AuditCaseCreate) -> AuditCase:
        if request.tenant_id != owner.tenant_id:
            raise AuditCaseAccessDenied("tenant mismatch")
        requested_members = set(request.member_user_ids)
        if requested_members:
            member_count = len(
                self.db.exec(
                    select(User.id).where(
                        User.tenant_id == owner.tenant_id,
                        User.id.in_(requested_members),
                    )
                ).all()
            )
            if member_count != len(requested_members):
                raise AuditCaseAccessDenied("audit case member is outside the tenant")
        case = AuditCase(
            tenant_id=owner.tenant_id,
            owner_user_id=owner.id,
            member_user_ids_json=sorted(requested_members),
            organization_name=request.organization_name,
            report_type=request.report_type,
            management_systems_json=list(request.management_systems),
            knowledge_base_version_ids_json=list(request.knowledge_base_version_ids),
        )
        self.db.add(case)
        record_case_event(
            self.db,
            case=case,
            actor_user_id=owner.id,
            event_type="audit_case.created",
            resource_type="audit_case",
            resource_id=case.id,
            metadata={
                "knowledge_base_version_ids": list(request.knowledge_base_version_ids),
            },
        )
        self.db.commit()
        self.db.refresh(case)
        return case

    def _assert_admin(self, actor: User) -> None:
        if actor.role != "admin":
            raise AuditCaseAccessDenied("tenant administrator required")

    def update_case(
        self,
        case: AuditCase,
        actor: User,
        request: AuditCaseUpdate,
    ) -> AuditCase:
        self._assert_admin(actor)
        case_id = case.id
        case = self.db.get(AuditCase, case_id)
        if case is None or case.tenant_id != actor.tenant_id:
            raise AuditCaseNotFound(case_id)
        self._assert_writable(case)

        if request.organization_name is not None:
            organization_name = request.organization_name.strip()
            if not organization_name:
                raise AuditCaseAccessDenied("organization name is required")
            case.organization_name = organization_name
        if request.report_type is not None:
            report_type = request.report_type.strip()
            if not report_type:
                raise AuditCaseAccessDenied("report type is required")
            case.report_type = report_type
        if request.management_systems is not None:
            case.management_systems_json = list(dict.fromkeys(
                value.strip()
                for value in request.management_systems
                if value.strip()
            ))
        if request.knowledge_base_version_ids is not None:
            version_ids = list(dict.fromkeys(request.knowledge_base_version_ids))
            if version_ids:
                versions = self.db.exec(
                    select(KnowledgeBaseVersion).where(
                        KnowledgeBaseVersion.tenant_id == case.tenant_id,
                        KnowledgeBaseVersion.id.in_(version_ids),
                        KnowledgeBaseVersion.status == "active",
                    )
                ).all()
                if {version.id for version in versions} != set(version_ids):
                    raise AuditCaseAccessDenied("invalid knowledge base version")
            case.knowledge_base_version_ids_json = version_ids

        case.updated_at = utc_now()
        self.db.add(case)
        record_case_event(
            self.db,
            case=case,
            actor_user_id=actor.id,
            event_type="audit_case.updated",
            resource_type="audit_case",
            resource_id=case.id,
            metadata={"count": 1},
        )
        self.db.commit()
        self.db.refresh(case)
        return case

    def replace_members(
        self,
        case: AuditCase,
        actor: User,
        request: AuditCaseMemberUpdate,
    ) -> AuditCase:
        self._assert_admin(actor)
        case_id = case.id
        case = self.db.get(AuditCase, case_id)
        if case is None or case.tenant_id != actor.tenant_id:
            raise AuditCaseNotFound(case_id)
        self._assert_writable(case)

        requested_ids = sorted(set(request.member_user_ids))
        if requested_ids:
            members = self.db.exec(
                select(User).where(
                    User.tenant_id == case.tenant_id,
                    User.id.in_(requested_ids),
                    User.source == "web",
                )
            ).all()
            if {member.id for member in members} != set(requested_ids):
                raise AuditCaseAccessDenied("internal project member required")

        case.member_user_ids_json = requested_ids
        case.updated_at = utc_now()
        self.db.add(case)
        record_case_event(
            self.db,
            case=case,
            actor_user_id=actor.id,
            event_type="audit_case.members_replaced",
            resource_type="audit_case",
            resource_id=case.id,
            metadata={"count": len(requested_ids)},
        )
        self.db.commit()
        self.db.refresh(case)
        return case

    def list_management_cases(
        self,
        actor: User,
        *,
        tenant_id: str,
        query: str = "",
        status: str | None = None,
        management_system: str | None = None,
        report_type: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> AuditCaseManagementPage:
        self._assert_admin(actor)
        if actor.tenant_id != tenant_id:
            raise AuditCaseAccessDenied("tenant mismatch")

        rows = self.db.exec(
            select(AuditCase)
            .where(AuditCase.tenant_id == tenant_id)
            .order_by(AuditCase.updated_at.desc())
        ).all()
        normalized_query = query.strip().casefold()
        normalized_report_type = report_type.strip() if report_type else ""
        normalized_system = management_system.strip() if management_system else ""
        filtered = [
            row
            for row in rows
            if (
                not normalized_query
                or normalized_query in row.organization_name.casefold()
            )
            and (
                not status
                or status == "all"
                or (status == "active" and row.status != "archived")
                or (status == "archived" and row.status == "archived")
            )
            and (
                not normalized_system
                or normalized_system in (row.management_systems_json or [])
            )
            and (
                not normalized_report_type
                or row.report_type == normalized_report_type
            )
        ]
        total = len(filtered)
        page_rows = filtered[max(offset, 0): max(offset, 0) + max(limit, 1)]
        case_ids = [row.id for row in page_rows]

        materials = []
        chunks = []
        if case_ids:
            materials = self.db.exec(
                select(AuditCaseMaterial).where(
                    AuditCaseMaterial.tenant_id == tenant_id,
                    AuditCaseMaterial.audit_case_id.in_(case_ids),
                    AuditCaseMaterial.is_current,
                )
            ).all()
            current_material_ids = [material.id for material in materials]
            chunks = (
                self.db.exec(
                    select(AuditCaseMaterialChunk).where(
                        AuditCaseMaterialChunk.tenant_id == tenant_id,
                        AuditCaseMaterialChunk.material_id.in_(current_material_ids),
                    )
                ).all()
                if current_material_ids
                else []
            )
        material_by_case: dict[str, list[AuditCaseMaterial]] = {}
        for material in materials:
            material_by_case.setdefault(material.audit_case_id, []).append(material)
        chunks_by_case: dict[str, list[AuditCaseMaterialChunk]] = {}
        for chunk in chunks:
            chunks_by_case.setdefault(chunk.audit_case_id, []).append(chunk)

        items: list[AuditCaseManagementRead] = []
        for row in page_rows:
            current_materials = material_by_case.get(row.id, [])
            ready = [
                material
                for material in current_materials
                if material.extraction_status == "succeeded"
                and material.processing_status == "succeeded"
            ]
            failed = [
                material
                for material in current_materials
                if material.extraction_status == "failed"
                or material.processing_status == "failed"
            ]
            current_chunks = chunks_by_case.get(row.id, [])
            successful_chunks = [
                chunk for chunk in current_chunks if chunk.processing_status == "succeeded"
            ]
            base = audit_case_read(row).model_dump()
            items.append(
                AuditCaseManagementRead(
                    **base,
                    material_total=len(current_materials),
                    material_ready=len(ready),
                    material_failed=len(failed),
                    file_coverage=(len(ready) / len(current_materials)) if current_materials else 0.0,
                    chunk_coverage=(len(successful_chunks) / len(current_chunks)) if current_chunks else 0.0,
                )
            )
        return AuditCaseManagementPage(items=items, total=total)

    def list_management_options(self, actor: User, *, tenant_id: str) -> AuditCaseManagementOptions:
        self._assert_admin(actor)
        if actor.tenant_id != tenant_id:
            raise AuditCaseAccessDenied("tenant mismatch")
        versions = self.db.exec(
            select(KnowledgeBaseVersion)
            .where(
                KnowledgeBaseVersion.tenant_id == tenant_id,
                KnowledgeBaseVersion.status == "active",
            )
            .order_by(KnowledgeBaseVersion.name, KnowledgeBaseVersion.version)
        ).all()
        return AuditCaseManagementOptions(
            knowledge_versions=[
                AuditCaseKnowledgeVersionOption(
                    id=version.id,
                    knowledge_base_id=version.knowledge_base_id,
                    name=version.name,
                    version=version.version,
                    description=version.description,
                    status=version.status,
                )
                for version in versions
            ],
            supported_extensions=list(SUPPORTED_AUDIT_MATERIAL_EXTENSIONS),
            max_material_bytes=MAX_AUDIT_MATERIAL_BYTES,
        )

    def list_events(self, case: AuditCase, actor: User) -> list[AuditCaseEventRead]:
        case = self.get_case_for_user(case.tenant_id, case.id, actor)
        rows = self.db.exec(
            select(AuditCaseEvent)
            .where(
                AuditCaseEvent.tenant_id == case.tenant_id,
                AuditCaseEvent.audit_case_id == case.id,
            )
            .order_by(AuditCaseEvent.created_at.desc())
        ).all()
        return [
            AuditCaseEventRead(
                id=row.id,
                audit_case_id=row.audit_case_id,
                actor_user_id=row.actor_user_id,
                event_type=row.event_type,
                resource_type=row.resource_type,
                resource_id=row.resource_id,
                metadata=dict(row.metadata_json or {}),
                created_at=row.created_at,
            )
            for row in rows
        ]

    def _assert_writable(self, case: AuditCase) -> None:
        if case.status == "archived":
            raise AuditCaseReadOnly("AUDIT_CASE_READ_ONLY")

    def add_material(
        self,
        case: AuditCase,
        actor: User,
        material_type: str,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> AuditCaseMaterial:
        case = self.get_case_for_user(case.tenant_id, case.id, actor)
        self._assert_writable(case)
        sha256 = hashlib.sha256(data).hexdigest()
        duplicate = self.db.exec(
            select(AuditCaseMaterial).where(
                AuditCaseMaterial.tenant_id == case.tenant_id,
                AuditCaseMaterial.audit_case_id == case.id,
                AuditCaseMaterial.sha256 == sha256,
            )
        ).first()
        if duplicate is not None:
            return duplicate

        same_name = self.db.exec(
            select(AuditCaseMaterial).where(
                AuditCaseMaterial.tenant_id == case.tenant_id,
                AuditCaseMaterial.audit_case_id == case.id,
                AuditCaseMaterial.filename == filename,
            )
        ).all()
        version = max((item.version for item in same_name), default=0) + 1
        current_same_name = [item for item in same_name if item.is_current]
        for item in current_same_name:
            item.is_current = False
            item.updated_at = utc_now()
            self.db.add(item)

        material = AuditCaseMaterial(
            tenant_id=case.tenant_id,
            audit_case_id=case.id,
            attachment_id=new_id("attachment"),
            material_type=material_type,
            filename=filename,
            content_type=content_type,
            sha256=sha256,
            size=len(data),
            storage_key="",
            version=version,
            supersedes_material_id=current_same_name[-1].id if current_same_name else None,
        )
        self.db.add(material)
        self.db.flush()
        material.storage_key = write_case_blob(
            tenant_id=case.tenant_id,
            audit_case_id=case.id,
            material_id=material.id,
            name="raw",
            data=data,
        )
        record_case_event(
            self.db,
            case=case,
            actor_user_id=actor.id,
            event_type="audit_case.material_added",
            resource_type="audit_case_material",
            resource_id=material.id,
            metadata={
                "filename": material.filename,
                "sha256": material.sha256,
                "version": material.version,
                "status": material.processing_status,
            },
        )
        self.db.commit()
        self.db.refresh(material)
        return material

    def get_material(self, case_id: str, material_id: str, user: User) -> AuditCaseMaterial:
        case = self.get_case_for_user(user.tenant_id, case_id, user)
        row = self.db.exec(
            select(AuditCaseMaterial).where(
                AuditCaseMaterial.tenant_id == case.tenant_id,
                AuditCaseMaterial.audit_case_id == case.id,
                AuditCaseMaterial.id == material_id,
            )
        ).first()
        if row is None:
            raise AuditCaseNotFound(material_id)
        return row

    def list_current_materials(self, case: AuditCase) -> list[AuditCaseMaterial]:
        return list(
            self.db.exec(
                select(AuditCaseMaterial)
                .where(
                    AuditCaseMaterial.tenant_id == case.tenant_id,
                    AuditCaseMaterial.audit_case_id == case.id,
                    AuditCaseMaterial.is_current,
                )
                .order_by(AuditCaseMaterial.filename, AuditCaseMaterial.version)
            ).all()
        )

    def process_material(
        self, case: AuditCase, material: AuditCaseMaterial
    ) -> AuditCaseMaterial:
        self._assert_writable(case)
        if material.tenant_id != case.tenant_id or material.audit_case_id != case.id:
            raise AuditCaseNotFound(material.id)
        material.processing_status = "processing"
        material.updated_at = utc_now()
        self.db.add(material)
        self.db.commit()
        self.db.exec(
            delete(AuditCaseMaterialChunk).where(
                AuditCaseMaterialChunk.tenant_id == case.tenant_id,
                AuditCaseMaterialChunk.audit_case_id == case.id,
                AuditCaseMaterialChunk.material_id == material.id,
            )
        )
        try:
            source = read_case_blob(material.storage_key)
            text, _format = extract_text(material.filename, source)
            if not text.strip():
                raise AuditMaterialProcessingError("EMPTY_EXTRACTED_TEXT")
            material.extracted_text_storage_key = write_case_blob(
                tenant_id=case.tenant_id,
                audit_case_id=case.id,
                material_id=material.id,
                name="extracted.txt",
                data=text.encode("utf-8"),
            )
            material.characters = len(text)
            material.extraction_status = "succeeded"
            for span in chunk_text(text):
                self.db.add(
                    AuditCaseMaterialChunk(
                        tenant_id=case.tenant_id,
                        audit_case_id=case.id,
                        material_id=material.id,
                        chunk_index=span.chunk_index,
                        start_char=span.start_char,
                        end_char=span.end_char,
                        content_sha256=span.content_sha256,
                        content=span.content,
                        processing_status="succeeded",
                    )
                )
            material.processing_status = "succeeded"
            material.error_code = None
        except (KnowledgeParseError, AuditMaterialProcessingError, OSError) as exc:
            material.extraction_status = "failed"
            material.processing_status = "failed"
            material.error_code = str(exc) or exc.__class__.__name__
        material.updated_at = utc_now()
        self.db.add(material)
        self.db.commit()
        self.db.refresh(material)
        return material

    def archive_case(self, case: AuditCase, actor: User) -> AuditCase:
        case = self.get_case_for_user(case.tenant_id, case.id, actor)
        if case.status != "archived":
            case.status = "archived"
            case.updated_at = utc_now()
            record_case_event(
                self.db,
                case=case,
                actor_user_id=actor.id,
                event_type="audit_case.archived",
                resource_type="audit_case",
                resource_id=case.id,
                metadata={"status": "archived"},
            )
            self.db.add(case)
            self.db.commit()
            self.db.refresh(case)
        return case

    def delete_case(self, case_id: str, actor: User) -> None:
        case = self.db.get(AuditCase, case_id)
        if case is None or case.tenant_id != actor.tenant_id:
            raise AuditCaseNotFound(case_id)
        if actor.role != "admin":
            raise AuditCaseAccessDenied("tenant administrator required")
        tenant_id = case.tenant_id

        record_case_event(
            self.db,
            case=case,
            actor_user_id=actor.id,
            event_type="audit_case.deleted",
            resource_type="audit_case",
            resource_id=case.id,
            metadata={"count": 1},
        )
        self.db.exec(
            delete(AuditCaseMaterialChunk).where(
                AuditCaseMaterialChunk.tenant_id == tenant_id,
                AuditCaseMaterialChunk.audit_case_id == case.id,
            )
        )
        self.db.exec(
            delete(AuditCaseMaterial).where(
                AuditCaseMaterial.tenant_id == tenant_id,
                AuditCaseMaterial.audit_case_id == case.id,
            )
        )
        self.db.exec(
            update(ChatSession)
            .where(
                ChatSession.tenant_id == tenant_id,
                ChatSession.audit_case_id == case.id,
            )
            .values(audit_case_id=None)
        )
        case_id = case.id
        self.db.delete(case)
        self.db.commit()
        delete_case_storage(tenant_id, case_id)
