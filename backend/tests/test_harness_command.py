from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.harness import (
    HarnessExecutor,
    HarnessLimits,
    HarnessToolCall,
    HarnessToolContext,
    build_command_tool_registry,
)
from app.harness import command as command_module


def test_command_registry_exposes_typed_exec_command() -> None:
    registry = build_command_tool_registry()

    assert registry.names() == ("exec_command",)
    registered = registry.get("exec_command")
    assert registered is not None
    assert registered.spec.side_effect == "write"
    schema = registered.spec.input_schema
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["command"]
    assert "cwd" not in schema["properties"]


def test_exec_command_fails_closed_without_bubblewrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(command_module.sys, "platform", "linux")
    monkeypatch.setattr(command_module.shutil, "which", lambda _name: None)

    result = _execute(tmp_path, {"command": "pwd"})

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "SANDBOX_UNAVAILABLE"


@pytest.mark.parametrize(
    "command",
    [
        "cat /etc/passwd",
        "cat ../outside.txt",
        "printf x > ../outside.txt",
        "rm -rf .",
        "curl https://example.com",
        "python3 -c 'print(1)'",
        "sleep 1 &",
    ],
)
def test_exec_command_rejects_escape_and_dangerous_commands(
    tmp_path: Path,
    command: str,
) -> None:
    result = _execute(tmp_path, {"command": command})

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "COMMAND_DENIED"


def test_exec_command_builds_fixed_isolated_argv_and_structured_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    workspace = (tmp_path / "workspace").resolve()
    monkeypatch.setattr(
        command_module,
        "_bubblewrap_executable",
        lambda: "/usr/bin/bwrap",
    )

    def fake_run(
        argv,
        *,
        cwd: Path,
        timeout_seconds: float,
        output_limit: int,
    ) -> command_module._BoundedProcessResult:
        captured.update(
            argv=list(argv),
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            output_limit=output_limit,
        )
        return command_module._BoundedProcessResult(
            returncode=0,
            stdout=b"done\n",
            stderr=b"",
            stdout_bytes=5,
            stderr_bytes=0,
            timed_out=False,
            output_truncated=False,
            duration_ms=7,
        )

    monkeypatch.setattr(command_module, "_run_bounded_process", fake_run)

    result = _execute(
        tmp_path,
        {
            "command": "printf done",
            "timeout_seconds": 2,
            "max_output_bytes": 512,
        },
    )

    assert result.success is True
    assert result.data is not None
    assert result.data["status"] == "completed"
    assert result.data["ok"] is True
    assert result.data["stdout"] == "done\n"
    assert result.data["cwd"] == "."
    assert result.data["sandbox"] == "bubblewrap"
    assert captured["cwd"] == workspace
    assert captured["timeout_seconds"] == 2
    assert captured["output_limit"] == 512
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[0] == "/usr/bin/bwrap"
    assert "--unshare-net" in argv
    assert "--clearenv" in argv
    assert _option_values(argv, "--remount-ro") == ["/"]
    assert _option_values(argv, "--bind") == [str(workspace), "/workspace"]
    assert _option_values(argv, "--chdir") == ["/workspace"]
    assert argv[-6:] == [
        "--",
        "/bin/bash",
        "--noprofile",
        "--norc",
        "-c",
        "printf done",
    ]


def test_exec_command_rejects_workspace_symlinks_before_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    outside.write_text("secret")
    try:
        (workspace / "escape").symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable on this platform")
    monkeypatch.setattr(
        command_module,
        "_bubblewrap_executable",
        lambda: "/usr/bin/bwrap",
    )

    result = _execute(tmp_path, {"command": "cat escape"})

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "SYMLINK_NOT_ALLOWED"


def test_bounded_subprocess_caps_output_and_terminates_timeout(tmp_path: Path) -> None:
    output = command_module._run_bounded_process(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 4096)"],
        cwd=tmp_path,
        timeout_seconds=2,
        output_limit=128,
    )

    assert output.returncode == 0
    assert output.stdout_bytes == 4096
    assert len(output.stdout) == 128
    assert output.output_truncated is True

    timeout = command_module._run_bounded_process(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        cwd=tmp_path,
        timeout_seconds=0.1,
        output_limit=128,
    )

    assert timeout.timed_out is True
    assert timeout.duration_ms < 1500


def test_exec_command_output_is_capped_by_harness_result_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, int] = {}
    monkeypatch.setattr(
        command_module,
        "_bubblewrap_executable",
        lambda: "/usr/bin/bwrap",
    )

    def fake_run(
        _argv,
        *,
        cwd: Path,
        timeout_seconds: float,
        output_limit: int,
    ) -> command_module._BoundedProcessResult:
        del cwd, timeout_seconds
        observed["output_limit"] = output_limit
        return command_module._BoundedProcessResult(
            returncode=-9,
            stdout=b"partial",
            stderr=b"",
            stdout_bytes=1000,
            stderr_bytes=0,
            timed_out=True,
            output_truncated=True,
            duration_ms=100,
        )

    monkeypatch.setattr(command_module, "_run_bounded_process", fake_run)
    limits = HarnessLimits(
        max_read_bytes=1024,
        max_file_bytes=1024,
        max_workspace_bytes=4096,
        max_entries=10,
        max_result_bytes=4096,
    )

    result = _execute(
        tmp_path,
        {"command": "sleep 2", "max_output_bytes": 4096},
        limits=limits,
    )

    assert result.success is True
    assert result.data is not None
    assert result.data["status"] == "timed_out"
    assert result.data["ok"] is False
    assert result.data["exit_code"] is None
    assert observed["output_limit"] == 1024


def _execute(
    tmp_path: Path,
    arguments: dict[str, object],
    *,
    limits: HarnessLimits | None = None,
):
    context = HarnessToolContext(
        run_id="run",
        task_frame_id="frame",
        workspace_root=(tmp_path / "workspace").resolve(),
        limits=limits or HarnessLimits(),
    )
    return HarnessExecutor(build_command_tool_registry()).execute(
        context,
        HarnessToolCall(
            call_id="call-exec-command",
            name="exec_command",
            arguments=arguments,
        ),
    )


def _option_values(argv: list[str], option: str) -> list[str]:
    index = argv.index(option)
    if option == "--bind":
        return argv[index + 1 : index + 3]
    return argv[index + 1 : index + 2]
