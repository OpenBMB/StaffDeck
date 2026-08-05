"""Run repeatable, real-process sandbox acceptance checks.

Usage: python3 packaging/smoke_sandbox_runtime.py /path/to/sandbox_runtime
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

SMOKE_PYTHON = Path(os.environ.get("STAFFDECK_SMOKE_PYTHON", sys.executable)).resolve()
CHECK_NAMES = (
    "network_all",
    "network_allowlist",
    "network_deny",
    "artifact",
    "sqlite_deny_read",
    "sqlite_hardlink_deny",
    "sibling_workspace_deny",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime", type=Path)
    parser.add_argument("--check", action="append", choices=CHECK_NAMES)
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    os.environ["STAFFDECK_SRT_RUNTIME"] = str(runtime)

    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root / "backend"))

    with tempfile.TemporaryDirectory(prefix="staffdeck-sandbox-smoke-") as raw_root:
        root = Path(raw_root)
        data_root = root / "host-data"
        data_root.mkdir()
        database = data_root / "skill_agent_loop.db"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE secret (value TEXT)")
            connection.execute("INSERT INTO secret VALUES ('must-not-leak')")
            connection.commit()
        os.environ.pop("DATABASE_URL", None)
        os.environ["ULTRARAG_DATA_DIR"] = str(data_root)

        from app import paths
        from app.config import get_settings
        from app.harness.command import run_sandboxed_process

        paths.is_frozen = lambda: True
        get_settings.cache_clear()
        workspace_root = data_root / "harness_workspaces" / "smoke"
        sibling_secret = data_root / "harness_workspaces" / "sibling" / "secret.txt"
        sibling_secret.parent.mkdir(parents=True)
        sibling_secret.write_text("must-not-leak", encoding="utf-8")
        checkers = {
            "network_all": lambda: _network_all(
                run_sandboxed_process, workspace_root / "network-all"
            ),
            "network_allowlist": lambda: _network_allowlist(
                run_sandboxed_process, workspace_root / "network-allowlist"
            ),
            "network_deny": lambda: _network_deny(
                run_sandboxed_process, workspace_root / "network-deny"
            ),
            "artifact": lambda: _artifact(
                run_sandboxed_process, workspace_root / "artifact"
            ),
            "sqlite_deny_read": lambda: _sqlite_deny_read(
                run_sandboxed_process, workspace_root / "sqlite", database
            ),
            "sqlite_hardlink_deny": lambda: _sqlite_hardlink_deny(
                run_sandboxed_process, workspace_root / "sqlite-hardlink", database
            ),
            "sibling_workspace_deny": lambda: _sibling_workspace_deny(
                run_sandboxed_process, workspace_root / "sibling-read", sibling_secret
            ),
        }
        selected = set(args.check or CHECK_NAMES)
        checks = {name: checkers[name]() for name in CHECK_NAMES if name in selected}
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if all(checks.values()) else 1


def _run(
    run_process,
    workspace: Path,
    code: str,
    *,
    network_mode: str,
    allowed_domains: tuple[str, ...] = (),
):
    workspace.mkdir(parents=True)
    script = workspace / "runner.py"
    script.write_text(code, encoding="utf-8")
    result = run_process(
        workspace=workspace,
        argv=[str(SMOKE_PYTHON), str(script)],
        cwd=workspace,
        timeout_seconds=60 if sys.platform == "win32" else 20,
        output_limit=20_000,
        network_mode=network_mode,
        allowed_domains=allowed_domains,
    )
    if os.environ.get("STAFFDECK_SMOKE_VERBOSE") == "1":
        print(
            json.dumps(
                {
                    "workspace": str(workspace),
                    "network_mode": network_mode,
                    "returncode": result.returncode,
                    "timed_out": result.timed_out,
                    "duration_ms": result.duration_ms,
                    "stdout": result.stdout.decode("utf-8", errors="replace"),
                    "stderr": result.stderr.decode("utf-8", errors="replace"),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
    return result


def _network_all(run_process, workspace: Path) -> bool:
    result = _run(
        run_process,
        workspace,
        (
            "from time import sleep\n"
            "from urllib.request import urlopen\n"
            "for attempt in range(3):\n"
            "    try:\n"
            "        with urlopen('http://example.com', timeout=8) as response:\n"
            "            print(response.status)\n"
            "            break\n"
            "    except Exception:\n"
            "        if attempt == 2:\n"
            "            raise\n"
            "        sleep(0.5)\n"
        ),
        network_mode="all",
    )
    return result.returncode == 0 and result.stdout.strip() == b"200"


def _network_deny(run_process, workspace: Path) -> bool:
    result = _run(
        run_process,
        workspace,
        (
            "from urllib.request import urlopen\n"
            "try:\n"
            "    urlopen('http://example.com', timeout=3)\n"
            "except Exception:\n"
            "    print('blocked')\n"
            "else:\n"
            "    raise SystemExit('network unexpectedly available')\n"
        ),
        network_mode="deny",
    )
    return result.returncode == 0 and result.stdout.strip() == b"blocked"


def _network_allowlist(run_process, workspace: Path) -> bool:
    result = _run(
        run_process,
        workspace,
        (
            "from time import sleep\n"
            "from urllib.request import urlopen\n"
            "allowed = False\n"
            "for attempt in range(3):\n"
            "    try:\n"
            "        with urlopen('http://example.com', timeout=8) as response:\n"
            "            allowed = response.status == 200\n"
            "            break\n"
            "    except Exception:\n"
            "        if attempt == 2:\n"
            "            raise\n"
            "        sleep(0.5)\n"
            "try:\n"
            "    urlopen('http://example.org', timeout=3)\n"
            "except Exception:\n"
            "    denied = True\n"
            "else:\n"
            "    denied = False\n"
            "print('ok' if allowed and denied else 'failed')\n"
        ),
        network_mode="allowlist",
        allowed_domains=("example.com",),
    )
    return result.returncode == 0 and result.stdout.strip() == b"ok"


def _artifact(run_process, workspace: Path) -> bool:
    result = _run(
        run_process,
        workspace,
        "from pathlib import Path\nPath('report.txt').write_text('sandbox artifact')\n",
        network_mode="deny",
    )
    artifact = workspace / "report.txt"
    return (
        result.returncode == 0
        and artifact.is_file()
        and artifact.read_text(encoding="utf-8") == "sandbox artifact"
    )


def _sqlite_deny_read(run_process, workspace: Path, database: Path) -> bool:
    result = _run(
        run_process,
        workspace,
        (
            "import sqlite3\n"
            f"database = {str(database)!r}\n"
            "try:\n"
            "    rows = sqlite3.connect(database).execute('SELECT value FROM secret').fetchall()\n"
            "except Exception:\n"
            "    print('blocked')\n"
            "else:\n"
            "    raise SystemExit(f'sqlite leaked: {rows!r}')\n"
        ),
        network_mode="deny",
    )
    return result.returncode == 0 and result.stdout.strip() == b"blocked"


def _sqlite_hardlink_deny(run_process, workspace: Path, database: Path) -> bool:
    result = _run(
        run_process,
        workspace,
        (
            "import os, sqlite3\n"
            f"database = {str(database)!r}\n"
            "try:\n"
            "    os.link(database, 'database-link.db')\n"
            "    rows = sqlite3.connect('database-link.db').execute('SELECT value FROM secret').fetchall()\n"
            "except Exception:\n"
            "    print('blocked')\n"
            "else:\n"
            "    raise SystemExit(f'sqlite hardlink leaked: {rows!r}')\n"
        ),
        network_mode="deny",
    )
    return result.returncode == 0 and result.stdout.strip() == b"blocked"


def _sibling_workspace_deny(
    run_process, workspace: Path, sibling_secret: Path
) -> bool:
    result = _run(
        run_process,
        workspace,
        (
            "from pathlib import Path\n"
            f"secret = Path({str(sibling_secret)!r})\n"
            "try:\n"
            "    secret.read_text()\n"
            "except Exception:\n"
            "    print('blocked')\n"
            "else:\n"
            "    raise SystemExit('sibling workspace leaked')\n"
        ),
        network_mode="deny",
    )
    return result.returncode == 0 and result.stdout.strip() == b"blocked"


if __name__ == "__main__":
    raise SystemExit(main())
