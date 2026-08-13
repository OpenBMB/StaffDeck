import mimetypes
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

import single_port_app


def _request(
    body: bytes,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    query_string: bytes = b"",
) -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/llm/v1/chat/completions",
            "raw_path": b"/llm/v1/chat/completions",
            "query_string": query_string,
            "headers": headers or [],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 5173),
        },
        receive,
    )


def test_javascript_assets_override_broken_windows_mime_mapping(tmp_path: Path) -> None:
    original_media_type = mimetypes.guess_type("bundle.js")[0]
    mimetypes.add_type("text/plain", ".js", strict=True)

    try:
        asset_dir = tmp_path / "assets"
        asset_dir.mkdir()
        (asset_dir / "bundle.js").write_text("export const ready = true;", encoding="utf-8")
        app = FastAPI()
        app.mount("/assets", single_port_app.FrontendStaticFiles(directory=asset_dir))

        response = TestClient(app).head("/assets/bundle.js")

        assert response.status_code == 200
        assert response.headers["content-type"] == "text/javascript; charset=utf-8"
    finally:
        mimetypes.add_type(original_media_type or "text/javascript", ".js", strict=True)


def test_valid_application_javascript_mapping_is_not_reported_as_correction(
    caplog,
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setitem(mimetypes.types_map, ".js", "application/javascript")
    asset_dir = tmp_path / "assets"
    asset_dir.mkdir()
    (asset_dir / "bundle.js").write_text("export const ready = true;", encoding="utf-8")
    app = FastAPI()
    app.mount("/assets", single_port_app.FrontendStaticFiles(directory=asset_dir))

    with caplog.at_level("INFO", logger="staffdeck.static"):
        response = TestClient(app).head("/assets/bundle.js")

    assert response.headers["content-type"] == "text/javascript; charset=utf-8"
    assert "Corrected frontend MIME" not in caplog.text
    assert "Frontend module MIME" not in caplog.text


def test_mime_diagnostic_does_not_record_requested_asset_name(
    caplog,
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setitem(mimetypes.types_map, ".js", "text/plain")
    asset_dir = tmp_path / "assets"
    asset_dir.mkdir()
    sensitive_name = "customer-secret-name.js"
    (asset_dir / sensitive_name).write_text("export {};", encoding="utf-8")
    app = FastAPI()
    app.mount("/assets", single_port_app.FrontendStaticFiles(directory=asset_dir))

    single_port_app.logger.addHandler(caplog.handler)
    try:
        with caplog.at_level("WARNING", logger="staffdeck.static"):
            response = TestClient(app).head(f"/assets/{sensitive_name}")
    finally:
        single_port_app.logger.removeHandler(caplog.handler)

    assert response.status_code == 200
    assert "Corrected frontend MIME suffix=.js" in caplog.text
    assert sensitive_name not in caplog.text


def test_pilotdeck_host_routing_requires_enabled_exact_host(monkeypatch) -> None:
    monkeypatch.setattr(single_port_app, "PILOTDECK_UPSTREAM", "http://127.0.0.1:13001")
    monkeypatch.setattr(
        single_port_app,
        "PILOTDECK_PUBLIC_HOSTS",
        {"pilotdeck.39.102.210.77.nip.io"},
    )

    assert single_port_app._is_pilotdeck_request(
        "pilotdeck.39.102.210.77.nip.io:10087"
    )
    assert not single_port_app._is_pilotdeck_request("39.102.210.77:10087")
    assert not single_port_app._is_pilotdeck_request(
        "pilotdeck.39.102.210.77.nip.io.example.com"
    )


def test_pilotdeck_path_routing_strips_only_the_dedicated_prefix(monkeypatch) -> None:
    monkeypatch.setattr(single_port_app, "PILOTDECK_UPSTREAM", "http://127.0.0.1:13001")
    monkeypatch.setattr(single_port_app, "PILOTDECK_PUBLIC_HOSTS", set())

    assert single_port_app._pilotdeck_proxy_path("39.102.210.77:10087", "/pilotdeck") == "/"
    assert single_port_app._pilotdeck_proxy_path("39.102.210.77:10087", "/pilotdeck/") == "/"
    assert single_port_app._pilotdeck_proxy_path(
        "39.102.210.77:10087", "/pilotdeck/api/auth/status"
    ) == "/api/auth/status"
    assert single_port_app._pilotdeck_proxy_path("39.102.210.77:10087", "/chat/") is None


def test_pilotdeck_target_url_preserves_path_query_and_websocket_scheme(monkeypatch) -> None:
    monkeypatch.setattr(single_port_app, "PILOTDECK_UPSTREAM", "http://127.0.0.1:13001")

    assert single_port_app._pilotdeck_target_url("/api/groups", "limit=10") == (
        "http://127.0.0.1:13001/api/groups?limit=10"
    )
    assert single_port_app._pilotdeck_target_url("/ws", "token=test", websocket=True) == (
        "ws://127.0.0.1:13001/ws?token=test"
    )


def test_pilotdeck_proxy_timeout_configures_every_httpx_phase() -> None:
    timeout = single_port_app._pilotdeck_proxy_timeout()

    assert timeout.connect == 10.0
    assert timeout.read is None
    assert timeout.write == 600.0
    assert timeout.pool == 10.0


@pytest.mark.asyncio
async def test_llm_relay_is_disabled_without_upstream(monkeypatch) -> None:
    monkeypatch.setattr(single_port_app, "LLM_RELAY_UPSTREAM", "")

    with pytest.raises(HTTPException) as exc_info:
        await single_port_app.llm_chat_completions_relay(_request(b"{}"))

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_llm_relay_forwards_auth_body_query_and_stream(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class ChunkStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"data: first\n\n"
            yield b"data: [DONE]\n\n"

    class FakeAsyncClient:
        def __init__(self, **kwargs) -> None:
            captured["client_kwargs"] = kwargs

        def build_request(self, method, url, *, headers, content):  # noqa: ANN001
            captured.update(
                method=method,
                url=url,
                headers=headers,
                content=content,
            )
            return httpx.Request(method, url, headers=headers, content=content)

        async def send(self, request, *, stream):  # noqa: ANN001
            captured["stream"] = stream
            return httpx.Response(
                200,
                request=request,
                headers={
                    "content-type": "text/event-stream",
                    "connection": "close",
                    "x-upstream-request-id": "req_123",
                },
                stream=ChunkStream(),
            )

        async def aclose(self) -> None:
            captured["client_closed"] = True

    monkeypatch.setattr(single_port_app, "LLM_RELAY_UPSTREAM", "https://llm.example")
    monkeypatch.setattr(single_port_app.httpx, "AsyncClient", FakeAsyncClient)
    payload = b'{"model":"demo","stream":true}'
    response = await single_port_app.llm_chat_completions_relay(
        _request(
            payload,
            headers=[
                (b"authorization", b"Bearer secret"),
                (b"content-type", b"application/json"),
                (b"cookie", b"must-not-leak=1"),
                (b"x-request-id", b"request-local"),
            ],
            query_string=b"trace=true",
        )
    )
    streamed = b"".join([chunk async for chunk in response.body_iterator])

    assert captured["method"] == "POST"
    assert captured["url"] == (
        "https://llm.example/llm/v1/chat/completions?trace=true"
    )
    assert captured["content"] == payload
    assert captured["headers"] == {
        "authorization": "Bearer secret",
        "content-type": "application/json",
        "x-request-id": "request-local",
    }
    assert captured["stream"] is True
    assert captured["client_closed"] is True
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    assert "connection" not in response.headers
    assert response.headers["x-accel-buffering"] == "no"
    assert streamed == b"data: first\n\ndata: [DONE]\n\n"


@pytest.mark.asyncio
async def test_llm_relay_rejects_oversized_body_before_upstream(monkeypatch) -> None:
    monkeypatch.setattr(single_port_app, "LLM_RELAY_UPSTREAM", "https://llm.example")
    monkeypatch.setattr(single_port_app, "LLM_RELAY_MAX_BODY_BYTES", 3)

    with pytest.raises(HTTPException) as exc_info:
        await single_port_app.llm_chat_completions_relay(
            _request(b"four", headers=[(b"content-length", b"4")])
        )

    assert exc_info.value.status_code == 413


@pytest.mark.asyncio
async def test_anthropic_messages_relay_uses_dedicated_upstream(monkeypatch) -> None:
    captured: dict[str, object] = {}
    expected = single_port_app.StreamingResponse(iter([b"ok"]))

    async def fake_relay(request, *, upstream, upstream_path):  # noqa: ANN001
        captured.update(
            request=request,
            upstream=upstream,
            upstream_path=upstream_path,
        )
        return expected

    monkeypatch.setattr(
        single_port_app,
        "ANTHROPIC_RELAY_UPSTREAM",
        "https://llm-center.modelbest.cn",
    )
    monkeypatch.setattr(single_port_app, "_relay_llm_request", fake_relay)
    request = _request(b"{}")

    response = await single_port_app.anthropic_messages_relay(request)

    assert response is expected
    assert captured == {
        "request": request,
        "upstream": "https://llm-center.modelbest.cn",
        "upstream_path": "/llm/v1/messages",
    }
    assert {
        "x-api-key",
        "anthropic-version",
        "anthropic-beta",
    } <= single_port_app.LLM_RELAY_REQUEST_HEADERS
