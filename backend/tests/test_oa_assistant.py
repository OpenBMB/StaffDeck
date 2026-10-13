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


def test_parse_invoice_uploads_classifies_each_invoice(monkeypatch) -> None:
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
    assert rows[("stay", "6")]["amount"] == "1280.00"
    assert rows[("stay", "3")]["amount"] == "300.00"
    assert rows[("stay", "6")]["invoice_type"] == "6%"
    assert rows[("flight", "9")]["scene"] == "境内机票"


def test_parse_invoice_uploads_one_row_per_invoice(monkeypatch) -> None:
    _patch_texts(monkeypatch, {
        b"t1": "铁路电子客票 税率9% 价税合计（小写）¥200.00",
        b"t2": "铁路电子客票 税率9% 价税合计（小写）¥50.00",
    })
    result = invoice_amount.parse_invoice_uploads([("a.pdf", b"t1"), ("b.pdf", b"t2")], _CATEGORIES, _RATE_MAP, "")
    # 两张同税率火车票 → 两行明细（一张票一行），报销总额仍累计
    assert len(result["rows"]) == 2
    assert [r["amount"] for r in result["rows"]] == ["200.00", "50.00"]
    assert {r["filename"] for r in result["rows"]} == {"a.pdf", "b.pdf"}
    assert result["total"] == "250.00"


def test_parse_invoice_uploads_rate_falls_back_to_tax_amount(monkeypatch) -> None:
    # 没有「税率 x%」，用 税额/(价税合计-税额) 反推 → 9%
    _patch_texts(monkeypatch, {b"x": "航空运输电子客票行程单 价税合计（小写）¥1,090.00 税额90.00"})
    result = invoice_amount.parse_invoice_uploads([("a.pdf", b"x")], _CATEGORIES, _RATE_MAP, "")
    assert result["rows"][0]["tax_rate"] == "9"


def test_parse_invoice_uploads_unknown_category_still_produces_a_row(monkeypatch) -> None:
    """票面类目是任意的：归不到场景也要出明细行（scene 留空，由面板按场景兜底）。

    回归：一张阿里云「*生产生活服务*软件服务费 6%」的电子专票，曾因为
    「没命中 住宿/机票/火车票 关键词」被整个丢掉（columns 只是场景提示，不是门槛）。
    """
    _patch_texts(
        monkeypatch,
        {b"u": "电子发票（增值税专用发票） *生产生活服务*软件服务费 税率6% 价税合计（小写）¥100.00"},
    )
    result = invoice_amount.parse_invoice_uploads(
        [("a.pdf", b"u")],
        _CATEGORIES,
        _RATE_MAP,
        "",
        {"增值税专用发票": "增值税专用发票"},
    )

    assert result["total"] == "100.00"
    assert result["items"][0]["error"] is None
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["scene"] == ""              # 归类不到就留空，由面板兜底
    assert row["category"] == ""
    assert row["amount"] == "100.00"
    assert row["tax_rate"] == "6"
    assert row["invoice_type"] == "6%"
    assert row["suggested_invoice_type"] == "增值税专用发票"


def test_unknown_category_keeps_default_invoice_type(monkeypatch) -> None:
    # 没有税率可用时（税率不在映射表里）落到 default_invoice_type，而不是丢掉整行
    _patch_texts(monkeypatch, {b"u": "某某服务费 税率7% 价税合计（小写）¥100.00"})
    result = invoice_amount.parse_invoice_uploads(
        [("a.pdf", b"u")], _CATEGORIES, _RATE_MAP, "普通发票"
    )
    assert len(result["rows"]) == 1
    assert result["rows"][0]["invoice_type"] == "普通发票"


def test_total_falls_back_to_currency_amount() -> None:
    # 铁路电子客票没有「价税合计」字样，只在票面印「￥87.00」
    assert invoice_amount.extract_invoice_total_text("电子发票（铁路电子客票）票价:二等座 ￥ 87.00") == "87.00"


def test_tax_rate_rejects_implausible_fallback() -> None:
    # 税额/(价税合计-税额) 反推出 ~60%，不在常见档位 → 不信
    assert invoice_amount.extract_tax_rate_text("价税合计（小写）¥252.12 税额 95.00", "252.12") is None


def test_tax_rate_falls_back_to_lone_percent() -> None:
    # 住宿票常见只印一个「6%」，既没有「税率」字样也没给税额
    assert invoice_amount.extract_tax_rate_text("住宿服务 6% 价税合计（小写）¥1280.00", "1280.00") == "6"
    # 多个百分数就不猜
    assert invoice_amount.extract_tax_rate_text("住宿服务 6% 税额 13% ¥100", "100.00") is None


def test_tax_rate_from_invoice_table_row() -> None:
    # 真实增值税发票：税率写在表格里，与金额相邻（不能被贪婪成 56%）
    text = (
        "项目名称 规格型号 单 位 数 量 单 价 金 额 税率/征收率 税 额 "
        "*生产生活服务*住宿 707.55 6% 42.45 价税合计（小写）¥750.00"
    )
    assert invoice_amount.extract_tax_rate_text(text, "750.00") == "6"


def test_category_default_tax_rate_used_when_invoice_has_none(monkeypatch) -> None:
    _patch_texts(monkeypatch, {b"t": "电子发票（铁路电子客票） ￥87.00"})
    categories = [{"key": "train", "scene": "境内火车票", "default_tax_rate": "9", "keywords": ["铁路"]}]
    result = invoice_amount.parse_invoice_uploads([("a.pdf", b"t")], categories, _RATE_MAP, "")
    assert result["rows"][0]["tax_rate"] == "9"
    assert result["rows"][0]["invoice_type"] == "9%"