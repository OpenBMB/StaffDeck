"""OCR 助手的开放 API。

与企业端 `POST /api/enterprise/agent-apps/ocr:run` 共用
`app.api.agent_apps.execute_ocr_upload`——同一套校验、同一套上游调用，只是换一种认证：
这里用账号级全量密钥（`sd_live_*`，scope `ocr:run`），无需登录会话。

路径经 `main.py` 的 `app.mount("/api/v1", create_public_api_app())` 挂载后，
对外是 `POST /api/v1/ocr`（multipart，字段名与 indra.md 的 `/v1/ocr` 一致）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.api.agent_apps import OCR_OPEN_API_SCOPE, execute_ocr_upload
from app.public_api.auth import PublicPrincipal, require_scopes

router = APIRouter(tags=["ocr"])


@router.post(
    "/ocr",
    summary="OCR documents and images",
    description=(
        "把 PDF / PNG / JPEG / TIFF / BMP / WebP 转成 Markdown、纯文本，或带逐行坐标与"
        "版面块的结构化 JSON。识别在 OCR 服务内完成，不消耗大模型额度。"
    ),
    response_model=None,
)
def run_ocr_public(
    files: list[UploadFile] = File(...),
    format: str = Form("json"),
    mode: str = Form(""),
    dpi: int | None = Form(None),
    max_pages: int | None = Form(None),
    password: str = Form(""),
    math: bool | None = Form(None),
    furniture: bool | None = Form(None),
    image_ocr: bool | None = Form(None),
    include_lines: bool | None = Form(None),
    include_blocks: bool | None = Form(None),
    include_timings: bool | None = Form(None),
    principal: PublicPrincipal = Depends(require_scopes(OCR_OPEN_API_SCOPE)),
) -> dict:
    # principal 仅用于鉴权与审计（中间件从 request.state 取），识别本身与租户/员工无关。
    del principal
    return execute_ocr_upload(
        files,
        format=format,
        mode=mode,
        dpi=dpi,
        max_pages=max_pages,
        password=password,
        math=math,
        furniture=furniture,
        image_ocr=image_ocr,
        include_lines=include_lines,
        include_blocks=include_blocks,
        include_timings=include_timings,
    )
