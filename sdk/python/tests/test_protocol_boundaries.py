from __future__ import annotations

import io
import json
import zlib

import httpx
import pytest

from staffdeck import ProtocolError, StaffDeck, StreamError, cli
from staffdeck.streaming import iter_sse_lines


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        yield from self.chunks

    def close(self):
        self.closed = True


@pytest.mark.parametrize("final_id", ["4", None, "missing"])
def test_empty_reconnect_cannot_confirm_completion(monkeypatch, final_id):
    monkeypatch.setattr("staffdeck.runs.time.sleep", lambda _: None)
    cursors = []
    streams = [
        b'id: 3\nevent: run.failed\ndata: {}\n\n',
        b"",
        b'id: 4\nevent: run.failed\ndata: {}\n\n',
    ]

    def handle(request):
        if not request.url.path.endswith("/events"):
            state = {"status": "failed"}
            if final_id != "missing":
                state["final_event_id"] = final_id
            return httpx.Response(200, json=state)
        cursors.append(request.headers.get("last-event-id"))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=Chunks([streams[len(cursors) - 1]]))

    with StaffDeck(base_url="https://test", api_key="key",
                   transport=httpx.MockTransport(handle)) as sdk:
        events = sdk.runs.events("run")
        assert next(events).id == "3"
        assert next(events).id == "4"
        if final_id != "4":
            with pytest.raises(StreamError) as caught:
                next(events)
            assert caught.value.last_event_id == "4"
        else:
            assert list(events) == []
    assert cursors == [None, "3", "3"]


@pytest.mark.parametrize("final_id", [4, True, "-1", "bad", "2", {}, []])
def test_invalid_or_behind_final_cursor_fails_closed(final_id):
    stream = Chunks([b'id: 3\ndata: {}\n\n'])

    def handle(request):
        if request.url.path.endswith("/events"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=stream)
        return httpx.Response(200, json={"status": "failed", "final_event_id": final_id})

    with StaffDeck(base_url="https://test", api_key="key",
                   transport=httpx.MockTransport(handle)) as sdk:
        events = sdk.runs.events("run", max_reconnects=0)
        assert next(events).id == "3"
        with pytest.raises(StreamError) as caught:
            next(events)
        assert caught.value.last_event_id == "3"
    assert stream.closed


@pytest.mark.parametrize("name", ["run.failed", "run.cancelled", "run.succeeded"])
def test_terminal_named_event_does_not_skip_persisted_tail(monkeypatch, name):
    monkeypatch.setattr("staffdeck.runs.time.sleep", lambda _: None)
    cursors = []

    def handle(request):
        if not request.url.path.endswith("/events"):
            return httpx.Response(200, json={"status": "failed", "final_event_id": "4"})
        cursor = request.headers.get("last-event-id")
        cursors.append(cursor)
        frames = {
            None: f'id: 3\nevent: {name}\ndata: {{}}\n\n'.encode(),
            "3": b'id: 4\nevent: run.failed\ndata: {"status":"failed"}\n\n',
            "4": b"",
        }
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=Chunks([frames[cursor]]))

    with StaffDeck(base_url="https://test", api_key="key",
                   transport=httpx.MockTransport(handle)) as sdk:
        assert [event.id for event in sdk.runs.events("run")] == ["3", "4"]
    assert cursors == [None, "3"]


def test_cli_stdin_uses_utf8_not_system_encoding(monkeypatch, capsys):
    monkeypatch.setenv("STAFFDECK_API_KEY", "key")
    monkeypatch.setenv("STAFFDECK_BASE_URL", "https://test")
    payload = {"input": "你好"}
    stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload, ensure_ascii=False).encode()),
                             encoding="cp1252")
    monkeypatch.setattr("sys.stdin", stdin)
    bodies = []

    def handle(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(202, json={"id": "run"})

    monkeypatch.setattr(cli, "StaffDeck", lambda **kwargs: StaffDeck(
        **kwargs, transport=httpx.MockTransport(handle)
    ))
    assert cli.main(["runs", "create", "--agent-id", "a", "--json", "-"]) == 0
    assert not capsys.readouterr().err
    assert bodies == [payload]


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_corrupt_gzip_is_sdk_error_and_closes_response(method):
    stream = Chunks([b"not gzip"])
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=stream)

    with (
        StaffDeck(base_url="https://test", api_key="key",
                  transport=httpx.MockTransport(handle)) as sdk,
        pytest.raises(ProtocolError),
    ):
        sdk.request(method, "agents")
    assert stream.closed
    assert len(calls) == 1


