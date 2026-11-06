"""Prepare an exported source/Docker/native build on its own trusted build machine."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
if not (ROOT / 'distribution/manifest.json').is_file():
    ROOT = ROOT.parents[1]  # invoked from packaging/distribution in a prepared checkout


def windows_command(args):
    args=[str(arg) for arg in args]
    name=Path(args[0]).stem.lower()
    if name not in {'npm','pnpm','corepack'}:
        return args
    node=shutil.which('node')
    shim=shutil.which(args[0])
    if not node or not shim:
        raise RuntimeError('Node.js and the selected package manager must be installed')
    actual=Path(subprocess.check_output([node,'-p','process.execPath'],text=True).strip())
    bases=[Path(shim).parent,actual.parent]
    relative={'npm':('npm/bin/npm-cli.js',),
              'pnpm':('pnpm/bin/pnpm.cjs','corepack/dist/pnpm.js'),
              'corepack':('corepack/dist/corepack.js',)}[name]
    script=next((base/'node_modules'/rel for base in bases for rel in relative
                 if (base/'node_modules'/rel).is_file()),None)
    if script is None:
        raise RuntimeError(f'Cannot locate the installed {name} JavaScript entry point')
    return [str(actual),str(script),*args[1:]]


def run(args, cwd=ROOT, env=None):
    command=windows_command(args) if os.name=='nt' else [str(arg) for arg in args]
    subprocess.run(command,cwd=cwd,env=env,check=True)


def package_manager(root):
    # The releases UI ships a tested npm lock and a legacy pnpm 6 lock.
    # Prefer its npm lock instead of sending it to the engine's newer pnpm.
    if (root / 'package-lock.json').is_file():
        return ['npm']
    if (root / 'pnpm-lock.yaml').is_file():
        metadata = root / 'package.json'
        manager = ''
        if metadata.is_file():
            manager = json.loads(metadata.read_text(encoding='utf-8')).get('packageManager', '')
        if manager.startswith('pnpm@'):
            # npm caches the project's pinned pnpm without requiring a global
            # installation, changing global versions, or relying on Corepack.
            return ['npm', 'exec', '--yes', f'--package={manager}', '--', 'pnpm']
        return [shutil.which('pnpm')] if shutil.which('pnpm') else ['corepack', 'pnpm']
    return ['npm']


def prepare(*, backend=True, cli_link=True):
    manifest = json.loads((ROOT / 'distribution/manifest.json').read_text(encoding='utf-8'))
    env = dict(os.environ, VITE_TENANT_ID=manifest['installation']['tenant_id'], VITE_API_BASE_URL='',
               RAYON_NUM_THREADS='2', UV_THREADPOOL_SIZE='2', NODE_OPTIONS='--max-old-space-size=2048')
    selected_ui = 'frontend-business' if manifest['assembly'].get('ui_variant') == 'business' else 'frontend-enterprise'
    for name in (selected_ui,):
        directory = ROOT / name
        if not (directory / 'package.json').is_file():
            continue
        pm = package_manager(directory)
        if pm[-1] == 'npm':
            run([*pm, 'ci', '--ignore-scripts', '--no-audit', '--no-fund'], directory, env)
            run([*pm, 'run', 'build', '--', '--base=/'], directory, env)
        else:
            run([*pm, 'install', '--frozen-lockfile', '--ignore-scripts'], directory, env)
            run([*pm, 'exec', 'tsc', '-b'], directory, env)
            run([*pm, 'exec', 'vite', 'build', '--base=/'], directory, env)
    engine = ROOT / 'harness-engine'
    pm = package_manager(engine)
    run([*pm, 'install', '--frozen-lockfile'] if pm[-1] != 'npm' else [*pm, 'ci', '--no-audit'], engine, env)
    # Build every runtime project with its normal strict TypeScript checks.
    # The upstream root aggregate also checks its development website/tests;
    # these do not emit runtime files and are not part of a customer package.
    for face in ('host', 'client'):
        project = f'tsconfig.distribution-{face}.json'
        run(['node', ROOT / 'packaging/distribution/make-solution-tsconfig.mjs',
             f'tsconfig.{face}.json', project], engine, env)
        run(['node', '--max-old-space-size=4096', engine / 'node_modules/typescript/bin/tsc', '-b', project], engine, env)
        run([*pm, 'exec', 'tsdown', f'--env.DSH_BUILD_FACE={face}'], engine, env)
    if not (engine / 'apps/cli/lib/bin.js').is_file():
        raise RuntimeError('Harness v3 build did not produce apps/cli/lib/bin.js')
    if backend:
        venv = ROOT / 'backend/.venv'
        run([sys.executable, '-m', 'venv', venv])
        python = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        run([python, '-m', 'pip', 'install', '-e', ROOT / 'backend'])
        run([python, ROOT / 'packaging/distribution/install_dependencies.py'])
        if cli_link:
            run([python, '-m', 'staffdeck_harness.cli_install', '--root', ROOT])
    # Serve the selected product UI, including its own business admin routes.
    # Never overlay it with the framework assembly/packaging console.
    edge = ROOT / 'edge-dist'
    edge.mkdir(exist_ok=True)
    selected = ROOT / selected_ui / 'dist'
    shutil.copytree(selected, edge, dirs_exist_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--without-backend', action='store_true')
    parser.add_argument('--no-cli-link', action='store_true', help='构建机仅构建，不注册用户终端命令')
    args = parser.parse_args()
    prepare(backend=not args.without_backend, cli_link=not args.no_cli_link)
