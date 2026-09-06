from __future__ import annotations

import hashlib

from sqlmodel import Session, select

from app.audit_cases.schema import (
    AuditCaseAccessDenied,
    AuditCaseDocumentConflict,
    AuditCaseDocumentCreate,
    AuditCaseDocumentFormatError,
    AuditCaseDocumentNotFound,
    AuditCaseDocumentVersionCreate,
    AuditCaseReadOnly,
)
from app.audit_cases.service import record_case_event
from app.db.models import AuditCase, AuditCaseDocument, AuditCaseDocumentVersion, User, utc_now
from app.project_data.permissions import ensure_project_role

_DOCUMENT_READ_ROLES = {"project_admin", "reviewer", "editor", "viewer"}
_DOCUMENT_WRITE_ROLES = {"project_admin", "reviewer", "editor"}
_SUPPORTED_CONTENT_FORMATS = frozenset({"markdown", "text"})


class AuditCaseDocumentService:
    def __init__(self, db: Session):
        self.db = db

    def _authorize_read(self, case: AuditCase, actor: User) -> None:
        if case.tenant_id != actor.tenant_id:
            raise AuditCaseAccessDenied("PROJECT_ROLE_REQUIRED")
        ensure_project_role(self.db, case, actor, _DOCUMENT_READ_ROLES)

    def _authorize_write(self, case: AuditCase, actor: User) -> None:
        if case.tenant_id != actor.tenant_id:
            raise AuditCaseAccessDenied("PROJECT_ROLE_REQUIRED")
        ensure_project_role(self.db, case, actor, _DOCUMENT_WRITE_ROLES)
        if case.status == "archived":
            raise AuditCaseReadOnly("AUDIT_CASE_READ_ONLY")

    def _document_for_case(self, case: AuditCase, document_id: str) -> AuditCaseDocument:
        document = self.db.exec(
            select(AuditCaseDocument).where(
                AuditCaseDocument.id == document_id,
                AuditCaseDocument.tenant_id == case.tenant_id,
                AuditCaseDocument.audit_case_id == case.id,
            )
        ).first()
        if document is None:
            raise AuditCaseDocumentNotFound(document_id)
        return document

    def _validate_content_format(self, content_format: str) -> None:
        if content_format not in _SUPPORTED_CONTENT_FORMATS:
            raise AuditCaseDocumentFormatError("DOCUMENT_CONTENT_FORMAT_UNSUPPORTED")

    @staticmethod
    def _new_version(
        case: AuditCase,
        document_id: str,
        version_number: int,
        content_format: str,
        content: str,
        change_note: str | None,
        actor: User,
    ) -> AuditCaseDocumentVersion:
        return AuditCaseDocumentVersion(
            tenant_id=case.tenant_id,
            audit_case_id=case.id,
            document_id=document_id,
            version=version_number,
            content_format=content_format,
            content=content,
            content_sha256=hashlib.sha256(content.encode()).hexdigest(),
            characters=len(content),
            change_note=change_note,
            created_by_user_id=actor.id,
        )

    def list_documents(self, case: AuditCase, actor: User) -> list[AuditCaseDocument]:
        self._authorize_read(case, actor)
        return list(
            self.db.exec(
                select(AuditCaseDocument)
                .where(
                    AuditCaseDocument.tenant_id == case.tenant_id,
                    AuditCaseDocument.audit_case_id == case.id,
                )
                .order_by(AuditCaseDocument.zone, AuditCaseDocument.document_key)
            ).all()
        )

    def create_document(
        self,
        case: AuditCase,
        actor: User,
        request: AuditCaseDocumentCreate,
    ) -> AuditCaseDocument:
        self._authorize_write(case, actor)
        self._validate_content_format(request.content_format)
        document = AuditCaseDocument(
            tenant_id=case.tenant_id,
            audit_case_id=case.id,
            document_key=request.document_key,
            title=request.title,
            document_type=request.document_type,
            zone=request.zone,
            source_material_id=request.source_material_id,
            created_by_user_id=actor.id,
            updated_by_user_id=actor.id,
        )
        version = self._new_version(
            case,
            document.id,
            1,
            request.content_format,
            request.content,
            request.change_note,
            actor,
        )
        document.active_version_id = version.id
        self.db.add(document)
        self.db.add(version)
        record_case_event(
            self.db,
            case=case,
            actor_user_id=actor.id,
            event_type="audit_case.document_created",
            resource_type="audit_case_document",
            resource_id=document.id,
            metadata={"version": version.version, "status": document.status},
        )
        self.db.commit()
        self.db.refresh(document)
        return document

    def get_document(
        self,
        case: AuditCase,
        actor: User,
        document_id: str,
    ) -> tuple[AuditCaseDocument, list[AuditCaseDocumentVersion]]:
        self._authorize_read(case, actor)
        document = self._document_for_case(case, document_id)
        versions = list(
            self.db.exec(
                select(AuditCaseDocumentVersion)
                .where(AuditCaseDocumentVersion.document_id == document.id)
                .order_by(AuditCaseDocumentVersion.version)
            ).all()
        )
        return document, versions

    def create_version(
        self,
        case: AuditCase,
        actor: User,
        document_id: str,
        request: AuditCaseDocumentVersionCreate,
    ) -> AuditCaseDocumentVersion:
        self._authorize_write(case, actor)
        self._assert_review_unlocked(case, document_id, actor)
        self._validate_content_format(request.content_format)
        document = self._document_for_case(case, document_id)
        if document.status == "archived":
            raise AuditCaseReadOnly("AUDIT_CASE_DOCUMENT_READ_ONLY")
        current = self.db.exec(
            select(AuditCaseDocumentVersion)
            .where(AuditCaseDocumentVersion.document_id == document.id)
            .order_by(AuditCaseDocumentVersion.version.desc())
        ).first()
        if current is None or request.expected_version != current.version:
            raise AuditCaseDocumentConflict("DOCUMENT_VERSION_CONFLICT")
        version = self._new_version(
            case,
            document.id,
            current.version + 1,
            request.content_format,
            request.content,
            request.change_note,
            actor,
        )
        document.active_version_id = version.id
        document.updated_by_user_id = actor.id
        document.updated_at = utc_now()
        self.db.add(version)
        self.db.add(document)
        record_case_event(
            self.db,
            case=case,
            actor_user_id=actor.id,
            event_type="audit_case.document_version_created",
            resource_type="audit_case_document_version",
            resource_id=version.id,
            metadata={"version": version.version, "status": document.status},
        )
        self.db.commit()
        self.db.refresh(version)
        return version

    def archive_document(
        self,
        case: AuditCase,
        actor: User,
        document_id: str,
        reason: str,
    ) -> AuditCaseDocument:
        self._authorize_write(case, actor)
        self._assert_review_unlocked(case, document_id, actor)
        document = self._document_for_case(case, document_id)
        if document.status == "archived":
            raise AuditCaseReadOnly("AUDIT_CASE_DOCUMENT_READ_ONLY")
        document.status = "archived"
        document.archive_reason = reason
        document.updated_by_user_id = actor.id
        document.updated_at = utc_now()
        self.db.add(document)
        record_case_event(
            self.db,
            case=case,
            actor_user_id=actor.id,
            event_type="audit_case.document_archived",
            resource_type="audit_case_document",
            resource_id=document.id,
            metadata={"status": document.status},
        )
        self.db.commit()
        self.db.refresh(document)
        return document

    def _assert_review_unlocked(self, case: AuditCase, document_id: str, actor: User) -> None:
        from app.audit_cases.workbench import begin_case_write, is_document_submitted

        begin_case_write(self.db, case)
        self._authorize_write(case, actor)
        if is_document_submitted(self.db, case, document_id):
            raise AuditCaseDocumentConflict("DOCUMENT_REVIEW_LOCKED")