def test_corrupt_compression_after_sse_delivery_preserves_cursor():
    compressor = zlib.compressobj(wbits=31)
    prefix = compressor.compress(b'id: 7\ndata: {}\n\n') + compressor.flush(zlib.Z_SYNC_FLUSH)
    # Invalid deflate block after a valid, flushed event (not a mock exception).
    stream = Chunks([prefix, b"\x07"])

    def handle(request):
        return httpx.Response(200, headers={
            "content-encoding": "gzip", "content-type": "text/event-stream",
        }, stream=stream)

    with StaffDeck(base_url="https://test", api_key="key",
                   transport=httpx.MockTransport(handle)) as sdk:
        events = sdk.runs.events("run", max_reconnects=0)
        assert next(events).id == "7"
        with pytest.raises(StreamError) as caught:
            next(events)
    assert caught.value.last_event_id == "7"
    assert stream.closed


@pytest.mark.parametrize("suffix", ["", "\n", "\r", "\r\n"])
def test_unterminated_sse_line_is_bounded_before_reading_more(suffix):
    consumed = []

    def chunks():
        for index in range(2):
            consumed.append(index)
            yield "x" * 524_288
        yield "x" + suffix
        pytest.fail("must reject oversized buffered line before consuming another chunk")

    with pytest.raises(ProtocolError, match="size limit"):
        list(iter_sse_lines(chunks()))
    assert consumed == [0, 1]


@pytest.mark.parametrize("newline", ["\n", "\r", "\r\n"])
def test_line_size_limit_allows_exact_boundary_and_resets(newline):
    chunks = ["123", "45", newline[:1], newline[1:], "67890", newline]
    assert list(iter_sse_lines(chunks, max_line_chars=5)) == ["12345", "67890"]


def test_invalid_utf8_stdin_is_usage_error_without_sending(monkeypatch, capsys):
    monkeypatch.setenv("STAFFDECK_API_KEY", "key")
    monkeypatch.setenv("STAFFDECK_BASE_URL", "https://test")
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(
        io.BytesIO(b'{"input":"\xff"}'), encoding="cp1252",
    ))
    monkeypatch.setattr(cli, "StaffDeck", lambda **_: pytest.fail("must not send"))
    assert cli.main(["runs", "create", "--agent-id", "a", "--json", "-"]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert json.loads(captured.err)["error"]["kind"] == "usage"


def test_corrupt_gzip_cli_returns_json_error(monkeypatch, capsys):
    monkeypatch.setenv("STAFFDECK_API_KEY", "key")
    monkeypatch.setenv("STAFFDECK_BASE_URL", "https://test")

    def handle(request):
        return httpx.Response(200, headers={"content-encoding": "gzip"},
                              stream=Chunks([b"not gzip"]))

    monkeypatch.setattr(cli, "StaffDeck", lambda **kwargs: StaffDeck(
        **kwargs, transport=httpx.MockTransport(handle)
    ))
    assert cli.main(["agents", "list"]) == 6
    captured = capsys.readouterr()
    assert not captured.out
    assert json.loads(captured.err)["error"]["kind"] == "protocol"
    assert "Traceback" not in captured.err


def test_terminal_named_event_without_drain_budget_exposes_cursor():
    def handle(request):
        if request.url.path.endswith("/events"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=Chunks([b'id: 3\nevent: run.failed\ndata: {}\n\n']))
        return httpx.Response(200, json={"status": "failed"})

    with StaffDeck(base_url="https://test", api_key="key",
                   transport=httpx.MockTransport(handle)) as sdk:
        events = sdk.runs.events("run", max_reconnects=0)
        assert next(events).id == "3"
        with pytest.raises(StreamError) as caught:
            next(events)
    assert caught.value.last_event_id == "3"
