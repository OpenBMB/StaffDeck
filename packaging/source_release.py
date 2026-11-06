"""Validate and archive this public OSS checkout, without an internal build service."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_ROOTS = {'business_plugger', 'frontend-business', 'extensions', 'deploy'}
PRIVATE_FILES = {
    'packaging/build_worker.py',
    'backend/src/staffdeck_harness/api/admin.py',
    'backend/src/staffdeck_harness/distribution/api.py',
    'backend/src/staffdeck_harness/distribution/bundle.py',
    'backend/src/staffdeck_harness/distribution/store.py',
}
STATE_SUFFIXES = {'.db', '.sqlite', '.sqlite3', '.jsonl', '.pem', '.key', '.p12', '.pfx'}


def version_value(value):
    value = value.removeprefix('v')
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?', value):
        raise ValueError('Version must be a semantic version, optionally prefixed with v')
    return value


def tracked_files(root=ROOT):
    return subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().strip('\0').split('\0')


def validate(root=ROOT, names=None):
    names = tracked_files(root) if names is None else names
    for name in names:
        path = Path(name)
        if (path.parts[0] in PRIVATE_ROOTS or name in PRIVATE_FILES
                or path.suffix in STATE_SUFFIXES or path.name in {'.installation-seed','installation-secrets.json'}
                or any(part in {'node_modules', '.venv', '__pycache__', '.dsh-home-test'} for part in path.parts)
                or any(part.startswith('.env') and part != '.env.example' for part in path.parts)):
            raise ValueError('Non-public or live-state path in OSS checkout: ' + name)
    for name in PRIVATE_FILES:
        if (root / name).exists():
            raise ValueError('Internal management implementation remains: ' + name)
    profile = json.loads((root / 'distribution/manifest.json').read_text())
    assembly = profile['assembly']
    if (assembly['security_profile'] != 'OSS_LOCAL' or assembly.get('extra_modules')
            or profile.get('module_packages') or profile['installation'].get('control_auth_provider')
            or assembly.get('ui_variant') != 'releases'):
        raise ValueError('Distribution must be the standalone OSS profile')
    for file in ('LICENSE', 'harness-engine/LICENSE', 'CLI.md', 'INSTALL.md'):
        if not (root / file).is_file():
            raise ValueError('Missing public release file: ' + file)
    gate = ast.parse((root / 'backend/src/staffdeck_harness/product_features.py').read_text())
    if not any(isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and
               t.id == 'FRAMEWORK_MANAGEMENT_INCLUDED' for t in n.targets) and
               isinstance(n.value, ast.Constant) and n.value.value is False for n in gate.body):
        raise ValueError('Framework management must be disabled in the public product')
    return names


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version')
    parser.add_argument('--output', type=Path, default=ROOT / 'oss-source-output')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    names = validate()
    if args.check:
        print(f'OSS boundary verified: {len(names)} tracked files')
        return
    version = version_value(args.version or json.loads((ROOT / 'distribution/manifest.json').read_text())['version'])
    args.output.mkdir(parents=True, exist_ok=True)
    archive = args.output / f'StaffDeck-OSS-{version}-source.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        for name in sorted(names):
            if name == 'distribution/manifest.json':
                value = json.loads((ROOT / name).read_text()); value['version'] = version
                bundle.writestr(name, json.dumps(value, ensure_ascii=False, indent=2))
            else:
                bundle.write(ROOT / name, name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (args.output / 'StaffDeck-source-SHA256SUMS.txt').write_text(f'{digest}  {archive.name}\n')
    print(archive)


if __name__ == '__main__':
    main()
