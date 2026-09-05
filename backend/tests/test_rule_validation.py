from __future__ import annotations

from typing import Any

import pytest

import app.rules.validation as rule_validation
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


def test_nested_condition_operator_is_validated() -> None:
    with pytest.raises(RuleValidationError, match="UNSUPPORTED_RULE_OPERATOR"):
        validate_rule_definition(
            _rule(
                condition={
                    "operator": "equals",
                    "conditions": [
                        {
                            "operator": "exec",
                            "field_key": "certification_project.scope",
                        }
                    ],
                }
            )
        )


def test_nested_allowed_condition_operators_are_accepted() -> None:
    validate_rule_definition(
        _rule(
            condition={
                "operator": "equals",
                "conditions": [
                    {
                        "operator": "has_evidence",
                        "field_key": "certification_project.scope",
                    }
                ],
            }
        )
    )


def test_nested_nonstring_condition_operator_has_stable_validation_error() -> None:
    with pytest.raises(RuleValidationError, match="UNSUPPORTED_RULE_OPERATOR"):
        validate_rule_definition(
            _rule(
                condition={
                    "operator": "equals",
                    "conditions": [{"operator": ["required"]}],
                }
            )
        )


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


@pytest.mark.parametrize(
    "condition",
    [
        {
            "operator": "required",
            "field_key": "certification_project.scope",
        },
        {
            "operator": "required",
            "field_key": "certification_project.scope",
            "allowed_results": [],
        },
        {
            "operator": "required",
            "field_key": "certification_project.scope",
            "allowed_results": "pass",
        },
    ],
)
def test_model_assisted_rule_requires_explicit_nonempty_result_contract(
    condition: dict[str, Any],
) -> None:
    with pytest.raises(RuleValidationError, match="MODEL_ASSISTED_RESULT_INVALID"):
        validate_rule_definition(
            _rule(execution_method="model_assisted", condition=condition)
        )


@pytest.mark.parametrize("blocking_value", [True, 1, "yes", ["block"]])
def test_model_assisted_rule_cannot_declare_truthy_direct_blocking(
    blocking_value: Any,
) -> None:
    with pytest.raises(
        RuleValidationError, match="MODEL_ASSISTED_DIRECT_BLOCKING_FORBIDDEN"
    ):
        validate_rule_definition(
            _rule(
                execution_method="model_assisted",
                condition={
                    "operator": "required",
                    "field_key": "certification_project.scope",
                    "blocking": blocking_value,
                    "allowed_results": ["pass", "fail", "indeterminate"],
                },
            )
        )


