import os
import signal
import subprocess
import textwrap
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name == 'nt', reason='POSIX packaging lifecycle')
def test_packaged_app_smoke_bounds_shutdown_of_an_unresponsive_child(tmp_path):
    app = tmp_path / 'App/Contents/MacOS'
    app.mkdir(parents=True)
    server = app / 'staffdeck'
    server.write_text(textwrap.dedent('''\
        #!/usr/bin/env python3
        import http.server, os, signal
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        class Health(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            def log_message(self, *args): pass
        http.server.HTTPServer(('127.0.0.1', int(os.environ['ULTRARAG_PORT'])), Health).serve_forever()
    '''))
    server.chmod(0o755)
    root = Path(__file__).resolve().parents[2]
    process = subprocess.Popen(
        ['bash', str(root / 'packaging/smoke_macos_app.sh'), str(app.parents[1])],
        env={**os.environ, 'TMPDIR': str(tmp_path)},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=25)
        assert process.returncode == 0, output
        assert 'health check passed' in output
        assert 'exceeded the shutdown grace period' in output
        assert not list(tmp_path.glob('staffdeck-macos-smoke.*'))
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
