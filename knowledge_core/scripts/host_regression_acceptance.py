"""Capture focused host/runtime regression commands and raw output."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pilot-root', type=Path, required=True)
    parser.add_argument('--node', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = args.output_dir.resolve(); output.mkdir(parents=True, exist_ok=True)
    groups = [
        ('pd', args.pilot_root, [str(args.node), '--test', *[str(path) for path in sorted((args.pilot_root / 'dist/tests/composition').glob('knowledge-*.spec.js'))]]),
        ('sd', root, [sys.executable, '-m', 'pytest', 'backend/tests/test_knowledge_agent.py', 'backend/tests_harness/test_composition.py', '-q', '--junitxml', str(output / 'sd.xml')]),
    ]
    env = {**os.environ, 'NODE_OPTIONS': '', 'PYTHONPATH': f'{root}/backend:{root}/backend/src:{root}/knowledge_core/src',
        'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1', 'ULTRARAG_DOTENV': '/nonexistent-km-acceptance.env'}
    report = {'status': 'not_accepted', 'groups': {}}
    for name, cwd, command in groups:
        with (output / (name + '.log')).open('w') as log:
            result = subprocess.run(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT)
        report['groups'][name] = {'command': command, 'exit_code': result.returncode, 'status': 'passed' if result.returncode == 0 else 'failed'}
    if all(row['status'] == 'passed' for row in report['groups'].values()): report['status'] = 'passed'
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__': raise SystemExit(main())
