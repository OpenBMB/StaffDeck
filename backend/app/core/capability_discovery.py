from __future__ import annotations

from difflib import SequenceMatcher
import json
import re
from typing import Iterable

from app.core.task_request_compiler import (
    CapabilityCatalogEntry,
    CapabilityDescriptor,
    CapabilityManifest,
)


CAPABILITY_CATALOG_BUDGET_CHARS = 8_000
CAPABILITY_SEARCH_MAX_RESULTS = 20

# These are the small, high-leverage Harness kernel.  Everything else remains
# authorized in the frozen server-side manifest but is progressively disclosed
# to the model only when it asks for the schema.
ALWAYS_EXPANDED_CAPABILITIES = {
    "capability_search",
    "capability_describe",
    "exec_command",
    "read_file",
    "extract_document_text",
    "write_file",
    "edit_file",
    "publish_artifact",
    "knowledge_search",
}


def project_capability_manifest(
    manifest: CapabilityManifest,
    *,
    budget_chars: int = CAPABILITY_CATALOG_BUDGET_CHARS,
) -> CapabilityManifest:
    """Return the compact manifest sent to one isolated Task Agent.

    ``manifest`` remains the complete authorization snapshot held by the
    invoker.  This projection controls context size only; it never grants a
    capability that was absent from the frozen snapshot.
    """

    budget_chars = max(256, int(budget_chars))
    expanded: list[CapabilityDescriptor] = []
    catalog_candidates: list[CapabilityCatalogEntry] = []
    for descriptor in sorted(
        manifest.available,
        key=lambda item: (item.kind, item.name, item.capability_id),
    ):
        if not descriptor.available:
            continue
        if _is_initially_expanded(descriptor):
            expanded.append(model_descriptor(descriptor))
            continue
        catalog_candidates.append(catalog_entry(descriptor))

    catalog, truncated = _budget_catalog(catalog_candidates, budget_chars)
    return CapabilityManifest(
        available=expanded,
        catalog=catalog,
        catalog_total=len(catalog_candidates),
        catalog_truncated=truncated,
        catalog_budget_chars=budget_chars,
        unavailable_references=[
            model_descriptor(item) for item in manifest.unavailable_references
        ],
        snapshot_revision=manifest.snapshot_revision,
    )


def search_capability_descriptors(
    descriptors: Iterable[CapabilityDescriptor],
    query: str,
    *,
    kinds: set[str] | None = None,
    limit: int = 8,
) -> list[CapabilityDescriptor]:
    normalized_query = _normalize(query)
    limit = max(1, min(int(limit), CAPABILITY_SEARCH_MAX_RESULTS))
    ranked: list[tuple[float, str, CapabilityDescriptor]] = []
    for descriptor in descriptors:
        if not descriptor.available or descriptor.kind == "internal":
            continue
        if kinds and descriptor.kind not in kinds:
            continue
        score = _relevance_score(normalized_query, descriptor)
        ranked.append((score, descriptor.name, descriptor))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    if not ranked:
        return []
    positive = [item for item in ranked if item[0] > 0]
    selected = positive if positive else ranked
    return [item[2] for item in selected[:limit]]


def catalog_entry(descriptor: CapabilityDescriptor) -> CapabilityCatalogEntry:
    return CapabilityCatalogEntry(
        capability_id=descriptor.capability_id,
        name=descriptor.name,
        kind=descriptor.kind,
        capability_scope=descriptor.capability_scope,
        description=_compact_description(descriptor.description, 240),
    )


def model_descriptor(descriptor: CapabilityDescriptor) -> CapabilityDescriptor:
    """Strip authorization/runtime internals from a model-visible descriptor."""

    if not descriptor.available:
        return descriptor.model_copy(
            update={
                "description": _compact_description(descriptor.description, 1_000),
                "input_schema": {},
                "metadata": {},
            }
        )
    metadata: dict[str, object] = {}
    if descriptor.kind == "file":
        for key in ("provider", "side_effect", "sandbox"):
            value = descriptor.metadata.get(key)
            if value not in (None, "", [], {}):
                metadata[key] = value
    elif descriptor.kind == "knowledge":
        allowed = descriptor.metadata.get("allowed_knowledge_base_ids") or []
        metadata["authorized_knowledge_base_count"] = len(allowed)
    elif descriptor.kind == "general_skill":
        metadata["execution_policy"] = descriptor.metadata.get(
            "execution_policy", "instructions_only"
        )
    elif descriptor.kind == "tool":
        for key in ("tool_type", "method"):
            value = descriptor.metadata.get(key)
            if value not in (None, "", [], {}):
                metadata[key] = value
    elif descriptor.kind == "internal":
        metadata["provider"] = "harness"
    return descriptor.model_copy(
        update={
            "description": _compact_description(descriptor.description, 1_000),
            "input_schema": _model_input_schema(descriptor.input_schema),
            "metadata": metadata,
        }
    )


