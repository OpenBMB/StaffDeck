import logging
import os
from pathlib import Path

import httpx
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from starlette.requests import Request
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

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

app.mount(
    "/assets",
    FrontendStaticFiles(directory=ENTERPRISE_DIST / "assets", check_dir=False),
    name="assets",
)
# 2026-08-11 修复：ECharts 本地 vendor（dist/vendor）需静态挂载，否则 /vendor/echarts.min.js 404 致对话框趋势图空白
app.mount(
    "/vendor",
    FrontendStaticFiles(directory=ENTERPRISE_DIST / "vendor", check_dir=False),
    name="vendor-assets",
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
