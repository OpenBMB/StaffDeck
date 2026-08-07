from __future__ import annotations

from pathlib import Path

import pytest

from app.harness import sandbox
from app.harness.errors import HarnessExecutionError


def _make_bundle(root: Path) -> None:
    (root / "bin").mkdir(parents=True)
    node = root / "bin" / sandbox._node_name()
    node.write_bytes(b"node")
    node.chmod(0o755)
    cli = root / "node_modules" / "@anthropic-ai" / "sandbox-runtime" / "dist"
    cli.mkdir(parents=True)
    (cli / "cli.js").write_text("// test", encoding="utf-8")


def test_resolve_srt_uses_explicit_bundle(monkeypatch, tmp_path: Path) -> None:
    _make_bundle(tmp_path)
    monkeypatch.setenv("STAFFDECK_SRT_RUNTIME", str(tmp_path))
    resolved = sandbox.resolve_srt()
    assert resolved == (
        (tmp_path / "bin" / sandbox._node_name()).resolve(),
        (tmp_path / "node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js").resolve(),
    )


def test_resolve_srt_prefers_source_bundle_over_global_install(
    monkeypatch, tmp_path: Path
) -> None:
    source_bundle = tmp_path / "source"
    global_bundle = tmp_path / "global"
    _make_bundle(source_bundle)
    _make_bundle(global_bundle)
    monkeypatch.delenv("STAFFDECK_SRT_RUNTIME", raising=False)
    monkeypatch.setattr(sandbox, "_source_runtime_root", lambda: source_bundle)
    monkeypatch.setattr(
        sandbox.shutil,
        "which",
        lambda name: str(
            global_bundle / "bin" / sandbox._node_name()
            if name == sandbox._node_name()
            else global_bundle / "node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js"
        ),
    )
    assert sandbox.resolve_srt() == (
        (source_bundle / "bin" / sandbox._node_name()).resolve(),
        (source_bundle / "node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js").resolve(),
    )


def test_resolve_srt_ignores_global_install_without_explicit_opt_in(
    monkeypatch, tmp_path: Path
) -> None:
    global_bundle = tmp_path / "global"
    _make_bundle(global_bundle)
    monkeypatch.delenv("STAFFDECK_SRT_RUNTIME", raising=False)
    monkeypatch.delenv("STAFFDECK_ALLOW_GLOBAL_SRT", raising=False)
    monkeypatch.setattr(sandbox, "_source_runtime_root", lambda: tmp_path / "missing")
    monkeypatch.setattr(
        sandbox.shutil,
        "which",
        lambda name: str(
            global_bundle / "bin" / sandbox._node_name()
            if name == sandbox._node_name()
            else global_bundle / "node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js"
        ),
    )

    assert sandbox.resolve_srt() is None


def test_resolve_srt_allows_global_install_with_explicit_opt_in(
    monkeypatch, tmp_path: Path
) -> None:
    global_bundle = tmp_path / "global"
    _make_bundle(global_bundle)
    monkeypatch.delenv("STAFFDECK_SRT_RUNTIME", raising=False)
    monkeypatch.setenv("STAFFDECK_ALLOW_GLOBAL_SRT", "true")
    monkeypatch.setattr(sandbox, "_source_runtime_root", lambda: tmp_path / "missing")
    monkeypatch.setattr(
        sandbox.shutil,
        "which",
        lambda name: str(
            global_bundle / "bin" / sandbox._node_name()
            if name == "node"
            else global_bundle / "node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js"
        ),
    )

    assert sandbox.resolve_srt() == (
        (global_bundle / "bin" / sandbox._node_name()).resolve(),
        (global_bundle / "node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js").resolve(),
    )


def test_network_policy_defaults_only_when_missing() -> None:
    assert sandbox.parse_network_policy(None) == "all"
    assert sandbox.parse_network_policy("") == "all"
    assert sandbox.parse_network_policy("deny") == "deny"
    with pytest.raises(HarnessExecutionError) as invalid:
        sandbox.parse_network_policy("unexpected")
    assert invalid.value.error.code == "SANDBOX_POLICY_INVALID"


def test_missing_bundle_does_not_report_sandbox(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("STAFFDECK_SRT_RUNTIME", str(tmp_path))
    monkeypatch.setattr(
        sandbox,
        "_source_runtime_root",
        lambda: tmp_path / "missing-source-bundle",
    )
    monkeypatch.setattr(sandbox, "_trusted_executable", lambda _name: False)
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: None)
    assert sandbox.resolve_srt() is None
    assert sandbox.available_backend() is None


def test_diagnostics_rejects_root_before_starting_srt(monkeypatch, tmp_path: Path) -> None:
    _make_bundle(tmp_path)
    monkeypatch.setenv("STAFFDECK_SRT_RUNTIME", str(tmp_path))
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(sandbox, "available_backend", lambda: "srt")

    report = sandbox.diagnostics()

    assert report.status == "unavailable"
    assert report.code == "SANDBOX_ROOT_USER"
    assert "普通服务账号" in (report.remediation or "")


def test_diagnostics_allows_root_for_bubblewrap_when_userns_is_available(
    monkeypatch,
) -> None:
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(sandbox, "available_backend", lambda: "bubblewrap")
    monkeypatch.setattr(sandbox, "_read_int", lambda _path: 100)

    report = sandbox.diagnostics()

    assert report.status == "ready"
    assert report.backend == "bubblewrap"


def test_diagnostics_rejects_disabled_user_namespaces(monkeypatch, tmp_path: Path) -> None:
    _make_bundle(tmp_path)
    monkeypatch.setenv("STAFFDECK_SRT_RUNTIME", str(tmp_path))
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.os, "geteuid", lambda: 1001, raising=False)
    monkeypatch.setattr(sandbox, "available_backend", lambda: "srt")
    monkeypatch.setattr(sandbox, "_read_int", lambda path: 0 if "unprivileged" in path else 100)

    report = sandbox.diagnostics()

    assert report.status == "unavailable"
    assert report.code == "SANDBOX_USERNS_DISABLED"


def test_windows_diagnostics_requires_successful_srt_initialization(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sandbox.sys, "platform", "win32")
    _make_bundle(tmp_path)
    monkeypatch.setenv("STAFFDECK_SRT_RUNTIME", str(tmp_path))
    monkeypatch.setattr(sandbox, "_windows_srt_ready", lambda *_args: False)

    report = sandbox.diagnostics()

    assert report.status == "degraded"
    assert report.code == "SANDBOX_UNSANDBOXED_FALLBACK"
    assert report.backend == "unsandboxed"
    assert "高风险" in (report.remediation or "")


def test_windows_srt_probe_uses_bundle_directory(monkeypatch, tmp_path: Path) -> None:
    _make_bundle(tmp_path)
    node = (tmp_path / "bin" / sandbox._node_name()).resolve()
    cli = (
        tmp_path / "node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js"
    ).resolve()
    observed: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        observed["cwd"] = kwargs["cwd"]
        return sandbox.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox._windows_srt_ready.cache_clear()

    assert sandbox._windows_srt_ready(node, cli) is True
    assert observed["cwd"] == node.parent


def test_windows_install_command_uses_bundled_node_and_cli(monkeypatch, tmp_path: Path) -> None:
    _make_bundle(tmp_path)
    monkeypatch.setenv("STAFFDECK_SRT_RUNTIME", str(tmp_path))

    command = sandbox.windows_install_command()

    assert str((tmp_path / "bin" / "node").resolve()) in command
    assert "cli.js" in command
    assert command.endswith("windows-install")
