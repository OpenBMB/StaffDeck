#!/usr/bin/env python3
"""app/core 结构校验：分层依赖、跨包循环、单文件行数。

重构期间用它守住三条底线，避免「搬完更乱」：

1. **只允许向左依赖**：``base ← converse ← plan ← capability ← sop ← engine``，
   ``knowledge_flow`` 只依赖 base，``artifacts`` 自成一体。
2. **跨包不出现循环**：包内互相调用允许（``engine`` 里的 ``invoke`` 与
   ``step_loop`` 本来就互相调），跨包回环一定会把 import 变成运行时炸弹。
3. **单文件不超过上限行数**：巨兽拆分完成后才能收紧到 800。

尚未迁移的 ``app/core/*.py`` 顶层模块按「不受约束」处理，所以迁移期可以先跑
不阻断；全部搬完后加 ``--strict`` 进 CI。

用法::

    python scripts/core_import_graph.py                       # 人类可读报告
    python scripts/core_import_graph.py --json /tmp/core.json  # 依赖图基线
    python scripts/core_import_graph.py --strict               # 有违规就非零退出
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict
from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parent.parent / "app" / "core"

#: 包 -> 层级。数字越小越底层，只允许依赖比自己小的层级。
PACKAGE_LAYERS = {
    "base": 1,
    "artifacts": 1,
    "converse": 2,
    "knowledge_flow": 2,
    "plan": 3,
    "capability": 4,
    "sop": 5,
    "engine": 6,
}

#: 明确允许的例外：跨层但语义合理，写在这里而不是放宽全局规则。
ALLOWED_EDGES = {
    # artifacts 只依赖 models / llm，不碰 core 内其它包
    ("artifacts", "base"),
}

MAX_FILE_LINES = 800


def iter_module_files() -> list[Path]:
    return sorted(
        path
        for path in CORE_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts and path.name != "__init__.py"
    )


def package_of(path: Path) -> str | None:
    """返回模块所属的二级包名；顶层平铺模块返回 None。"""

    relative = path.relative_to(CORE_ROOT)
    return relative.parts[0] if len(relative.parts) > 1 else None


def module_name(path: Path) -> str:
    relative = path.relative_to(CORE_ROOT).with_suffix("")
    return ".".join(relative.parts)


def imported_core_modules(path: Path) -> set[str]:
    """解析出一个文件里引用的 app.core 模块（不含自身）。"""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("app.core."):
                found.add(node.module[len("app.core."):])
            elif node.module == "app.core":
                found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app.core."):
                    found.add(alias.name[len("app.core."):])
    return found


def build_graph() -> tuple[dict[str, set[str]], dict[str, int]]:
    modules = {module_name(path): path for path in iter_module_files()}
    known = set(modules)
    graph: dict[str, set[str]] = {}
    sizes: dict[str, int] = {}
    for name, path in modules.items():
        graph[name] = {dep for dep in imported_core_modules(path) if dep in known}
        sizes[name] = len(path.read_text(encoding="utf-8").splitlines())
    return graph, sizes


def module_package(name: str) -> str | None:
    return name.split(".")[0] if "." in name else None


def find_layer_violations(graph: dict[str, set[str]]) -> list[str]:
    problems: list[str] = []
    for source, deps in graph.items():
        source_pkg = module_package(source)
        if source_pkg not in PACKAGE_LAYERS:
            continue
        for dep in deps:
            dep_pkg = module_package(dep)
            if dep_pkg not in PACKAGE_LAYERS or dep_pkg == source_pkg:
                continue
            if (source_pkg, dep_pkg) in ALLOWED_EDGES:
                continue
            if PACKAGE_LAYERS[dep_pkg] > PACKAGE_LAYERS[source_pkg]:
                problems.append(
                    f"层级越界: {source}(L{PACKAGE_LAYERS[source_pkg]}) -> "
                    f"{dep}(L{PACKAGE_LAYERS[dep_pkg]})"
                )
    return problems


def find_package_cycles(graph: dict[str, set[str]]) -> list[str]:
    """在包级别找环：A -> B -> A。"""

    edges: dict[str, set[str]] = defaultdict(set)
    for source, deps in graph.items():
        source_pkg = module_package(source)
        if source_pkg is None:
            continue
        for dep in deps:
            dep_pkg = module_package(dep)
            if dep_pkg and dep_pkg != source_pkg:
                edges[source_pkg].add(dep_pkg)

    problems: list[str] = []
    state: dict[str, int] = {}

    def walk(node: str, stack: list[str]) -> None:
        state[node] = 1
        stack.append(node)
        for nxt in sorted(edges[node]):
            if state.get(nxt) == 1:
                cycle = stack[stack.index(nxt):] + [nxt]
                problems.append("跨包循环: " + " -> ".join(cycle))
            elif state.get(nxt, 0) == 0:
                walk(nxt, stack)
        stack.pop()
        state[node] = 2

    for node in sorted(edges):
        if state.get(node, 0) == 0:
            walk(node, [])
    return problems


def find_oversized(sizes: dict[str, int], max_lines: int) -> list[str]:
    return [
        f"文件过大: {name} {count} 行 > {max_lines}"
        for name, count in sorted(sizes.items(), key=lambda item: -item[1])
        if count > max_lines
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="app/core 结构校验")
    parser.add_argument("--json", type=Path, help="把依赖图写到指定 JSON")
    parser.add_argument("--strict", action="store_true", help="有违规就返回非零")
    parser.add_argument("--max-lines", type=int, default=MAX_FILE_LINES)
    args = parser.parse_args()

    graph, sizes = build_graph()
    layer_problems = find_layer_violations(graph)
    cycle_problems = find_package_cycles(graph)
    size_problems = find_oversized(sizes, args.max_lines)

    packages = defaultdict(list)
    for name in graph:
        packages[module_package(name) or "(顶层平铺)"].append(name)

    print(f"app/core: {len(graph)} 个模块, {sum(sizes.values()):,} 行")
    print()
    for pkg in sorted(packages, key=lambda key: (key == "(顶层平铺)", key)):
        layer = PACKAGE_LAYERS.get(pkg)
        tag = f"L{layer}" if layer else "未迁移"
        print(f"  {pkg:16s} {tag:6s} {len(packages[pkg]):3d} 个模块")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "modules": {name: sorted(deps) for name, deps in sorted(graph.items())},
                    "sizes": sizes,
                    "layers": PACKAGE_LAYERS,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n依赖图已写入 {args.json}")

    problems = layer_problems + cycle_problems + size_problems
    print()
    if problems:
        print(f"发现 {len(problems)} 处问题：")
        for item in problems:
            print(f"  - {item}")
        return 1 if args.strict else 0
    print("结构校验通过：无层级越界、无跨包循环、无超大文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
