from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SourceRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    material_id: str | None = None
    material_version_id: str | None = None
    document_id: str | None = None
    location: str = ""
    evidence_excerpt: str = ""


class ProjectDataCandidateCreate(BaseModel):
    field_key: str = Field(min_length=1)
    value: Any
    source: SourceRef | None = None
    expected_revision: int | None = Field(default=None, ge=0)
    note: str | None = None


class ProjectDataValueRead(BaseModel):
    id: str
    field_key: str
    value: Any
    status: str
    revision: int
    source: SourceRef | None = None
    updated_by_user_id: str | None = None


class ProjectDataCandidateRead(BaseModel):
    id: str
    field_key: str
    value: Any
    source: SourceRef | None = None
    status: str
    expected_revision: int | None = None
    submitted_by_user_id: str
    decision_reason: str | None = None


class ProjectDataConflictRead(BaseModel):
    id: str
    field_key: str
    status: str
    current_revision: int
    candidate_ids: list[str]
    resolved_candidate_id: str | None = None
    resolution_reason: str | None = None


class RuleEvaluationContext(BaseModel):
    """Immutable inputs used by a single rule-evaluation pass."""

    model_config = ConfigDict(frozen=True)

    project_fields: dict[str, Any] = Field(default_factory=dict)
    project_field_revisions: dict[str, int] = Field(default_factory=dict)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    workflow_node: str = ""
    information_domain: str = ""
    target_ref: str | None = None


class ProjectFieldValidationError(ValueError):
    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)


class ProjectDataConflictError(RuntimeError):
    def __init__(self, message: str = "PROJECT_DATA_CONFLICT"):
        super().__init__(message)


class ProjectDataNotFound(LookupError):
    pass
