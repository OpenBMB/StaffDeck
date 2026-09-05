from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import (
    AuditCase,
    AuditCaseDocument,
    AuditCaseDocumentVersion,
    AuditCaseMaterial,
)


class AuditCaseCreate(BaseModel):
    tenant_id: str
    organization_name: str = Field(min_length=1, max_length=200)
    report_type: str = Field(min_length=1, max_length=100)
    agent_id: str | None = None
    knowledge_scope_mode: Literal["agent_default", "custom"] = "custom"
    management_systems: list[str] = Field(default_factory=list)
    knowledge_base_version_ids: list[str] = Field(default_factory=list)
    member_user_ids: list[str] = Field(default_factory=list)


class AuditCaseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    owner_user_id: str
    agent_id: str | None = None
    member_user_ids: list[str]
    organization_name: str
    report_type: str
    management_systems: list[str]
    status: str
    knowledge_scope_mode: Literal["agent_default", "custom"] = "custom"
    knowledge_base_version_ids: list[str]
    active_report_version_id: str | None = None
    created_at: datetime
    updated_at: datetime


class AuditCaseUpdate(BaseModel):
    organization_name: str | None = Field(default=None, min_length=1, max_length=200)
    report_type: str | None = Field(default=None, min_length=1, max_length=100)
    management_systems: list[str] | None = None
    knowledge_base_version_ids: list[str] | None = None


class AuditCaseMemberUpdate(BaseModel):
    member_user_ids: list[str] = Field(default_factory=list)


class AuditCaseManagementRead(AuditCaseRead):
    material_total: int = 0
    material_ready: int = 0
    material_failed: int = 0
    file_coverage: float = 0.0
    chunk_coverage: float = 0.0


class AuditCaseManagementPage(BaseModel):
    items: list[AuditCaseManagementRead]
    total: int


class AuditCaseChoiceOption(BaseModel):
    value: str
    label: str
    code: str | None = None


class AuditCaseMaterialTypeOption(BaseModel):
    value: str
    label: str
    group: str
    description: str


class AuditCaseKnowledgeVersionOption(BaseModel):
    id: str
    knowledge_base_id: str
    name: str
    version: str
    description: str | None = None
    status: str
    document_count: int = 0
    chunk_count: int = 0
    is_agent_branch: bool = False
    recommended: bool = False
    duplicate_group: str | None = None


class AuditCaseAgentOption(BaseModel):
    id: str
    name: str
    description: str | None = None
    knowledge_base_version_ids: list[str] = Field(default_factory=list)


class AuditCaseManagementOptions(BaseModel):
    agent_options: list[AuditCaseAgentOption] = Field(default_factory=list)
    knowledge_versions: list[AuditCaseKnowledgeVersionOption] = Field(default_factory=list)
    audit_types: list[AuditCaseChoiceOption] = Field(default_factory=list)
    management_systems: list[AuditCaseChoiceOption] = Field(default_factory=list)
    material_types: list[AuditCaseMaterialTypeOption] = Field(default_factory=list)
    supported_extensions: list[str] = Field(default_factory=list)
    max_material_bytes: int


class AuditCaseEventRead(BaseModel):
    id: str
    audit_case_id: str
    actor_user_id: str
    event_type: str
    resource_type: str
    resource_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class AuditCaseMaterialRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    audit_case_id: str
    attachment_id: str
    material_type: str
    filename: str
    content_type: str
    sha256: str
    size: int
    characters: int
    page_count: int = 0
    extraction_method: str | None = None
    extraction_engine: str | None = None
    extraction_engine_version: str | None = None
    extraction_warnings: list[str] = Field(default_factory=list)
    extracted_text_sha256: str | None = None
    processing_job_id: str | None = None
    extraction_status: str
    processing_status: str
    version: int
    is_current: bool
    supersedes_material_id: str | None = None
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime


class AuditCaseDocumentCreate(BaseModel):
    document_key: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    document_type: str = Field(min_length=1, max_length=100)
    zone: str = Field(min_length=1, max_length=100)
    content_format: str
    content: str
    change_note: str | None = Field(default=None, max_length=500)
    source_material_id: str | None = None


class AuditCaseDocumentVersionCreate(BaseModel):
    expected_version: int = Field(ge=1)
    content_format: str
    content: str
    change_note: str | None = Field(default=None, max_length=500)


class AuditCaseDocumentArchiveRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class AuditCaseDocumentVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    document_id: str
    version: int
    content_format: str
    content: str
    content_sha256: str
    characters: int
    change_note: str | None = None
    created_by_user_id: str
    created_at: datetime


class AuditCaseDocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    audit_case_id: str
    document_key: str
    title: str
    document_type: str
    zone: str
    status: str
    active_version_id: str | None = None
    source_material_id: str | None = None
    archive_reason: str | None = None
    created_by_user_id: str
    updated_by_user_id: str
    created_at: datetime
    updated_at: datetime
    active_version: AuditCaseDocumentVersionRead | None = None


class AuditCaseDocumentDetailRead(BaseModel):
    document: AuditCaseDocumentRead
    versions: list[AuditCaseDocumentVersionRead] = Field(default_factory=list)


