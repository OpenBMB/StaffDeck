from __future__ import annotations

from typing import Any

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    AuditCase,
    RuleEvaluation,
    User,
)
from app.project_data.schema import RuleEvaluationContext
from app.rules.schema import (
    RuleDefinitionCreate,
    RuleSetCreate,
)
from app.rules.service import (
    RuleBindingError,
    RuleBindingMigrationRequired,
    RuleBindingService,
    RuleEvaluationError,
    RuleLibraryService,
    evaluate_deterministic_rule,
    evaluate_rule_without_model,
)


def _user(user_id: str, tenant_id: str, role: str = "member") -> User:
    return User(
        id=user_id,
        tenant_id=tenant_id,
        username=user_id,
        role=role,
        source="web",
        password_hash="test",
    )


def _rule_definition(**overrides: Any) -> RuleDefinitionCreate:
    values: dict[str, Any] = {
        "rule_key": "scope.required",
        "name": "认证范围必填",
        "description": "认证范围必须有可核验内容",
        "workflow_nodes": ["collect"],
        "information_domains": ["certification_project"],
        "document_types": ["audit_plan"],
        "field_keys": ["certification_project.scope"],
        "execution_level": "mandatory",
        "execution_method": "deterministic",
        "condition": {
            "operator": "required",
            "field_key": "certification_project.scope",
        },
        "input_requirements": [],
        "evidence_requirements": [{"kind": "project_field"}],
        "source_refs": [{"internal_policy_ref": "POL-001"}],
        "sequence": 0,
        "enabled": True,
    }
    values.update(overrides)
    return RuleDefinitionCreate(**values)


@pytest.fixture
def binding_context(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'rule-binding.db'}")
    SQLModel.metadata.create_all(engine)
    db = Session(engine, expire_on_commit=False)
    admin = _user("admin-1", "tenant_demo", role="admin")
    reviewer = _user("reviewer-1", "tenant_demo", role="member")
    case = AuditCase(
        id="case-1",
        tenant_id="tenant_demo",
        owner_user_id=reviewer.id,
        member_user_ids_json=[reviewer.id],
        organization_name="甲公司",
        report_type="认证审核",
    )
    db.add_all([admin, reviewer, case])
    db.commit()
    rule_set = RuleLibraryService(db).create_rule_set(
        admin,
        RuleSetCreate(
            tenant_id="tenant_demo",
            key="energy.audit",
            name="能源审核规则",
            management_systems=["EnMS"],
            audit_types=["recertification"],
            business_domain="energy",
        ),
    )
    draft = RuleLibraryService(db).create_draft_version(
        rule_set, admin, [_rule_definition()]
    )
    published = RuleLibraryService(db).publish_version(draft.id, admin)
    yield db, engine, admin, reviewer, case, rule_set, draft, published
    db.close()


def _publish_next_version(db, admin, rule_set, **rule_overrides):
    draft = RuleLibraryService(db).create_draft_version(
        rule_set,
        admin,
        [_rule_definition(**rule_overrides)],
    )
    return RuleLibraryService(db).publish_version(draft.id, admin)


def _context(**overrides: Any) -> RuleEvaluationContext:
    values: dict[str, Any] = {
        "project_fields": {},
        "evidence_refs": [],
        "workflow_node": "collect",
        "information_domain": "certification_project",
        "target_ref": None,
    }
    values.update(overrides)
    return RuleEvaluationContext(**values)


def test_project_can_bind_only_published_rule_versions(binding_context) -> None:
    db, _engine, admin, _reviewer, case, rule_set, _draft, published = binding_context
    unpublished = RuleLibraryService(db).create_draft_version(
        rule_set, admin, [_rule_definition(name="未发布规则")]
    )
    with pytest.raises(RuleBindingError, match="PUBLISHED_RULE_VERSION_REQUIRED"):
        RuleBindingService(db).bind_published_version(
            case, unpublished.id, admin, "manual"
        )

    binding = RuleBindingService(db).bind_published_version(
        case, published.id, admin, "manual"
    )
    assert binding.rule_set_version_id == published.id
    assert binding.status == "current"


