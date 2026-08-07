from __future__ import annotations

import base64
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.harness import (
    HarnessExecutionError,
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
    monkeypatch.setattr(command_module, "available_backend", lambda: None)
    monkeypatch.setattr(
        command_module,
        "require_backend",
        lambda: (_ for _ in ()).throw(
            HarnessExecutionError("SANDBOX_UNAVAILABLE", "no sandbox")
        ),
    )

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
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    # These cases exercise the POSIX validator. Keep them independent from
    # the host platform so Windows SRT readiness does not mask the assertion.
    monkeypatch.setattr(command_module.sys, "platform", "linux")
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
    monkeypatch.setattr(command_module, "available_backend", lambda: "bubblewrap")
    monkeypatch.setattr(command_module, "available_backend", lambda: None)

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
    assert "--unshare-net" not in argv
    assert "--unshare-all" not in argv
    assert "--unshare-user" in argv
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


def test_exec_command_allows_newline_separated_statements_inside_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(command_module, "available_backend", lambda: "bubblewrap")
    monkeypatch.setattr(
        command_module,
        "_bubblewrap_executable",
        lambda: "/usr/bin/bwrap",
    )

    def fake_run(argv, **_kwargs):
        captured["argv"] = list(argv)
        return command_module._BoundedProcessResult(
            returncode=0,
            stdout=b"one\ntwo\n",
            stderr=b"",
            stdout_bytes=8,
            stderr_bytes=0,
            timed_out=False,
            output_truncated=False,
            duration_ms=1,
        )

    monkeypatch.setattr(command_module, "_run_bounded_process", fake_run)
    script = "printf 'one\\n'\nprintf 'two\\n'"

    result = _execute(tmp_path, {"command": script})

    assert result.success is True
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[-1] == script


def test_exec_command_validates_every_line_of_multiline_script(tmp_path: Path) -> None:
    result = _execute(
        tmp_path,
        {"command": "printf safe\ncat /etc/passwd"},
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "COMMAND_DENIED"


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


def test_bubblewrap_network_policy_is_exact(tmp_path: Path) -> None:
    workspace = tmp_path.resolve()
    common = {
        "sandbox_executable": "/usr/bin/bwrap",
        "workspace": workspace,
        "command": "true",
    }

    assert "--unshare-net" not in command_module._bubblewrap_argv(
        **common, network_mode="all"
    )
    assert "--unshare-net" in command_module._bubblewrap_argv(
        **common, network_mode="deny"
    )
    with pytest.raises(HarnessExecutionError) as unsupported:
        command_module._bubblewrap_argv(**common, network_mode="allowlist")
    assert unsupported.value.error.code == "SANDBOX_POLICY_UNSUPPORTED"


def test_srt_all_network_requires_reviewed_runtime_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(command_module, "_srt_supports_allow_all", lambda: False)

    with pytest.raises(HarnessExecutionError) as unsupported:
        command_module._write_srt_settings(tmp_path, network_mode="all")

    assert unsupported.value.error.code == "SANDBOX_POLICY_UNSUPPORTED"


def test_srt_network_settings_preserve_exact_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(command_module, "_srt_supports_allow_all", lambda: True)

    all_path = command_module._write_srt_settings(tmp_path, network_mode="all")
    allow_path = command_module._write_srt_settings(
        tmp_path, network_mode="allowlist", allowed_domains=("api.example.com",)
    )
    deny_path = command_module._write_srt_settings(tmp_path, network_mode="deny")
    try:
        assert json.loads(all_path.read_text())["network"] == {
            "allowedDomains": [],
            "deniedDomains": [],
            "strictAllowlist": True,
            "allowAllDomains": True,
        }
        assert json.loads(allow_path.read_text())["network"]["allowedDomains"] == [
            "api.example.com"
        ]
        assert json.loads(deny_path.read_text())["network"]["deniedDomains"] == ["*"]
    finally:
        all_path.unlink()
        allow_path.unlink()
        deny_path.unlink()


def test_windows_srt_relies_on_dedicated_user_for_profile_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(command_module.sys, "platform", "win32")
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        command_module,
        "get_settings",
        lambda: SimpleNamespace(database_url="sqlite:///./skill_agent_loop.db"),
    )

    settings_path = command_module._write_srt_settings(
        tmp_path / "workspace", network_mode="deny"
    )
    try:
        deny_read = json.loads(settings_path.read_text())["filesystem"]["denyRead"]
    finally:
        settings_path.unlink()

    assert "~/.ssh" not in deny_read
    assert "~/.aws" not in deny_read
    assert "~/.config" not in deny_read
    assert deny_read == []


@pytest.mark.parametrize(
    "command",
    [
        "Write-Output ok",
        "Set-Content -Path result.txt -Value ok\nGet-Content result.txt",
        "Get-ChildItem .",
        "python runner.py",
        "Invoke-WebRequest https://example.com",
    ],
)
def test_windows_validator_allows_workspace_power_shell_workflows(command: str) -> None:
    command_module._validate_windows_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "Get-Content ..\\outside.txt",
        "Get-Content C:\\Windows\\win.ini",
        "Get-Content $env:USERPROFILE\\.ssh\\id_rsa",
        "Remove-Item result.txt",
        "Start-Process powershell.exe",
        "Invoke-Expression 'Get-Process'",
    ],
)
def test_windows_validator_rejects_escape_and_nested_host_commands(command: str) -> None:
    with pytest.raises(HarnessExecutionError) as denied:
        command_module._validate_windows_command(command)

    assert denied.value.error.code == "COMMAND_DENIED"


