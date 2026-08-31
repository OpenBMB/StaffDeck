"""Verify release packages keep their required runtime and build-time contracts."""

import importlib
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import yaml

# 渠道(微信/企微)打包必需依赖:PyInstaller hiddenimports 防回归删漏
REQUIRED_MODULES = ("aibot", "websockets", "aiohttp", "pyee", "dotenv", "cryptography")


def test_packaging_dependencies_importable() -> None:
    for module in REQUIRED_MODULES:
        importlib.import_module(module)


def test_pyinstaller_spec_keeps_channel_hiddenimports() -> None:
    spec_path = Path(__file__).resolve().parents[2] / "packaging" / "ultrarag.spec"
    content = spec_path.read_text(encoding="utf-8")
    for module in REQUIRED_MODULES:
        assert f'"{module}"' in content, f"packaging/ultrarag.spec 缺少 hiddenimport: {module}"


def test_macos_bundle_keeps_webkit_packaging_support() -> None:
    root = Path(__file__).resolve().parents[2]
    spec = (root / "packaging" / "ultrarag.spec").read_text(encoding="utf-8")
    build_script = (root / "packaging" / "build_macos.sh").read_text(encoding="utf-8")
    pyproject = (root / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    assert '"WebKit"' in spec
    assert "pyobjc-framework-WebKit" in build_script
    assert "pyobjc-framework-WebKit" in pyproject


def test_pyinstaller_bundle_contains_the_build_version_resource() -> None:
    spec_path = Path(__file__).resolve().parents[2] / "packaging" / "ultrarag.spec"
    content = spec_path.read_text(encoding="utf-8")
    assert 'staffdeck-version.txt' in content
    assert '(str(VERSION_FILE), ".")' in content


def test_pyinstaller_bundle_contains_lark_sdk_metadata() -> None:
    spec_path = Path(__file__).resolve().parents[2] / "packaging" / "ultrarag.spec"
    content = spec_path.read_text(encoding="utf-8")
    assert 'copy_metadata("lark-channel-sdk")' in content


def test_release_builds_run_packaged_lark_sdk_smoke() -> None:
    packaging_dir = Path(__file__).resolve().parents[2] / "packaging"
    for script_name in ("build_macos.sh", "build_linux.sh", "build_windows.ps1"):
        content = (packaging_dir / script_name).read_text(encoding="utf-8")
        assert "--packaging-smoke" in content, f"{script_name} 未校验冻结产物中的 Lark SDK"


def test_windows_release_supports_external_signer_and_fails_closed() -> None:
    root = Path(__file__).resolve().parents[2]
    build = (root / "packaging" / "build_windows.ps1").read_text(encoding="utf-8")
    signer = (root / "packaging" / "sign_windows.ps1").read_text(encoding="utf-8")

    assert "WINDOWS_SIGNER_SCRIPT" in build
    assert "UNSIGNED" in build
    assert 'Get-AuthenticodeSignature -FilePath $target' in signer
    assert '$signature.Status -ne "Valid"' in signer
    for extension in ('".exe"', '".dll"', '".pyd"', '".node"'):
        assert extension in build


def test_pyinstaller_bundle_contains_distribution_metadata_resource() -> None:
    """Catch packaged applications omitting their immutable release repository identity."""
    spec_path = Path(__file__).resolve().parents[2] / "packaging" / "ultrarag.spec"
    content = spec_path.read_text(encoding="utf-8")

    assert "staffdeck-distribution.json" in content
    assert "DISTRIBUTION_METADATA_FILE" in content
    assert '(str(DISTRIBUTION_METADATA_FILE), ".")' in content


def test_release_workflow_binds_distribution_to_running_repository() -> None:
    """Catch release jobs building packages that trust a hard-coded distributor."""
    workflow_path = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "release.yml"
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    build_step = next(step for step in workflow["jobs"]["build"]["steps"] if step.get("name") == "Build")

    assert build_step["env"]["STAFFDECK_RELEASE_REPOSITORY"] == "${{ github.repository }}"


def test_pyinstaller_spec_imports_backend_modules_before_analysis(tmp_path: Path) -> None:
    """Run the real spec in isolation; writes stay in tmp_path and import failures fail the test."""
    source_root = Path(__file__).resolve().parents[2]
    isolated_root = tmp_path / "checkout"
    isolated_backend = isolated_root / "backend"
    isolated_packaging = isolated_root / "packaging"
    isolated_app = isolated_backend / "app"

    isolated_app.mkdir(parents=True)
    isolated_packaging.mkdir(parents=True)
    (isolated_root / "frontend-enterprise" / "dist").mkdir(parents=True)
    shutil.copy2(source_root / "packaging" / "ultrarag.spec", isolated_packaging / "ultrarag.spec")
    shutil.copy2(source_root / "backend" / "app" / "distribution.py", isolated_app / "distribution.py")
    (isolated_app / "__init__.py").write_text("", encoding="utf-8")
    (isolated_backend / "VERSION").write_text("0.0.0-test\n", encoding="utf-8")

    spec_runner = textwrap.dedent(
        """
        import runpy
        import sys
        import types

        pyinstaller = types.ModuleType("PyInstaller")
        pyinstaller.__path__ = []
        utils = types.ModuleType("PyInstaller.utils")
        utils.__path__ = []
        hooks = types.ModuleType("PyInstaller.utils.hooks")
        hooks.collect_data_files = lambda *args, **kwargs: []
        hooks.collect_submodules = lambda *args, **kwargs: []
        hooks.copy_metadata = lambda *args, **kwargs: []
        sys.modules.update(
            {
                "PyInstaller": pyinstaller,
                "PyInstaller.utils": utils,
                "PyInstaller.utils.hooks": hooks,
            }
        )

        def analysis(*args, **kwargs):
            'Return the minimal Analysis shape consumed by the spec.'
            return types.SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[])

        def build_target(*args, **kwargs):
            'Stand in for build targets without producing filesystem artifacts.'
            return types.SimpleNamespace()

        runpy.run_path(
            sys.argv[1],
            init_globals={
                "Analysis": analysis,
                "PYZ": build_target,
                "EXE": build_target,
                "COLLECT": build_target,
                "BUNDLE": build_target,
            },
        )
        """
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", spec_runner, str(isolated_packaging / "ultrarag.spec")],
        cwd=isolated_backend,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