def test_existing_binding_is_not_silently_replaced(binding_context) -> None:
    db, _engine, admin, _reviewer, case, rule_set, _draft, first = binding_context
    second = _publish_next_version(db, admin, rule_set, name="第二版")
    RuleBindingService(db).bind_published_version(case, first.id, admin, "manual")

    with pytest.raises(
        RuleBindingMigrationRequired,
        match="RULE_MIGRATION_CONFIRMATION_REQUIRED",
    ):
        RuleBindingService(db).bind_published_version(case, second.id, admin, "manual")


def test_migration_creates_new_binding_and_preserves_old_snapshot(binding_context) -> None:
    db, _engine, admin, _reviewer, case, rule_set, _draft, first = binding_context
    second = _publish_next_version(
        db,
        admin,
        rule_set,
        name="第二版",
        condition={
            "operator": "equals",
            "field_key": "certification_project.scope",
            "value": "限定范围",
        },
    )
    binding_service = RuleBindingService(db)
    old = binding_service.bind_published_version(case, first.id, admin, "manual")

    preview = binding_service.preview_migration(case, [second.id], admin)
    assert preview.changed_rule_keys == ["scope.required"]

    current = binding_service.migrate(case, [second.id], admin, "规则修订")
    assert current[0].rule_set_version_id == second.id
    db.refresh(old)
    assert old.status == "superseded"
    assert old.rule_set_version_id == first.id


def test_migration_requires_reason_and_does_not_mutate_on_failure(binding_context) -> None:
    db, _engine, admin, _reviewer, case, rule_set, _draft, first = binding_context
    second = _publish_next_version(db, admin, rule_set, name="第二版")
    binding_service = RuleBindingService(db)
    old = binding_service.bind_published_version(case, first.id, admin, "manual")

    with pytest.raises(RuleBindingError, match="RULE_MIGRATION_REASON_REQUIRED"):
        binding_service.migrate(case, [second.id], admin, "  ")

    db.refresh(old)
    assert old.status == "current"


def test_cross_tenant_published_version_cannot_be_bound(binding_context) -> None:
    db, _engine, _admin, _reviewer, case, _rule_set, _draft, published = binding_context
    foreign_admin = _user("foreign-admin", "tenant_other", role="admin")
    db.add(foreign_admin)
    db.commit()
    with pytest.raises(RuleBindingError, match="RULE_TENANT_ACCESS_DENIED"):
        RuleBindingService(db).bind_published_version(
            case, published.id, foreign_admin, "manual"
        )


def test_conflicting_mandatory_rules_cannot_be_resolved_by_priority(binding_context) -> None:
    db, _engine, admin, _reviewer, case, _rule_set, _draft, first = binding_context
    second_set = RuleLibraryService(db).create_rule_set(
        admin,
        RuleSetCreate(
            tenant_id="tenant_demo",
            key="energy.audit.secondary",
            name="第二规则集",
        ),
    )
    second_draft = RuleLibraryService(db).create_draft_version(
        second_set,
        admin,
        [
            _rule_definition(
                rule_key="scope.alternate",
                condition={
                    "operator": "equals",
                    "field_key": "certification_project.scope",
                    "value": "范围 A",
                },
            ),
            _rule_definition(
                rule_key="scope.conflict",
                condition={
                    "operator": "equals",
                    "field_key": "certification_project.scope",
                    "value": "范围 B",
                },
            )
        ],
    )
    second = RuleLibraryService(db).publish_version(second_draft.id, admin)
    binding_service = RuleBindingService(db)
    binding_service.bind_published_version(case, first.id, admin, "manual")

    with pytest.raises(RuleBindingError, match="CONFLICTING_MANDATORY_RULES"):
        binding_service.migrate(case, [first.id, second.id], admin, "增加第二规则集")


def test_unbound_project_reports_stable_binding_error(binding_context) -> None:
    db, _engine, _admin, reviewer, case, _rule_set, _draft, _published = binding_context
    with pytest.raises(RuleBindingError, match="RULE_BINDING_NOT_INITIALIZED"):
        RuleLibraryService(db).evaluate_project(case, reviewer, _context())


