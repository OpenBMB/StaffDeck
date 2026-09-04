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
_FINGERPRINT_FIELDS = (
    "rule_key",
    "name",
    "description",
    "workflow_nodes",
    "information_domains",
    "document_types",
    "field_keys",
    "execution_level",
    "execution_method",
    "condition",
    "input_requirements",
    "evidence_requirements",
    "source_refs",
    "sequence",
    "enabled",
)
_FINGERPRINT_SEMANTIC_METADATA_KEYS = frozenset(
    {"tokencount", "timestamptolerance"}
)
_SECRET_KEY_WORDS = frozenset(
    {"credential", "credentials", "passphrase", "password", "secret"}
)
_SECRET_KEY_MARKERS = (
    "secret",
    "password",
    "passphrase",
    "accesstoken",
    "refreshtoken",
    "authtoken",
    "apitoken",
    "clienttoken",
    "credential",
    "credentials",
    "apikey",
    "privatekey",
)
_LIFECYCLE_KEY_WORDS = frozenset(
    {
        "created",
        "creation",
        "draft",
        "drafted",
        "drafting",
        "modified",
        "modification",
        "published",
        "publication",
        "updated",
        "update",
    }
)
_TIME_KEY_WORDS = frozenset(
    {"at", "date", "epoch", "millis", "milliseconds", "ms", "on", "time", "utc"}
)
_TIMESTAMP_KEY_MARKERS = (
    "createdat",
    "createdtime",
    "createdtimestamp",
    "draftedat",
    "draftedtime",
    "draftedtimestamp",
    "updatedat",
    "updatedtime",
    "updatedtimestamp",
    "modifiedat",
    "modifiedtime",
    "modifiedtimestamp",
    "publishedat",
    "publishedtime",
    "publishedtimestamp",
    "timestamp",
    "timestamps",
)
_IDENTITY_KEY_WORDS = frozenset(
    {"account", "actor", "by", "email", "id", "identity", "name", "principal", "subject", "uuid"}
)
_IDENTITY_KEY_MARKERS = (
    "author",
    "createdby",
    "draftedby",
    "drafter",
    "draftinguser",
    "draftuser",
    "modifiedby",
    "publishedby",
    "updatedby",
    "userid",
    "useridentity",
    "username",
)
_DIRECT_BLOCKING_KEYS = frozenset({"blocking", "directblocking", "isblocking"})


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


