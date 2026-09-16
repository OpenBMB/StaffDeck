"""Clean HTTP chunked EOF must not masquerade as complete event delivery."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from staffdeck import StaffDeck, StreamError


@pytest.mark.parametrize("empty_connections", [1, 2])
def test_clean_empty_chunked_reconnect_requires_final_cursor(monkeypatch, empty_connections):
    monkeypatch.setattr("staffdeck.runs.time.sleep", lambda _: None)
    cursors = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path.endswith("/events"):
                cursors.append(self.headers.get("Last-Event-ID"))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                if len(cursors) == 1:
                    body = b'id: 3\nevent: run.failed\ndata: {}\n\n'
                elif len(cursors) <= empty_connections + 1:
                    body = b""
                else:
                    body = b'id: 4\nevent: run.failed\ndata: {}\n\n'
                if body:
                    self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n")
                # Valid HTTP ending, not a ReadError or truncated transfer encoding.
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            else:
                body = json.dumps({"status": "failed", "final_event_id": "4"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with StaffDeck(base_url=f"http://127.0.0.1:{server.server_port}", api_key="fixture") as sdk:
            events = sdk.runs.events("run")
            assert next(events).id == "3"
            if empty_connections == 1:
                assert [event.id for event in events] == ["4"]
            else:
                with pytest.raises(StreamError) as caught:
                    list(events)
                assert caught.value.last_event_id == "3"
        assert cursors == [None, "3", "3"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