def test_model_assisted_rule_rejects_nested_direct_blocking_metadata() -> None:
    with pytest.raises(
        RuleValidationError, match="MODEL_ASSISTED_DIRECT_BLOCKING_FORBIDDEN"
    ):
        validate_rule_definition(
            _rule(
                execution_method="model_assisted",
                condition={
                    "operator": "required",
                    "field_key": "certification_project.scope",
                    "allowed_results": ["pass", "fail", "indeterminate"],
                    "metadata": {"directBlocking": "enabled"},
                },
            )
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"input_requirements": [{"metadata": {"blocking": "yes"}}]},
        {"evidence_requirements": [{"metadata": {"direct_blocking": 1}}]},
        {"source_refs": [{"internal_policy_ref": "POL-001", "isBlocking": [1]}]},
    ],
)
def test_model_assisted_rule_rejects_direct_blocking_in_any_metadata(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(
        RuleValidationError, match="MODEL_ASSISTED_DIRECT_BLOCKING_FORBIDDEN"
    ):
        validate_rule_definition(
            _rule(
                execution_method="model_assisted",
                condition={
                    "operator": "required",
                    "field_key": "certification_project.scope",
                    "allowed_results": ["pass", "fail", "indeterminate"],
                },
                **overrides,
            )
        )


def test_model_assisted_rule_allows_nonblocking_explanatory_metadata() -> None:
    validate_rule_definition(
        _rule(
            execution_method="model_assisted",
            condition={
                "operator": "required",
                "field_key": "certification_project.scope",
                "allowed_results": ["pass", "fail", "indeterminate"],
                "metadata": {"nonBlockingReason": "human review only"},
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


@pytest.mark.parametrize(
    ("overrides", "error_code"),
    [
        ({"workflow_nodes": ["collect", " "]}, "INVALID_RULE_WORKFLOW_NODE"),
        (
            {"information_domains": ["certification_project", "\t"]},
            "INVALID_RULE_INFORMATION_DOMAIN",
        ),
        ({"evidence_requirements": [{}]}, "INVALID_RULE_EVIDENCE_REQUIREMENT"),
        (
            {"evidence_requirements": [{"kind": "  "}]},
            "INVALID_RULE_EVIDENCE_REQUIREMENT",
        ),
    ],
)
def test_rule_collection_items_must_be_nonblank(
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


@pytest.mark.parametrize(
    "metadata_key",
    [
        "created_at",
        "updatedAt",
        "timestamp",
        "drafted_by_user_id",
        "draftedByUserId",
        "client_secret",
        "clientSecret",
        "API-TOKEN",
    ],
)
def test_fingerprint_ignores_nonsemantic_metadata_variants(
    metadata_key: str,
) -> None:
    first = fingerprint_rule_set_version(
        [
            _rule(
                input_requirements=[
                    {"kind": "api", "metadata": {metadata_key: "first-value"}}
                ]
            )
        ]
    )
    second = fingerprint_rule_set_version(
        [
            _rule(
                input_requirements=[
                    {"kind": "api", "metadata": {metadata_key: "second-value"}}
                ]
            )
        ]
    )
    assert first == second


def _rule_with_nested_metadata(
    container_name: str,
    metadata_key: str,
    metadata_value: str,
) -> RuleDefinitionCreate:
    nested_metadata = {"metadata": {"details": {metadata_key: metadata_value}}}
    if container_name == "condition":
        return _rule(
            condition={
                "operator": "required",
                "field_key": "certification_project.scope",
                **nested_metadata,
            }
        )
    if container_name == "input_requirements":
        return _rule(input_requirements=[{"kind": "api", **nested_metadata}])
    if container_name == "evidence_requirements":
        return _rule(
            evidence_requirements=[{"kind": "project_field", **nested_metadata}]
        )
    return _rule(
        source_refs=[
            {
                "knowledge_base_version_id": "kbv-1",
                "document_id": "doc-1",
                "section": "7.3",
                **nested_metadata,
            }
        ]
    )


@pytest.mark.parametrize(
    "metadata_key",
    [
        "secret_value",
        "Secret-Value",
        "password_hash",
        "credential_blob",
        "access-token-value",
        "apiKeyMaterial",
        "private_key_pem",
        "created_at_epoch",
        "updatedAtEpoch",
        "published-time-ms",
        "drafted_timestamp_epoch",
        "draft_user_id",
        "Draft-User-ID",
        "author_identity",
        "user-name",
    ],
)
@pytest.mark.parametrize(
    "container_name",
    ["condition", "input_requirements", "evidence_requirements", "source_refs"],
)
def test_fingerprint_ignores_nested_sensitive_lifecycle_and_author_metadata(
    container_name: str,
    metadata_key: str,
) -> None:
    first = fingerprint_rule_set_version(
        [_rule_with_nested_metadata(container_name, metadata_key, "first-value")]
    )
    second = fingerprint_rule_set_version(
        [_rule_with_nested_metadata(container_name, metadata_key, "second-value")]
    )
    assert first == second


def test_fingerprint_never_hashes_sensitive_metadata_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hashed_payloads: list[bytes] = []
    real_sha256 = rule_validation.hashlib.sha256

    def capture_sha256(payload: bytes) -> Any:
        hashed_payloads.append(payload)
        return real_sha256(payload)

    monkeypatch.setattr(rule_validation.hashlib, "sha256", capture_sha256)

    fingerprint_rule_set_version(
        [
            _rule(
                condition={
                    "operator": "required",
                    "field_key": "certification_project.scope",
                    "metadata": {"secret_value": "condition-sensitive-value"},
                },
                input_requirements=[
                    {
                        "kind": "api",
                        "metadata": {"password_hash": "input-sensitive-value"},
                    }
                ],
                evidence_requirements=[
                    {
                        "kind": "project_field",
                        "metadata": {"private-key-pem": "evidence-sensitive-value"},
                    }
                ],
                source_refs=[
                    {
                        "knowledge_base_version_id": "kbv-1",
                        "document_id": "doc-1",
                        "section": "7.3",
                        "metadata": {"api_token_value": "source-sensitive-value"},
                    }
                ],
            )
        ]
    )

    assert len(hashed_payloads) == 1
    hashed_payload = hashed_payloads[0].decode("utf-8")
    assert "condition-sensitive-value" not in hashed_payload
    assert "input-sensitive-value" not in hashed_payload
    assert "evidence-sensitive-value" not in hashed_payload
    assert "source-sensitive-value" not in hashed_payload


def test_fingerprint_retains_nested_semantic_values() -> None:
    first = fingerprint_rule_set_version(
        [_rule(input_requirements=[{"kind": "api", "metadata": {"threshold": 1}}])]
    )
    second = fingerprint_rule_set_version(
        [_rule(input_requirements=[{"kind": "api", "metadata": {"threshold": 2}}])]
    )
    assert first != second


@pytest.mark.parametrize(
    "semantic_key",
    [
        "token_count",
        "timestamp_tolerance",
        "authorization_required",
        "secretary_approval",
    ],
)
def test_fingerprint_retains_metadata_that_is_semantic_despite_its_name(
    semantic_key: str,
) -> None:
    first = fingerprint_rule_set_version(
        [
            _rule(
                input_requirements=[
                    {"kind": "model", "metadata": {semantic_key: 100}}
                ]
            )
        ]
    )
    second = fingerprint_rule_set_version(
        [
            _rule(
                input_requirements=[
                    {"kind": "model", "metadata": {semantic_key: 200}}
                ]
            )
        ]
    )
    assert first != second
