"""Prepare a relocatable Node + Anthropic Sandbox Runtime bundle.

The resulting directory contains ``bin/node`` and the SRT package. Runtime
code invokes the package entrypoint with the bundled Node binary, so the
installed app does not depend on a user's global npm installation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

PACKAGE = "@anthropic-ai/sandbox-runtime@0.0.67"
MANIFEST_DIR = Path(__file__).resolve().parent
PACKAGE_JSON = MANIFEST_DIR / "sandbox-runtime-package.json"
PACKAGE_LOCK = MANIFEST_DIR / "sandbox-runtime-package-lock.json"
NODE_VERSION = os.environ.get("STAFFDECK_NODE_VERSION", "v22.19.0")
PATCH_MARKER = "staffdeck-allow-all-domains-patch-v1"
PATCHED_SHA256 = {
    "sandbox-config.js": "17a9bdd4cce375bb098f9c02eb564cf80806079571d4ff784e2af7d27db446bb",
    "sandbox-manager.js": "dca5176508d6ee31807e2138eea82bcbb6f6e5e40c67aff2ac9958d3bc893b4c",
}
SRT_INTEGRITY = "sha512-4doSyr6KNdc/4zARMXYEawhFu3z6bPQjgKRq3lKp6dbgEYVMv39oaLJ28QsDc7TmLvrLqzHW+VzD2LAXxvnw8A=="
NODE_SHA256 = {
    "node-v22.19.0-darwin-arm64.tar.gz": "c59006db713c770d6ec63ae16cb3edc11f49ee093b5c415d667bb4f436c6526d",
    "node-v22.19.0-darwin-x64.tar.gz": "3cfed4795cd97277559763c5f56e711852d2cc2420bda1cea30c8aa9ac77ce0c",
    "node-v22.19.0-linux-x64.tar.gz": "d36e56998220085782c0ca965f9d51b7726335aed2f5fc7321c6c0ad233aa96d",
    "node-v22.19.0-linux-arm64.tar.gz": "d32817b937219b8f131a28546035183d79e7fd17a86e38ccb8772901a7cd9009",
    "node-v22.19.0-win-arm64.zip": "e4a7336010d58ff35b53d9dd5869095c56089c70913cf22508cf8183593e56b2",
    "node-v22.19.0-win-x64.zip": "ea3fad0e67a991d8477d8c01344b56e69c676ccb733f065b22436994b1253f86",
}


def _apply_allow_all_domains_patch(destination: Path) -> None:
    """Apply the reviewed upstream PR #283 behavior to the bundled dist files."""
    package_root = destination / "node_modules" / "@anthropic-ai" / "sandbox-runtime"
    config_path = package_root / "dist" / "sandbox" / "sandbox-config.js"
    manager_path = package_root / "dist" / "sandbox" / "sandbox-manager.js"
    if not config_path.is_file() or not manager_path.is_file():
        raise SystemExit("SRT bundle layout is unexpected; refusing to patch it.")
    config = config_path.read_text(encoding="utf-8")
    manager = manager_path.read_text(encoding="utf-8")
    if PATCH_MARKER in config and PATCH_MARKER in manager:
        _verify_patched_dist(config_path, manager_path)
        return
    config_anchor = "    allowUnixSockets: z\n"
    config_insert = (
        "    // " + PATCH_MARKER + "\n"
        "    allowAllDomains: z.boolean().optional(),\n"
    )
    manager_anchor = "    // Check allowed domains\n"
    manager_insert = (
        "    // " + PATCH_MARKER + "\n"
        "    if (config.network.allowAllDomains) {\n"
        "        logForDebugging(`Allowed by allowAllDomains: ${host}:${port}`);\n"
        "        return true;\n"
        "    }\n"
    )
    if config.count(config_anchor) != 1 or manager.count(manager_anchor) != 1:
        raise SystemExit("SRT patch anchors changed; refusing an unverified patch.")
    config_path.write_bytes(
        config.replace(config_anchor, config_insert + config_anchor).encode("utf-8")
    )
    manager_path.write_bytes(
        manager.replace(manager_anchor, manager_insert + manager_anchor).encode("utf-8")
    )
    if PATCH_MARKER not in config_path.read_text(encoding="utf-8") or PATCH_MARKER not in manager_path.read_text(encoding="utf-8"):
        raise SystemExit("SRT patch verification failed.")
    _verify_patched_dist(config_path, manager_path)


def _verify_patched_dist(config_path: Path, manager_path: Path) -> None:
    for path in (config_path, manager_path):
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != PATCHED_SHA256[path.name]:
            raise SystemExit(f"Patched SRT dist hash mismatch: {path.name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm") or shutil.which("npm")
    if not npm:
        raise SystemExit("npm is required to prepare the SRT runtime bundle.")

    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    bin_dir = destination / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    _download_node_runtime(destination)
    shutil.copy2(PACKAGE_JSON, destination / "package.json")
    shutil.copy2(PACKAGE_LOCK, destination / "package-lock.json")
    subprocess.run(
        [
            npm, "ci", "--prefix", str(destination), "--no-audit", "--no-fund",
            "--omit=dev",
        ],
        check=True,
    )
    _verify_srt_integrity(destination)
    _apply_allow_all_domains_patch(destination)
    print(f"SRT runtime ready at {destination}")
    return 0


