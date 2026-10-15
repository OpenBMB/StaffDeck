"""Inspect actual startup storage/routes/imports in separate processes."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

COMBINATIONS = {
    'minimal': {'preset': 'minimal'}, 'full': {'preset': 'full'},
    'authentication': {'modules': {'authentication': True}},
    'catalog': {'modules': {'catalog': True}},
    'catalog_versions': {'modules': {'catalog': True, 'versions': True}},
    'concepts': {'modules': {'concepts': True}}, 'discovery': {'modules': {'discovery': True}},
    'concepts_discovery': {'modules': {'concepts': True, 'discovery': True}},
    'spaces_catalog_versions': {'modules': {'spaces': True, 'catalog': True, 'versions': True}},
}


def inspect_composition(name, settings, root):
    code = '''
import json, sys
from pathlib import Path
from sqlalchemy import inspect
from fastapi.testclient import TestClient
from knowledge_core.configuration import Configuration
from knowledge_core.runtime import Runtime
from knowledge_core.api import create_app
from knowledge_core.identity import StaticBearerIdentity, Principal
settings=json.loads(sys.argv[1]); path=Path(sys.argv[2]); name=sys.argv[3]
configuration=Configuration(**settings)
identity=StaticBearerIdentity({'test':Principal('test')}) if configuration.enabled('authentication') else None
class Publisher:
    def publish(self, suggestion, idempotency_key): raise RuntimeError('Inspection never publishes')
runtime=Runtime('sqlite:///'+str(path),configuration=configuration,identity=identity,
                publisher=Publisher() if configuration.enabled('publication') else None)
with TestClient(create_app(runtime)) as client:
    tables=inspect(runtime.engine).get_table_names()
    config=client.get('/api/km/config').json()
    imports=[module for module in sys.modules if module.startswith('knowledge_core.')]
    routes=[{'path':route.path,'methods':sorted(getattr(route,'methods',[]))} for route in client.app.routes]
    violations=[]
    if not hasattr(runtime,'plugins'): violations.append('Full bypasses module factories and uses legacy service/storage')
    if any(table.startswith('knowledge_') for table in tables): violations.append('Legacy all-module ORM tables remain')
    if any(module in imports for module in ('knowledge_core.service','knowledge_core.models','knowledge_core.full_runtime')):
        violations.append('Legacy runtime implementation loaded')
    from knowledge_core.assembly import REGISTRY
    for module,enabled in configuration.modules.items():
        if not enabled:
            if any(table in tables for table in REGISTRY[module].tables): violations.append('Disabled module owns stored tables: '+module)
            if 'knowledge_core.modules.'+module in imports: violations.append('Disabled implementation imported: '+module)
    print(json.dumps({'status':'failed' if violations else 'passed','configuration':config,'tables':tables,'imports':imports,'routes':routes,'violations':violations}))
'''
    database = root / (name + '.sqlite')
    if database.exists(): raise ValueError('Use a fresh output directory for composition inspection')
    process = subprocess.run([sys.executable, '-c', code, json.dumps(settings), str(database), name], capture_output=True, text=True)
    if process.returncode:
        return {'status': 'failed', 'error': process.stderr, 'configuration': settings}
    return json.loads(process.stdout)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    root = args.output_dir.resolve(); root.mkdir(parents=True, exist_ok=True)
    checks = []
    for name, configuration in COMBINATIONS.items():
        result = inspect_composition(name, configuration, root)
        (root / (name + '.json')).write_text(json.dumps(result, indent=2))
        checks.append({'name': name, 'status': result['status'], 'reason': '; '.join(result.get('violations', [])),
                       'evidence': [root.name + '/' + name + '.json']})
    result = {'status': 'passed' if all(row['status'] == 'passed' for row in checks) else 'failed', 'checks': checks}
    (root.parent / 'module_compositions.json').write_text(json.dumps(result, indent=2))
    print(json.dumps({name: row['status'] for name, row in zip(COMBINATIONS, checks)}))
    raise SystemExit(0 if result['status'] == 'passed' else 1)
