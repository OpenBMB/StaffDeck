#!/usr/bin/env python3
"""批量改写 app.core 的模块路径。

重构期间每次 ``git mv`` 之后跑一遍，把 ``app/`` 与 ``tests/`` 里的引用改到新路径。
只做**字面替换**，不解析语法：测试里 ``monkeypatch.setattr("app.core.x.fn")``
这种字符串引用也必须跟着改，否则测试会静默失效。

幂等：``MOVES`` 的 key 是旧路径，跑第二遍时文件里已没有旧路径，不会再动。

用法::

    python scripts/rewrite_core_imports.py            # 改文件
    python scripts/rewrite_core_imports.py --check    # 只报告有多少处待改
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

#: 旧模块路径 -> 新模块路径。
MOVES: dict[str, str] = {
    # 批次 1：base
    "app.core.cancellation": "app.core.base.cancellation",
    "app.core.ui_flags": "app.core.base.ui_flags",
    "app.core.task_request_compiler": "app.core.base.request_compiler",
    "app.core.context_projection": "app.core.base.context_projection",
    "app.core.conversation_context": "app.core.base.conversation_context",
    "app.core.conversation_projection": "app.core.base.conversation_projection",
    # 批次 1：converse
    "app.core.response_generator": "app.core.converse.response",
    "app.core.agent_identity_prompt": "app.core.converse.identity",
    # 批次 2：plan
    "app.core.turn_planner": "app.core.plan.planner",
    "app.core.task_frame_policy": "app.core.plan.frame_policy",
    "app.core.slash_commands": "app.core.plan.slash",
    "app.core.router": "app.core.plan.router",
    # 批次 2：capability
    "app.core.capability_manifest": "app.core.capability.manifest",
    "app.core.capability_discovery": "app.core.capability.discovery",
    "app.core.tool_replay_policy": "app.core.capability.replay_policy",
    "app.core.published_deliverables": "app.core.capability.deliverables",
    # 批次 2：sop（graph_rules 归 base：它被 plan 与 sop 共用，必须放在两者之下）
    "app.core.graph_rules": "app.core.base.graph_rules",
    "app.core.sop_scenes": "app.core.sop.scenes",
    "app.core.sop_step_executor": "app.core.sop.step_executor",
    "app.core.sop_edge_eval": "app.core.sop.edge_eval",
    "app.core.slot_form": "app.core.sop.slots.form",
    "app.core.slot_display": "app.core.sop.slots.display",
    "app.core.slot_hydration_policy": "app.core.sop.slots.hydration",
    # 批次 2：knowledge_flow
    "app.core.knowledge_prefetch": "app.core.knowledge_flow.prefetch",
    "app.core.laya_router": "app.core.knowledge_flow.routing",
    "app.core.laya_client": "app.core.knowledge_flow.laya_client",
    # 批次 2：artifacts
    "app.core.slides_deck": "app.core.artifacts.slides",
    "app.core.slides_pptx": "app.core.artifacts.slides_pptx",
    "app.core.invoice_amount": "app.core.artifacts.invoice",
    "app.core.doc_review": "app.core.artifacts.doc_review",
}

SCAN_ROOTS = ("app", "tests", "scripts")


def iter_python_files() -> list[Path]:
    files: list[Path] = []
    # 本文件自己存着旧路径（MOVES 的 key），扫进来会把映射表改坏
    myself = Path(__file__).resolve()
    for root in SCAN_ROOTS:
        directory = BASE / root
        if not directory.is_dir():
            continue
        files.extend(
            path
            for path in directory.rglob("*.py")
            if "__pycache__" not in path.parts and path.resolve() != myself
        )
    return files


#: ``from app.core import <旧模块名> [as alias]`` 这种模块级导入：字面替换抓不到，
#: 必须单独重写成 ``from app.core.<新包> import <新模块> [as alias]``。
_MODULE_IMPORT = re.compile(r"from app\.core import (\w+)(\s+as\s+\w+)?")


def rewrite(text: str) -> tuple[str, int]:
    hits = 0
    for old, new in MOVES.items():
        count = text.count(old)
        if count:
            text = text.replace(old, new)
            hits += count

    def _module_form(match: re.Match[str]) -> str:
        nonlocal hits
        name, alias = match.group(1), match.group(2) or ""
        target = MOVES.get(f"app.core.{name}")
        if not target:
            return match.group(0)
        hits += 1
        package, module = target.rsplit(".", 1)
        # target 是完整路径（app.core.base.cancellation），拼回去时要去掉公共前缀
        package = package[len("app.core."):] if package.startswith("app.core.") else package
        if module != name and not alias:
            # 模块改名时必须保留原绑定名：`from app.core import invoice_amount` 改成
            # `from app.core.artifacts import invoice` 会让调用点的 invoice_amount 全部 NameError。
            return f"from app.core.{package} import {module} as {name}"
        return f"from app.core.{package} import {module}{alias}"

    return _MODULE_IMPORT.sub(_module_form, text), hits


def main() -> int:
    parser = argparse.ArgumentParser(description="改写 app.core 模块路径")
    parser.add_argument("--check", action="store_true", help="只报告，不写文件")
    args = parser.parse_args()

    total = 0
    touched: list[tuple[Path, int]] = []
    for path in iter_python_files():
        original = path.read_text(encoding="utf-8")
        updated, hits = rewrite(original)
        if not hits:
            continue
        # 自身就是被搬走的模块文件时跳过：它的 import 由脚本统一处理，路径改的是别的模块
        total += hits
        touched.append((path, hits))
        if not args.check:
            path.write_text(updated, encoding="utf-8")

    for path, hits in sorted(touched, key=lambda item: -item[1])[:15]:
        print(f"{hits:4d}  {path.relative_to(BASE)}")
    if len(touched) > 15:
        print(f"      ... 另外 {len(touched) - 15} 个文件")

    verb = "待改" if args.check else "已改"
    print(f"\n{verb} {total} 处引用，涉及 {len(touched)} 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