def test_required_rule_fails_when_field_is_missing() -> None:
    rule = _rule_definition()
    result = evaluate_deterministic_rule(
        rule,
        _context(),
    )
    assert result.status == "failed"
    assert result.blocking is True


def test_warning_rule_does_not_block() -> None:
    rule = _rule_definition(execution_level="warning")
    result = evaluate_deterministic_rule(rule, _context())
    assert result.status == "warning"
    assert result.blocking is False


def test_model_assisted_result_is_human_review_only() -> None:
    rule = _rule_definition(
        execution_level="guidance",
        execution_method="model_assisted",
        condition={"operator": "required", "allowed_results": ["pass", "fail", "indeterminate"]},
    )
    result = evaluate_rule_without_model(rule, _context())
    assert result.status == "indeterminate"
    assert result.blocking is False


@pytest.mark.parametrize(
    ("operator", "condition", "project_fields", "evidence_refs", "expected"),
    [
        (
            "equals",
            {"operator": "equals", "field_key": "x", "value": "A"},
            {"x": "A"},
            [],
            "passed",
        ),
        (
            "not_equals",
            {"operator": "not_equals", "field_key": "x", "value": "A"},
            {"x": "B"},
            [],
            "passed",
        ),
        (
            "in",
            {"operator": "in", "field_key": "x", "values": ["A", "B"]},
            {"x": "B"},
            [],
            "passed",
        ),
        (
            "gte",
            {"operator": "gte", "field_key": "x", "value": 3},
            {"x": 4},
            [],
            "passed",
        ),
        (
            "lte",
            {"operator": "lte", "field_key": "x", "value": 3},
            {"x": 2},
            [],
            "passed",
        ),
        (
            "matches",
            {"operator": "matches", "field_key": "x", "pattern": r"^A-\d+$"},
            {"x": "A-12"},
            [],
            "passed",
        ),
        (
            "has_evidence",
            {"operator": "has_evidence"},
            {},
            [{"source_ref": "page:1"}],
            "passed",
        ),
    ],
)
def test_deterministic_evaluator_supports_declared_operators(
    operator, condition, project_fields, evidence_refs, expected
) -> None:
    rule = _rule_definition(
        rule_key=f"operator.{operator}",
        execution_level="guidance",
        field_keys=[],
        condition=condition,
    )
    result = evaluate_deterministic_rule(
        rule,
        _context(project_fields=project_fields, evidence_refs=evidence_refs),
    )
    assert result.status == expected
    assert result.blocking is False


def test_unsupported_evaluation_operator_is_stable() -> None:
    rule = _rule_definition(condition={"operator": "contains", "field_key": "x"})
    with pytest.raises(RuleEvaluationError, match="UNSUPPORTED_RULE_OPERATOR"):
        evaluate_deterministic_rule(rule, _context())


def test_evaluate_project_persists_revision_and_evidence(binding_context) -> None:
    db, _engine, admin, reviewer, case, _rule_set, _draft, published = binding_context
    RuleBindingService(db).bind_published_version(case, published.id, admin, "manual")
    context = _context(
        project_fields={"certification_project.scope": "能源管理"},
        project_field_revisions={"certification_project.scope": 7},
        evidence_refs=[{"source_ref": "material-1:page-2"}],
        target_ref="section-4",
    )

    evaluations = RuleLibraryService(db).evaluate_project(case, reviewer, context)

    assert len(evaluations) == 1
    assert evaluations[0].status == "passed"
    assert evaluations[0].input_revision == 7
    assert evaluations[0].evidence_refs_json == [{"source_ref": "material-1:page-2"}]
    persisted = db.exec(
        select(RuleEvaluation).where(RuleEvaluation.id == evaluations[0].id)
    ).one()
    assert persisted.rule_set_version_id == published.id


def test_current_bindings_are_visible_only_to_authorized_project_users(binding_context) -> None:
    db, _engine, admin, reviewer, case, _rule_set, _draft, published = binding_context
    RuleBindingService(db).bind_published_version(case, published.id, admin, "manual")
    bindings = RuleBindingService(db).list_current_bindings(case, reviewer)
    assert len(bindings) == 1
    assert bindings[0].status == "current"
