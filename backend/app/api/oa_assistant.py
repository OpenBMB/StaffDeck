"""OA 浏览器代填（书签面板）的后端。

这条链路**不在服务端开浏览器**：
- 面板脚本在用户浏览器里、OA 页面自己的 origin 中执行（由书签注入），
  因此服务器部署也能用，也不需要用户装任何东西。
- 这里只负责下发脚本（配置内嵌，避开跨域 fetch / CORS）、下发场景清单、
  接收面板上报、以及内存里解析发票 PDF 算金额。

端点的鉴权口径：
- `/panel.js`、`/report`、`/invoice-amounts` 都是公开的 —— 请求来自用户浏览器里的 OA 页面，
  没有 StaffDeck 登录态。脚本/配置不含用户数据；发票上传只做内存解析、不落盘。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from app.core.invoice_amount import is_pdf_bytes, parse_invoice_uploads

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/enterprise/oa-assistant", tags=["enterprise:oa-assistant"])

# 面板下载与金额解析都是公开端点：请求来自用户浏览器里的 OA 页面，
# 没有 StaffDeck 登录态。脚本/配置不含用户数据；上传只做内存解析、不落盘。
MAX_INVOICE_FILES = 30
MAX_INVOICE_MB = 20

# 允许 OA 页面跨域调用（fetch 上传发票 PDF）。
CORS_HEADERS = {"Access-Control-Allow-Origin": "*"}

# backend/app/api/oa_assistant.py -> backend/config/oa_assistant.json
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "oa_assistant.json"
_PANEL_JS = Path(__file__).resolve().parent.parent / "web" / "oa_panel.js"
_RECORDER_JS = Path(__file__).resolve().parent.parent / "web" / "oa_recorder.js"


def _load_config() -> dict[str, Any]:
    """直接读配置文件，不依赖任何浏览器运行时。"""
    return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))


def _js_response(body: str, status_code: int = 200) -> Response:
    return Response(
        body,
        media_type="application/javascript; charset=utf-8",
        status_code=status_code,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@router.get("/panel.js")
def panel_script() -> Response:
    """下发面板脚本（公开）：配置以内嵌 JSON 形式拼在文件头，面板无需任何跨域请求。"""
    if not _PANEL_JS.exists():
        return _js_response("/* StaffDeck: 面板脚本缺失（backend/app/web/oa_panel.js） */", 500)
    try:
        config = _load_config()
    except Exception as exc:  # 配置坏了要说清，否则前端只有一个空面板
        logger.warning("读取 oa_assistant.json 失败：%s", exc)
        return _js_response(f"/* StaffDeck: 配置读取失败：{exc} */", 500)

    header = (
        "/* StaffDeck OA 代填面板 —— 本文件由后端生成，配置已内嵌，勿手工修改。 */\n"
        f"window.__SD_OA_CONFIG__ = {json.dumps(config, ensure_ascii=False)};\n"
    )
    return _js_response(header + _PANEL_JS.read_text(encoding="utf-8"))


@router.get("/recorder.js")
def recorder_script() -> Response:
    """下发页面录制器（公开，排查定位器用）；用户在 OA 页控制台加载后操作并 dump。"""
    if not _RECORDER_JS.exists():
        return _js_response("/* StaffDeck: 录制器缺失（backend/app/web/oa_recorder.js） */", 500)
    return _js_response(_RECORDER_JS.read_text(encoding="utf-8"))


@router.post("/report")
async def report(request: Request) -> dict[str, Any]:
    """接收面板的执行/探查上报（sendBeacon，无鉴权、失败静默）。

    仅写后端日志，便于开发期观察用户浏览器里的真实执行结果。
    """
    raw = (await request.body()).decode("utf-8", "replace")
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data = {"raw": raw[:1000]}
    logger.info("[oa-assistant] %s", json.dumps(data, ensure_ascii=False)[:4000])
    return {"ok": True}


def _invoice_response(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers=CORS_HEADERS)


@router.post("/invoice-amounts")
async def invoice_amounts(files: list[UploadFile] = File(default=[])) -> JSONResponse:
    """解析面板上传的电子发票 PDF 并累加金额（公开、不落盘）。

    面板拿它做「自动计算报销金额」：文件仍保留在用户浏览器里直接传给 OA，
    这里只是借后端 pypdf 解一下价税合计。扩展名/MIME 都不可信，只认 %PDF- 文件头。
    """
    if not files:
        return _invoice_response({"items": [], "rows": [], "total": "0.00"})
    if len(files) > MAX_INVOICE_FILES:
        return _invoice_response({"detail": f"一次最多解析 {MAX_INVOICE_FILES} 个附件"}, 400)

    uploads: list[tuple[str, bytes]] = []
    for item in files:
        name = Path(item.filename or "attachment.pdf").name
        data = await item.read()
        if not name.lower().endswith(".pdf"):
            return _invoice_response({"detail": f"{name} 不是 PDF 文件"}, 400)
        if not is_pdf_bytes(data):
            return _invoice_response({"detail": f"{name} 内容不是有效的 PDF（缺少 %PDF- 文件头）"}, 400)
        if len(data) > MAX_INVOICE_MB * 1024 * 1024:
            return _invoice_response({"detail": f"{name} 超过 {MAX_INVOICE_MB}MB"}, 400)
        uploads.append((name, data))

    try:
        config = _load_config()
    except Exception as exc:
        logger.warning("读取 oa_assistant.json 失败：%s", exc)
        config = {}
    parse_cfg = config.get("invoice_parse", {}) or {}
    result = parse_invoice_uploads(
        uploads,
        categories=parse_cfg.get("categories"),
        rate_invoice_types=parse_cfg.get("tax_rate_invoice_types"),
        default_invoice_type=parse_cfg.get("default_invoice_type", ""),
        kind_to_type=parse_cfg.get("kind_to_invoice_type"),
        transport_categories=parse_cfg.get("transport_categories"),
    )
    logger.info(
        "[oa-assistant] 解析 %d 张发票，合计 %s，生成 %d 行明细",
        len(uploads), result.get("total"), len(result.get("rows") or []),
    )
    return _invoice_response(result)