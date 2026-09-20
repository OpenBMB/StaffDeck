from __future__ import annotations

import os
import shlex
import subprocess
import sys


def run_configured(mode: str, env_key: str) -> int:
    command = os.environ.get(env_key, "").strip()
    if not command:
        print(f"BLOCKED: {env_key} is not configured", file=sys.stderr)
        return 2
    completed = subprocess.run(shlex.split(command), check=False)
    if completed.returncode != 0:
        print(f"BLOCKED: {mode} implementation exited {completed.returncode}", file=sys.stderr)
    return completed.returncode
