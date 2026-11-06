from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.harness import HarnessExecutor, HarnessToolCall, HarnessToolContext, HarnessRegistry
from app.harness import skill_script as skill_script_module
from app.harness.skill_script import register_skill_script_tools


def test_run_skill_script_uses_typed_argv_for_materialized_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / ".harness" / "skill-packages" / "demo-digest"
    package.mkdir(parents=True)
    script = package / "runner.py"
    script.write_text("print('ok')", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_run_sandboxed_process(**kwargs: object):
        captured.update(kwargs)
        return SimpleNamespace(
            returncode=0,
            timed_out=False,
            stdout=b"ok\n",
            stderr=b"",
            stdout_bytes=3,
            stderr_bytes=0,
            output_truncated=False,
            duration_ms=5,
            isolation_mode="test",
        )

    monkeypatch.setattr(
        skill_script_module,
        "run_sandboxed_process",
        fake_run_sandboxed_process,
    )
    monkeypatch.setattr(
        skill_script_module,
        "_script_argv",
        lambda path, argv: ["python-runtime", str(path), *argv],
    )
    registry = register_skill_script_tools(HarnessRegistry())
    executor = HarnessExecutor(registry)

    result = executor.execute(
        HarnessToolContext(
            run_id="run-test",
            workspace_root=tmp_path.resolve(),
            sandbox_enabled=False,
        ),
        HarnessToolCall(
            call_id="call-test",
            name="run_skill_script",
            arguments={
                "script_path": script.relative_to(tmp_path).as_posix(),
                "argv": ["--city", "北京"],
                "stdin": "input",
            },
        ),
    )

    assert result.success is True
    assert result.data is not None
    assert result.data["ok"] is True
    assert captured["argv"] == ["python-runtime", str(script), "--city", "北京"]
    assert captured["stdin_bytes"] == b"input"


def test_run_skill_script_reports_nonzero_exit_as_failed_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / ".harness" / "skill-packages" / "demo-digest"
    package.mkdir(parents=True)
    script = package / "runner.py"
    script.write_text("raise SystemExit(2)", encoding="utf-8")

    monkeypatch.setattr(
        skill_script_module,
        "run_sandboxed_process",
        lambda **_kwargs: SimpleNamespace(
            returncode=2,
            timed_out=False,
            stdout=b"",
            stderr=b"failed\n",
            stdout_bytes=0,
            stderr_bytes=7,
            output_truncated=False,
            duration_ms=4,
            isolation_mode="test",
        ),
    )
    registry = register_skill_script_tools(HarnessRegistry())
    result = HarnessExecutor(registry).execute(
        HarnessToolContext(
            run_id="run-test-failed",
            workspace_root=tmp_path.resolve(),
            sandbox_enabled=False,
        ),
        HarnessToolCall(
            call_id="call-test-failed",
            name="run_skill_script",
            arguments={"script_path": script.relative_to(tmp_path).as_posix()},
        ),
    )

    assert result.success is True
    assert result.data is not None
    assert result.data["ok"] is False
    assert result.data["status"] == "failed"
    assert result.data["exit_code"] == 2
