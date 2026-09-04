from __future__ import annotations

from typing import Any

import pytest

from app.rules.schema import RuleDefinitionCreate, RuleValidationError
from app.rules.validation import (
    fingerprint_rule_set_version,
    validate_rule_definition,
    validate_rule_set,
)


def _rule(**overrides: Any) -> RuleDefinitionCreate:
    values: dict[str, Any] = {
        "rule_key": "scope.required",
        "name": "认证范围必填",
        "description": "认证范围必须有可核验内容",
        "workflow_nodes": ["collect"],
        "information_domains": ["certification_project"],
        "document_types": ["audit_plan"],
        "field_keys": ["certification_project.scope"],
        "execution_level": "warning",
        "execution_method": "deterministic",
        "condition": {
            "operator": "required",
            "field_key": "certification_project.scope",
        },
        "input_requirements": [],
        "evidence_requirements": [{"kind": "project_field"}],
        "source_refs": [
            {
                "knowledge_base_version_id": "kbv-1",
                "document_id": "doc-1",
                "section": "7.3",
            }
        ],
        "sequence": 0,
        "enabled": True,
    }
    values.update(overrides)
    return RuleDefinitionCreate(**values)


def test_mandatory_model_assisted_rule_is_rejected() -> None:
    rule = _rule(execution_level="mandatory", execution_method="model_assisted")
    with pytest.raises(
        RuleValidationError, match="MANDATORY_RULE_MUST_BE_DETERMINISTIC"
    ):
        validate_rule_definition(rule)


def test_mandatory_rule_requires_source_and_evidence() -> None:
    rule = _rule(
        execution_level="mandatory",
        execution_method="deterministic",
        source_refs=[],
        evidence_requirements=[],
    )
    with pytest.raises(RuleValidationError, match="MANDATORY_RULE_SOURCE_REQUIRED"):
        validate_rule_definition(rule)


def test_rule_keys_are_unique_within_a_version() -> None:
    with pytest.raises(RuleValidationError, match="DUPLICATE_RULE_KEY"):
        validate_rule_set(
            [_rule(rule_key="scope.required"), _rule(rule_key="scope.required")]
        )


def test_fingerprint_is_order_independent_after_sequence_normalization() -> None:
    first = fingerprint_rule_set_version(
        [_rule(rule_key="a", sequence=10), _rule(rule_key="b", sequence=5)]
    )
    second = fingerprint_rule_set_version(
        [_rule(rule_key="b", sequence=5), _rule(rule_key="a", sequence=10)]
    )
    assert first == second


@pytest.mark.parametrize(
    "rule_key",
    ["", "Scope.required", "scope/required", "a" * 161],
)
def test_rule_key_must_be_a_bounded_stable_key(rule_key: str) -> None:
    with pytest.raises(RuleValidationError, match="INVALID_RULE_KEY"):
        validate_rule_definition(_rule(rule_key=rule_key))


@pytest.mark.parametrize(
    ("overrides", "error_code"),
    [
        ({"workflow_nodes": []}, "RULE_WORKFLOW_NODE_REQUIRED"),
        ({"information_domains": []}, "RULE_INFORMATION_DOMAIN_REQUIRED"),
    ],
)
def test_rule_requires_workflow_node_and_information_domain(
    overrides: dict[str, Any], error_code: str
) -> None:
    with pytest.raises(RuleValidationError, match=error_code):
        validate_rule_definition(_rule(**overrides))


def test_unknown_field_reference_is_rejected() -> None:
    with pytest.raises(RuleValidationError, match="UNKNOWN_FIELD_KEY"):
        validate_rule_definition(
            _rule(
                field_keys=["tenant.undeclared"],
                condition={"operator": "required", "field_key": "tenant.undeclared"},
            )
        )


def test_unknown_field_reference_in_requirements_is_rejected() -> None:
    with pytest.raises(RuleValidationError, match="UNKNOWN_FIELD_KEY"):
        validate_rule_definition(
            _rule(input_requirements=[{"field_key": "tenant.undeclared"}])
        )


def test_unsupported_condition_operator_is_rejected() -> None:
    with pytest.raises(RuleValidationError, match="UNSUPPORTED_RULE_OPERATOR"):
        validate_rule_definition(_rule(condition={"operator": "delete"}))


def test_source_reference_requires_complete_provenance() -> None:
    with pytest.raises(RuleValidationError, match="INVALID_RULE_SOURCE_REF"):
        validate_rule_definition(
            _rule(source_refs=[{"knowledge_base_version_id": "kbv-1"}])
        )


def test_internal_policy_reference_is_valid_provenance() -> None:
    validate_rule_definition(_rule(source_refs=[{"internal_policy_ref": "POL-001"}]))


def test_model_assisted_results_are_limited_to_human_review_outcomes() -> None:
    with pytest.raises(RuleValidationError, match="MODEL_ASSISTED_RESULT_INVALID"):
        validate_rule_definition(
            _rule(
                execution_method="model_assisted",
                condition={
                    "operator": "required",
                    "field_key": "certification_project.scope",
                    "allowed_results": ["pass", "blocked"],
                },
            )
        )


def test_model_assisted_rule_cannot_declare_direct_blocking() -> None:
    with pytest.raises(
        RuleValidationError, match="MODEL_ASSISTED_DIRECT_BLOCKING_FORBIDDEN"
    ):
        validate_rule_definition(
            _rule(
                execution_method="model_assisted",
                condition={
                    "operator": "required",
                    "field_key": "certification_project.scope",
                    "blocking": True,
                },
            )
        )


@pytest.mark.parametrize(
    ("overrides", "error_code"),
    [
        ({"sequence": -1}, "INVALID_RULE_SEQUENCE"),
        ({"enabled": "yes"}, "INVALID_RULE_ENABLED"),
    ],
)
def test_sequence_and_enabled_are_strictly_validated(
    overrides: dict[str, Any], error_code: str
) -> None:
    with pytest.raises(RuleValidationError, match=error_code):
        validate_rule_definition(_rule(**overrides))


def test_fingerprint_ignores_secret_metadata() -> None:
    first = fingerprint_rule_set_version(
        [_rule(input_requirements=[{"kind": "api", "secret": "first"}])]
    )
    second = fingerprint_rule_set_version(
        [_rule(input_requirements=[{"kind": "api", "secret": "second"}])]
    )
    assert first == second
