import importlib
from pathlib import Path

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
