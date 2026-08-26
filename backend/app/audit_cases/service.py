from __future__ import annotations

import hashlib
from typing import Any

from sqlmodel import Session, delete, select, update

from app.audit_cases.chunking import chunk_text
from app.audit_cases.schema import (
    AuditCaseAccessDenied,
    AuditCaseCreate,
    AuditCaseNotFound,
    AuditCaseReadOnly,
    AuditMaterialProcessingError,
)
from app.audit_cases.storage import delete_case_storage, read_case_blob, write_case_blob
from app.db.models import (
    AuditCase,
    AuditCaseEvent,
    AuditCaseMaterial,
    AuditCaseMaterialChunk,
    ChatSession,
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
