"""电子发票 PDF 价税合计提取（报销流程助手）。

从上传的电子发票 PDF 里解析「价税合计（小写）¥xxx」金额，多个发票累加成报销总金额。
只做文本匹配：数电票/增值税电子普通发票的版式里「价税合计」行都带（小写）¥金额；
解析失败的文件在结果里标注原因，由前端提示，不阻塞流程。
"""

from __future__ import annotations

import io
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pypdf import PdfReader

# 「（小写）」/「(小写)」后面跟可选货币符的金额；去空白后匹配以兼容版式空格
_AMOUNT_RE = re.compile(r"[（(]\s*小写\s*[）)]\s*[¥￥]?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
# 兜底：价税合计关键字之后的第一串金额（有的发票没有「小写」字样）
_TOTAL_RE = re.compile(r"价税合计[^0-9]{0,30}?[¥￥]?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
# 票面税率（%）：「税率 9%」这种
_RATE_RE = re.compile(r"税率[^0-9%]{0,8}([0-9]{1,2}(?:\.[0-9]{1,2})?)\s*%")
# 税额：税率缺失时用 税额/(价税合计-税额) 反推
_TAX_RE = re.compile(r"税额[^0-9]{0,8}([0-9][0-9,]*(?:\.[0-9]{1,2})?)")


def _pdf_text(pdf_path: Path) -> str:
    """提取 PDF 全部文本（去空白拼接，规避版式里字与字之间的空格/换行）。"""
    reader = PdfReader(str(pdf_path))
    return _reader_text(reader)


def _reader_text(reader: PdfReader) -> str:
    parts: list[str] = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return "".join(parts)


def extract_invoice_total_bytes(data: bytes) -> str | None:
    """从内存中的 PDF 字节解析价税合计（书签面板上传的文件不经落盘）。"""
    try:
        reader = PdfReader(io.BytesIO(data))
        text = _reader_text(reader)
    except Exception:
        return None
    return extract_invoice_total_text(text)


def extract_invoice_text_bytes(data: bytes) -> str | None:
    """从内存中的 PDF 字节提取全量文本（供分类/税率解析复用）。"""
    try:
        reader = PdfReader(io.BytesIO(data))
        return _reader_text(reader)
    except Exception:
        return None


def extract_invoice_total_text(text: str) -> str | None:
    """从发票文本里解析价税合计金额字符串（两位小数）；解析失败返回 None。"""
    if not text:
        return None
    compact = re.sub(r"\s+", "", text)
    for pattern in (_AMOUNT_RE, _TOTAL_RE):
        match = pattern.search(compact)
        if not match:
            continue
        raw = match.group(1).replace(",", "")
        try:
            return f"{Decimal(raw):.2f}"
        except InvalidOperation:
            continue
    return None


def extract_invoice_total(pdf_path: Path) -> str | None:
    """解析单张发票 PDF 的价税合计金额；解析失败返回 None。"""
    try:
        text = _pdf_text(pdf_path)
    except Exception:
        return None
    return extract_invoice_total_text(text)


def _norm_rate(raw: str) -> str | None:
    """税率归一：9.0 → 「9」，6.5 保留一位。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0 or value > 100:
        return None
    if abs(value - round(value)) < 0.2:
        return str(int(round(value)))
    return f"{value:.1f}".rstrip("0").rstrip(".")


def extract_tax_rate_text(text: str, total: str | None = None) -> str | None:
    """票面税率（百分数，归一成「9」「6」这类字符串）。

    先找「税率 x%」；找不到时，用 税额/(价税合计-税额) 反推。解析失败返回 None。
    """
    if not text:
        return None
    compact = re.sub(r"\s+", "", text)
    match = _RATE_RE.search(compact)
    if match:
        return _norm_rate(match.group(1))
    if total:
        try:
            tax_match = _TAX_RE.search(compact)
            if tax_match:
                tax = Decimal(tax_match.group(1).replace(",", ""))
                base = Decimal(total) - tax
                if base > 0:
                    return _norm_rate(str(float(tax / base * 100)))
        except (InvalidOperation, ValueError):
            return None
    return None


def _classify(text: str, categories: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """按关键词把发票归到报销场景；命中第一个 category 就返回。"""
    for category in categories or []:
        for keyword in category.get("keywords") or []:
            if keyword and keyword in text:
                return category
    return None


def parse_invoice_uploads(
    uploads: list[tuple[str, bytes]],
    categories: list[dict[str, Any]] | None = None,
    rate_invoice_types: dict[str, str] | None = None,
    default_invoice_type: str = "",
) -> dict[str, Any]:
    """解析发票（价税合计 + 税率 + 类别），并按「类别 + 税率」聚合成报销明细行。

    返回 {items, rows, total}：items 是逐张结果（供前端展示/纠错）；rows 是聚合行
    （scene + 税率映射出的 invoice_type + 累计 amount）。解析失败只在 items 里标原因。
    """
    rate_map = {str(k): v for k, v in (rate_invoice_types or {}).items()}
    items: list[dict[str, Any]] = []
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    total = Decimal("0")
    for filename, data in uploads:
        if not data:
            items.append({"filename": filename, "amount": None, "tax_rate": None, "category": None, "error": "空文件"})
            continue
        text = extract_invoice_text_bytes(data) or ""
        amount = extract_invoice_total_text(text)
        rate = extract_tax_rate_text(text, amount)
        category = _classify(re.sub(r"\s+", "", text), categories)
        if amount is None:
            items.append({"filename": filename, "amount": None, "tax_rate": rate, "category": (category or {}).get("key"), "error": "未能解析出价税合计（非电子发票版式或扫描件）"})
            continue
        total += Decimal(amount)
        items.append({"filename": filename, "amount": amount, "tax_rate": rate, "category": (category or {}).get("key"), "error": None})
        if category is None:
            continue
        key = (category["key"], rate or "")
        row = rows.get(key)
        if row is None:
            row = {
                "category": category["key"],
                "scene": category.get("scene") or category.get("key"),
                "tax_rate": rate or "",
                "invoice_type": rate_map.get(rate or "", default_invoice_type),
                "amount": Decimal("0"),
                "count": 0,
            }
            rows[key] = row
        row["amount"] += Decimal(amount)
        row["count"] += 1
    return {
        "items": items,
        "rows": [{**row, "amount": f"{row['amount']:.2f}"} for row in rows.values()],
        "total": f"{total:.2f}",
    }


def sum_invoice_amounts(paths: list[str]) -> dict[str, Any]:
    """逐张解析并累加。返回 {items, total}；items 里带每张的金额或错误原因。"""
    items: list[dict[str, str | None]] = []
    total = Decimal("0")
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            items.append({"path": raw, "filename": path.name, "amount": None, "error": "文件不存在"})
            continue
        amount = extract_invoice_total(path)
        if amount is None:
            items.append({"path": raw, "filename": path.name, "amount": None, "error": "未能解析出价税合计（非电子发票版式或扫描件）"})
            continue
        try:
            total += Decimal(amount)
        except InvalidOperation:
            items.append({"path": raw, "filename": path.name, "amount": None, "error": "金额格式异常"})
            continue
        items.append({"path": raw, "filename": path.name, "amount": amount, "error": None})
    return {"items": items, "total": f"{total:.2f}"}


def sum_invoice_amounts_from_uploads(uploads: list[tuple[str, bytes]]) -> dict[str, Any]:
    """同 sum_invoice_amounts，但面向内存中的上传文件（书签面板路径，不落盘）。

    uploads 为 (filename, bytes) 列表；返回结构一致，便于前端复用同一段渲染逻辑。
    """
    items: list[dict[str, str | None]] = []
    total = Decimal("0")
    for filename, data in uploads:
        if not data:
            items.append({"path": filename, "filename": filename, "amount": None, "error": "空文件"})
            continue
        amount = extract_invoice_total_bytes(data)
        if amount is None:
            items.append({"path": filename, "filename": filename, "amount": None, "error": "未能解析出价税合计（非电子发票版式或扫描件）"})
            continue
        try:
            total += Decimal(amount)
        except InvalidOperation:
            items.append({"path": filename, "filename": filename, "amount": None, "error": "金额格式异常"})
            continue
        items.append({"path": filename, "filename": filename, "amount": amount, "error": None})
    return {"items": items, "total": f"{total:.2f}"}


def is_pdf_bytes(data: bytes) -> bool:
    """文件头校验：只收真正的 PDF（%PDF-），不信任客户端的扩展名/MIME。"""
    return data[:5] == b"%PDF-"
