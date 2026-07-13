# packaging/fetch_runtime_python.py
# 用法: python packaging/fetch_runtime_python.py <dest_dir> [--expect-arch arm64|x86_64]
#
# 下载对应平台的 python-build-standalone（install_only 变体自带 pip），
# 预装通用技能高频包后，供 build 脚本拷进最终产物的 runtime/ 目录。
import os
import platform
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

# 已知稳定 release（执行前用 curl -sI 核实资产可达；失效则更新此处并同步 docs）
BASE = "https://github.com/astral-sh/python-build-standalone/releases/download/20240415"
ASSETS = {
    ("Darwin", "arm64"): "cpython-3.11.9+20240415-aarch64-apple-darwin-install_only.tar.gz",
    ("Darwin", "x86_64"): "cpython-3.11.9+20240415-x86_64-apple-darwin-install_only.tar.gz",
    ("Linux", "x86_64"): "cpython-3.11.9+20240415-x86_64-unknown-linux-gnu-install_only.tar.gz",
    ("Windows", "AMD64"): "cpython-3.11.9+20240415-x86_64-pc-windows-msvc-install_only.tar.gz",
}
# 预装清单（第一版）：含 Word(python-docx) / Excel(openpyxl) 处理；不预装 pandas/numpy（体积大，用户需要时联网装）
PRELOAD = ["requests", "httpx", "beautifulsoup4", "lxml", "python-docx",
           "openpyxl", "python-dateutil"]

# 架构别名归一：Windows 返回 AMD64，mac/linux 返回 x86_64/arm64/aarch64
ARCH_ALIASES = {
    "amd64": "x86_64", "x86_64": "x86_64", "x64": "x86_64",
    "arm64": "arm64", "aarch64": "arm64",
}


def _norm_arch(value: str) -> str:
    return ARCH_ALIASES.get(value.lower(), value.lower())


def _machine() -> str:
    machine = platform.machine() or os.environ.get("PROCESSOR_ARCHITECTURE", "")
    if machine:
        return machine
    if platform.system() == "Windows" and sys.maxsize > 2**32:
        return "AMD64"
    return machine


def main(argv: list[str]) -> int:
    dest = argv[0]
    expect_arch = None
    if "--expect-arch" in argv:
        expect_arch = argv[argv.index("--expect-arch") + 1]

    key = (platform.system(), _machine())
    if key not in ASSETS:
        print(f"不支持的平台/架构: {key}", file=sys.stderr)
        return 3
    # 确保附带 Python 架构与 PyInstaller 产物架构一致（归一化后比较）
    if expect_arch and _norm_arch(expect_arch) != _norm_arch(key[1]):
        print(f"架构不匹配: 期望 {expect_arch}, 实际 {key[1]}", file=sys.stderr)
        return 4

    asset = ASSETS[key]
    dest_dir = Path(dest)
    # 清理旧残留，保证干净解压（避免重复运行时 pip/site-packages 半升级损坏）
    if dest_dir.exists():
        import shutil
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    tgz = dest_dir / asset
    print(f"下载 {BASE}/{asset} ...")
    urllib.request.urlretrieve(f"{BASE}/{asset}", tgz)
    with tarfile.open(tgz) as tar:
        tar.extractall(dest_dir)
    py = dest_dir / "python" / ("python.exe" if key[0] == "Windows" else "bin/python3")

    # install_only 变体自带 pip（24.0），直接装预装包；不升级 pip（升级易触发 resolvelib 半损坏）
    subprocess.run(
        [
            str(py), "-m", "pip", "install",
            "--no-cache-dir", "--disable-pip-version-check",
            "--timeout", "30", "--retries", "5",
            *PRELOAD,
        ],
        check=True,
    )

    # 验证附带 Python 的 SSL 证书 + 关键包可用（否则技能里 https/word/excel 会失败）
    check = subprocess.run(
        [str(py), "-c",
         "import ssl, requests, docx, openpyxl; "
         "print(ssl.get_default_verify_paths().cafile or 'certifi')"],
        capture_output=True, text=True,
    )
    if check.returncode != 0:
        print(f"附带 Python 自检失败（ssl/requests/docx/openpyxl）：{check.stderr}", file=sys.stderr)
        return 5
    print(f"runtime ready at {py} (ssl: {check.stdout.strip()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
