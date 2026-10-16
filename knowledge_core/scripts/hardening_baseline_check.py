"""Compare the additional legacy Harness fixture suite at HEAD and the worktree."""
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile

root = Path(__file__).resolve().parents[2]
output = root / 'knowledge_core/acceptance/2026-10-15/source-deployment/additional-hardening'
output.mkdir(parents=True, exist_ok=True)
paths = ['backend/src/staffdeck_harness/capabilities/host.py',
    'backend/src/staffdeck_harness/capabilities/local_services.py',
    'backend/src/staffdeck_harness/composition/compiler.py',
    'backend/src/staffdeck_harness/modules/builtin.py',
    'backend/src/staffdeck_harness/bridge/capability_mcp.py']
env = {**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1', 'ULTRARAG_DOTENV': '/nonexistent-km-acceptance.env'}
report = {'scope': 'Additional pre-existing Harness fixtures; excluded from required KM counts', 'runs': {}}
with tempfile.TemporaryDirectory(prefix='km-head-comparison-') as temporary:
    snapshot = Path(temporary)
    shutil.copytree(root / 'backend/src/staffdeck_harness', snapshot / 'backend/src/staffdeck_harness',
        ignore=shutil.ignore_patterns('__pycache__'))
    for path in paths:
        target = snapshot / path; target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.check_output(['git', 'show', 'HEAD:' + path], cwd=root))
    for name in ('head', 'current'):
        env['PYTHONPATH'] = (str(snapshot / 'backend/src') + ':' if name == 'head' else '') + f'{root}/backend:{root}/backend/src:{root}/knowledge_core/src'
        command = [sys.executable, '-m', 'pytest', 'backend/tests_harness/test_capability_host_hardening.py', '-q']
        with (output / (name + '.log')).open('w') as log:
            result = subprocess.run(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
        report['runs'][name] = {'command': command, 'exit_code': result.returncode}
report['same_failures'] = [line for line in (output / 'head.log').read_text().splitlines() if line.startswith('FAILED ')] == [line for line in (output / 'current.log').read_text().splitlines() if line.startswith('FAILED ')]
(output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report))
