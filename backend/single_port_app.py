import asyncio
import logging
import os
from pathlib import Path

import httpx
import websockets
from fastapi import HTTPException
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from starlette.requests import Request
from starlette.staticfiles import StaticFiles
from starlette.types import Scope
from starlette.websockets import WebSocket, WebSocketDisconnect

from app import paths
from app.main import app


logger = logging.getLogger("staffdeck.static")
ROOT_DIR = paths.resource_dir()
# frozen: dist 被收集到 _MEIPASS/frontend-enterprise/dist
# dev:    resource_dir()==backend/，需回到仓库根找 frontend-enterprise
ENTERPRISE_DIST = (
    ROOT_DIR / "frontend-enterprise" / "dist"
    if paths.is_frozen()
    else ROOT_DIR.parent / "frontend-enterprise" / "dist"
)
SPA_INDEX_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}
SITE_CHAT_UPSTREAM = os.getenv(
    "STAFFDECK_SITE_CHAT_UPSTREAM",
    "http://127.0.0.1:10187",
).rstrip("/")
PILOTDECK_UPSTREAM = os.getenv("STAFFDECK_PILOTDECK_UPSTREAM", "").rstrip("/")
PILOTDECK_PUBLIC_URL = os.getenv("STAFFDECK_PILOTDECK_PUBLIC_URL", "").rstrip("/")
PILOTDECK_PUBLIC_HOSTS = {
    item.strip().lower()
    for item in os.getenv("STAFFDECK_PILOTDECK_PUBLIC_HOSTS", "").split(",")
    if item.strip()
}
LLM_RELAY_UPSTREAM = os.getenv("STAFFDECK_LLM_RELAY_UPSTREAM", "").rstrip("/")
ANTHROPIC_RELAY_UPSTREAM = os.getenv(
    "STAFFDECK_ANTHROPIC_RELAY_UPSTREAM", ""
).rstrip("/")
LLM_RELAY_MAX_BODY_BYTES = int(
    os.getenv("STAFFDECK_LLM_RELAY_MAX_BODY_BYTES", str(32 * 1024 * 1024))
)
LLM_RELAY_REQUEST_HEADERS = {
    "accept",
    "anthropic-beta",
    "anthropic-version",
    "api-key",
    "authorization",
    "content-type",
    "user-agent",
    "x-api-key",
    "x-request-id",
}
HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
FRONTEND_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".mjs": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".wasm": "application/wasm",
}


def _request_host_without_port(host: str) -> str:
    value = host.strip().lower()
    if value.startswith("["):
        return value.split("]", 1)[0].lstrip("[")
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def _is_pilotdeck_request(host: str) -> bool:
    return bool(
        PILOTDECK_UPSTREAM
        and PILOTDECK_PUBLIC_HOSTS
        and _request_host_without_port(host) in PILOTDECK_PUBLIC_HOSTS
    )


def _pilotdeck_proxy_path(host: str, path: str) -> str | None:
    """Return the upstream path for either the virtual host or /pilotdeck prefix."""
    if _is_pilotdeck_request(host):
        return path
    if not PILOTDECK_UPSTREAM:
        return None
    if path == "/pilotdeck" or path == "/pilotdeck/":
        return "/"
    if path.startswith("/pilotdeck/"):
        return path.removeprefix("/pilotdeck")
    return None


def _pilotdeck_target_url(path: str, query: str = "", *, websocket: bool = False) -> str:
    upstream = PILOTDECK_UPSTREAM
    if websocket:
        if upstream.startswith("https://"):
            upstream = f"wss://{upstream.removeprefix('https://')}"
        elif upstream.startswith("http://"):
            upstream = f"ws://{upstream.removeprefix('http://')}"
    target = f"{upstream}{path}"
    return f"{target}?{query}" if query else target


def _pilotdeck_proxy_timeout() -> httpx.Timeout:
    """Keep streamed responses open while bounding connect, write, and pool waits."""
    return httpx.Timeout(connect=10.0, read=None, write=600.0, pool=10.0)