@pytest.mark.parametrize(
    "command",
    [
        "python3 heart_png.py && python3 - <<'PY'\nprint('x')\nPY",
        "py -3 heart_png.py",
    ],
)
def test_windows_validator_explains_bash_and_python_runtime_mismatch(
    command: str,
) -> None:
    with pytest.raises(HarnessExecutionError) as denied:
        command_module._validate_windows_command(command)

    assert denied.value.error.code == "COMMAND_DENIED"
    assert "General Skill" in denied.value.error.message or "PowerShell" in denied.value.error.message


def test_packaged_windows_shell_aliases_bundled_python(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"bundled")
    monkeypatch.setattr(command_module.sys, "platform", "win32")
    monkeypatch.setattr(command_module.sys, "executable", str(tmp_path / "staffdeck.exe"))

    encoded = command_module._windows_powershell_command("python3 -c 'print(1)'").split()[-1]
    script = base64.b64decode(encoded).decode("utf-16le")

    assert "Set-Alias -Name python3" in script
    assert str(runtime / "python.exe") in script


def test_srt_protects_frozen_default_database_without_blocking_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(command_module.sys, "platform", "linux")
    data_root = (tmp_path / "data").resolve()
    workspace = data_root / "harness_workspaces" / "task"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(data_root))
    monkeypatch.setattr("app.paths.is_frozen", lambda: True)
    monkeypatch.setattr(command_module, "_srt_supports_allow_all", lambda: True)
    monkeypatch.setattr(
        command_module,
        "get_settings",
        lambda: SimpleNamespace(database_url="sqlite:///./skill_agent_loop.db"),
    )

    settings_path = command_module._write_srt_settings(
        workspace, network_mode="all"
    )
    try:
        filesystem = json.loads(settings_path.read_text())["filesystem"]
        deny_read = filesystem["denyRead"]
    finally:
        settings_path.unlink()

    database = str(data_root / "skill_agent_loop.db")
    assert database in deny_read
    assert database + "-wal" in deny_read
    assert str(data_root) in deny_read
    assert str(data_root / "logs") in deny_read
    assert str(data_root / "network.json") in deny_read
    assert str(data_root / "connector-locks") in deny_read
    assert filesystem["allowRead"] == [str(workspace)]


def test_srt_process_uses_private_short_temporary_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    settings_path = tmp_path / "srt-settings.json"
    settings_path.write_text("{}", encoding="utf-8")
    captured: dict[str, object] = {}
    settings_kwargs: dict[str, object] = {}
    monkeypatch.setattr(command_module, "available_backend", lambda: "srt")
    monkeypatch.setattr(command_module, "ensure_backend_usable", lambda _backend: None)
    def fake_settings(*_args, **kwargs):
        settings_kwargs.update(kwargs)
        return settings_path

    monkeypatch.setattr(command_module, "_write_srt_settings", fake_settings)
    monkeypatch.setattr(command_module, "_srt_argv", lambda **_kwargs: ["srt"])

    def fake_run(argv, **kwargs):
        captured.update(argv=list(argv), **kwargs)
        return command_module._BoundedProcessResult(
            returncode=0,
            stdout=b"ok",
            stderr=b"",
            stdout_bytes=2,
            stderr_bytes=0,
            timed_out=False,
            output_truncated=False,
            duration_ms=1,
        )

    monkeypatch.setattr(command_module, "_run_bounded_process", fake_run)

    command_module.run_sandboxed_process(
        workspace=workspace,
        argv=[sys.executable, "-c", "print('ok')"],
        env={"TMPDIR": "/host/tmp"},
    )

    process_env = captured["env"]
    assert isinstance(process_env, dict)
    sandbox_temp = Path(process_env["TMPDIR"])
    assert sandbox_temp.name.startswith("sd-")
    assert sandbox_temp.parent == Path(tempfile.gettempdir())
    assert settings_kwargs["sandbox_temp"] == sandbox_temp
    assert not sandbox_temp.exists()


