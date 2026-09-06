from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from itertools import combinations
from typing import Any, Literal

from sqlalchemy import update
from sqlmodel import Session, func, select

from app.audit_cases.service import record_case_event
from app.db.models import (
    AuditCase,
    AuditCaseDocument,
    AuditCaseDocumentVersion,
    ProjectDataFieldDefinition,
    ProjectRuleBinding,
    RuleDefinition,
    RuleEvaluation,
    RuleSet,
    RuleSetVersion,
    User,
    utc_now,
)
from app.project_data.permissions import ensure_project_role
from app.project_data.schema import RuleEvaluationContext
from app.rules.schema import (
    RuleAccessDenied,
    RuleDefinitionCreate,
    RuleSetCreate,
    RuleValidationError,
    RuleVersionImmutableError,
)
from app.rules.validation import (
    fingerprint_rule_set_version,
    validate_rule_definition_with_fields,
    validate_rule_set_with_fields,
)

_STABLE_KEY = re.compile(r"[a-z0-9_.-]{1,160}\Z", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_BINDING_WRITE_ROLES = {"project_admin", "reviewer"}
_BINDING_READ_ROLES = {"project_admin", "reviewer", "editor", "viewer"}


class RuleBindingError(ValueError):
    """Stable domain error codes for project rule binding operations."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class RuleBindingMigrationRequired(RuleBindingError):
    def __init__(self) -> None:
        super().__init__("RULE_MIGRATION_CONFIRMATION_REQUIRED")


class RuleBindingNotInitialized(RuleBindingError):
    def __init__(self) -> None:
        super().__init__("RULE_BINDING_NOT_INITIALIZED")


class RuleMigrationPreview:
    def __init__(
        self,
        *,
        added_rule_keys: list[str],
        removed_rule_keys: list[str],
        changed_rule_keys: list[str],
        unchanged_rule_keys: list[str],
        impacted_information_domains: list[str],
        impacted_workflow_nodes: list[str],
    ) -> None:
        self.added_rule_keys = added_rule_keys
        self.removed_rule_keys = removed_rule_keys
        self.changed_rule_keys = changed_rule_keys
        self.unchanged_rule_keys = unchanged_rule_keys
        self.impacted_information_domains = impacted_information_domains
        self.impacted_workflow_nodes = impacted_workflow_nodes


class RuleEvaluationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DeterministicRuleResult:
    status: str
    blocking: bool = False
    message: str = ""
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        del mode
        return {
            "status": self.status,
            "blocking": self.blocking,
            "message": self.message,
            "evidence_refs": [dict(item) for item in self.evidence_refs],
        }


def _ensure_tenant_admin(actor: User, tenant_id: str) -> None:
    if actor.tenant_id != tenant_id or actor.role != "admin":
        raise RuleAccessDenied()


def _rule_definition_create(row: RuleDefinition) -> RuleDefinitionCreate:
    return RuleDefinitionCreate(
        rule_key=row.rule_key,
        name=row.name,
        description=row.description,
        workflow_nodes=list(row.workflow_nodes_json),
        information_domains=list(row.information_domains_json),
        document_types=list(row.document_types_json),
        field_keys=list(row.field_keys_json),
        execution_level=row.execution_level,
        execution_method=row.execution_method,
        condition=dict(row.condition_json),
        input_requirements=list(row.input_requirements_json),
        evidence_requirements=list(row.evidence_requirements_json),
        source_refs=list(row.source_refs_json),
        sequence=row.sequence,
        enabled=row.enabled,
    )


def _rule_fingerprint(row: RuleDefinition) -> str:
    return fingerprint_rule_set_version([_rule_definition_create(row)])


def _rule_scope(row: RuleDefinition) -> tuple[tuple[str, ...], ...]:
    return (
        tuple(sorted(row.workflow_nodes_json)),
        tuple(sorted(row.information_domains_json)),
        tuple(sorted(row.document_types_json)),
        tuple(sorted(row.field_keys_json)),
    )


def _condition_value(condition: dict[str, Any]) -> Any:
    for key in ("value", "expected", "expected_value", "values"):
        if key in condition:
            return condition[key]
    return None


def _conditions_contradict(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left.get("field_key") != right.get("field_key"):
        return False
    left_operator = left.get("operator")
    right_operator = right.get("operator")
    left_value = _condition_value(left)
    right_value = _condition_value(right)

    if left_operator == right_operator == "equals":
        return left_value != right_value
    if {left_operator, right_operator} == {"equals", "not_equals"}:
        equals_value = left_value if left_operator == "equals" else right_value
        not_equals_value = left_value if left_operator == "not_equals" else right_value
        return equals_value == not_equals_value
    if left_operator == right_operator == "in":
        try:
            return set(left_value).isdisjoint(set(right_value))
        except TypeError:
            return False

    equals_condition = None
    other_condition = None
    if left_operator == "equals":
        equals_condition, other_condition = left, right
    elif right_operator == "equals":
        equals_condition, other_condition = right, left
    if equals_condition is not None and other_condition is not None:
        expected = _condition_value(equals_condition)
        other_value = _condition_value(other_condition)
        operator = other_condition.get("operator")
        try:
            if operator == "in":
                return expected not in other_value
            if operator == "gte":
                return expected < other_value
            if operator == "lte":
                return expected > other_value
        except TypeError:
            return False

    bounds = {left_operator: left_value, right_operator: right_value}
    if left_operator != right_operator and {left_operator, right_operator} == {
        "gte",
        "lte",
    }:
        try:
            return bounds["gte"] > bounds["lte"]
        except TypeError:
            return False
    return False


def _validate_mandatory_rule_conflicts(rules: list[RuleDefinition]) -> None:
    mandatory = [
        rule
        for rule in rules
        if rule.enabled and rule.execution_level == "mandatory"
    ]
    for left, right in combinations(mandatory, 2):
        if _rule_scope(left) != _rule_scope(right):
            continue
        if _conditions_contradict(left.condition_json, right.condition_json):
            raise RuleBindingError("CONFLICTING_MANDATORY_RULES")


def _rule_value(rule: RuleDefinition | RuleDefinitionCreate, name: str) -> Any:
    """Read a rule field from either the persisted model or its input schema."""

    persisted_name = f"{name}_json"
    if hasattr(rule, persisted_name):
        return getattr(rule, persisted_name)
    return getattr(rule, name)


def _field_value(context: RuleEvaluationContext, field_key: str) -> Any:
    return context.project_fields.get(field_key)


def _is_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _condition_matches(condition: dict[str, Any], context: RuleEvaluationContext) -> bool:
    operator = condition.get("operator")
    field_key = condition.get("field_key")
    value = _field_value(context, field_key) if isinstance(field_key, str) else None

    if operator == "required":
        return _is_present(value)
    if operator == "equals":
        return _is_present(value) and value == _condition_value(condition)
    if operator == "not_equals":
        return _is_present(value) and value != _condition_value(condition)
    if operator == "in":
        values = _condition_value(condition)
        try:
            return _is_present(value) and value in values
        except TypeError:
            return False
    if operator == "gte":
        try:
            return _is_present(value) and value >= _condition_value(condition)
        except TypeError:
            return False
    if operator == "lte":
        try:
            return _is_present(value) and value <= _condition_value(condition)
        except TypeError:
            return False
    if operator == "matches":
        pattern = condition.get("pattern")
        if not isinstance(pattern, str):
            raise RuleEvaluationError("INVALID_RULE_PATTERN")
        try:
            return _is_present(value) and re.search(pattern, str(value)) is not None
        except re.error as exc:
            raise RuleEvaluationError("INVALID_RULE_PATTERN") from exc
    if operator == "has_evidence":
        return bool(context.evidence_refs)
    raise RuleEvaluationError("UNSUPPORTED_RULE_OPERATOR")


def evaluate_deterministic_rule(
    rule: RuleDefinition | RuleDefinitionCreate,
    context: RuleEvaluationContext,
) -> DeterministicRuleResult:
    """Evaluate the intentionally small, auditable Phase 1 rule subset."""

    condition = dict(_rule_value(rule, "condition") or {})
    passed = _condition_matches(condition, context)
    level = str(getattr(rule, "execution_level", "guidance"))
    evidence_refs = [dict(item) for item in context.evidence_refs]
    if passed:
        return DeterministicRuleResult(
            status="passed",
            blocking=False,
            message="rule condition passed",
            evidence_refs=evidence_refs,
        )
    if level == "warning":
        return DeterministicRuleResult(
            status="warning",
            blocking=False,
            message="rule condition needs attention",
            evidence_refs=evidence_refs,
        )
    return DeterministicRuleResult(
        status="failed",
        blocking=level == "mandatory",
        message="rule condition failed",
        evidence_refs=evidence_refs,
    )


def evaluate_rule_without_model(
    rule: RuleDefinition | RuleDefinitionCreate,
    context: RuleEvaluationContext,
) -> DeterministicRuleResult:
    """Evaluate deterministic rules and leave model-assisted rules indeterminate."""

    if getattr(rule, "execution_method", "deterministic") == "model_assisted":
        return DeterministicRuleResult(
            status="indeterminate",
            blocking=False,
            message="model-assisted rule requires human or model review",
            evidence_refs=[dict(item) for item in context.evidence_refs],
        )
    return evaluate_deterministic_rule(rule, context)


class RuleBindingService:
    def __init__(self, db: Session):
        self.db = db

    def _authorize_read(self, case: AuditCase, actor: User) -> None:
        if actor.tenant_id != case.tenant_id:
            raise RuleBindingError("RULE_TENANT_ACCESS_DENIED")
        ensure_project_role(self.db, case, actor, _BINDING_READ_ROLES)

    def _authorize_write(self, case: AuditCase, actor: User) -> None:
        from app.audit_cases.workbench import begin_case_write

        if actor.tenant_id != case.tenant_id:
            raise RuleBindingError("RULE_TENANT_ACCESS_DENIED")
        ensure_project_role(self.db, case, actor, _BINDING_WRITE_ROLES)
        begin_case_write(self.db, case)
        ensure_project_role(self.db, case, actor, _BINDING_WRITE_ROLES)
        if case.status == "archived":
            raise RuleBindingError("AUDIT_CASE_READ_ONLY")

    def _rules_for_versions(self, version_ids: list[str]) -> list[RuleDefinition]:
        if not version_ids:
            return []
        return list(
            self.db.exec(
                select(RuleDefinition)
                .where(RuleDefinition.rule_set_version_id.in_(version_ids))
                .order_by(RuleDefinition.rule_key, RuleDefinition.sequence)
            ).all()
        )

    def _published_version(
        self,
        case: AuditCase,
        version_id: str,
    ) -> RuleSetVersion:
        version = self.db.get(RuleSetVersion, version_id)
        if (
            version is None
            or version.tenant_id != case.tenant_id
            or version.status != "published"
            or _SHA256.fullmatch(version.content_sha256) is None
        ):
            raise RuleBindingError("PUBLISHED_RULE_VERSION_REQUIRED")
        rules = self._rules_for_versions([version.id])
        actual_hash = fingerprint_rule_set_version(
            [_rule_definition_create(rule) for rule in rules]
        )
        if actual_hash != version.content_sha256:
            raise RuleBindingError("PUBLISHED_RULE_VERSION_REQUIRED")
        return version

    def _published_versions(
        self,
        case: AuditCase,
        version_ids: list[str],
    ) -> list[RuleSetVersion]:
        versions = [self._published_version(case, version_id) for version_id in version_ids]
        if len({version.id for version in versions}) != len(versions):
            raise RuleBindingError("PUBLISHED_RULE_VERSION_REQUIRED")
        if len({version.rule_set_id for version in versions}) != len(versions):
            raise RuleBindingError("PUBLISHED_RULE_VERSION_REQUIRED")
        _validate_mandatory_rule_conflicts(
            self._rules_for_versions([version.id for version in versions])
        )
        return versions

    def _document_scope(
        self,
        case: AuditCase,
        document_id: str | None,
        document_version_id: str | None,
        *,
        for_write: bool,
    ) -> tuple[str | None, str | None]:
        if document_id is None:
            if document_version_id is not None:
                raise RuleBindingError("RULE_DOCUMENT_SCOPE_REQUIRED")
            return None, None
        if for_write:
            from app.db.models import AuditWorkItem

            locked = self.db.exec(
                select(AuditWorkItem.id).where(
                    AuditWorkItem.tenant_id == case.tenant_id,
                    AuditWorkItem.audit_case_id == case.id,
                    AuditWorkItem.document_id == document_id,
                    AuditWorkItem.status.in_(["submitted", "approved"]),
                )
            ).first()
            if locked is not None:
                raise RuleBindingError("DOCUMENT_REVIEW_LOCKED")
        document = self.db.exec(
            select(AuditCaseDocument).where(
                AuditCaseDocument.id == document_id,
                AuditCaseDocument.tenant_id == case.tenant_id,
                AuditCaseDocument.audit_case_id == case.id,
            )
        ).first()
        if document is None:
            raise RuleBindingError("RULE_DOCUMENT_NOT_FOUND")
        if for_write and document.status == "archived":
            raise RuleBindingError("RULE_DOCUMENT_ARCHIVED")
        active_version_id = document.active_version_id
        if not active_version_id:
            raise RuleBindingError("RULE_DOCUMENT_VERSION_REQUIRED")
        selected_version_id = document_version_id or active_version_id
        version = self.db.exec(
            select(AuditCaseDocumentVersion).where(
                AuditCaseDocumentVersion.id == selected_version_id,
                AuditCaseDocumentVersion.tenant_id == case.tenant_id,
                AuditCaseDocumentVersion.audit_case_id == case.id,
                AuditCaseDocumentVersion.document_id == document.id,
            )
        ).first()
        if version is None:
            raise RuleBindingError("RULE_DOCUMENT_VERSION_REQUIRED")
        if version.id != active_version_id:
            raise RuleBindingError("RULE_DOCUMENT_VERSION_STALE")
        return document.id, version.id

    def _current_bindings(
        self,
        case: AuditCase,
        document_id: str | None = None,
        document_version_id: str | None = None,
    ) -> list[ProjectRuleBinding]:
        statement = select(ProjectRuleBinding).where(
            ProjectRuleBinding.tenant_id == case.tenant_id,
            ProjectRuleBinding.audit_case_id == case.id,
            ProjectRuleBinding.status == "current",
        )
        if document_id is None:
            statement = statement.where(ProjectRuleBinding.document_id.is_(None))
        else:
            statement = statement.where(ProjectRuleBinding.document_id == document_id)
            if document_version_id is not None:
                statement = statement.where(
                    ProjectRuleBinding.document_version_id == document_version_id
                )
        return list(
            self.db.exec(
                statement.order_by(ProjectRuleBinding.priority, ProjectRuleBinding.bound_at)
            ).all()
        )

    def _supersede_stale_document_bindings(
        self,
        case: AuditCase,
        document_id: str | None,
        document_version_id: str | None,
    ) -> None:
        if document_id is None or document_version_id is None:
            return
        now = utc_now()
        for binding in self._current_bindings(case, document_id):
            if binding.document_version_id == document_version_id:
                continue
            binding.status = "superseded"
            binding.updated_at = now
            self.db.add(binding)

    @staticmethod
    def _validate_source(selection_source: str) -> None:
        if selection_source not in {"recommended", "manual"}:
            raise RuleBindingError("INVALID_RULE_SELECTION_SOURCE")

    def bind_published_version(
        self,
        case: AuditCase,
        version_id: str,
        actor: User,
        selection_source: Literal["recommended", "manual"],
        *,
        document_id: str | None = None,
        document_version_id: str | None = None,
    ) -> ProjectRuleBinding:
        self._authorize_write(case, actor)
        self._validate_source(selection_source)
        document_id, document_version_id = self._document_scope(
            case, document_id, document_version_id, for_write=True
        )
        self._supersede_stale_document_bindings(case, document_id, document_version_id)
        version = self._published_version(case, version_id)
        current = self._current_bindings(case, document_id, document_version_id)
        for binding in current:
            if binding.rule_set_id != version.rule_set_id:
                continue
            if binding.rule_set_version_id == version.id:
                self.db.commit()
                return binding
            raise RuleBindingMigrationRequired()
        _validate_mandatory_rule_conflicts(
            self._rules_for_versions(
                [binding.rule_set_version_id for binding in current] + [version.id]
            )
        )
        binding = ProjectRuleBinding(
            tenant_id=case.tenant_id,
            audit_case_id=case.id,
            document_id=document_id,
            document_version_id=document_version_id,
            rule_set_id=version.rule_set_id,
            rule_set_version_id=version.id,
            selection_source=selection_source,
            status="current",
            priority=len(current),
            bound_by_user_id=actor.id,
        )
        self.db.add(binding)
        record_case_event(
            self.db,
            case=case,
            actor_user_id=actor.id,
            event_type="rule_binding_created",
            resource_type="project_rule_binding",
            resource_id=binding.id,
            metadata={
                "rule_set_version_id": binding.rule_set_version_id,
                "document_id": binding.document_id,
                "document_version_id": binding.document_version_id,
                "status": "current",
            },
        )
        self.db.commit()
        self.db.refresh(binding)
        return binding

    def replace_current_bindings(
        self,
        case: AuditCase,
        version_ids: list[str],
        actor: User,
        selection_source: Literal["recommended", "manual"],
        *,
        document_id: str | None = None,
        document_version_id: str | None = None,
    ) -> list[ProjectRuleBinding]:
        self._authorize_write(case, actor)
        self._validate_source(selection_source)
        document_id, document_version_id = self._document_scope(
            case, document_id, document_version_id, for_write=True
        )
        self._supersede_stale_document_bindings(case, document_id, document_version_id)
        versions = self._published_versions(case, version_ids)
        current = self._current_bindings(case, document_id, document_version_id)
        if current:
            if [row.rule_set_version_id for row in current] == version_ids:
                self.db.commit()
                return current
            raise RuleBindingMigrationRequired()
        bindings = [
            ProjectRuleBinding(
                tenant_id=case.tenant_id,
                audit_case_id=case.id,
                document_id=document_id,
                document_version_id=document_version_id,
                rule_set_id=version.rule_set_id,
                rule_set_version_id=version.id,
                selection_source=selection_source,
                status="current",
                priority=priority,
                bound_by_user_id=actor.id,
            )
            for priority, version in enumerate(versions)
        ]
        self.db.add_all(bindings)
        if bindings:
            record_case_event(
                self.db,
                case=case,
                actor_user_id=actor.id,
                event_type="rule_binding_created",
                resource_type="project_rule_binding",
                resource_id=bindings[0].id,
                metadata={
                    "rule_set_version_id": bindings[0].rule_set_version_id,
                    "document_id": bindings[0].document_id,
                    "document_version_id": bindings[0].document_version_id,
                    "status": "current",
                    "count": len(bindings),
                },
            )
        self.db.commit()
        for binding in bindings:
            self.db.refresh(binding)
        return bindings

    def list_current_bindings(
        self,
        case: AuditCase,
        actor: User,
        *,
        document_id: str | None = None,
        document_version_id: str | None = None,
    ) -> list[ProjectRuleBinding]:
        self._authorize_read(case, actor)
        document_id, document_version_id = self._document_scope(
            case, document_id, document_version_id, for_write=False
        )
        bindings = self._current_bindings(case, document_id, document_version_id)
        if not bindings:
            raise RuleBindingNotInitialized()
        return bindings

    def preview_migration(
        self,
        case: AuditCase,
        target_version_ids: list[str],
        actor: User,
        *,
        document_id: str | None = None,
        document_version_id: str | None = None,
    ) -> RuleMigrationPreview:
        self._authorize_write(case, actor)
        document_id, document_version_id = self._document_scope(
            case, document_id, document_version_id, for_write=True
        )
        current = self._current_bindings(case, document_id, document_version_id)
        if not current:
            raise RuleBindingNotInitialized()
        target_versions = self._published_versions(case, target_version_ids)
        old_rules = self._rules_for_versions(
            [binding.rule_set_version_id for binding in current]
        )
        new_rules = self._rules_for_versions([version.id for version in target_versions])
        old_by_key = {rule.rule_key: rule for rule in old_rules}
        new_by_key = {rule.rule_key: rule for rule in new_rules}
        old_keys = set(old_by_key)
        new_keys = set(new_by_key)
        added = sorted(new_keys - old_keys)
        removed = sorted(old_keys - new_keys)
        shared = old_keys & new_keys
        changed = sorted(
            key
            for key in shared
            if _rule_fingerprint(old_by_key[key]) != _rule_fingerprint(new_by_key[key])
        )
        unchanged = sorted(shared - set(changed))
        impacted_keys = set(added) | set(removed) | set(changed)
        impacted_rules = [
            rule
            for rule in [*old_rules, *new_rules]
            if rule.rule_key in impacted_keys
        ]
        return RuleMigrationPreview(
            added_rule_keys=added,
            removed_rule_keys=removed,
            changed_rule_keys=changed,
            unchanged_rule_keys=unchanged,
            impacted_information_domains=sorted(
                {
                    domain
                    for rule in impacted_rules
                    for domain in rule.information_domains_json
                }
            ),
            impacted_workflow_nodes=sorted(
                {
                    node
                    for rule in impacted_rules
                    for node in rule.workflow_nodes_json
                }
            ),
        )

    def migrate(
        self,
        case: AuditCase,
        target_version_ids: list[str],
        actor: User,
        reason: str,
        *,
        document_id: str | None = None,
        document_version_id: str | None = None,
    ) -> list[ProjectRuleBinding]:
        self._authorize_write(case, actor)
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise RuleBindingError("RULE_MIGRATION_REASON_REQUIRED")
        document_id, document_version_id = self._document_scope(
            case, document_id, document_version_id, for_write=True
        )
        current = self._current_bindings(case, document_id, document_version_id)
        if not current:
            raise RuleBindingNotInitialized()
        versions = self._published_versions(case, target_version_ids)
        if [row.rule_set_version_id for row in current] == target_version_ids:
            return current

        old_by_rule_set = {binding.rule_set_id: binding for binding in current}
        now = utc_now()
        for binding in current:
            binding.status = "superseded"
            binding.updated_at = now
            self.db.add(binding)
        bindings = [
            ProjectRuleBinding(
                tenant_id=case.tenant_id,
                audit_case_id=case.id,
                document_id=document_id,
                document_version_id=document_version_id,
                rule_set_id=version.rule_set_id,
                rule_set_version_id=version.id,
                selection_source="manual",
                status="current",
                priority=priority,
                bound_by_user_id=actor.id,
                bound_at=now,
                supersedes_binding_id=(
                    old_by_rule_set[version.rule_set_id].id
                    if version.rule_set_id in old_by_rule_set
                    else None
                ),
                created_at=now,
                updated_at=now,
            )
            for priority, version in enumerate(versions)
        ]
        self.db.add_all(bindings)
        migrated_version_id = (
            bindings[0].rule_set_version_id
            if bindings
            else target_version_ids[0]
            if target_version_ids
            else ""
        )
        record_case_event(
            self.db,
            case=case,
            actor_user_id=actor.id,
            event_type="rule_binding_migrated",
            resource_type="project_rule_binding",
            resource_id=bindings[0].id if bindings else case.id,
            metadata={
                "rule_set_version_id": migrated_version_id,
                "document_id": document_id,
                "document_version_id": document_version_id,
                "status": "migrated",
                "count": len(bindings),
            },
        )
        self.db.commit()
        for binding in bindings:
            self.db.refresh(binding)
        return bindings


class RuleLibraryService:
    def __init__(self, db: Session):
        self.db = db

    def _declared_field_keys(self, tenant_id: str) -> frozenset[str]:
        rows = self.db.exec(
            select(ProjectDataFieldDefinition.field_key).where(
                ProjectDataFieldDefinition.tenant_id == tenant_id,
                ProjectDataFieldDefinition.status == "active",
            )
        ).all()
        return frozenset(rows)

    def _rule_set_for_admin(self, rule_set: RuleSet, actor: User) -> RuleSet:
        _ensure_tenant_admin(actor, rule_set.tenant_id)
        persisted = self.db.get(RuleSet, rule_set.id)
        if persisted is None or persisted.tenant_id != actor.tenant_id:
            raise RuleAccessDenied()
        return persisted

    def _version_for_admin(self, version_id: str, actor: User) -> RuleSetVersion:
        if actor.role != "admin":
            raise RuleAccessDenied()
        version = self.db.get(RuleSetVersion, version_id)
        if version is None or version.tenant_id != actor.tenant_id:
            raise RuleAccessDenied()
        return version

    def _rules_for_version(self, version_id: str) -> list[RuleDefinition]:
        return list(
            self.db.exec(
                select(RuleDefinition)
                .where(RuleDefinition.rule_set_version_id == version_id)
                .order_by(RuleDefinition.rule_key, RuleDefinition.sequence)
            ).all()
        )

    def _persist_rules(
        self,
        version: RuleSetVersion,
        rules: list[RuleDefinitionCreate],
    ) -> None:
        for rule in rules:
            self.db.add(
                RuleDefinition(
                    tenant_id=version.tenant_id,
                    rule_set_version_id=version.id,
                    rule_key=rule.rule_key,
                    name=rule.name,
                    description=rule.description,
                    workflow_nodes_json=list(rule.workflow_nodes),
                    information_domains_json=list(rule.information_domains),
                    document_types_json=list(rule.document_types),
                    field_keys_json=list(rule.field_keys),
                    execution_level=rule.execution_level,
                    execution_method=rule.execution_method,
                    condition_json=dict(rule.condition),
                    input_requirements_json=list(rule.input_requirements),
                    evidence_requirements_json=list(rule.evidence_requirements),
                    source_refs_json=list(rule.source_refs),
                    sequence=rule.sequence,
                    enabled=rule.enabled,
                )
            )

    def _compare_and_swap_draft(
        self,
        version: RuleSetVersion,
        **values: object,
    ) -> None:
        result = self.db.exec(
            update(RuleSetVersion)
            .where(
                RuleSetVersion.id == version.id,
                RuleSetVersion.tenant_id == version.tenant_id,
                RuleSetVersion.status == "draft",
                RuleSetVersion.content_sha256 == version.content_sha256,
                RuleSetVersion.updated_at == version.updated_at,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self.db.rollback()
            raise RuleVersionImmutableError()

    def create_rule_set(self, actor: User, request: RuleSetCreate) -> RuleSet:
        _ensure_tenant_admin(actor, request.tenant_id)
        if _STABLE_KEY.fullmatch(request.key) is None:
            raise RuleValidationError("INVALID_RULE_SET_KEY")
        existing = self.db.exec(
            select(RuleSet).where(
                RuleSet.tenant_id == request.tenant_id,
                RuleSet.key == request.key,
            )
        ).first()
        if existing is not None:
            raise RuleValidationError("RULE_SET_KEY_EXISTS")
        rule_set = RuleSet(
            tenant_id=request.tenant_id,
            key=request.key,
            name=request.name,
            description=request.description,
            management_systems_json=list(request.management_systems),
            audit_types_json=list(request.audit_types),
            business_domain=request.business_domain,
            status="active",
        )
        self.db.add(rule_set)
        self.db.commit()
        self.db.refresh(rule_set)
        return rule_set

    def create_draft_version(
        self,
        rule_set: RuleSet,
        actor: User,
        rules: list[RuleDefinitionCreate],
    ) -> RuleSetVersion:
        persisted_rule_set = self._rule_set_for_admin(rule_set, actor)
        declared = self._declared_field_keys(persisted_rule_set.tenant_id)
        validate_rule_set_with_fields(rules, declared)
        latest = self.db.exec(
            select(func.max(RuleSetVersion.version)).where(
                RuleSetVersion.tenant_id == persisted_rule_set.tenant_id,
                RuleSetVersion.rule_set_id == persisted_rule_set.id,
            )
        ).one()
        version = RuleSetVersion(
            tenant_id=persisted_rule_set.tenant_id,
            rule_set_id=persisted_rule_set.id,
            version=(latest or 0) + 1,
            status="draft",
            content_sha256=fingerprint_rule_set_version(rules),
        )
        self.db.add(version)
        self._persist_rules(version, rules)
        self.db.commit()
        self.db.refresh(version)
        return version

    def validate_version(self, version_id: str, actor: User) -> list[str]:
        version = self._version_for_admin(version_id, actor)
        declared = self._declared_field_keys(version.tenant_id)
        rules = [_rule_definition_create(row) for row in self._rules_for_version(version.id)]
        errors: list[str] = []
        seen: set[str] = set()
        for rule in rules:
            if rule.rule_key in seen and "DUPLICATE_RULE_KEY" not in errors:
                errors.append("DUPLICATE_RULE_KEY")
            seen.add(rule.rule_key)
            try:
                validate_rule_definition_with_fields(rule, declared)
            except RuleValidationError as exc:
                if exc.code not in errors:
                    errors.append(exc.code)
        return errors

    def publish_version(self, version_id: str, actor: User) -> RuleSetVersion:
        version = self._version_for_admin(version_id, actor)
        if version.status != "draft":
            raise RuleVersionImmutableError()
        declared = self._declared_field_keys(version.tenant_id)
        rules = [_rule_definition_create(row) for row in self._rules_for_version(version.id)]
        validate_rule_set_with_fields(rules, declared)
        now = utc_now()
        content_sha256 = fingerprint_rule_set_version(rules)
        self._compare_and_swap_draft(
            version,
            content_sha256=content_sha256,
            status="published",
            published_by_user_id=actor.id,
            published_at=now,
            updated_at=now,
        )
        self.db.commit()
        self.db.refresh(version)
        return version

    def replace_rules(
        self,
        version_id: str,
        actor: User,
        rules: list[RuleDefinitionCreate],
    ) -> RuleSetVersion:
        version = self._version_for_admin(version_id, actor)
        if version.status != "draft":
            raise RuleVersionImmutableError()
        declared = self._declared_field_keys(version.tenant_id)
        validate_rule_set_with_fields(rules, declared)
        existing_rules = self._rules_for_version(version.id)
        now = utc_now()
        if now <= version.updated_at:
            now = version.updated_at + timedelta(microseconds=1)
        content_sha256 = fingerprint_rule_set_version(rules)
        self._compare_and_swap_draft(
            version,
            content_sha256=content_sha256,
            updated_at=now,
        )
        for existing in existing_rules:
            self.db.delete(existing)
        self.db.flush()
        self._persist_rules(version, rules)
        self.db.commit()
        self.db.refresh(version)
        return version

    def evaluate_project(
        self,
        case: AuditCase,
        actor: User,
        context: RuleEvaluationContext,
        *,
        document_id: str | None = None,
        document_version_id: str | None = None,
    ) -> list[RuleEvaluation]:
        """Evaluate the exact rule versions currently pinned to a project.

        This deliberately does not invoke a model.  Model-assisted rules are
        stored as indeterminate results so a later workflow phase can add a
        reviewed/model-backed decision without changing the project snapshot.
        """

        binding_service = RuleBindingService(self.db)
        bindings = binding_service.list_current_bindings(
            case,
            actor,
            document_id=document_id,
            document_version_id=document_version_id,
        )
        version_ids = [binding.rule_set_version_id for binding in bindings]
        rules_by_version = {
            version_id: binding_service._rules_for_versions([version_id])
            for version_id in version_ids
        }
        evaluations: list[RuleEvaluation] = []
        for binding in bindings:
            for rule in rules_by_version[binding.rule_set_version_id]:
                if not rule.enabled:
                    continue
                result = evaluate_rule_without_model(rule, context)
                rule_field_keys = set(rule.field_keys_json or [])
                condition_field_key = rule.condition_json.get("field_key")
                if isinstance(condition_field_key, str):
                    rule_field_keys.add(condition_field_key)
                input_revision = max(
                    (
                        context.project_field_revisions.get(field_key, 0)
                        for field_key in rule_field_keys
                    ),
                    default=0,
                )
                evaluations.append(
                    RuleEvaluation(
                        tenant_id=case.tenant_id,
                        audit_case_id=case.id,
                        document_id=binding.document_id,
                        document_version_id=binding.document_version_id,
                        rule_set_version_id=binding.rule_set_version_id,
                        rule_definition_id=rule.id,
                        workflow_node=context.workflow_node
                        or (rule.workflow_nodes_json[0] if rule.workflow_nodes_json else ""),
                        information_domain=context.information_domain
                        or (
                            rule.information_domains_json[0]
                            if rule.information_domains_json
                            else ""
                        ),
                        target_ref=context.target_ref or "",
                        input_revision=input_revision,
                        status=result.status,
                        result_json=result.model_dump(mode="json"),
                        evidence_refs_json=[dict(item) for item in result.evidence_refs],
                        executor_type=rule.execution_method,
                        executor_version="phase1",
                    )
                )
        self.db.add_all(evaluations)
        if evaluations:
            record_case_event(
                self.db,
                case=case,
                actor_user_id=actor.id,
                event_type="rule_evaluation_completed",
                resource_type="rule_evaluation",
                resource_id=evaluations[0].id,
                metadata={
                    "rule_evaluation_id": evaluations[0].id,
                    "status": "completed",
                    "count": len(evaluations),
                },
            )
        self.db.commit()
        for evaluation in evaluations:
            self.db.refresh(evaluation)
        return evaluations
