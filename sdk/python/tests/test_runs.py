from __future__ import annotations

from contextlib import closing

import httpx
import pytest

from staffdeck import APIError, RunFailedError, StaffDeck, StreamError, WaitTimeout
from staffdeck.errors import ProtocolError
from staffdeck.streaming import parse_events


def client(handler):
    return StaffDeck(base_url="https://test", api_key="key", transport=httpx.MockTransport(handler))


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks, *, fail=False):
        self.chunks = chunks
        self.fail = fail
        self.closed = False

    def __iter__(self):
        yield from self.chunks
        if self.fail:
            raise httpx.ReadError("connection lost")

    def close(self):
        self.closed = True


def event_response(stream):
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)


def test_sse_framing_unicode_multiline_comments():
    stream = Chunks([b"\xef\xbb\xbf: comment\r\nid: 1\r\nevent: run.output.delta\r\ndata: {\r\n",
                     'data: "content":"你好"}\r\n\r\n'.encode()])

    def handle(request):
        if request.url.path.endswith("events"):
            if request.headers.get("last-event-id") == "1":
                return event_response(Chunks([]))
            return event_response(stream)
        return httpx.Response(200, json={"status": "succeeded"})

    with client(handle) as sdk:
        events = list(sdk.runs.events("run"))
    assert len(events) == 1
    assert events[0].id == "1"
    assert events[0].data == {"content": "你好"}
    assert stream.closed


def test_stream_reconnect_uses_last_delivered_id_and_deduplicates(monkeypatch):
    monkeypatch.setattr("staffdeck.runs.time.sleep", lambda _: None)
    calls = []
    first = Chunks([b'id: 1\nevent: run.output.delta\ndata: {"content":"one"}\n\n'], fail=True)
    second = Chunks([b'id: 1\ndata: {}\n\nid: 2\nevent: run.succeeded\ndata: {}\n\n'])

    def handle(request):
        calls.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("events"):
            assert request.headers["accept"] == "text/event-stream"
            if len(calls) == 1:
                assert "last-event-id" not in request.headers
                return event_response(first)
            assert request.headers["last-event-id"] == "1"
            return event_response(second)
        return httpx.Response(200, json={"status": "succeeded"})

    with client(handle) as sdk:
        assert [e.id for e in sdk.runs.events("run")] == ["1", "2"]
    assert first.closed and second.closed
    assert len(calls) == 3


def test_normal_eof_reconnects_until_terminal_state(monkeypatch):
    monkeypatch.setattr("staffdeck.runs.time.sleep", lambda _: None)
    streams = 0

    def handle(request):
        nonlocal streams
        if request.url.path.endswith("events"):
            streams += 1
            assert request.headers.get("last-event-id") == ("5" if streams == 1 else "6")
            name = "run.output.delta" if streams == 1 else "run.succeeded"
            return event_response(Chunks([
                f'id: {streams + 5}\nevent: {name}\ndata: {{}}\n\n'.encode()
            ]))
        return httpx.Response(200, json={"status": "running" if streams == 1 else "succeeded"})

    with client(handle) as sdk:
        assert [e.id for e in sdk.runs.events("run", last_event_id="5")] == ["6", "7"]


def test_stream_exhaustion_exposes_resume_cursor_and_closes(monkeypatch):
    monkeypatch.setattr("staffdeck.runs.time.sleep", lambda _: None)
    streams = []

    def handle(request):
        stream = Chunks([b'id: 7\ndata: {}\n\n'], fail=True)
        streams.append(stream)
        return event_response(stream)

    with client(handle) as sdk, pytest.raises(StreamError) as caught:
        list(sdk.runs.events("run", max_reconnects=1))
    assert caught.value.run_id == "run"
    assert caught.value.last_event_id == "7"
    assert len(streams) == 2
    assert all(s.closed for s in streams)


def test_early_stream_close_releases_connection():
    stream = Chunks([b'id: 1\ndata: {}\n\nid: 2\ndata: {}\n\n'])
    with client(lambda r: event_response(stream)) as sdk, closing(sdk.runs.events("run")) as events:
        assert next(events).id == "1"
    assert stream.closed