@app.middleware("http")
async def pilotdeck_host_proxy(request: Request, call_next):  # noqa: ANN001
    """Route the PilotDeck virtual host or /pilotdeck prefix through this process."""
    upstream_path = _pilotdeck_proxy_path(
        request.headers.get("host", ""),
        request.url.path,
    )
    if upstream_path is None:
        return await call_next(request)

    request_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "host"
    }
    request_headers["x-forwarded-host"] = request.headers.get("host", "")
    request_headers["x-forwarded-proto"] = request.url.scheme
    if request.client:
        request_headers["x-forwarded-for"] = request.client.host

    client = httpx.AsyncClient(timeout=_pilotdeck_proxy_timeout())
    upstream_request = client.build_request(
        request.method,
        _pilotdeck_target_url(upstream_path, request.url.query),
        headers=request_headers,
        content=request.stream(),
    )
    try:
        upstream_response = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail="PilotDeck upstream unavailable") from exc

    response_headers = {
        key: value
        for key, value in upstream_response.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }
    location = response_headers.get("location")
    if location and PILOTDECK_PUBLIC_URL and location.startswith(PILOTDECK_UPSTREAM):
        response_headers["location"] = f"{PILOTDECK_PUBLIC_URL}{location[len(PILOTDECK_UPSTREAM):]}"
    response_headers["x-accel-buffering"] = "no"

    async def stream_body():
        try:
            async for chunk in upstream_response.aiter_raw():
                yield chunk
        finally:
            await upstream_response.aclose()
            await client.aclose()

    return StreamingResponse(
        stream_body(),
        status_code=upstream_response.status_code,
        headers=response_headers,
    )


@app.websocket("/{pilotdeck_path:path}")
async def pilotdeck_websocket_proxy(websocket: WebSocket, pilotdeck_path: str) -> None:
    """Relay PilotDeck chat and shell sockets selected by Host or path prefix."""
    public_path = f"/{pilotdeck_path}"
    upstream_path = _pilotdeck_proxy_path(
        websocket.headers.get("host", ""),
        public_path,
    )
    if upstream_path is None:
        await websocket.close(code=4404)
        return

    await websocket.accept()
    upstream_headers = {
        key: value
        for key, value in websocket.headers.items()
        if key.lower()
        not in {
            *HOP_BY_HOP_HEADERS,
            "host",
            "sec-websocket-accept",
            "sec-websocket-extensions",
            "sec-websocket-key",
            "sec-websocket-protocol",
            "sec-websocket-version",
        }
    }
    origin = upstream_headers.get("origin")
    if origin:
        upstream_headers["origin"] = PILOTDECK_UPSTREAM
    target = _pilotdeck_target_url(
        upstream_path,
        websocket.scope.get("query_string", b"").decode("ascii"),
        websocket=True,
    )
    try:
        async with websockets.connect(
            target,
            additional_headers=upstream_headers,
            open_timeout=10,
            close_timeout=5,
            max_size=None,
        ) as upstream:
            async def client_to_upstream() -> None:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    if message.get("text") is not None:
                        await upstream.send(message["text"])
                    elif message.get("bytes") is not None:
                        await upstream.send(message["bytes"])

            async def upstream_to_client() -> None:
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            tasks = {
                asyncio.create_task(client_to_upstream()),
                asyncio.create_task(upstream_to_client()),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
    except (WebSocketDisconnect, websockets.ConnectionClosed):
        pass
    except Exception:
        logger.exception("PilotDeck WebSocket proxy failed path=%s", public_path)
        try:
            await websocket.close(code=4502)
        except RuntimeError:
            pass


class FrontendStaticFiles(StaticFiles):
    """Serve Vite assets with stable MIME types across Windows machines."""

    def file_response(
        self,
        full_path: os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        suffix = Path(full_path).suffix.lower()
        media_type = FRONTEND_CONTENT_TYPES.get(suffix)
        if media_type:
            detected_media_type = response.headers.get("Content-Type")
            response.headers["Content-Type"] = media_type
            detected_base_type = (detected_media_type or "").partition(";")[0].strip().lower()
            allowed_base_types = {media_type.partition(";")[0].lower()}
            if suffix in {".js", ".mjs"}:
                allowed_base_types.add("application/javascript")
            if detected_base_type not in allowed_base_types:
                logger.warning(
                    "Corrected frontend MIME suffix=%s detected=%s forced=%s",
                    suffix,
                    detected_media_type,
                    media_type,
                )
        return response


def spa_index_response(index_path: Path) -> FileResponse:
    return FileResponse(index_path, headers=SPA_INDEX_HEADERS)


@app.api_route(
    "/api/site-chat/{site_path:path}",
    methods=["GET", "POST", "OPTIONS"],
    include_in_schema=False,
)
async def site_chat_proxy(site_path: str, request: Request) -> StreamingResponse:
    target_url = f"{SITE_CHAT_UPSTREAM}/api/site-chat/{site_path}"
    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"

    request_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "host"
    }
    request_headers["x-forwarded-host"] = request.headers.get("host", "")
    request_headers["x-forwarded-proto"] = request.url.scheme
    if request.client:
        request_headers["x-forwarded-for"] = request.client.host

    client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))
    upstream_request = client.build_request(
        request.method,
        target_url,
        headers=request_headers,
        content=await request.body(),
    )
    try:
        upstream_response = await client.send(upstream_request, stream=True)
    except Exception:
        await client.aclose()
        raise

    response_headers = {
        key: value
        for key, value in upstream_response.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }
    response_headers["x-accel-buffering"] = "no"

    async def stream_body():
        try:
            async for chunk in upstream_response.aiter_raw():
                yield chunk
        finally:
            await upstream_response.aclose()
            await client.aclose()

    return StreamingResponse(
        stream_body(),
        status_code=upstream_response.status_code,
        headers=response_headers,
    )


