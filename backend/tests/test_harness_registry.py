from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from app.harness import (
    HarnessExecutor,
    HarnessLimits,
    HarnessRegistry,
    HarnessToolCall,
    HarnessToolContext,
    build_file_tool_registry,
)


def test_file_tool_registry_exposes_exact_model_tool_names() -> None:
    registry = build_file_tool_registry()

    assert registry.names() == (
        "read_file",
        "extract_document_text",
        "write_file",
        "edit_file",
        "list_directory",
        "glob",
        "grep",
        "file_info",
        "publish_artifact",
        "mkdir",
        "delete_file",
        "move_file",
        "copy_file",
    )
    model_tools = registry.model_tools()
    assert [item["function"]["name"] for item in model_tools] == list(registry.names())
    assert all(item["function"]["parameters"]["type"] == "object" for item in model_tools)
    assert registry.get("delete_file").spec.side_effect == "delete"  # type: ignore[union-attr]


def test_registry_rejects_duplicate_and_invalid_names() -> None:
    class Arguments(BaseModel):
        model_config = ConfigDict(extra="forbid")

    registry = HarnessRegistry()
    registry.register(
        name="example",
        description="Example",
        argument_model=Arguments,
        handler=lambda _context, _arguments: {"ok": True},
    )

    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            name="example",
            description="Example",
            argument_model=Arguments,
            handler=lambda _context, _arguments: {"ok": True},
        )
    with pytest.raises(ValueError, match="invalid Harness tool name"):
        registry.register(
            name="../bad",
            description="Bad",
            argument_model=Arguments,
            handler=lambda _context, _arguments: {"ok": True},
        )


def test_executor_returns_structured_validation_and_missing_tool_errors(
    tmp_path: Path,
) -> None:
    executor = HarnessExecutor(build_file_tool_registry())
    context = _context(tmp_path)

    invalid = executor.execute(
        context,
        HarnessToolCall(
            call_id="invalid",
            name="read_file",
            arguments={"path": "a.txt", "unexpected": True},
        ),
    )
    missing = executor.execute(
        context,
        HarnessToolCall(call_id="missing", name="missing_tool"),
    )

    assert invalid.success is False
    assert invalid.error is not None
    assert invalid.error.code == "INVALID_ARGUMENTS"
    assert invalid.error.details["errors"][0]["path"] == "unexpected"  # type: ignore[index]
    assert missing.success is False
    assert missing.error is not None
    assert missing.error.code == "TOOL_NOT_FOUND"


def test_executor_enforces_json_result_size(tmp_path: Path) -> None:
    class Arguments(BaseModel):
        model_config = ConfigDict(extra="forbid")

    registry = HarnessRegistry()
    registry.register(
        name="large_result",
        description="Return a large result",
        argument_model=Arguments,
        handler=lambda _context, _arguments: {"value": "x" * 100},
    )
    executor = HarnessExecutor(registry)
    context = HarnessToolContext(
        run_id="run",
        workspace_root=(tmp_path / "workspace").resolve(),
        limits=HarnessLimits(
            max_read_bytes=128,
            max_file_bytes=128,
            max_workspace_bytes=1024,
            max_entries=10,
            max_result_bytes=32,
        ),
    )

    result = executor.execute(
        context,
        HarnessToolCall(call_id="large", name="large_result"),
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "RESULT_TOO_LARGE"
    assert result.error.details["actual_bytes"] > result.error.details["max_bytes"]


def test_context_requires_an_absolute_workspace() -> None:
    with pytest.raises(ValueError, match="workspace_root must be absolute"):
        HarnessToolContext(run_id="run", workspace_root=Path("relative"))


def _context(tmp_path: Path) -> HarnessToolContext:
    return HarnessToolContext(
        run_id="run",
        task_frame_id="frame",
        workspace_root=(tmp_path / "workspace").resolve(),
    )
