"""发票解析自测 CLI（开发工具）。

用法：
    cd backend
    uv run python scripts/parse_invoices.py ~/Downloads/发票/*.pdf
    uv run python scripts/parse_invoices.py ~/Downloads/发票目录

输出每张发票的 金额 / 税率 / 归类，以及聚合出的报销明细行（出差报销用）。
配置来自 config/oa_assistant.json 的 invoice_parse 段。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.invoice_amount import parse_invoice_uploads  # noqa: E402


def _load_config() -> dict:
    path = Path(__file__).resolve().parent.parent / "config" / "oa_assistant.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _collect(args: list[str]) -> list[Path]:
    out: list[Path] = []
    for raw in args:
        path = Path(raw).expanduser()
        if path.is_dir():
            out += sorted(path.glob("*.pdf"))
        elif path.exists():
            out.append(path)
        else:
            print(f"跳过不存在的路径：{raw}")
    return out


def main() -> int:
    paths = _collect(sys.argv[1:])
    if not paths:
        print("用法：python scripts/parse_invoices.py <pdf 或目录> ...")
        return 1

    cfg = _load_config().get("invoice_parse", {}) or {}
    uploads = [(path.name, path.read_bytes()) for path in paths]
    result = parse_invoice_uploads(
        uploads,
        categories=cfg.get("categories"),
        rate_invoice_types=cfg.get("tax_rate_invoice_types"),
        default_invoice_type=cfg.get("default_invoice_type", ""),
    )

    print(f"共 {len(paths)} 张发票\n")
    for item in result["items"]:
        flag = "✓" if item["error"] is None else "✗"
        line = (
            f"{flag} {item['filename'][:44]:46} 金额={str(item['amount']):>9} "
            f"税率={str(item['tax_rate']):>4} 类别={item['category'] or '-'}"
        )
        if item["error"]:
            line += f"  ({item['error']})"
        print(line)

    print(f"\n合计：{result['total']}")
    rows = result["rows"]
    if rows:
        print("\n生成报销明细行（出差报销）：")
        for row in rows:
            print(
                f"  {row['scene']:<6} 税率 {str(row['tax_rate']):<3} "
                f"发票类型「{row['invoice_type']}」 金额 {row['amount']}（{row['count']} 张）"
            )
    else:
        print("\n没有生成明细行（发票类别不在 境内住宿 / 境内机票 / 境内火车票 内）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())