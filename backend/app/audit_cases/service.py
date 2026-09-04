from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, RLock
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, delete, select, update

from app.async_jobs import AsyncJob, enqueue_async_job, get_async_job_queue
from app.audit_cases.chunking import chunk_text, page_refs_for_span
from app.audit_cases.schema import (
    AuditCaseAccessDenied,
    AuditCaseChoiceOption,
    AuditCaseCreate,
    AuditCaseEventRead,
    AuditCaseKnowledgeVersionOption,
    AuditCaseManagementOptions,
    AuditCaseManagementPage,
    AuditCaseManagementRead,
    AuditCaseMaterialTypeOption,
    AuditCaseMemberUpdate,
    AuditCaseNotFound,
    AuditCaseReadOnly,
    AuditCaseUpdate,
    AuditMaterialAlreadyExists,
    AuditMaterialCategoryConflict,
    AuditMaterialFormatError,
    AuditMaterialProcessingError,
    AuditMaterialTooLarge,
    audit_case_read,
)
from app.audit_cases.storage import delete_case_storage, read_case_blob, write_case_blob
from app.db import engine
from app.db.models import (
    AuditCase,
    AuditCaseEvent,
    AuditCaseMaterial,
    AuditCaseMaterialChunk,
    AuditCaseMemberRole,
    ChatSession,
    KnowledgeBaseVersion,
    KnowledgeChunk,
    KnowledgeDocument,
    User,
    new_id,
    utc_now,
)
from app.documents.extraction import (
    DocumentExtractionError,
    DocumentExtractionResult,
    ExtractedPage,
)
from app.knowledge.parser import KnowledgeParseError, extract_document, extract_text
from app.project_data.permissions import resolve_project_role

_DEFAULT_EXTRACT_TEXT = extract_text
_MATERIAL_LOCKS: dict[str, RLock] = {}
_MATERIAL_LOCKS_GUARD = Lock()


def _material_lock(material_id: str) -> RLock:
    with _MATERIAL_LOCKS_GUARD:
        return _MATERIAL_LOCKS.setdefault(material_id, RLock())


@contextmanager
def _hold_material_locks(material_ids: list[str]):
    locks = [_material_lock(material_id) for material_id in sorted(set(material_ids))]
    for lock in locks:
        lock.acquire()
    try:
        yield
    finally:
        for lock in reversed(locks):
            lock.release()

