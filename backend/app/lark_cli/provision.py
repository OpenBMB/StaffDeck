"""lark-cli 二进制供给。

**懒加载**：安装发生在首次真正执行 lark-cli 命令时（``service.invoke_lark_cli``
调用 ``ensure_lark_cli``），不在服务启动、不在构建期；源码树里不含任何二进制。
代价是首个用户的首次调用会同步等待安装（约 46MB，超时上限 600s）——换来的是
从不使用飞书能力的部署完全不产生下载。

解析顺序：
1. ``lark_cli_binary_path``：管理员显式指定，不存在即报错（离线部署走这条）。
2. 受管副本：``user_data_dir()/lark_cli/runtime/node_modules/@larksuite/cli/bin/``，
   存在即直接使用，无任何网络动作。
3. 自动安装（``lark_cli_auto_install`` 为真时）：``npm install`` 官方包后，
   显式执行包内 ``scripts/install.js`` 下载对应平台二进制。

安装的三个刻意选择：版本锁死在 ``PINNED_VERSION`` 且不自动升级（上游变更不得
静默改变生产行为）；``--ignore-scripts`` 关掉 npm 生命周期脚本、改为显式调用官方
安装器，使"下载二进制"是一次有意为之的动作而非装包副作用；二进制完整性由官方
安装器自带的 checksums.txt SHA-256 校验保证，不重复实现。

并发安全：进程内 RLock + 跨进程文件锁，模式照抄 general_skills/runtime_env.py。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from app import paths
from app.config import get_settings

PINNED_VERSION = "1.0.89"
_INSTALL_TIMEOUT_SECONDS = 600.0
_PREPARE_LOCK = threading.RLock()


class LarkCliProvisionError(Exception):
    pass


def install_root() -> Path:
    return paths.user_data_dir() / "lark_cli" / "runtime"


def _managed_binary_path() -> Path:
    name = "lark-cli.exe" if sys.platform == "win32" else "lark-cli"
    return install_root() / "node_modules" / "@larksuite" / "cli" / "bin" / name


def ensure_lark_cli() -> Path:
    """返回可执行的 lark-cli 路径；必要时自动安装。"""

    configured = str(get_settings().lark_cli_binary_path or "").strip()
    if configured:
        binary = Path(configured).expanduser()
        if not binary.is_file():
            raise LarkCliProvisionError(
                f"lark_cli_binary_path 指向的文件不存在：{binary}"
            )
        return binary
    binary = _managed_binary_path()
    if binary.is_file():
        return binary
    if not get_settings().lark_cli_auto_install:
        raise LarkCliProvisionError(
            "lark-cli 未安装且自动安装已关闭；请手动安装后配置 lark_cli_binary_path。"
        )
    with _PREPARE_LOCK, _provision_file_lock():
        if binary.is_file():
            return binary
        _install_managed_copy()
    if not binary.is_file():
        raise LarkCliProvisionError("lark-cli 安装流程结束但未找到二进制。")
    return binary


def _install_managed_copy() -> None:
    npm = shutil.which("npm")
    node = shutil.which("node")
    if not npm or not node:
        raise LarkCliProvisionError(
            "自动安装 lark-cli 需要 node/npm；请安装 Node.js 或手动配置 lark_cli_binary_path。"
        )
    root = install_root()
    root.mkdir(parents=True, exist_ok=True)
    _run_step(
        [
            npm,
            "install",
            f"@larksuite/cli@{PINNED_VERSION}",
            "--prefix",
            str(root),
            "--no-audit",
            "--no-fund",
            # 关掉生命周期脚本：下载二进制改由下面显式调用官方安装器完成。
            "--ignore-scripts",
        ],
        cwd=root,
        step="npm install",
    )
    package_dir = root / "node_modules" / "@larksuite" / "cli"
    installer = package_dir / "scripts" / "install.js"
    if not installer.is_file():
        raise LarkCliProvisionError("npm 安装完成但官方包内缺少 scripts/install.js。")
    # 官方 install.js 自带 checksums.txt SHA-256 校验，二进制完整性由它保证。
    _run_step([node, str(installer)], cwd=package_dir, step="binary download")


def _run_step(argv: list[str], *, cwd: Path, step: str) -> None:
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_INSTALL_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise LarkCliProvisionError(f"lark-cli {step} 超时。") from exc
    except OSError as exc:
        raise LarkCliProvisionError(f"lark-cli {step} 启动失败：{exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-2_000:]
        raise LarkCliProvisionError(
            f"lark-cli {step} 失败（exit {completed.returncode}）：{detail}"
        )


@contextmanager
def _provision_file_lock() -> Iterator[None]:
    lock_path = paths.user_data_dir() / "lark-cli-provision.lock"
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
