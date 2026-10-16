"""Manual persistent source deployment; no login agent or host runtime required."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start', 'stop', 'restart', 'status'])
    parser.add_argument('--directory', type=Path, default=Path.home() / '.local/share/knowledge-core/km-1013')
    parser.add_argument('--port', type=int, default=18890)
    args = parser.parse_args()
    root = args.directory.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    pidfile = root / 'service.pid'
    statefile = root / 'service.json'

    def running():
        if not pidfile.exists(): return None
        try:
            pid = int(pidfile.read_text())
            os.kill(pid, 0)
            # A PID may have been reused after a reboot.
            command = subprocess.check_output(['ps', '-p', str(pid), '-o', 'command='], text=True)
            return pid if 'knowledge_core.cli' in command and str(root / 'configuration.json') in command else None
        except (ValueError, ProcessLookupError, subprocess.CalledProcessError): return None

    def ready():
        try:
            with urlopen(f'http://127.0.0.1:{args.port}/health/ready', timeout=1) as response:
                return response.status == 200
        except OSError: return False

    def stop():
        pid = running()
        if pid:
            os.kill(pid, signal.SIGTERM)
            for _ in range(200):
                if not running(): break
                time.sleep(.1)
            else: raise RuntimeError('Service did not stop normally; inspect service.log')
        pidfile.unlink(missing_ok=True)

    if args.action == 'status':
        print(json.dumps({'running': bool(running()), 'pid': running(), 'ready': ready(), 'directory': str(root), 'url': f'http://127.0.0.1:{args.port}/'}))
        return
    if args.action in {'stop', 'restart'}: stop()
    if args.action == 'stop': return
    if running():
        raise RuntimeError('Service already running; use status or restart')
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', args.port)) == 0: raise RuntimeError('Port occupied; no process was stopped')
    config = root / 'configuration.json'
    if not config.exists(): config.write_text(json.dumps({'preset': 'full', 'modules': {'publication': False}}, indent=2) + '\n')
    python = root / 'venv/bin/python'
    if not python.exists(): raise RuntimeError('Install source into ' + str(python.parent.parent) + ' first')
    command = [str(python), '-m', 'knowledge_core.cli', '--config', str(config), '--database-url', 'sqlite:///' + str(root / 'knowledge.sqlite'), '--host', '127.0.0.1', '--port', str(args.port)]
    model = root / 'model.json'
    if model.exists():
        if model.stat().st_mode & 0o077: raise RuntimeError('model.json must be private (mode 0600)')
        command += ['--model-config', str(model)]
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('KM_', 'STAFFDECK_')) and key != 'PYTHONPATH'}
    with (root / 'service.log').open('ab') as log:
        child = subprocess.Popen(command, cwd=root, env=env, start_new_session=True, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    pidfile.write_text(str(child.pid))
    for _ in range(600):
        if child.poll() is not None:
            pidfile.unlink(missing_ok=True)
            raise RuntimeError('Service startup failed; inspect ' + str(root / 'service.log'))
        if ready(): break
        time.sleep(.1)
    else:
        stop()
        raise RuntimeError('Service readiness timed out; inspect service.log')
    state = {'pid': child.pid, 'url': f'http://127.0.0.1:{args.port}/', 'directory': str(root), 'source': str(Path(__file__).resolve().parents[1]), 'model_configured': model.exists()}
    statefile.write_text(json.dumps(state, indent=2) + '\n')
    print(json.dumps(state))


if __name__ == '__main__':
    main()