_CASE_EVENT_METADATA_KEYS = {
    "filename",
    "sha256",
    "version",
    "status",
    "error_code",
    "knowledge_base_version_ids",
    "report_version_id",
    "count",
    "field_key",
    "candidate_id",
    "conflict_id",
    "rule_set_version_id",
    "rule_evaluation_id",
    "revision",
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
SUPPORTED_AUDIT_MATERIAL_TYPES = frozenset(
    {
        "audit_notice",
        "permanent_site_list",
        "temporary_site_list",
        "document_review_report",
        "audit_team_preparation_record",
        "opening_closing_attendance",
        "organization_information_confirmation",
        "opening_meeting_record",
        "closing_meeting_record",
        "audit_record_form",
        "nonconformity_report",
        "improvement_suggestion_report",
        "stage_one_audit_report",
        "stage_one_findings_summary",
        "audit_report",
        "surveillance_audit_plan",
        "audit_performance_tracking",
        "other_material",
        # Retain the pre-Phase-1 API values so existing projects and API
        # clients remain readable/operable; the new UI does not expose them
        # as upload categories.
        "audit_plan",
        "audit_record",
    }
)

AUDIT_TYPE_OPTIONS = (
    ("一阶段审核", "一阶段审核", "A"),
    ("认证审核", "认证审核", "B"),
    ("第一次监督审核", "第一次监督审核", "C"),
    ("第二次监督审核", "第二次监督审核", "D"),
    ("再认证审核", "再认证审核", "E"),
    ("补充审核", "补充审核", None),
    ("扩项审核", "扩项审核", None),
    ("确认审核", "确认审核", None),
    ("暂停恢复审核", "暂停恢复审核", None),
    ("标准转换审核", "标准转换审核", None),
    ("证后监督", "证后监督", "H"),
    ("其他审核", "其他审核", None),
)

MANAGEMENT_SYSTEM_OPTIONS = (
    "质量管理体系",
    "环境管理体系",
    "职业健康安全管理体系",
    "信息安全管理体系",
    "信息技术服务管理体系",
    "能源管理体系",
    "食品安全管理体系",
    "其他管理体系",
)

MATERIAL_TYPE_OPTIONS = (
    ("audit_notice", "审核通知单", "审核前准备", "审核通知与任务安排"),
    ("permanent_site_list", "常设场所清单", "审核前准备", "常设场所和审核范围"),
    ("temporary_site_list", "临时场所清单", "审核前准备", "临时场所和审核范围"),
    ("document_review_report", "文审报告", "审核前准备", "文件审核结论与记录"),
    ("audit_team_preparation_record", "审核组准备会记录", "审核前准备", "审核组准备会过程记录"),
    ("opening_closing_attendance", "首末次会议签到表", "会议与信息确认", "首末次会议参会签到"),
    ("organization_information_confirmation", "受审核组织信息确认表", "会议与信息确认", "受审核组织基本信息确认"),
    ("opening_meeting_record", "管理体系审核首次会议记录", "会议与信息确认", "首次会议过程记录"),
    ("closing_meeting_record", "管理体系审核末次会议记录", "会议与信息确认", "末次会议过程记录"),
    ("audit_record_form", "审核记录表", "审核实施与发现", "条款审核记录与证据"),
    ("nonconformity_report", "不符合项报告", "审核实施与发现", "不符合项及证据"),
    ("improvement_suggestion_report", "改进建议报告", "审核实施与发现", "改进建议及依据"),
    ("stage_one_audit_report", "一阶段审核报告", "审核实施与发现", "一阶段审核结论"),
    ("stage_one_findings_summary", "一阶段审核发现问题汇总表", "审核实施与发现", "一阶段问题汇总"),
    ("audit_report", "审核报告", "报告与监督", "既有审核报告或参考报告"),
    ("surveillance_audit_plan", "监督审核策划表", "报告与监督", "监督审核策划信息"),
    ("audit_performance_tracking", "审核过程绩效跟踪分析表", "报告与监督", "审核过程绩效跟踪"),
    ("other_material", "其他资料", "报告与监督", "无法归入上述类别的资料"),
)


def _normalized_knowledge_name(name: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", name.casefold())


def _validate_material_upload(filename: str, data: bytes) -> None:
    suffix = Path(filename).suffix.lower()
    if suffix == ".doc":
        raise AuditMaterialFormatError("UNSUPPORTED_DOCUMENT_FORMAT")
    if suffix not in SUPPORTED_AUDIT_MATERIAL_EXTENSIONS:
        raise AuditMaterialFormatError("UNSUPPORTED_DOCUMENT_FORMAT")
    if len(data) > MAX_AUDIT_MATERIAL_BYTES:
        raise AuditMaterialTooLarge("AUDIT_MATERIAL_TOO_LARGE")


def _extract_audit_material(filename: str, content: bytes) -> DocumentExtractionResult:
    """Extract without losing the legacy monkeypatch seam used by API clients/tests."""

    if extract_text is not _DEFAULT_EXTRACT_TEXT:
        text, file_type = extract_text(filename, content)
        page = ExtractedPage(page_number=1, text=text, char_count=len(text))
        return DocumentExtractionResult(
            text=text,
            pages=[page],
            page_refs=[page.ref],
            source_sha256=hashlib.sha256(content).hexdigest(),
            source_page_count=1,
            method="native",
            engine=file_type or "text",
            engine_version="legacy-compat",
            char_count=len(text),
        )
    try:
        return extract_document(filename, content).extraction
    except KnowledgeParseError as exc:
        message = str(exc)
        code, _, detail = message.partition(":")
        raise DocumentExtractionError(code or "DOCUMENT_EXTRACTION_FAILED", detail.strip() or message) from exc


def _page_ranges(result: DocumentExtractionResult) -> list[tuple[str, int, int]]:
    """Locate each extracted page in the rendered full text for auditable refs."""

    ranges: list[tuple[str, int, int]] = []
    cursor = 0
    for page in result.pages:
        if not page.text:
            continue
        start = result.text.find(page.text, cursor)
        if start < 0:
            start = result.text.find(page.text)
        if start < 0:
            continue
        end = start + len(page.text)
        ranges.append((page.ref, start, end))
        cursor = end
    if not ranges and result.text:
        ranges.append(("page:1", 0, len(result.text)))
    return ranges


def _material_error_code(exc: Exception) -> str:
    if isinstance(exc, DocumentExtractionError):
        return exc.code
    if isinstance(exc, AuditMaterialProcessingError):
        return str(exc) or "AUDIT_MATERIAL_PROCESSING_FAILED"
    if isinstance(exc, KnowledgeParseError):
        return "DOCUMENT_EXTRACTION_FAILED"
    return str(exc) or exc.__class__.__name__


def _is_shared_in_memory_bind(db_bind: Any) -> bool:
    """StaticPool in-memory SQLite cannot safely run a second ORM session."""

    pool = getattr(db_bind, "pool", None)
    url = getattr(db_bind, "url", None)
    return (
        pool is not None
        and pool.__class__.__name__ == "StaticPool"
        and getattr(url, "get_backend_name", lambda: "")() == "sqlite"
    )


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
        return resolve_project_role(self.db, case, user) is not None

    def get_case_for_user(self, tenant_id: str, case_id: str, user: User) -> AuditCase:
        row = self.db.get(AuditCase, case_id)
        if row is None or row.tenant_id != tenant_id or not self.can_access(row, user):
            raise AuditCaseNotFound(case_id)
        return row

    def _sync_member_roles(
        self,
        case: AuditCase,
        *,
        member_user_ids: set[str],
        actor_user_id: str,
    ) -> None:
        existing_roles = self.db.exec(
            select(AuditCaseMemberRole).where(
                AuditCaseMemberRole.tenant_id == case.tenant_id,
                AuditCaseMemberRole.audit_case_id == case.id,
            )
        ).all()
        roles_by_user_id = {role.user_id: role for role in existing_roles}

        if case.owner_user_id not in roles_by_user_id:
            self.db.add(
                AuditCaseMemberRole(
                    tenant_id=case.tenant_id,
                    audit_case_id=case.id,
                    user_id=case.owner_user_id,
                    role="project_admin",
                    created_by_user_id=actor_user_id,
                )
            )

        for user_id in member_user_ids - {case.owner_user_id}:
            if user_id not in roles_by_user_id:
                self.db.add(
                    AuditCaseMemberRole(
                        tenant_id=case.tenant_id,
                        audit_case_id=case.id,
                        user_id=user_id,
                        role="editor",
                        created_by_user_id=actor_user_id,
                    )
                )

        for user_id, role in roles_by_user_id.items():
            if user_id != case.owner_user_id and user_id not in member_user_ids:
                self.db.delete(role)

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
        self._sync_member_roles(
            case,
            member_user_ids=requested_members,
            actor_user_id=owner.id,
        )
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
        self._sync_member_roles(
            case,
            member_user_ids=set(requested_ids),
            actor_user_id=actor.id,
        )
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

        version_ids = [version.id for version in versions]
        document_counts = {
            version_id: count
            for version_id, count in self.db.exec(
                select(
                    KnowledgeDocument.knowledge_base_version_id,
                    func.count(KnowledgeDocument.id),
                )
                .where(
                    KnowledgeDocument.tenant_id == tenant_id,
                    KnowledgeDocument.knowledge_base_version_id.in_(version_ids),
                )
                .group_by(KnowledgeDocument.knowledge_base_version_id)
            ).all()
            if version_id is not None
        } if version_ids else {}
        chunk_counts = {
            version_id: count
            for version_id, count in self.db.exec(
                select(
                    KnowledgeChunk.knowledge_base_version_id,
                    func.count(KnowledgeChunk.id),
                )
                .where(
                    KnowledgeChunk.tenant_id == tenant_id,
                    KnowledgeChunk.knowledge_base_version_id.in_(version_ids),
                )
                .group_by(KnowledgeChunk.knowledge_base_version_id)
            ).all()
            if version_id is not None
        } if version_ids else {}
        duplicate_groups: dict[str, set[str]] = {}
        for version in versions:
            normalized_name = _normalized_knowledge_name(version.name)
            if normalized_name:
                duplicate_groups.setdefault(normalized_name, set()).add(version.knowledge_base_id)
        duplicate_group_by_kb = {
            knowledge_base_id: normalized_name
            for normalized_name, knowledge_base_ids in duplicate_groups.items()
            if len(knowledge_base_ids) > 1
            for knowledge_base_id in knowledge_base_ids
        }
        versions_by_knowledge_base: dict[str, list[KnowledgeBaseVersion]] = {}
        for version in versions:
            versions_by_knowledge_base.setdefault(version.knowledge_base_id, []).append(version)
        recommended_ids: set[str] = set()
        for knowledge_base_versions in versions_by_knowledge_base.values():
            recommended = max(
                knowledge_base_versions,
                key=lambda version: (
                    bool(document_counts.get(version.id) or chunk_counts.get(version.id))
                    and "-branch.agent_" not in version.version.casefold(),
                    bool(document_counts.get(version.id) or chunk_counts.get(version.id)),
                    chunk_counts.get(version.id, 0),
                    document_counts.get(version.id, 0),
                    version.version,
                    version.id,
                ),
            )
            recommended_ids.add(recommended.id)
        return AuditCaseManagementOptions(
            knowledge_versions=[
                AuditCaseKnowledgeVersionOption(
                    id=version.id,
                    knowledge_base_id=version.knowledge_base_id,
                    name=version.name,
                    version=version.version,
                    description=version.description,
                    status=version.status,
                    document_count=document_counts.get(version.id, 0),
                    chunk_count=chunk_counts.get(version.id, 0),
                    is_agent_branch="-branch.agent_" in version.version.casefold(),
                    recommended=version.id in recommended_ids,
                    duplicate_group=duplicate_group_by_kb.get(version.knowledge_base_id),
                )
                for version in versions
            ],
            audit_types=[
                AuditCaseChoiceOption(value=value, label=label, code=code)
                for value, label, code in AUDIT_TYPE_OPTIONS
            ],
            management_systems=[
                AuditCaseChoiceOption(value=value, label=value)
                for value in MANAGEMENT_SYSTEM_OPTIONS
            ],
            material_types=[
                AuditCaseMaterialTypeOption(
                    value=value,
                    label=label,
                    group=group,
                    description=description,
                )
                for value, label, group, description in MATERIAL_TYPE_OPTIONS
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
        if material_type not in SUPPORTED_AUDIT_MATERIAL_TYPES:
            raise AuditMaterialFormatError("UNSUPPORTED_MATERIAL_TYPE")
        _validate_material_upload(filename, data)
        sha256 = hashlib.sha256(data).hexdigest()
        same_hash = self.db.exec(
            select(AuditCaseMaterial).where(
                AuditCaseMaterial.tenant_id == case.tenant_id,
                AuditCaseMaterial.audit_case_id == case.id,
                AuditCaseMaterial.sha256 == sha256,
            )
        ).all()
        duplicate = next(
            (item for item in same_hash if item.material_type == material_type),
            None,
        )
        if duplicate is not None:
            return duplicate
        if same_hash:
            raise AuditMaterialCategoryConflict("MATERIAL_CATEGORY_CONFLICT")

        same_name = self.db.exec(
            select(AuditCaseMaterial).where(
                AuditCaseMaterial.tenant_id == case.tenant_id,
                AuditCaseMaterial.audit_case_id == case.id,
                AuditCaseMaterial.material_type == material_type,
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

    def list_materials(
        self,
        case: AuditCase,
        *,
        include_history: bool = False,
    ) -> list[AuditCaseMaterial]:
        statement = select(AuditCaseMaterial).where(
            AuditCaseMaterial.tenant_id == case.tenant_id,
            AuditCaseMaterial.audit_case_id == case.id,
        )
        if not include_history:
            statement = statement.where(AuditCaseMaterial.is_current)
        return list(
            self.db.exec(
                statement.order_by(
                    AuditCaseMaterial.material_type,
                    AuditCaseMaterial.filename,
                    AuditCaseMaterial.version,
                )
            ).all()
        )

    def replace_material(
        self,
        case: AuditCase,
        actor: User,
        material: AuditCaseMaterial,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> AuditCaseMaterial:
        case = self.get_case_for_user(case.tenant_id, case.id, actor)
        self._assert_writable(case)
        current = self.db.exec(
            select(AuditCaseMaterial).where(
                AuditCaseMaterial.tenant_id == case.tenant_id,
                AuditCaseMaterial.audit_case_id == case.id,
                AuditCaseMaterial.id == material.id,
            )
        ).first()
        if current is None:
            raise AuditCaseNotFound(material.id)
        if not current.is_current:
            raise AuditMaterialProcessingError("MATERIAL_NOT_CURRENT")
        _validate_material_upload(filename, data)
        material_type = current.material_type
        sha256 = hashlib.sha256(data).hexdigest()
        same_hash = self.db.exec(
            select(AuditCaseMaterial).where(
                AuditCaseMaterial.tenant_id == case.tenant_id,
                AuditCaseMaterial.audit_case_id == case.id,
                AuditCaseMaterial.sha256 == sha256,
            )
        ).all()
        if any(item.material_type == material_type for item in same_hash):
            raise AuditMaterialAlreadyExists("MATERIAL_ALREADY_EXISTS")
        if any(item.material_type != material_type for item in same_hash):
            raise AuditMaterialCategoryConflict("MATERIAL_CATEGORY_CONFLICT")

        latest_version = self.db.exec(
            select(AuditCaseMaterial.version).where(
                AuditCaseMaterial.tenant_id == case.tenant_id,
                AuditCaseMaterial.audit_case_id == case.id,
                AuditCaseMaterial.material_type == material_type,
            ).order_by(AuditCaseMaterial.version.desc())
        ).first()
        version = (latest_version or current.version) + 1
        current.is_current = False
        current.updated_at = utc_now()
        self.db.add(current)
        replacement = AuditCaseMaterial(
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
            supersedes_material_id=current.id,
        )
        self.db.add(replacement)
        self.db.flush()
        replacement.storage_key = write_case_blob(
            tenant_id=case.tenant_id,
            audit_case_id=case.id,
            material_id=replacement.id,
            name="raw",
            data=data,
        )
        record_case_event(
            self.db,
            case=case,
            actor_user_id=actor.id,
            event_type="audit_case.material_replaced",
            resource_type="audit_case_material",
            resource_id=replacement.id,
            metadata={
                "filename": replacement.filename,
                "sha256": replacement.sha256,
                "version": replacement.version,
                "status": replacement.processing_status,
            },
        )
        self.db.commit()
        self.db.refresh(replacement)
        return replacement

    def process_one_material(
        self,
        case: AuditCase,
        actor: User,
        material_id: str,
    ) -> AuditCaseMaterial:
        case = self.get_case_for_user(case.tenant_id, case.id, actor)
        self._assert_writable(case)
        material = self.db.exec(
            select(AuditCaseMaterial).where(
                AuditCaseMaterial.tenant_id == case.tenant_id,
                AuditCaseMaterial.audit_case_id == case.id,
                AuditCaseMaterial.id == material_id,
                AuditCaseMaterial.is_current,
            )
        ).first()
        if material is None:
            raise AuditCaseNotFound(material_id)
        return self.process_material(case, material, actor_user_id=actor.id)

    def read_material_source(self, material_id: str) -> bytes:
        material = self.db.get(AuditCaseMaterial, material_id)
        if material is None:
            raise AuditCaseNotFound(material_id)
        return read_case_blob(material.storage_key)

    def enqueue_material_processing(
        self,
        case: AuditCase,
        actor: User,
        material_id: str,
    ) -> AsyncJob:
        """Persist the job identity before submitting work to the process-local queue."""

        case = self.get_case_for_user(case.tenant_id, case.id, actor)
        self._assert_writable(case)
        material = self.db.exec(
            select(AuditCaseMaterial).where(
                AuditCaseMaterial.tenant_id == case.tenant_id,
                AuditCaseMaterial.audit_case_id == case.id,
                AuditCaseMaterial.id == material_id,
                AuditCaseMaterial.is_current,
            )
        ).first()
        if material is None:
            raise AuditCaseNotFound(material_id)

        queue = get_async_job_queue()
        if (
            material.extraction_status == "succeeded"
            and material.processing_status == "succeeded"
        ):
            return AsyncJob(
                id=material.processing_job_id or f"material-{material.id}",
                name="audit_case.material.extract",
                status="succeeded",
                metadata={"material_id": material.id, "reused": True},
            )
        if material.processing_job_id:
            existing = queue.get(material.processing_job_id)
            if existing is not None:
                return existing
            # A normal duplicate request must not submit twice. Startup
            # recovery calls this method with a cleared job id below.
            return AsyncJob(
                id=material.processing_job_id,
                name="audit_case.material.extract",
                status="queued",
                metadata={"material_id": material.id, "recovered": True},
            )
        else:
            job_id = new_id("job")
            material.processing_job_id = job_id
        material.processing_status = "pending"
        if material.extraction_status == "failed":
            material.extraction_status = "pending"
        material.error_code = None
        material.updated_at = utc_now()
        self.db.add(material)
        self.db.commit()

        try:
            job = enqueue_async_job(
                "audit_case.material.extract",
                _run_material_processing,
                self.db.get_bind(),
                case.tenant_id,
                case.id,
                material.id,
                actor.id,
                job_id=job_id,
                metadata={
                    "job_id": job_id,
                    "tenant_id": case.tenant_id,
                    "audit_case_id": case.id,
                    "material_id": material.id,
                },
            )
        except Exception as exc:
            material.processing_status = "failed"
            material.extraction_status = "failed"
            material.error_code = "ASYNC_JOB_ENQUEUE_FAILED"
            material.updated_at = utc_now()
            self.db.add(material)
            self.db.commit()
            raise AuditMaterialProcessingError(str(exc) or "ASYNC_JOB_ENQUEUE_FAILED") from exc
        return job

    def retry_material(self, case: AuditCase, actor: User, material_id: str) -> AuditCaseMaterial:
        case = self.get_case_for_user(case.tenant_id, case.id, actor)
        self._assert_writable(case)
        material = self.db.exec(
            select(AuditCaseMaterial).where(
                AuditCaseMaterial.tenant_id == case.tenant_id,
                AuditCaseMaterial.audit_case_id == case.id,
                AuditCaseMaterial.id == material_id,
                AuditCaseMaterial.is_current,
            )
        ).first()
        if material is None:
            raise AuditCaseNotFound(material_id)
        material.processing_job_id = None
        material.extraction_status = "pending"
        material.processing_status = "pending"
        material.error_code = None
        material.updated_at = utc_now()
        self.db.add(material)
        self.db.commit()
        self.enqueue_material_processing(case, actor, material.id)
        return self.get_material(case.id, material.id, actor)

    def process_material(
        self,
        case: AuditCase,
        material: AuditCaseMaterial,
        *,
        actor_user_id: str | None = None,
    ) -> AuditCaseMaterial:
        with _material_lock(material.id):
            return self._process_material(
                case,
                material,
                actor_user_id=actor_user_id,
            )

    def _process_material(
        self,
        case: AuditCase,
        material: AuditCaseMaterial,
        *,
        actor_user_id: str | None = None,
    ) -> AuditCaseMaterial:
        self._assert_writable(case)
        if material.tenant_id != case.tenant_id or material.audit_case_id != case.id:
            raise AuditCaseNotFound(material.id)
        material.processing_status = "processing"
        material.error_code = None
        material.updated_at = utc_now()
        self.db.add(material)
        self.db.commit()
        try:
            source = read_case_blob(material.storage_key)
            extraction = _extract_audit_material(material.filename, source)
            if not extraction.text.strip():
                error_code = (
                    "PDF_TEXT_LAYER_MISSING"
                    if Path(material.filename).suffix.lower() == ".pdf"
                    else "EMPTY_EXTRACTED_TEXT"
                )
                raise AuditMaterialProcessingError(error_code)
            material.extracted_text_storage_key = write_case_blob(
                tenant_id=case.tenant_id,
                audit_case_id=case.id,
                material_id=material.id,
                name="extracted.txt",
                data=extraction.text.encode("utf-8"),
            )
            # The old chunk set is replaced only after the complete extracted
            # text has been durably written, so a failed OCR attempt never
            # destroys the last auditable result.
            self.db.exec(
                delete(AuditCaseMaterialChunk).where(
                    AuditCaseMaterialChunk.tenant_id == case.tenant_id,
                    AuditCaseMaterialChunk.audit_case_id == case.id,
                    AuditCaseMaterialChunk.material_id == material.id,
                )
            )
            material.characters = len(extraction.text)
            material.page_count = extraction.source_page_count
            material.extraction_method = extraction.method
            material.extraction_engine = extraction.engine
            material.extraction_engine_version = extraction.engine_version
            material.extraction_warnings_json = list(extraction.warnings)
            material.extracted_text_sha256 = hashlib.sha256(
                extraction.text.encode("utf-8")
            ).hexdigest()
            material.extraction_status = "succeeded"
            page_ranges = _page_ranges(extraction)
            for span in chunk_text(extraction.text):
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
                        page_refs_json=page_refs_for_span(
                            span.start_char, span.end_char, page_ranges
                        ),
                        chunking_status="succeeded",
                        processing_status="pending",
                    )
                )
            material.processing_status = "succeeded"
            material.error_code = None
        except (DocumentExtractionError, KnowledgeParseError, AuditMaterialProcessingError, OSError) as exc:
            material.extraction_status = "failed"
            material.processing_status = "failed"
            material.error_code = _material_error_code(exc)
        material.updated_at = utc_now()
        self.db.add(material)
        record_case_event(
            self.db,
            case=case,
            actor_user_id=actor_user_id or case.owner_user_id,
            event_type="audit_case.material_processed",
            resource_type="audit_case_material",
            resource_id=material.id,
            metadata={
                "filename": material.filename,
                "sha256": material.sha256,
                "version": material.version,
                "status": material.processing_status,
                "error_code": material.error_code,
            },
        )
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
        material_ids = [
            row
            for row in self.db.exec(
                select(AuditCaseMaterial.id).where(
                    AuditCaseMaterial.tenant_id == actor.tenant_id,
                    AuditCaseMaterial.audit_case_id == case_id,
                )
            ).all()
        ]
        with _hold_material_locks(material_ids):
            self._delete_case_locked(case_id, actor)

    def _delete_case_locked(self, case_id: str, actor: User) -> None:
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


def _run_material_processing(
    db_bind: Any,
    tenant_id: str,
    case_id: str,
    material_id: str,
    actor_user_id: str,
) -> None:
    """Run one job with a fresh session; request sessions never cross threads."""

    # Test clients commonly bind a StaticPool in-memory database. It has one
    # connection by design, so a background session would race the request
    # session; the explicit process endpoint remains the deterministic path in
    # that setup. File-backed production SQLite uses independent connections.
    if _is_shared_in_memory_bind(db_bind):
        return

    with Session(db_bind) as db:
        case = db.exec(
            select(AuditCase).where(
                AuditCase.tenant_id == tenant_id,
                AuditCase.id == case_id,
            )
        ).first()
        material = db.exec(
            select(AuditCaseMaterial).where(
                AuditCaseMaterial.tenant_id == tenant_id,
                AuditCaseMaterial.audit_case_id == case_id,
                AuditCaseMaterial.id == material_id,
                AuditCaseMaterial.is_current,
            )
        ).first()
        if case is None or material is None:
            return
        AuditCaseService(db).process_material(
            case,
            material,
            actor_user_id=actor_user_id,
        )


def recover_pending_material_jobs(db_bind: Any | None = None) -> int:
    """Requeue pending/processing materials after an application restart."""

    bind = db_bind or engine
    queued = 0
    with Session(bind) as db:
        rows = db.exec(
            select(AuditCaseMaterial, AuditCase)
            .join(AuditCase, AuditCase.id == AuditCaseMaterial.audit_case_id)
            .where(
                AuditCaseMaterial.is_current,
                AuditCaseMaterial.processing_status.in_(["pending", "processing"]),
            )
        ).all()
        for material, case in rows:
            owner = db.get(User, case.owner_user_id)
            if owner is None:
                continue
            material.processing_job_id = None
            db.add(material)
            db.commit()
            AuditCaseService(db).enqueue_material_processing(case, owner, material.id)
            queued += 1
    return queued
