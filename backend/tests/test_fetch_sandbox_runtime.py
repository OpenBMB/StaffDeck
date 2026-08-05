from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "packaging" / "fetch_sandbox_runtime.py"
SPEC = importlib.util.spec_from_file_location("staffdeck_fetch_sandbox_runtime", SCRIPT)
assert SPEC and SPEC.loader
fetch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch)


def test_reviewed_node_archives_have_built_in_hashes() -> None:
    expected = {
        "darwin-arm64", "darwin-x64", "linux-arm64", "linux-x64",
        "win-arm64", "win-x64",
    }

    assert expected == {
        filename.removeprefix("node-v22.14.0-").removesuffix(".tar.gz").removesuffix(".zip")
        for filename in fetch.NODE_SHA256
    }


def test_windows_machine_falls_back_to_process_bitness(monkeypatch) -> None:
    monkeypatch.setattr(fetch.platform, "machine", lambda: "")
    monkeypatch.setattr(fetch.platform, "system", lambda: "Windows")
    monkeypatch.delenv("PROCESSOR_ARCHITECTURE", raising=False)
    monkeypatch.setattr(fetch.sys, "maxsize", 2**63 - 1)

    assert fetch._machine() == "amd64"


def test_srt_lock_integrity_is_enforced(tmp_path: Path) -> None:
    lock = json.loads(fetch.PACKAGE_LOCK.read_text(encoding="utf-8"))
    (tmp_path / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    for relative, package in lock["packages"].items():
        if not relative:
            continue
        package_root = tmp_path / relative
        package_root.mkdir(parents=True)
        (package_root / "package.json").write_text(
            json.dumps({"version": package["version"]}), encoding="utf-8"
        )
    fetch._verify_srt_integrity(tmp_path)

    lock["packages"]["node_modules/zod"]["integrity"] = "sha512-changed"
    (tmp_path / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(SystemExit, match="dependency graph"):
        fetch._verify_srt_integrity(tmp_path)
