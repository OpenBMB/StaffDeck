from __future__ import annotations

import os
import subprocess
import sys
import threading
import venv
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO, Iterator

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

from app.config import get_settings

IMPORT_NAMES = {
    "beautifulsoup4": "bs4",
    "python-docx": "docx",
    "python-dateutil": "dateutil",
}
_RUNTIME_PREPARE_LOCK = threading.RLock()


class GeneralSkillRuntimeError(RuntimeError):
    pass


def ensure_runtime_python() -> Path:
    with _RUNTIME_PREPARE_LOCK:
        settings = get_settings()
        python_path = _resolve_runtime_python(
            settings.general_skill_runtime_python,
            settings.general_skill_runtime_venv,
        )
        with _runtime_prepare_file_lock(python_path):
            if not python_path.exists():
                _create_runtime_venv(python_path)
            if settings.general_skill_runtime_auto_install and settings.general_skill_network_install:
                _ensure_packages(python_path, settings.general_skill_runtime_package_list)
        return python_path


def runtime_environment(
    base_env: dict[str, str] | None = None,
    *,
    python_path: Path | None = None,
) -> dict[str, str]:
    python_path = python_path or ensure_runtime_python()
    env = dict(base_env or os.environ)
    bin_dir = python_path.parent
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["VIRTUAL_ENV"] = str(bin_dir.parent)
    env["GENERAL_SKILL_RUNTIME_PYTHON"] = str(python_path)
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def _backend_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def _bundled_python() -> Path:
    # 打包态：附带的 python-build-standalone 位置随平台不同。
    # - macOS .app：runtime 在 Contents/Resources/runtime（放 Resources 是为了 codesign 密封通过），
    #   sys.executable 在 Contents/MacOS/staffdeck，需跳到同级的 Resources。
    # - Linux/Windows onedir：runtime 在可执行文件同级 runtime/。
    # 不用 resource_dir()==sys._MEIPASS（onedir 下指向 _internal/）。
    exe_dir = Path(sys.executable).resolve().parent
    if sys.platform == "darwin" and exe_dir.name == "MacOS" and exe_dir.parent.name == "Contents":
        root = exe_dir.parent / "Resources" / "runtime"
    else:
        root = exe_dir / "runtime"
    return (root / "python.exe") if sys.platform == "win32" else (root / "bin" / "python3")


def _resolve_runtime_python(explicit_python: str, explicit_venv: str) -> Path:
    if explicit_python.strip():
        return Path(explicit_python).expanduser()
    if explicit_venv.strip():
        return _python_in_venv(Path(explicit_venv).expanduser())
    from app import paths
    if paths.is_frozen() and _bundled_python().exists():
        return _bundled_python()
    backend_venv = _backend_dir() / ".venv"
    if _python_in_venv(backend_venv).exists():
        return _python_in_venv(backend_venv)
    return _python_in_venv(_backend_dir() / ".runtime_venv")


def _python_in_venv(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _create_runtime_venv(python_path: Path) -> None:
    venv_dir = python_path.parent.parent
    venv_dir.mkdir(parents=True, exist_ok=True)
    venv.EnvBuilder(with_pip=True, clear=False).create(venv_dir)
    if not python_path.exists():
        raise GeneralSkillRuntimeError(f"通用技能运行环境创建失败：{python_path}")


def _ensure_packages(python_path: Path, packages: list[str]) -> None:
    settings = get_settings()
    requirements = [_parse_requirement(package) for package in packages]
    missing = [
        str(requirement)
        for requirement in requirements
        if not _requirement_satisfied(python_path, requirement)
        or not _can_import(python_path, _import_name(requirement.name))
    ]
    if not missing:
        return
    args = [str(python_path), "-m", "pip", "install", *missing]
    if settings.general_skill_pip_index_url.strip():
        args += ["-i", settings.general_skill_pip_index_url.strip()]
    env = os.environ.copy()
    try:
        import certifi
        env["SSL_CERT_FILE"] = certifi.where()
        env["PIP_CERT"] = certifi.where()
    except (ImportError, OSError):
        pass
    result = subprocess.run(
        args,
        cwd=str(_backend_dir()),
        text=True,
        capture_output=True,
        timeout=settings.general_skill_pip_timeout_seconds,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        raise GeneralSkillRuntimeError(
            "通用技能运行环境依赖安装失败："
            + ", ".join(missing)
            + "\n"
            + (result.stderr or result.stdout or "").strip()
        )


def _can_import(python_path: Path, import_name: str) -> bool:
    result = subprocess.run(
        [
            str(python_path),
            "-c",
            "import importlib,sys; importlib.import_module(sys.argv[1])",
            import_name,
        ],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    return result.returncode == 0


def _requirement_satisfied(python_path: Path, requirement: Requirement) -> bool:
    if requirement.marker is not None and not requirement.marker.evaluate():
        return True
    result = subprocess.run(
        [
            str(python_path),
            "-c",
            "import importlib.metadata,sys; print(importlib.metadata.version(sys.argv[1]))",
            requirement.name,
        ],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        return False
    try:
        version = Version(result.stdout.strip())
    except InvalidVersion:
        return False
    return not requirement.specifier or version in requirement.specifier


def _parse_requirement(value: str) -> Requirement:
    try:
        requirement = Requirement(value)
    except InvalidRequirement as exc:
        raise GeneralSkillRuntimeError(f"通用技能依赖声明无效：{value}") from exc
    if requirement.url:
        raise GeneralSkillRuntimeError("通用技能自动依赖仅支持包索引中的版本约束。")
    return requirement


def _import_name(package: str) -> str:
    normalized = package.strip()
    return IMPORT_NAMES.get(normalized, normalized.replace("-", "_"))


@contextmanager
def _runtime_prepare_file_lock(python_path: Path) -> Iterator[None]:
    from app import paths

    digest = sha256(str(python_path.resolve()).encode("utf-8")).hexdigest()[:16]
    lock_path = paths.user_data_dir() / f"runtime-prepare-{digest}.lock"
    handle = lock_path.open("a+b")
    try:
        _lock_file(handle)
        yield
    finally:
        _unlock_file(handle)
        handle.close()


def _lock_file(handle: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
