"""Record the requested regressions, including collected test identities."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": f"{root / 'backend'}:{root / 'knowledge_core/src'}",
           "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "ULTRARAG_DOTENV": "/nonexistent-km-acceptance.env"}
    groups = {
        "core": ["knowledge_core/tests"],
        "compatibility": [str(path.relative_to(root)) for path in sorted((root / "backend/tests").glob("test_knowledge*.py"))]
            + ["backend/tests/test_public_staffdeck_facade.py", "backend/tests/test_public_knowledge_auto_upload.py",
               "backend/tests/test_public_pilotdeck_domain_host.py", "backend/tests/test_public_pilotdeck_knowledge_read.py"],
    }
    report = {"status": "not_accepted", "groups": {}}
    for name, paths in groups.items():
        command = [sys.executable, "-m", "pytest", *paths, "-q"]
        collected = subprocess.run([*command, "--collect-only"], cwd=root, env=env, capture_output=True, text=True)
        (output / f"{name}-collected.log").write_text(collected.stdout + collected.stderr)
        with (output / f"{name}.log").open("w") as log:
            result = subprocess.run([*command, "--junitxml", str(output / f"{name}.xml")], cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
        report["groups"][name] = {"command": command, "collection_exit_code": collected.returncode,
                                 "exit_code": result.returncode, "status": "passed" if result.returncode == collected.returncode == 0 else "failed"}
    if all(row["status"] == "passed" for row in report["groups"].values()): report["status"] = "passed"
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