@pytest.mark.parametrize("frame", ["id: 1\ndata: invalid\n\n", "data: {}\n\n"])
def test_invalid_frames_are_not_silently_dropped(frame):
    with pytest.raises(ProtocolError):
        list(parse_events(frame.splitlines()))
    with pytest.raises(ProtocolError):
        list(parse_events(["data: " + "a" * 40], max_event_chars=20))


def test_stream_rejects_wrong_content_type_and_auth_errors():
    with client(lambda r: httpx.Response(200, json={})) as sdk, pytest.raises(StreamError):
        list(sdk.runs.events("run"))
    with (
        client(lambda r: httpx.Response(403, json={"code": "FORBIDDEN"})) as sdk,
        pytest.raises(APIError) as caught,
    ):
        list(sdk.runs.events("run"))
    assert caught.value.status_code == 403


def test_wait_observes_terminal_state_before_result(monkeypatch):
    monkeypatch.setattr("staffdeck.runs.time.sleep", lambda _: None)
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("result"):
            assert len(calls) == 3
            return httpx.Response(200, json={"reply": "done"})
        return httpx.Response(200, json={"status": "queued" if len(calls) == 1 else "succeeded"})

    with client(handle) as sdk:
        assert sdk.runs.wait("run").data == {"reply": "done"}


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_wait_does_not_treat_failed_or_cancelled_as_success(status):
    with (
        client(lambda r: httpx.Response(200, json={"status": status, "error": {"secret": 1}})) as sdk,
        pytest.raises(RunFailedError) as caught,
    ):
        sdk.runs.wait("run")
    assert caught.value.status == status
    assert caught.value.job["error"] == {"secret": 1}
    assert "secret" not in str(caught.value)


def test_wait_timeout_is_bounded_and_does_not_cancel(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("staffdeck.runs.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("staffdeck.runs.time.sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    calls = []

    def handle(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.extensions["timeout"]["read"] <= 2
        return httpx.Response(200, json={"status": "queued"})

    with client(handle) as sdk, pytest.raises(WaitTimeout):
        sdk.runs.wait("run", timeout=2, poll_interval=1)
    assert clock[0] == 2
    assert len(calls) == 2


def test_partial_frame_reconnects_even_if_job_has_finished(monkeypatch):
    monkeypatch.setattr("staffdeck.runs.time.sleep", lambda _: None)
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("events"):
            if len(calls) == 1:
                return event_response(Chunks([b'id: 1\ndata: {}\n\nid: 2\ndata: {']))
            assert request.headers["last-event-id"] == "1"
            return event_response(Chunks([b'id: 2\nevent: run.succeeded\ndata: {}\n\n']))
        return httpx.Response(200, json={"status": "succeeded"})

    with client(handle) as sdk:
        assert [e.id for e in sdk.runs.events("run")] == ["1", "2"]


def test_stream_transient_http_error_retry_after(monkeypatch):
    sleeps = []
    monkeypatch.setattr("staffdeck.runs.time.sleep", sleeps.append)
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, headers={"Retry-After": "3"})
        if request.url.path.endswith("events"):
            return event_response(Chunks([b'id: 1\nevent: run.succeeded\ndata: {}\n\n']))
        return httpx.Response(200, json={"status": "succeeded"})

    with client(handle) as sdk:
        assert len(list(sdk.runs.events("run"))) == 1
    assert sleeps == [3]


@pytest.mark.parametrize("data", [{}, {"status": []}, {"status": None}, {"status": "new"}, []])
def test_invalid_status_raises_protocol_error_instead_of_crashing_or_waiting(data):
    def handle(request):
        if request.url.path.endswith("events"):
            return event_response(Chunks([]))
        return httpx.Response(200, json=data)

    with client(handle) as sdk:
        with pytest.raises(ProtocolError):
            sdk.runs.wait("run")
        with pytest.raises(StreamError):
            list(sdk.runs.events("run"))


@pytest.mark.parametrize("cursor", ["9" * 5000, str(2**63), "-1", "１２", "", "1\n"])
def test_invalid_sequence_ids_are_rejected_before_delivery(cursor):
    with (
        client(lambda r: pytest.fail("Invalid cursor must not reach HTTP")) as sdk,
        pytest.raises(ValueError),
    ):
        list(sdk.runs.events("run", last_event_id=cursor))
    with pytest.raises(ProtocolError):
        list(parse_events([f"id: {cursor}", "data: {}", ""]))
