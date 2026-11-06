"""Build the framework's standalone console client without a frontend/Node build.

Run with a build environment containing httpx and PyInstaller. The resulting
client connects to an already running StaffDeck server; it does not embed one.
The full desktop build includes this same entry point beside the server binary.
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> None:
    if sys.version_info < (3, 11):
        raise SystemExit("StaffDeck CLI requires Python 3.11 or newer to build.")
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=root / "packaging/out/cli")
    parser.add_argument("--work", type=Path, default=root / "packaging/build/cli")
    args = parser.parse_args()
    source = root / "backend/src"
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--console", "--name", "staffdeck-cli", "--paths", str(source),
        "--add-data", f"{source / 'staffdeck/skills'}:staffdeck/skills",
        "--distpath", str(args.output.resolve()), "--workpath", str(args.work.resolve()),
        "--specpath", str(args.work.resolve()), str(root / "backend/cli_launcher.py"),
    ], cwd=root, check=True)
    suffix = ".exe" if sys.platform == "win32" else ""
    executable = args.output.resolve() / "staffdeck-cli" / f"staffdeck-cli{suffix}"
    shutil.copy2(root / "packaging/distribution/CLI.md", executable.parent / "CLI.md")
    for command in (["--version"], ["--help"], ["guide"]):
        subprocess.run([str(executable), *command], check=True, stdout=subprocess.DEVNULL)
    print(executable)


if __name__ == "__main__":
    main()
