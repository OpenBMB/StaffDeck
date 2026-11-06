"""Exercise a native OSS installation and its real CLI using isolated local data."""
import argparse
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', type=Path, required=True)
    parser.add_argument('--cli', type=Path, required=True)
    parser.add_argument('--restart-with-cli', action='store_true',
                        help='Restart persisted data using the console serve command')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='staffdeck-native-smoke-') as temporary:
        data = Path(temporary)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        env = dict(os.environ, ULTRARAG_DATA_DIR=temporary, ULTRARAG_PORT=str(port),
                   ULTRARAG_PORT_RANGE_START=str(port), ULTRARAG_PORT_RANGE_END=str(port),
                   STAFFDECK_HEADLESS='true', STAFFDECK_CONFIG_DIR=str(data/'profiles'),
                   STAFFDECK_BASE_URL=f'http://127.0.0.1:{port}', STAFFDECK_TENANT_ID='tenant_demo')
        for key in ('APP_SECRET','DATABASE_URL','STAFFDECK_API_KEY','STAFFDECK_INITIAL_ADMIN_PASSWORD'):
            env.pop(key, None)
        with (data/'launcher.log').open('w') as log:
            def start_server(executable=None):
                print('OSS smoke: starting server', flush=True)
                child = subprocess.Popen([str((executable or args.server).resolve()), 'serve'], env=env,
                                         stdout=log, stderr=subprocess.STDOUT)
                deadline = time.monotonic() + 300
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        raise RuntimeError('Native OSS server exited before health check')
                    try:
                        with urllib.request.urlopen(env['STAFFDECK_BASE_URL']+'/api/health',timeout=2) as response:
                            if json.load(response).get('app')=='StaffDeck':
                                print('OSS smoke: server ready', flush=True)
                                return child
                    except (OSError,ValueError):pass
                    time.sleep(1)
                child.terminate();child.wait(timeout=20)
                raise RuntimeError('Native OSS server health timed out')

            def stop_server(child):
                print('OSS smoke: stopping server', flush=True)
                child.terminate()
                try:child.wait(timeout=20)
                except subprocess.TimeoutExpired:child.kill();child.wait()

            process = None
            try:
                process = start_server()
                cli = str(args.cli.resolve())
                print('OSS smoke: CLI help', flush=True)
                subprocess.run([cli,'--help'],env=env,check=True,stdout=subprocess.DEVNULL,timeout=90)
                print('OSS smoke: CLI login', flush=True)
                subprocess.run([cli,'login','--password-stdin'],input='admin\n',text=True,env=env,check=True,timeout=90)
                print('OSS smoke: CLI employee list', flush=True)
                out = subprocess.check_output([cli,'agent','list'],env=env,text=True,encoding='utf-8',timeout=90)
                for name in ('IT','人事','行政','财务','法务'):assert name in out,name
                stop_server(process)
                process = start_server(args.cli if args.restart_with_cli else None)
                # The existing CLI credential must survive restart; its server-side
                # verifier uses the persisted application signing material.
                print('OSS smoke: saved credential after restart', flush=True)
                subprocess.run([cli,'auth','status'],env=env,check=True,timeout=90)
                assert len((data/'.installation-seed').read_bytes()) == 32
                assert not (data/'installation-secrets.json').exists()
                print('OSS smoke: CLI logout', flush=True)
                subprocess.run([cli,'logout'],env=env,check=True,timeout=90)
                print('OSS login, defaults, restart persistence and CLI acceptance passed.',flush=True)
            except Exception:
                log.flush()
                # Do not print settings, installation secrets or profile credentials.
                for diagnostic in (data/'launcher.log',data/'logs/staffdeck.log'):
                    if not diagnostic.is_file():continue
                    print('Native smoke failed. Diagnostic tail: '+diagnostic.name,flush=True)
                    for line in diagnostic.read_text(encoding='utf-8',errors='replace').splitlines()[-60:]:
                        if not any(word in line.lower() for word in ('secret','password','authorization','token','api_key')):
                            print(line,flush=True)
                raise
            finally:
                if process is not None and process.poll() is None:
                    stop_server(process)
            print('OSS smoke: removing isolated test data',flush=True)
    print('OSS smoke: cleanup complete',flush=True)


if __name__=='__main__':main()