class AuditCaseCoverageRead(BaseModel):
    current_material_count: int
    successful_material_count: int
    failed_material_count: int
    total_chunk_count: int
    successful_chunk_count: int
    file_coverage: float
    chunk_coverage: float


class AuditCaseProcessRead(BaseModel):
    status: str
    code: str | None = None
    materials: list[AuditCaseMaterialRead] = Field(default_factory=list)
    coverage: AuditCoverageSnapshot
    evidence: dict[str, Any] | None = None
    knowledge: dict[str, Any] | None = None


class AuditCaseProcessRequest(BaseModel):
    model_config_id: str | None = None


class AuditCoverageSnapshot(BaseModel):
    file_coverage: float
    chunk_coverage: float
    element_coverage: float
    publish_allowed: bool
    blockers: list[str]
    files_total: int
    files_succeeded: int
    chunks_total: int
    chunks_succeeded: int
    elements_total: int
    elements_resolved: int
    current_material_count: int = 0
    successful_material_count: int = 0
    failed_material_count: int = 0
    total_chunk_count: int = 0
    successful_chunk_count: int = 0
    pending_material_ids: list[str] = Field(default_factory=list)
    failed_material_ids: list[str] = Field(default_factory=list)
    pending_chunk_ids: list[str] = Field(default_factory=list)


class AuditReportSectionRead(BaseModel):
    id: str
    section_id: str
    title: str
    sequence: int
    status: str
    retry_count: int
    error_code: str | None = None
    draft_markdown: str = ""
    citation_ids: list[str] = Field(default_factory=list)
    rule_definition_ids: list[str] = Field(default_factory=list)


class AuditReportRead(BaseModel):
    id: str
    tenant_id: str
    audit_case_id: str
    source_document_id: str | None = None
    source_document_version_id: str | None = None
    version: int
    status: str
    material_version_ids: list[str] = Field(default_factory=list)
    knowledge_base_version_ids: list[str] = Field(default_factory=list)
    rule_set_version_ids: list[str] = Field(default_factory=list)
    rule_traceability_status: str = "not_configured"
    coverage_snapshot: dict[str, Any] = Field(default_factory=dict)
    final_storage_key: str | None = None
    sections: list[AuditReportSectionRead] = Field(default_factory=list)


class AuditReportCreateRequest(BaseModel):
    model_config_id: str | None = None
    source_document_id: str | None = None
    source_document_version_id: str | None = None
    publish: bool = False
    confirmed_by: str | None = None


class AuditReportPublishRequest(BaseModel):
    pass


class AuditReportNotFound(LookupError):
    pass


class AuditReportDownloadNotReady(RuntimeError):
    pass


class AuditCaseNotFound(LookupError):
    pass


class AuditCaseAccessDenied(PermissionError):
    pass


class AuditCaseReadOnly(RuntimeError):
    pass


class AuditCaseDocumentConflict(RuntimeError):
    pass


class AuditCaseDocumentFormatError(ValueError):
    pass


class AuditCaseDocumentNotFound(LookupError):
    pass


class AuditCaseDocumentReadOnly(RuntimeError):
    pass


class AuditMaterialProcessingError(RuntimeError):
    pass


class AuditMaterialAlreadyExists(ValueError):
    pass


class AuditMaterialCategoryConflict(ValueError):
    pass


class AuditMaterialFormatError(ValueError):
    pass


class AuditMaterialTooLarge(ValueError):
    pass


def audit_case_read(row: AuditCase) -> AuditCaseRead:
    return AuditCaseRead(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_user_id=row.owner_user_id,
        agent_id=row.agent_id,
        member_user_ids=list(row.member_user_ids_json or []),
        organization_name=row.organization_name,
        report_type=row.report_type,
        management_systems=list(row.management_systems_json or []),
        status=row.status,
        knowledge_scope_mode=row.knowledge_scope_mode,
        knowledge_base_version_ids=list(row.knowledge_base_version_ids_json or []),
        active_report_version_id=row.active_report_version_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def audit_case_material_read(row: AuditCaseMaterial) -> AuditCaseMaterialRead:
    return AuditCaseMaterialRead(
        id=row.id,
        audit_case_id=row.audit_case_id,
        attachment_id=row.attachment_id,
        material_type=row.material_type,
        filename=row.filename,
        content_type=row.content_type,
        sha256=row.sha256,
        size=row.size,
        characters=row.characters,
        page_count=row.page_count,
        extraction_method=row.extraction_method,
        extraction_engine=row.extraction_engine,
        extraction_engine_version=row.extraction_engine_version,
        extraction_warnings=list(row.extraction_warnings_json or []),
        extracted_text_sha256=row.extracted_text_sha256,
        processing_job_id=row.processing_job_id,
        extraction_status=row.extraction_status,
        processing_status=row.processing_status,
        version=row.version,
        is_current=row.is_current,
        supersedes_material_id=row.supersedes_material_id,
        error_code=row.error_code,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def audit_case_document_read(row: AuditCaseDocument) -> AuditCaseDocumentRead:
    return AuditCaseDocumentRead.model_validate(row)


def audit_case_document_version_read(
    row: AuditCaseDocumentVersion,
) -> AuditCaseDocumentVersionRead:
    return AuditCaseDocumentVersionRead.model_validate(row)