def _download_node_runtime(destination: Path) -> None:
    system = platform.system().lower()
    machine = _machine()
    arch = {
        "x86_64": "x64",
        "amd64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine)
    if not arch:
        raise SystemExit(f"Unsupported Node architecture: {machine}")
    if system == "darwin":
        target = f"darwin-{arch}"
        suffix = "tar.gz"
    elif system == "linux":
        target = f"linux-{arch}"
        suffix = "tar.gz"
    elif system == "windows":
        target = f"win-{arch}"
        suffix = "zip"
    else:
        raise SystemExit(f"Unsupported Node platform: {system}")
    filename = f"node-{NODE_VERSION}-{target}.{suffix}"
    url = f"https://nodejs.org/dist/{NODE_VERSION}/{filename}"
    with tempfile.TemporaryDirectory(prefix="staffdeck-node-") as temp:
        archive = Path(temp) / filename
        try:
            socket.setdefaulttimeout(60)
            urllib.request.urlretrieve(url, archive)
        except Exception as exc:
            raise SystemExit(f"Failed to download Node runtime from {url}: {exc}") from exc
        expected_hash = (
            os.environ.get("STAFFDECK_NODE_SHA256", "").strip().lower()
            or NODE_SHA256.get(filename, "")
        )
        if not expected_hash:
            raise SystemExit(
                f"No trusted SHA256 is configured for {filename}; set STAFFDECK_NODE_SHA256."
            )
        actual_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            raise SystemExit(
                f"Node runtime SHA256 mismatch: expected {expected_hash}, got {actual_hash}"
            )
        target_bin = destination / "bin"
        target_bin.mkdir(parents=True, exist_ok=True)
        node_name = "node.exe" if system == "windows" else "node"
        target_node = target_bin / node_name
        if suffix == "tar.gz":
            with tarfile.open(archive, "r:gz") as handle:
                members = [
                    member
                    for member in handle.getmembers()
                    if member.name.endswith("/bin/node") and member.isfile()
                ]
                if len(members) != 1:
                    raise SystemExit("Node archive does not contain exactly one bin/node.")
                source = handle.extractfile(members[0])
                if source is None:
                    raise SystemExit("Node executable cannot be read from the archive.")
                with source, target_node.open("wb") as output:
                    shutil.copyfileobj(source, output)
        else:
            with zipfile.ZipFile(archive) as handle:
                members = [
                    name
                    for name in handle.namelist()
                    if name.endswith("/node.exe") and not name.endswith("/")
                ]
                if len(members) != 1:
                    raise SystemExit("Node archive does not contain exactly one node.exe.")
                with handle.open(members[0]) as source, target_node.open("wb") as output:
                    shutil.copyfileobj(source, output)
        if system != "windows":
            target_node.chmod(target_node.stat().st_mode | 0o111)


def _machine() -> str:
    machine = platform.machine() or os.environ.get("PROCESSOR_ARCHITECTURE", "")
    if machine:
        return machine.lower()
    if platform.system().lower() == "windows" and sys.maxsize > 2**32:
        return "amd64"
    return machine.lower()


def _verify_srt_integrity(destination: Path) -> None:
    lock_path = destination / "package-lock.json"
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        trusted = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))
        packages = _normalized_lock_packages(lock["packages"])
        trusted_packages = _normalized_lock_packages(trusted["packages"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SystemExit("SRT package lock is missing or malformed; refusing the bundle.") from exc
    if packages != trusted_packages:
        raise SystemExit("SRT dependency graph does not match the reviewed lockfile.")
    for relative, package in trusted_packages.items():
        if not relative:
            continue
        try:
            installed = json.loads(
                (destination / relative / "package.json").read_text(encoding="utf-8")
            )
        except (OSError, TypeError, json.JSONDecodeError) as exc:
            raise SystemExit(f"SRT dependency is missing: {relative}") from exc
        if installed.get("version") != package.get("version"):
            raise SystemExit(f"SRT dependency version mismatch: {relative}")


def _normalized_lock_packages(packages: object) -> dict[str, object]:
    if not isinstance(packages, dict):
        raise TypeError("packages must be an object")
    normalized: dict[str, object] = {}
    for raw_key, value in packages.items():
        key = str(raw_key).replace("\\", "/")
        marker = "node_modules/"
        if marker in key:
            key = marker + key.rsplit(marker, 1)[1]
        elif key:
            continue
        normalized[key] = value
    return normalized


if __name__ == "__main__":
    raise SystemExit(main())
