from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from .schema import ProjectDataCandidateCreate, ProjectFieldValidationError


class FieldDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    field_key: str
    label: str
    value_type: str
    information_domain: str
    scope: str
    required: bool = False
    editable: bool = True
    sync_policy: str = "confirm"
    validator_name: str | None = None
    source_optional: bool = False


_TEXT_LABELS = {
    "organization.legal_name": "企业法定名称",
    "organization.registered_address": "注册地址",
    "organization.credit_code": "统一社会信用代码",
    "organization.contact_person": "联系人",
    "organization.contact_phone": "联系电话",
    "certification_project.audit_type": "审核类型",
    "certification_project.management_system": "管理体系",
    "certification_project.scope": "认证范围",
    "certification_project.applicable_standard": "适用标准",
    "certification_project.audit_objective": "审核目标",
    "certification_project.audit_stage": "审核阶段",
    "audit_event.team_leader": "审核组长",
    "audit_event.audit_team_roles": "审核组角色",
    "site.permanent_sites": "常设场所",
    "site.temporary_sites": "临时场所",
    "energy.review.boundary": "能源管理体系边界",
    "energy.review.baseline_period": "基准期",
    "energy.review.objectives": "能源目标",
    "energy.review.targets": "能源指标",
    "energy.review.action_plans": "能源行动计划",
    "audit_evidence.conclusion": "审核结论",
    "document.number": "文件编号",
    "document.revision": "文件版本",
    "document.approver": "批准人",
}

_VALUE_TYPES = {
    "audit_event.start_date": "date",
    "audit_event.end_date": "date",
    "audit_event.audit_team_members": "list",
    "audit_event.site_ids": "list",
    "audit_event.audit_days": "number",
    "audit_event.opening_time": "datetime",
    "audit_event.closing_time": "datetime",
    "energy.review.significant_energy_uses": "list",
    "energy.review.performance_indicators": "list",
    "audit_evidence.nonconformities": "list",
    "audit_evidence.improvement_suggestions": "list",
    "audit_evidence.observations": "list",
    "document.issue_date": "date",
}

_SYSTEM_KEYS = (
    "organization.legal_name",
    "organization.registered_address",
    "organization.credit_code",
    "organization.contact_person",
    "organization.contact_phone",
    "certification_project.audit_type",
    "certification_project.management_system",
    "certification_project.scope",
    "certification_project.applicable_standard",
    "certification_project.audit_objective",
    "certification_project.audit_stage",
    "audit_event.start_date",
    "audit_event.end_date",
    "audit_event.team_leader",
    "audit_event.audit_team_members",
    "audit_event.audit_team_roles",
    "audit_event.site_ids",
    "audit_event.audit_days",
    "audit_event.opening_time",
    "audit_event.closing_time",
    "site.permanent_sites",
    "site.temporary_sites",
    "energy.review.boundary",
    "energy.review.significant_energy_uses",
    "energy.review.baseline_period",
    "energy.review.performance_indicators",
    "energy.review.objectives",
    "energy.review.targets",
    "energy.review.action_plans",
    "audit_evidence.nonconformities",
    "audit_evidence.improvement_suggestions",
    "audit_evidence.observations",
    "audit_evidence.conclusion",
    "document.number",
    "document.revision",
    "document.issue_date",
    "document.approver",
)

SYSTEM_FIELD_DEFINITIONS = [
    FieldDefinition(
        field_key=field_key,
        label=_TEXT_LABELS.get(field_key, field_key.rsplit(".", 1)[-1]),
        value_type=_VALUE_TYPES.get(field_key, "text"),
        information_domain=field_key.split(".", 1)[0],
        scope="project",
        required=field_key in {"organization.legal_name", "certification_project.scope"},
        validator_name=_VALUE_TYPES.get(field_key),
    )
    for field_key in _SYSTEM_KEYS
]


def get_field_definition(
    field_key: str,
    *,
    tenant_id: str | None = None,
    tenant_definitions: list[FieldDefinition] | None = None,
) -> FieldDefinition:
    del tenant_id
    for definition in tenant_definitions or []:
        if definition.field_key == field_key:
            return definition
    for definition in SYSTEM_FIELD_DEFINITIONS:
        if definition.field_key == field_key:
            return definition
    raise ProjectFieldValidationError("UNKNOWN_FIELD_KEY")


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def validate_field_value(definition: FieldDefinition, value: Any) -> None:
    if _is_empty(value):
        raise ProjectFieldValidationError("EMPTY_VALUE")

    kind = definition.value_type
    if kind == "text" and not isinstance(value, str):
        raise ProjectFieldValidationError("INVALID_TEXT")
    if kind == "date":
        try:
            if not isinstance(value, str):
                raise TypeError
            date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ProjectFieldValidationError("INVALID_DATE") from exc
    elif kind == "datetime":
        try:
            if not isinstance(value, str):
                raise TypeError
            datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ProjectFieldValidationError("INVALID_DATETIME") from exc
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ProjectFieldValidationError("INVALID_INTEGER")
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ProjectFieldValidationError("INVALID_NUMBER")
    elif kind == "boolean" and not isinstance(value, bool):
        raise ProjectFieldValidationError("INVALID_BOOLEAN")
    elif kind == "list" and not isinstance(value, list):
        raise ProjectFieldValidationError("INVALID_LIST")
    elif kind == "object" and not isinstance(value, dict):
        raise ProjectFieldValidationError("INVALID_OBJECT")

    if definition.validator_name == "non_empty_text" and _is_empty(value):
        raise ProjectFieldValidationError("EMPTY_VALUE")


def validate_candidate_request(
    request: ProjectDataCandidateCreate,
    definition: FieldDefinition | None = None,
) -> None:
    if _is_empty(request.value):
        raise ProjectFieldValidationError("EMPTY_VALUE")
    if request.source is None:
        if definition is not None and definition.source_optional:
            return
        raise ProjectFieldValidationError("SOURCE_REQUIRED")
    if (
        not request.source.location.strip()
        or not request.source.evidence_excerpt.strip()
    ) and not (definition is not None and definition.source_optional):
        raise ProjectFieldValidationError("SOURCE_REQUIRED")
