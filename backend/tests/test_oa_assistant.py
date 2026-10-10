"""OA 浏览器代填（书签面板）后端测试。

只覆盖两个公开端点：`/panel.js` 下发与 `/report` 上报。面板逻辑在浏览器里跑，
这里不引入浏览器依赖。
"""

from __future__ import annotations

import asyncio
import io
import json

from fastapi import UploadFile
from starlette.requests import Request

from app.api import oa_assistant
from app.core import invoice_amount


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(data))


def _request(body: bytes) -> Request:
    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def test_panel_script_bakes_config() -> None:
    response = oa_assistant.panel_script()
    assert response.status_code == 200
    body = response.body.decode("utf-8")
    # 配置内嵌 → 面板不需要任何跨域请求
    assert "window.__SD_OA_CONFIG__" in body
    # 真实的字段定位器随配置下发
    assert "data-fieldmark" in body
    # 面板本体（UI/引擎）也在里面
    assert "__SD_OA_PANEL__" in body
    assert "no-store" in response.headers["cache-control"]


def test_panel_script_includes_panel_defaults() -> None:
    config = oa_assistant._load_config()
    body = oa_assistant.panel_script().body.decode("utf-8")
    assert json.dumps(config["panel"], ensure_ascii=False) in body


def test_report_accepts_json_and_garbage() -> None:
    assert asyncio.run(oa_assistant.report(_request(b'{"kind":"run"}')))["ok"] is True
    # 非 JSON 不能把上报链路打挂
    assert asyncio.run(oa_assistant.report(_request(b"not-json")))["ok"] is True
    assert asyncio.run(oa_assistant.report(_request(b"")))["ok"] is True


def _invoice_body(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def test_invoice_amounts_empty() -> None:
    response = asyncio.run(oa_assistant.invoice_amounts(files=[]))
    assert response.status_code == 200
    body = _invoice_body(response)
    assert body["total"] == "0.00"
    assert body["rows"] == []


def test_invoice_amounts_rejects_non_pdf_extension() -> None:
    response = asyncio.run(oa_assistant.invoice_amounts(files=[_upload("a.txt", b"%PDF-1.4")]))
    assert response.status_code == 400
    assert "PDF" in _invoice_body(response)["detail"]


def test_invoice_amounts_rejects_bad_header() -> None:
    # 扩展名对但内容不是 PDF：不信客户端，只认 %PDF- 文件头
    response = asyncio.run(oa_assistant.invoice_amounts(files=[_upload("a.pdf", b"definitely not pdf")]))
    assert response.status_code == 400
    assert "%PDF-" in _invoice_body(response)["detail"]


def test_invoice_amounts_happy_path_sets_cors(monkeypatch) -> None:
    captured = {}

    def fake(uploads, **kwargs):
        captured["uploads"] = uploads
        return {
            "items": [{"filename": "a.pdf", "amount": "12.00", "error": None}],
            "rows": [{"category": "stay", "scene": "境内住宿", "amount": "12.00"}],
            "total": "12.00",
        }

    monkeypatch.setattr(oa_assistant, "parse_invoice_uploads", fake)
    response = asyncio.run(oa_assistant.invoice_amounts(files=[_upload("a.pdf", b"%PDF-1.4 stub")]))
    assert response.status_code == 200
    body = _invoice_body(response)
    assert body["total"] == "12.00"
    assert body["rows"][0]["scene"] == "境内住宿"
    assert captured["uploads"] == [("a.pdf", b"%PDF-1.4 stub")]
    # OA 页面跨域调用需要 CORS 头
    assert response.headers["access-control-allow-origin"] == "*"


# ── 发票解析：分类 + 税率分组（monkeypatch 掉 PDF 文本提取） ──

_CATEGORIES = [
    {"key": "stay", "scene": "境内住宿", "keywords": ["住宿"]},
    {"key": "flight", "scene": "境内机票", "keywords": ["航空运输电子客票行程单", "机票"]},
    {"key": "train", "scene": "境内火车票", "keywords": ["铁路电子客票", "火车"]},
]
_RATE_MAP = {"13": "13%", "9": "9%", "6": "6%", "3": "3%"}


def _patch_texts(monkeypatch, texts):
    monkeypatch.setattr(invoice_amount, "extract_invoice_text_bytes", lambda data: texts.get(data, ""))


def test_parse_invoice_uploads_groups_by_category_and_rate(monkeypatch) -> None:
    _patch_texts(monkeypatch, {
        b"stay6": "住宿服务 税率6% 价税合计（小写）¥1,280.00",
        b"stay3": "住宿服务 税率3% 价税合计（小写）¥300.00",
        b"flight9": "航空运输电子客票行程单 税率9% 价税合计（小写）¥860.00",
    })
    result = invoice_amount.parse_invoice_uploads(
        [("a.pdf", b"stay6"), ("b.pdf", b"stay3"), ("c.pdf", b"flight9")],
        _CATEGORIES, _RATE_MAP, "其他",
    )
    assert result["total"] == "2440.00"
    rows = {(r["category"], r["tax_rate"]): r for r in result["rows"]}
    # 住宿 6% 与 3% 拆成两行（税率不同）
    assert rows[("stay", "6")]["amount"] == "1280.00"
    assert rows[("stay", "3")]["amount"] == "300.00"
    assert rows[("stay", "6")]["invoice_type"] == "6%"
    assert rows[("flight", "9")]["scene"] == "境内机票"


def test_parse_invoice_uploads_same_rate_accumulates(monkeypatch) -> None:
    _patch_texts(monkeypatch, {
        b"t1": "铁路电子客票 税率9% 价税合计（小写）¥200.00",
        b"t2": "铁路电子客票 税率9% 价税合计（小写）¥50.00",
    })
    result = invoice_amount.parse_invoice_uploads([("a.pdf", b"t1"), ("b.pdf", b"t2")], _CATEGORIES, _RATE_MAP, "")
    assert len(result["rows"]) == 1
    assert result["rows"][0]["amount"] == "250.00"
    assert result["rows"][0]["count"] == 2


def test_parse_invoice_uploads_rate_falls_back_to_tax_amount(monkeypatch) -> None:
    # 没有「税率 x%」，用 税额/(价税合计-税额) 反推 → 9%
    _patch_texts(monkeypatch, {b"x": "航空运输电子客票行程单 价税合计（小写）¥1,090.00 税额90.00"})
    result = invoice_amount.parse_invoice_uploads([("a.pdf", b"x")], _CATEGORIES, _RATE_MAP, "")
    assert result["rows"][0]["tax_rate"] == "9"


def test_parse_invoice_uploads_unknown_category_only_affects_items(monkeypatch) -> None:
    _patch_texts(monkeypatch, {b"u": "某某服务费 税率6% 价税合计（小写）¥100.00"})
    result = invoice_amount.parse_invoice_uploads([("a.pdf", b"u")], _CATEGORIES, _RATE_MAP, "")
    assert result["rows"] == []          # 归不了类就不生成明细行
    assert result["total"] == "100.00"   # 但金额仍累计、item 上标了类别缺失