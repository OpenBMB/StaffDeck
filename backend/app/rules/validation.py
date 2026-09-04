from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.project_data.fields import SYSTEM_FIELD_DEFINITIONS
from app.rules.schema import RuleDefinitionCreate, RuleValidationError

_STABLE_KEY = re.compile(r"[a-z0-9_.-]{1,160}\Z", re.ASCII)
_ALLOWED_OPERATORS = frozenset(
    {"required", "equals", "not_equals", "in", "gte", "lte", "matches", "has_evidence"}
)
_SYSTEM_FIELD_KEYS = frozenset(item.field_key for item in SYSTEM_FIELD_DEFINITIONS)
_MODEL_RESULTS = frozenset({"pass", "fail", "indeterminate"})
_SECRET_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "credential",
        "credentials",
        "password",
        "refresh_token",
        "secret",
        "token",
    }
)


def _validate_source_ref(source_ref: dict[str, Any]) -> None:
    internal_policy_ref = source_ref.get("internal_policy_ref")
    if isinstance(internal_policy_ref, str) and internal_policy_ref.strip():
        return
    required = ("knowledge_base_version_id", "document_id", "section")
    if all(
        isinstance(source_ref.get(key), str) and source_ref[key].strip()
        for key in required
    ):
        return
    raise RuleValidationError("INVALID_RULE_SOURCE_REF")


def _referenced_field_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        found = {
            item
            for key, item in value.items()
            if key == "field_key" and isinstance(item, str)
        }
        for item in value.values():
            found.update(_referenced_field_keys(item))
        return found
    if isinstance(value, list):
        found: set[str] = set()
        for item in value:
            found.update(_referenced_field_keys(item))
        return found
    return set()


def _validate_rule_definition(
    rule: RuleDefinitionCreate,
    declared_field_keys: frozenset[str],
) -> None:
    if not isinstance(rule.rule_key, str) or _STABLE_KEY.fullmatch(rule.rule_key) is None:
        raise RuleValidationError("INVALID_RULE_KEY")
    if not rule.workflow_nodes:
        raise RuleValidationError("RULE_WORKFLOW_NODE_REQUIRED")
    if not rule.information_domains:
        raise RuleValidationError("RULE_INFORMATION_DOMAIN_REQUIRED")
    if rule.execution_level == "mandatory" and rule.execution_method != "deterministic":
        raise RuleValidationError("MANDATORY_RULE_MUST_BE_DETERMINISTIC")
    if rule.execution_level == "mandatory" and not rule.source_refs:
        raise RuleValidationError("MANDATORY_RULE_SOURCE_REQUIRED")
    if rule.execution_level == "mandatory" and not rule.evidence_requirements:
        raise RuleValidationError("MANDATORY_RULE_EVIDENCE_REQUIRED")

    if rule.execution_method == "model_assisted":
        allowed_results = rule.condition.get("allowed_results")
        if allowed_results is not None and (
            not isinstance(allowed_results, list)
            or not set(allowed_results).issubset(_MODEL_RESULTS)
        ):
            raise RuleValidationError("MODEL_ASSISTED_RESULT_INVALID")
        if rule.condition.get("blocking") is True:
            raise RuleValidationError("MODEL_ASSISTED_DIRECT_BLOCKING_FORBIDDEN")

    operator = rule.condition.get("operator")
    if operator not in _ALLOWED_OPERATORS:
        raise RuleValidationError("UNSUPPORTED_RULE_OPERATOR")

    known_field_keys = _SYSTEM_FIELD_KEYS | declared_field_keys
    referenced_field_keys = set(rule.field_keys)
    referenced_field_keys.update(_referenced_field_keys(rule.condition))
    referenced_field_keys.update(_referenced_field_keys(rule.input_requirements))
    referenced_field_keys.update(_referenced_field_keys(rule.evidence_requirements))
    if not referenced_field_keys.issubset(known_field_keys):
        raise RuleValidationError("UNKNOWN_FIELD_KEY")

    for source_ref in rule.source_refs:
        _validate_source_ref(source_ref)
    if isinstance(rule.sequence, bool) or not isinstance(rule.sequence, int) or rule.sequence < 0:
        raise RuleValidationError("INVALID_RULE_SEQUENCE")
    if not isinstance(rule.enabled, bool):
        raise RuleValidationError("INVALID_RULE_ENABLED")


def validate_rule_definition(rule: RuleDefinitionCreate) -> None:
    _validate_rule_definition(rule, frozenset())


def validate_rule_definition_with_fields(
    rule: RuleDefinitionCreate,
    declared_field_keys: set[str] | frozenset[str],
) -> None:
    _validate_rule_definition(rule, frozenset(declared_field_keys))


def validate_rule_set(rules: list[RuleDefinitionCreate]) -> None:
    validate_rule_set_with_fields(rules, frozenset())


def validate_rule_set_with_fields(
    rules: list[RuleDefinitionCreate],
    declared_field_keys: set[str] | frozenset[str],
) -> None:
    seen: set[str] = set()
    for rule in rules:
        if rule.rule_key in seen:
            raise RuleValidationError("DUPLICATE_RULE_KEY")
        seen.add(rule.rule_key)
        _validate_rule_definition(rule, frozenset(declared_field_keys))


def _without_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_secrets(item)
            for key, item in value.items()
            if key.lower() not in _SECRET_KEYS
        }
    if isinstance(value, list):
        return [_without_secrets(item) for item in value]
    return value


def fingerprint_rule_set_version(rules: list[RuleDefinitionCreate]) -> str:
    normalized = [
        _without_secrets(rule.model_dump(mode="json"))
        for rule in sorted(rules, key=lambda item: (item.rule_key, item.sequence))
    ]
    canonical = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
