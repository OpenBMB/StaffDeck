from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import update
from sqlmodel import Session, func, select

from app.db.models import (
    ProjectDataFieldDefinition,
    RuleDefinition,
    RuleSet,
    RuleSetVersion,
    User,
    utc_now,
)
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