def _validate_condition_operators(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "operator" and (
                not isinstance(item, str) or item not in _ALLOWED_OPERATORS
            ):
                raise RuleValidationError("UNSUPPORTED_RULE_OPERATOR")
            _validate_condition_operators(item)
    elif isinstance(value, list):
        for item in value:
            _validate_condition_operators(item)


def _has_nonblank_content(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return bool(value) and any(_has_nonblank_content(item) for item in value.values())
    if isinstance(value, list):
        return bool(value) and any(_has_nonblank_content(item) for item in value)
    return value is not None


def _contains_truthy_blocking_metadata(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = _normalize_metadata_key(key)
            is_blocking_key = normalized_key in _DIRECT_BLOCKING_KEYS or (
                normalized_key.endswith("blocking")
                and not normalized_key.startswith(("nonblocking", "notblocking"))
            )
            if is_blocking_key and bool(item):
                return True
            if _contains_truthy_blocking_metadata(item):
                return True
    elif isinstance(value, list):
        return any(_contains_truthy_blocking_metadata(item) for item in value)
    return False


def _validate_rule_definition(
    rule: RuleDefinitionCreate,
    declared_field_keys: frozenset[str],
) -> None:
    if not isinstance(rule.rule_key, str) or _STABLE_KEY.fullmatch(rule.rule_key) is None:
        raise RuleValidationError("INVALID_RULE_KEY")
    if not rule.workflow_nodes:
        raise RuleValidationError("RULE_WORKFLOW_NODE_REQUIRED")
    if any(not isinstance(item, str) or not item.strip() for item in rule.workflow_nodes):
        raise RuleValidationError("INVALID_RULE_WORKFLOW_NODE")
    if not rule.information_domains:
        raise RuleValidationError("RULE_INFORMATION_DOMAIN_REQUIRED")
    if any(
        not isinstance(item, str) or not item.strip()
        for item in rule.information_domains
    ):
        raise RuleValidationError("INVALID_RULE_INFORMATION_DOMAIN")
    if any(not _has_nonblank_content(item) for item in rule.evidence_requirements):
        raise RuleValidationError("INVALID_RULE_EVIDENCE_REQUIREMENT")
    if rule.execution_level == "mandatory" and rule.execution_method != "deterministic":
        raise RuleValidationError("MANDATORY_RULE_MUST_BE_DETERMINISTIC")
    if rule.execution_level == "mandatory" and not rule.source_refs:
        raise RuleValidationError("MANDATORY_RULE_SOURCE_REQUIRED")
    if rule.execution_level == "mandatory" and not rule.evidence_requirements:
        raise RuleValidationError("MANDATORY_RULE_EVIDENCE_REQUIRED")

    if rule.execution_method == "model_assisted":
        allowed_results = rule.condition.get("allowed_results")
        if (
            not isinstance(allowed_results, list)
            or not allowed_results
            or not all(isinstance(item, str) for item in allowed_results)
            or not set(allowed_results).issubset(_MODEL_RESULTS)
        ):
            raise RuleValidationError("MODEL_ASSISTED_RESULT_INVALID")
        metadata_containers = (
            rule.condition,
            rule.input_requirements,
            rule.evidence_requirements,
            rule.source_refs,
        )
        if any(
            _contains_truthy_blocking_metadata(value)
            for value in metadata_containers
        ):
            raise RuleValidationError("MODEL_ASSISTED_DIRECT_BLOCKING_FORBIDDEN")

    _validate_condition_operators(rule.condition)
    if "operator" not in rule.condition:
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


def _normalize_metadata_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _metadata_key_words(key: Any) -> frozenset[str]:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(key))
    separated = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", separated)
    return frozenset(
        part.lower() for part in re.findall(r"[A-Za-z0-9]+", separated)
    )


def _has_boundaryless_marker(normalized_key: str, markers: tuple[str, ...]) -> bool:
    return any(normalized_key.endswith(marker) for marker in markers)


def _is_nonsemantic_metadata_key(key: Any) -> bool:
    normalized_key = _normalize_metadata_key(key)
    if normalized_key in _FINGERPRINT_SEMANTIC_METADATA_KEYS:
        return False

    words = _metadata_key_words(key)
    if (
        words.intersection(_SECRET_KEY_WORDS)
        or "token" in words
        or {"api", "key"}.issubset(words)
        or {"private", "key"}.issubset(words)
        or _has_boundaryless_marker(normalized_key, _SECRET_KEY_MARKERS)
    ):
        return True

    if (
        words.intersection({"timestamp", "timestamps"})
        or (
            words.intersection(_LIFECYCLE_KEY_WORDS)
            and words.intersection(_TIME_KEY_WORDS)
        )
        or _has_boundaryless_marker(normalized_key, _TIMESTAMP_KEY_MARKERS)
    ):
        return True

    draft_words = words.intersection({"draft", "drafted", "drafting", "drafter"})
    return bool(
        "drafter" in words
        or (
            "author" in words
            and (len(words) == 1 or words.intersection(_IDENTITY_KEY_WORDS))
        )
        or (draft_words and ("user" in words or words.intersection(_IDENTITY_KEY_WORDS)))
        or ("user" in words and words.intersection(_IDENTITY_KEY_WORDS))
        or ("by" in words and words.intersection(_LIFECYCLE_KEY_WORDS))
        or _has_boundaryless_marker(normalized_key, _IDENTITY_KEY_MARKERS)
    )


def _without_nonsemantic_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_nonsemantic_metadata(item)
            for key, item in value.items()
            if not _is_nonsemantic_metadata_key(key)
        }
    if isinstance(value, list):
        return [_without_nonsemantic_metadata(item) for item in value]
    return value


def fingerprint_rule_set_version(rules: list[RuleDefinitionCreate]) -> str:
    normalized = []
    for rule in sorted(rules, key=lambda item: (item.rule_key, item.sequence)):
        dumped = rule.model_dump(mode="json")
        semantic_rule = {key: dumped[key] for key in _FINGERPRINT_FIELDS}
        normalized.append(_without_nonsemantic_metadata(semantic_rule))
    canonical = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
