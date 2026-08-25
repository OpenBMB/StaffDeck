from __future__ import annotations

import json
from typing import Any, Literal

import pytest

from app.core.capability_discovery import (
    ALWAYS_EXPANDED_CAPABILITIES,
    CAPABILITY_CATALOG_BUDGET_CHARS,
    CAPABILITY_SEARCH_MAX_RESULTS,
    model_descriptor,
    project_capability_manifest,
    search_capability_descriptors,
)
from app.core.task_request_compiler import (
    CapabilityDescriptor,
    CapabilityKind,
    CapabilityManifest,
)


def _descriptor(
    name: str,
    *,
    kind: CapabilityKind = "tool",
    capability_scope: Literal["general", "sop_specific"] = "general",
    description: str = "",
    input_schema: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    available: bool = True,
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_id=f"cap-{name}",
        name=name,
        kind=kind,
        capability_scope=capability_scope,
        description=description,
        input_schema=input_schema or {"type": "object"},
        metadata=metadata or {},
        available=available,
    )


def _catalog_chars(manifest: CapabilityManifest) -> int:
    return len(
        json.dumps(
            [item.model_dump(mode="json") for item in manifest.catalog],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _forbidden_schema_paths(value: object, path: str = "$") -> list[str]:
    forbidden = {"default", "examples", "$comment"}
    matches: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if key in forbidden:
                matches.append(child_path)
            matches.extend(_forbidden_schema_paths(item, child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            matches.extend(_forbidden_schema_paths(item, f"{path}[{index}]"))
    return matches


def test_projected_catalog_respects_8k_budget_and_marks_truncation() -> None:
    descriptors = [
        _descriptor(
            f"tool_{index:03d}_{'x' * 48}",
            description=(f"第 {index} 个目录能力。" + "详细说明" * 160),
        )
        for index in range(200)
    ]

    projected = project_capability_manifest(
        CapabilityManifest(available=descriptors)
    )

    assert projected.catalog_budget_chars == CAPABILITY_CATALOG_BUDGET_CHARS == 8_000
    assert _catalog_chars(projected) <= CAPABILITY_CATALOG_BUDGET_CHARS
    assert projected.catalog_total == len(descriptors)
    assert projected.catalog_truncated is True
    assert 0 < len(projected.catalog) < projected.catalog_total
    assert projected.available == []
    assert [item.name for item in projected.catalog] == [
        item.name for item in descriptors[: len(projected.catalog)]
    ]


def test_initial_projection_expands_only_kernel_and_sop_explicit_capabilities() -> None:
    kernel = [
        _descriptor(
            name,
            kind=(
                "internal"
                if name
                in {
                    "capability_search",
                    "capability_describe",
                    "list_published_deliverables",
                    "read_published_deliverable",
                }
                else "knowledge"
                if name == "knowledge_search"
                else "file"
            ),
        )
        for name in sorted(ALWAYS_EXPANDED_CAPABILITIES)
    ]
    sop_scoped = _descriptor(
        "sop.submit_expense",
        capability_scope="sop_specific",
        metadata={"sop_explicitly_allowed": True},
    )
    sop_marked = _descriptor(
        "shared.lookup_for_sop",
        metadata={"sop_explicitly_allowed": True},
    )
    catalog_only = [
        _descriptor("general.lookup"),
        _descriptor("general_skill.report", kind="general_skill"),
    ]
    unavailable = _descriptor("disabled.lookup", available=False)

    projected = project_capability_manifest(
        CapabilityManifest(
            available=[*catalog_only, sop_scoped, unavailable, *kernel, sop_marked]
        )
    )

    assert {item.name for item in projected.available} == {
        *ALWAYS_EXPANDED_CAPABILITIES,
        sop_scoped.name,
        sop_marked.name,
    }
    assert {item.name for item in projected.catalog} == {
        "general.lookup",
        "general_skill.report",
    }
    assert projected.catalog_total == 2
    assert unavailable.name not in {
        *(item.name for item in projected.available),
        *(item.name for item in projected.catalog),
    }
    assert all(not hasattr(item, "input_schema") for item in projected.catalog)
    assert all(not hasattr(item, "metadata") for item in projected.catalog)


def test_capability_search_honors_kind_limit_and_stable_name_order() -> None:
    tools = [
        _descriptor(f"tool_{index:02d}", description="zzzz shared capability")
        for index in reversed(range(25))
    ]
    skills = [
        _descriptor(
            f"skill_{index:02d}",
            kind="general_skill",
            description="zzzz shared capability",
        )
        for index in range(4)
    ]
    hidden = _descriptor(
        "hidden_tool",
        description="zzzz shared capability",
        available=False,
    )
    internal = _descriptor(
        "capability_search",
        kind="internal",
        description="zzzz shared capability",
    )
    candidates = [*tools, *skills, hidden, internal]

    first = search_capability_descriptors(
        candidates,
        "zzzz",
        kinds={"tool"},
        limit=999,
    )
    second = search_capability_descriptors(
        reversed(candidates),
        "zzzz",
        kinds={"tool"},
        limit=999,
    )

    expected_names = [
        f"tool_{index:02d}" for index in range(CAPABILITY_SEARCH_MAX_RESULTS)
    ]
    assert len(first) == CAPABILITY_SEARCH_MAX_RESULTS == 20
    assert [item.name for item in first] == expected_names
    assert [item.name for item in second] == expected_names
    assert {item.kind for item in first} == {"tool"}
    assert len(search_capability_descriptors(candidates, "zzzz", limit=3)) == 3
    assert len(search_capability_descriptors(candidates, "zzzz", limit=0)) == 1


@pytest.mark.parametrize(
    ("kind", "metadata", "expected"),
    [
        (
            "file",
            {
                "provider": "builtin.fs",
                "side_effect": "write",
                "sandbox": "task-frame",
                "content_digest": "secret-digest",
                "workspace_root": "/private/workspace",
            },
            {
                "provider": "builtin.fs",
                "side_effect": "write",
                "sandbox": "task-frame",
            },
        ),
        (
            "knowledge",
            {
                "allowed_knowledge_base_ids": ["kb-secret-1", "kb-secret-2"],
                "allowed_knowledge_version_ids": ["kbv-secret"],
                "knowledge_version_by_base_id": {"kb-secret-1": "kbv-secret"},
                "content_digest": "secret-digest",
            },
            {"authorized_knowledge_base_count": 2},
        ),
        (
            "general_skill",
            {
                "execution_policy": "inspect_then_decide",
                "content_digest": "secret-content",
                "package_digest": "secret-package",
                "permissions": {"network": True},
                "runtime_config": {"env": {"TOKEN": "secret"}},
                "sop_explicitly_allowed": True,
            },
            {"execution_policy": "inspect_then_decide"},
        ),
        (
            "tool",
            {
                "tool_type": "http",
                "method": "POST",
                "source_tool_name": "private.provider.name",
                "content_digest": "secret-digest",
                "sop_explicitly_allowed": True,
            },
            {"tool_type": "http", "method": "POST"},
        ),
        (
            "internal",
            {
                "provider": "untrusted-value",
                "implementation": "private.module:function",
            },
            {"provider": "harness"},
        ),
    ],
)
def test_model_descriptor_exposes_only_kind_specific_safe_metadata(
    kind: CapabilityKind,
    metadata: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    projected = model_descriptor(
        _descriptor(f"demo.{kind}", kind=kind, metadata=metadata)
    )

    assert projected.metadata == expected


def test_model_descriptor_recursively_scrubs_non_operational_schema_annotations() -> None:
    schema = {
        "$comment": "top-level internal note",
        "type": "object",
        "default": {},
        "examples": [{"mode": "fast"}],
        "properties": {
            "mode": {
                "type": "string",
                "description": "Execution mode",
                "enum": ["fast", "safe"],
                "default": "safe",
                "examples": ["fast"],
                "$comment": "provider-specific mode",
            },
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "$comment": "nested note",
                    "properties": {
                        "value": {
                            "type": "integer",
                            "minimum": 1,
                            "default": 1,
                            "examples": [2],
                        }
                    },
                },
            },
        },
        "required": ["mode"],
        "$defs": {
            "label": {
                "type": "string",
                "$comment": "definition note",
                "examples": ["internal example"],
            }
        },
    }
    source = _descriptor("schema.demo", input_schema=schema)

    projected = model_descriptor(source)

    assert _forbidden_schema_paths(projected.input_schema) == []
    assert projected.input_schema["type"] == "object"
    assert projected.input_schema["required"] == ["mode"]
    assert projected.input_schema["properties"]["mode"]["enum"] == [
        "fast",
        "safe",
    ]
    assert projected.input_schema["properties"]["items"]["items"]["properties"][
        "value"
    ]["minimum"] == 1
    assert _forbidden_schema_paths(source.input_schema)
