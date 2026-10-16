"""Install and exercise the wheel outside both source trees."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--browser-script", type=Path, required=True)
    parser.add_argument("--playwright-module", required=True)
    parser.add_argument("--chrome", required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {"status": "not_accepted", "wheel": args.wheel.name}
    with tempfile.TemporaryDirectory(prefix="km-artifact-") as directory:
        root = Path(directory)
        env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "STAFFDECK_KM_URL", "NODE_OPTIONS"}}
        subprocess.run([sys.executable, "-m", "venv", str(root / "venv")], check=True, env=env)
        python = str(root / "venv/bin/python")
        with (output / "installation.log").open("w") as log:
            subprocess.run([python, "-m", "pip", "install", str(args.wheel.resolve())], check=True, stdout=log, stderr=subprocess.STDOUT, cwd=root, env=env)
        with (output / "service.log").open("w") as log:
            audit = str(output / "runtime-imports.json")
            launcher = f'''import json,runpy,sys
from pathlib import Path
from knowledge_core.kernel.runtime import KernelRuntime
original_close = KernelRuntime.close
def audited_close(self):
    original_close(self)
    Path({audit!r}).write_text(json.dumps([n for n in sys.modules if n.startswith(('app.', 'staffdeck.', 'staffdeck_harness.', 'harness.'))]))
KernelRuntime.close = audited_close
runpy.run_module('knowledge_core.cli',run_name='__main__')
'''
            (root / 'full.json').write_text(json.dumps({'preset': 'full'}))
            owner = subprocess.Popen([python, "-c", launcher, "--config", str(root / 'full.json'), "--database-url", f"sqlite:///{root}/km.sqlite", "--port", "18787"], cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                for _ in range(100):
                    try:
                        if httpx.get("http://127.0.0.1:18787/health/ready").status_code == 200: break
                    except httpx.HTTPError: pass
                    time.sleep(.1)
                else: raise RuntimeError("Artifact service did not become ready")
                browser_env = {**env, "KM_ACCEPTANCE_URL": "http://127.0.0.1:18787", "KM_PLAYWRIGHT_MODULE": args.playwright_module,
                               "PLAYWRIGHT_EXECUTABLE_PATH": args.chrome, "KM_SCREENSHOT_PREFIX": str(output / "independent")}
                browser = subprocess.run([args.node, str(args.browser_script.resolve())], env=browser_env, cwd=root, capture_output=True, text=True)
                (output / "browser.log").write_text(browser.stdout + browser.stderr)
                if browser.returncode: raise RuntimeError("Artifact browser workflow failed")
                report["browser"] = json.loads(browser.stdout.strip().splitlines()[-1])
                inspection = '''import json,sys,sqlite3,knowledge_core
db=sqlite3.connect("km.sqlite")
print(json.dumps({"package":knowledge_core.__file__,"tables":[r[0] for r in db.execute("select name from sqlite_master where type='table'")],"host_imports":[n for n in sys.modules if n.startswith(("app.","staffdeck.","staffdeck_harness.","harness."))]}))'''
                report["isolation"] = json.loads(subprocess.check_output([python, "-c", inspection], cwd=root, env=env, text=True))
                assert not report["isolation"]["host_imports"]
                assert all(name.startswith("km_") for name in report["isolation"]["tables"])
                report["status"] = "passed"
            except Exception as exc:
                report["error"] = str(exc)
            finally:
                owner.terminate()
                try: owner.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    owner.kill()
                    owner.wait()
                    report["status"] = "not_accepted"
                report["service_exit_code"] = owner.returncode
                if not Path(audit).is_file() or json.loads(Path(audit).read_text()):
                    report["status"] = "not_accepted"
                    report["error"] = "Runtime host import audit missing or failed"
    (output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
