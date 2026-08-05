from pathlib import Path

import pytest

from app.harness.errors import HarnessExecutionError
from app.harness.execution_context import SandboxExecutionContext


def test_bubblewrap_maps_only_declared_path_fields(tmp_path: Path) -> None:
    workspace = tmp_path / "task"
    skill = workspace / "run" / "skill"
    skill.mkdir(parents=True)
    context = SandboxExecutionContext(workspace, "bubblewrap")

    assert context.map_argv([str(workspace / "runner.py")]) == ["/workspace/runner.py"]
    assert context.map_env(
        {"SKILL_WORKSPACE": str(skill), "QUERY": f"inspect {workspace}-old"},
        path_keys=("SKILL_WORKSPACE",),
    ) == {
        "SKILL_WORKSPACE": "/workspace/run/skill",
        "QUERY": f"inspect {workspace}-old",
    }
    assert context.map_payload(
        {"skill_workspace": str(skill), "query": str(workspace)},
        path_keys=("skill_workspace",),
    ) == {
        "skill_workspace": "/workspace/run/skill",
        "query": str(workspace),
    }


def test_execution_context_rejects_cwd_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "task"
    workspace.mkdir()
    context = SandboxExecutionContext(workspace, "srt")

    with pytest.raises(HarnessExecutionError):
        context.host_cwd(tmp_path)


def test_execution_context_rejects_unknown_backend(tmp_path: Path) -> None:
    workspace = tmp_path / "task"
    workspace.mkdir()

    with pytest.raises(HarnessExecutionError) as invalid:
        SandboxExecutionContext.create(workspace, "bublewrap")

    assert invalid.value.error.code == "SANDBOX_BACKEND_INVALID"
