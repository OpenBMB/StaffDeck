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

# 部分铁路电子客票的 FontDescriptor 同时带 /FontFile2 与 /FontFile3（还指向同一对象），
# pypdf 会直接抛 PdfReadError，导致整页文本取不到（金额/税率全丢）。这里只保留第一个 FontFile。
def _apply_pypdf_font_workaround() -> None:
    try:
        import pypdf._font as _pypdf_font

        if getattr(_pypdf_font.Font, "_sd_tolerant_font_files", False):
            return
        original = _pypdf_font.Font._parse_font_descriptor

        def tolerant(font_descriptor_obj):
            keys = [k for k in ("/FontFile", "/FontFile2", "/FontFile3") if k in font_descriptor_obj]
            for extra in keys[1:]:
                try:
                    del font_descriptor_obj[extra]
                except Exception:
                    pass
            return original(font_descriptor_obj)

        _pypdf_font.Font._parse_font_descriptor = staticmethod(tolerant)
        _pypdf_font.Font._sd_tolerant_font_files = True
    except Exception:
        pass


_apply_pypdf_font_workaround()

# 「（小写）」/「(小写)」后面跟可选货币符的金额；去空白后匹配以兼容版式空格
_AMOUNT_RE = re.compile(r"[（(]\s*小写\s*[）)]\s*[¥￥]?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
# 兜底：价税合计关键字之后的第一串金额（有的发票没有「小写」字样）
_TOTAL_RE = re.compile(r"价税合计[^0-9]{0,30}?[¥￥]?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
# 票面税率（%）：「税率 9%」这种
_RATE_RE = re.compile(r"税率[^0-9%]{0,8}([0-9]{1,2}(?:\.[0-9]{1,2})?)\s*%")
# 税额：税率缺失时用 税额/(价税合计-税额) 反推
_TAX_RE = re.compile(r"税额[^0-9]{0,8}([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
# 兜底：票面出现的 ￥金额（铁路电子客票没有「价税合计」字样，只印「￥87.00」）
_MONEY_RE = re.compile(r"[¥￥]\s*([0-9][0-9,]*\.[0-9]{2})")
# 票面百分数（有的票不写「税率」也不给税额，只印一个 6%）。
# 前面不能紧贴数字/小数点：否则「707.55 6%」会被贪婪成「56%」。
_PCT_RE = re.compile(r"(?<![0-9.])([0-9]{1,2}(?:\.[0-9]{1,2})?)\s*%")
# 可接受的税率档位：反推出来的税率只在这些档位附近才采纳（防止误取到别的数字）
_PLAUSIBLE_RATES = (1, 3, 5, 6, 9, 10, 13)


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
    # 铁路电子客票这类只印「票价 … ￥87.00」：取票面上最大的 ￥金额（通常是总票价）
    money = []
    for raw in _MONEY_RE.findall(compact):
        try:
            money.append(Decimal(raw.replace(",", "")))
        except InvalidOperation:
            continue
    if money:
        return f"{max(money):.2f}"
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


def _snap_rate(value: float) -> str | None:
    """反推的税率只在常见档位附近才采纳，否则视为没识别到。"""
    for rate in _PLAUSIBLE_RATES:
        if abs(value - rate) <= 0.5:
            return str(rate)
    return None


# 发票种类：票面标题「电子发票（普通发票）/（增值税专用发票）」或「增值税XX发票」
_KIND_RE = re.compile(r"(电子发票[（(][^）)]{1,14}[）)])")
_KIND_FALLBACK_RE = re.compile(r"(增值税[^，,。；; ]{0,10}?发票)")


def extract_invoice_kind(text: str) -> str:
    """从票面识别发票种类名：如「电子发票（普通发票）」「增值税专用发票」「铁路电子客票」。"""
    compact = re.sub(r"\s+", "", text or "")
    match = _KIND_RE.search(compact)
    if match:
        return match.group(1).replace("(", "（").replace(")", "）")
    match = _KIND_FALLBACK_RE.search(compact)
    if match:
        return match.group(1)
    return ""


def extract_tax_rate_text(text: str, total: str | None = None) -> str | None:
    """票面税率（百分数，归一成「9」「6」这类字符串）。

    先找「税率 x%」；找不到时，用 税额/(价税合计-税额) 反推（只认常见档位）。
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
                    return _snap_rate(float(tax / base * 100))
        except (InvalidOperation, ValueError):
            return None
    # 最后兑底：全票只有一个百分数且落在常见档位时就认它
    # （住宿/增值税发票常见：税率写在表格里，与金额相邻）
    percents = set()
    for raw in _PCT_RE.findall(text):
        snapped = _norm_rate(raw)
        if not snapped:
            continue
        try:
            if float(snapped) in _PLAUSIBLE_RATES:
                percents.add(snapped)
        except ValueError:
            continue
    if len(percents) == 1:
        return percents.pop()
    return None


def _classify(text: str, categories: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """按关键词把发票归到报销场景；命中第一个 category 就返回。"""
    for category in categories or []:
        for keyword in category.get("keywords") or []:
            if keyword and keyword in text:
                return category
    return None


def _suggest_invoice_type(kind: str, category_key: str, kind_map: dict[str, str] | None, transport_categories: list[str] | None) -> str:
    """由票面种类（+ 类别）推断 OA 发票类型名。

    规则（按 kind_map 的顺序子串匹配）：
    - 含「增值税专用发票」→ 增值税专用发票（不分电子/纸质，再靠税率细分）
    - 含「铁路电子客票」/「航空…行程单」→ 增值税电子普通发票（境内客运）
    - 含「增值税普通发票」→ 交通类（火车/机票）归「增值税电子普通发票（境内客运）」，其他归「增值税普通发票」
    - 含「普通发票」（无增值税）→ 电子普通发票
    """
    if not kind:
        return ""
    for key, value in (kind_map or {}).items():
        if key and key in kind:
            return value
    if "普通发票" in kind:
        if category_key in (transport_categories or []):
            return "增值税电子普通发票（境内客运）"
        return "增值税普通发票" if "增值税" in kind else "电子普通发票"
    return ""


def parse_invoice_uploads(
    uploads: list[tuple[str, bytes]],
    categories: list[dict[str, Any]] | None = None,
    rate_invoice_types: dict[str, str] | None = None,
    default_invoice_type: str = "",
    kind_to_type: dict[str, str] | None = None,
    transport_categories: list[str] | None = None,
) -> dict[str, Any]:
    """解析发票（价税合计 + 税率 + 类别 + 种类），一张发票生成一行报销明细。

    返回 {items, rows, total}：items 是逐张结果（供前端展示/纠错）；rows 是明细行。
    kind_to_type 把票面种类（如「电子发票（普通发票）」）映射成 OA 发票类型。
    """
    rate_map = {str(k): v for k, v in (rate_invoice_types or {}).items()}
    kind_map = {str(k): v for k, v in (kind_to_type or {}).items()}
    items: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    total = Decimal("0")
    for filename, data in uploads:
        if not data:
            items.append({"filename": filename, "amount": None, "tax_rate": None, "category": None, "kind": "", "error": "空文件"})
            continue
        text = extract_invoice_text_bytes(data) or ""
        amount = extract_invoice_total_text(text)
        category = _classify(re.sub(r"\s+", "", text), categories)
        kind = extract_invoice_kind(text)
        # 火车票/机票行程单不印税率，用类别上的默认税率（如旅客运输 9%）
        rate = extract_tax_rate_text(text, amount) or (category or {}).get("default_tax_rate")
        if amount is None:
            items.append({"filename": filename, "amount": None, "tax_rate": rate, "category": (category or {}).get("key"), "kind": kind, "error": "未能解析出价税合计（非电子发票版式或扫描件）"})
            continue
        total += Decimal(amount)
        items.append({"filename": filename, "amount": amount, "tax_rate": rate, "category": (category or {}).get("key"), "kind": kind, "error": None})
        if category is None:
            continue
        # 一张发票一行报销明细（不按类别合并；报销总额在 total / 面板金额）
        rows.append({
            "category": category["key"],
            "scene": category.get("scene") or category.get("key"),
            "tax_rate": rate or "",
            "invoice_type": rate_map.get(rate or "", default_invoice_type),
            "suggested_invoice_type": _suggest_invoice_type(kind, category["key"], kind_map, transport_categories),
            "kind": kind,
            "amount": f"{Decimal(amount):.2f}",
            "filename": filename,
        })
    return {"items": items, "rows": rows, "total": f"{total:.2f}"}


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