@app.post(
    "/llm/v1/chat/completions",
    include_in_schema=False,
)
async def llm_chat_completions_relay(request: Request) -> StreamingResponse:
    """Relay one OpenAI-compatible endpoint without exposing an arbitrary proxy."""
    return await _relay_llm_request(
        request,
        upstream=LLM_RELAY_UPSTREAM,
        upstream_path="/llm/v1/chat/completions",
    )


@app.post(
    "/llm/v1/messages",
    include_in_schema=False,
)
async def anthropic_messages_relay(request: Request) -> StreamingResponse:
    """Relay the fixed Anthropic-compatible messages endpoint."""
    return await _relay_llm_request(
        request,
        upstream=ANTHROPIC_RELAY_UPSTREAM,
        upstream_path="/llm/v1/messages",
    )


async def _relay_llm_request(
    request: Request,
    *,
    upstream: str,
    upstream_path: str,
) -> StreamingResponse:
    if not upstream:
        raise HTTPException(status_code=404, detail="LLM relay is not enabled")

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > LLM_RELAY_MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Request body is too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc

    body = await request.body()
    if len(body) > LLM_RELAY_MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request body is too large")

    target_url = f"{upstream}{upstream_path}"
    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"
    request_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() in LLM_RELAY_REQUEST_HEADERS
    }

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=None, write=60.0, pool=10.0)
    )
    upstream_request = client.build_request(
        "POST",
        target_url,
        headers=request_headers,
        content=body,
    )
    try:
        upstream_response = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail="LLM relay upstream unavailable") from exc

    response_headers = {
        key: value
        for key, value in upstream_response.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }
    response_headers["x-accel-buffering"] = "no"

    async def stream_body():
        try:
            async for chunk in upstream_response.aiter_raw():
                yield chunk
        finally:
            await upstream_response.aclose()
            await client.aclose()

    return StreamingResponse(
        stream_body(),
        status_code=upstream_response.status_code,
        headers=response_headers,
    )

app.mount(
    "/assets",
    FrontendStaticFiles(directory=ENTERPRISE_DIST / "assets", check_dir=False),
    name="assets",
)
app.mount(
    "/enterprise/assets",
    FrontendStaticFiles(directory=ENTERPRISE_DIST / "assets", check_dir=False),
    name="enterprise-assets",
)
app.mount(
    "/chat/assets",
    FrontendStaticFiles(directory=ENTERPRISE_DIST / "assets", check_dir=False),
    name="chat-assets",
)
app.mount(
    "/workspace/assets",
    FrontendStaticFiles(directory=ENTERPRISE_DIST / "assets", check_dir=False),
    name="workspace-assets",
)


@app.get("/", include_in_schema=False)
def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/chat/")


@app.get("/pilotdeck", include_in_schema=False)
@app.get("/pilotdeck/", include_in_schema=False)
def pilotdeck_redirect() -> RedirectResponse:
    if not PILOTDECK_PUBLIC_URL:
        raise HTTPException(status_code=404, detail="PilotDeck is not enabled")
    return RedirectResponse(url=f"{PILOTDECK_PUBLIC_URL}/")


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.png", include_in_schema=False)
@app.get("/staffdeck-icon.png", include_in_schema=False)
def brand_icon(request: Request) -> FileResponse:
    # 品牌图标：从前端 dist 根目录 serve（favicon.ico/png、apple-touch-icon）
    name = request.url.path.lstrip("/")
    target = ENTERPRISE_DIST / name
    if not target.exists():
        target = ENTERPRISE_DIST / "favicon.ico"
    return FileResponse(target)


@app.get("/enterprise", include_in_schema=False)
@app.get("/enterprise/{path:path}", include_in_schema=False)
def enterprise_app(path: str = "") -> FileResponse:
    return spa_index_response(ENTERPRISE_DIST / "index.html")


@app.get("/login", include_in_schema=False)
@app.get("/chat", include_in_schema=False)
@app.get("/chat/{path:path}", include_in_schema=False)
@app.get("/workspace", include_in_schema=False)
@app.get("/workspace/{path:path}", include_in_schema=False)
def chat_app(path: str = "") -> FileResponse:
    return spa_index_response(ENTERPRISE_DIST / "index.html")