def test_srt_argv_uses_direct_command_mode_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = tmp_path / "node.exe"
    cli = tmp_path / "cli.js"
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(command_module.sys, "platform", "win32")
    monkeypatch.setattr(command_module, "resolve_srt", lambda: (node, cli))

    argv = command_module._srt_argv(
        settings_path=settings,
        command='"C:\\Program Files\\Python\\python.exe" runner.py',
    )

    assert argv[:5] == [
        str(node),
        str(cli),
        "--settings",
        str(settings),
        "-c",
    ]
    assert argv[5].startswith("powershell.exe -NoProfile -NonInteractive -EncodedCommand ")
    decoded = base64.b64decode(argv[5].rsplit(" ", 1)[1]).decode("utf-16le")
    assert decoded.endswith('"C:\\Program Files\\Python\\python.exe" runner.py')
    assert "$OutputEncoding" in decoded


def test_windows_broker_environment_keeps_system_paths_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(command_module.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("PATH", r"C:\Windows\System32")

    result = command_module._managed_process_environment(
        {"ARGUMENTS": "safe", "OPENAI_API_KEY": "must-not-leak"}
    )

    assert result["LOCALAPPDATA"] == r"C:\Users\test\AppData\Local"
    assert result["SystemRoot"] == r"C:\Windows"
    assert result["PATH"] == r"C:\Windows\System32"
    assert result["ARGUMENTS"] == "safe"
    assert "OPENAI_API_KEY" not in result


def test_sandboxed_process_maps_structured_paths_and_cwd_for_bubblewrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = (tmp_path / "task").resolve()
    skill_dir = workspace / "run" / "skill"
    skill_dir.mkdir(parents=True)
    runner = workspace / "run" / "runner.py"
    runner.write_text("print('ok')", encoding="utf-8")
    captured: dict[str, object] = {}
    monkeypatch.setattr(command_module, "available_backend", lambda: "bubblewrap")
    monkeypatch.setattr(command_module, "_bubblewrap_executable", lambda: "/usr/bin/bwrap")

    def fake_run(argv, *, cwd, timeout_seconds, output_limit, stdin_bytes, env):
        captured.update(argv=list(argv), cwd=cwd, stdin=stdin_bytes, env=env)
        return command_module._BoundedProcessResult(
            returncode=0,
            stdout=b"{}",
            stderr=b"",
            stdout_bytes=2,
            stderr_bytes=0,
            timed_out=False,
            output_truncated=False,
            duration_ms=1,
        )

    monkeypatch.setattr(command_module, "_run_bounded_process", fake_run)
    command_module.run_sandboxed_process(
        workspace=workspace,
        argv=[sys.executable, str(runner)],
        cwd=skill_dir,
        stdin_json={"skill_workspace": str(skill_dir), "query": str(workspace)},
        stdin_path_keys=("skill_workspace",),
        env={"SKILL_WORKSPACE": str(skill_dir), "QUERY": f"inspect {workspace}-old"},
        env_path_keys=("SKILL_WORKSPACE",),
    )

    sandbox_argv = captured["argv"]
    assert isinstance(sandbox_argv, list)
    assert _option_values(sandbox_argv, "--chdir") == ["/workspace/run/skill"]
    assert "/workspace/run/runner.py" in sandbox_argv[-1]
    assert json.loads(captured["stdin"]) == {
        "skill_workspace": "/workspace/run/skill",
        "query": str(workspace),
    }
    assert captured["env"] == {
        "SKILL_WORKSPACE": "/workspace/run/skill",
        "QUERY": f"inspect {workspace}-old",
    }
    assert captured["cwd"] == skill_dir


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


def test_bounded_subprocess_terminates_when_cancelled(tmp_path: Path) -> None:
    started = command_module.time.monotonic()

    with pytest.raises(HarnessExecutionError) as cancelled:
        command_module._run_bounded_process(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            cwd=tmp_path,
            timeout_seconds=5,
            output_limit=128,
            is_cancelled=lambda: command_module.time.monotonic() - started > 0.05,
        )

    assert cancelled.value.error.code == "SANDBOX_EXECUTION_CANCELLED"
    assert command_module.time.monotonic() - started < 1.5


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
    monkeypatch.setattr(command_module, "available_backend", lambda: "bubblewrap")

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