def _model_input_schema(schema: dict[str, object]) -> dict[str, object]:
    """Project configured JSON Schema without examples or sensitive defaults.

    Administrators control external capability schemas.  Treat their prose as
    untrusted catalog data and expose only the validation vocabulary required
    for a model to construct a call.
    """

    allowed_keys = {
        "$defs",
        "$ref",
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "description",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "items",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "multipleOf",
        "not",
        "oneOf",
        "pattern",
        "prefixItems",
        "properties",
        "required",
        "title",
        "type",
        "uniqueItems",
    }

    def project(value: object, *, depth: int = 0) -> object:
        if depth > 16:
            return {}
        if isinstance(value, dict):
            result: dict[str, object] = {}
            for raw_key, nested in value.items():
                key = str(raw_key)
                if key not in allowed_keys:
                    continue
                if key in {"default", "example", "examples", "$comment"}:
                    continue
                if key == "description":
                    result[key] = _compact_description(str(nested or ""), 400)
                elif key in {"properties", "$defs"} and isinstance(nested, dict):
                    result[key] = {
                        str(child_key): project(child_value, depth=depth + 1)
                        for child_key, child_value in list(nested.items())[:128]
                    }
                else:
                    result[key] = project(nested, depth=depth + 1)
            return result
        if isinstance(value, list):
            return [project(item, depth=depth + 1) for item in value[:128]]
        if isinstance(value, str):
            return value[:2_000]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return str(value)[:2_000]

    projected = project(schema)
    return projected if isinstance(projected, dict) else {}


def _is_initially_expanded(descriptor: CapabilityDescriptor) -> bool:
    return (
        descriptor.kind == "internal"
        or descriptor.name in ALWAYS_EXPANDED_CAPABILITIES
        or descriptor.capability_scope == "sop_specific"
        or bool(descriptor.metadata.get("sop_explicitly_allowed"))
    )


def _budget_catalog(
    entries: list[CapabilityCatalogEntry],
    budget_chars: int,
) -> tuple[list[CapabilityCatalogEntry], bool]:
    # Match Codex's progressive-disclosure behavior: shorten descriptions
    # before omitting entries.  The measured budget is the compact JSON payload
    # actually sent to the model, not Python object size.
    for description_limit in (240, 120, 64, 0):
        shortened = [
            item.model_copy(
                update={
                    "description": _compact_description(
                        item.description,
                        description_limit,
                    )
                }
            )
            for item in entries
        ]
        if _catalog_chars(shortened) <= budget_chars:
            return shortened, False

    included: list[CapabilityCatalogEntry] = []
    for item in entries:
        candidate = [*included, item.model_copy(update={"description": ""})]
        if _catalog_chars(candidate) > budget_chars:
            break
        included = candidate
    return included, len(included) < len(entries)


def _catalog_chars(entries: list[CapabilityCatalogEntry]) -> int:
    return len(
        json.dumps(
            [item.model_dump(mode="json") for item in entries],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _compact_description(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _normalize(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _relevance_score(query: str, descriptor: CapabilityDescriptor) -> float:
    if not query:
        return 0.0
    name = _normalize(descriptor.name)
    description = _normalize(descriptor.description)
    haystack = f"{name} {description} {descriptor.kind.replace('_', ' ')}"
    score = 0.0
    if query == name:
        score += 200.0
    if query in name or name in query:
        score += 100.0
    if query in haystack:
        score += 60.0
    query_terms = _search_terms(query)
    for term in query_terms:
        if term in name:
            score += 24.0
        elif term in description:
            score += 10.0
    score += SequenceMatcher(None, query, name).ratio() * 12.0
    return score


def _search_terms(value: str) -> set[str]:
    terms = {
        token
        for token in re.findall(r"[a-z0-9_.-]+|[\u3400-\u9fff]+", value)
        if token
    }
    for token in tuple(terms):
        if re.fullmatch(r"[\u3400-\u9fff]+", token) and len(token) > 1:
            terms.update(token[index : index + 2] for index in range(len(token) - 1))
    return terms


__all__ = [
    "ALWAYS_EXPANDED_CAPABILITIES",
    "CAPABILITY_CATALOG_BUDGET_CHARS",
    "CAPABILITY_SEARCH_MAX_RESULTS",
    "catalog_entry",
    "model_descriptor",
    "project_capability_manifest",
    "search_capability_descriptors",
]
